#!/usr/bin/env python3
"""Run Qwen via Bailian OpenAI-compatible API on a directory of prompt files.

Default settings target the Beijing region OpenAI-compatible endpoint and the
newer `qwen3.6-plus` model.

Usage:
    python scripts/run_prompt_dir_qwen_api_batch.py \
        --trace-dir output/run_20260415_v12_direct \
        --prompt-dir sql_prompts \
        --output-dir gen_results_qwen-api \
        --shard-id 0 --num-shards 4 \
        --model qwen3.6-plus
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


QWEN_BEIJING_CHAT_COMPLETIONS = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
DEFAULT_API_KEY_ENV = ("DASHSCOPE_API_KEY", "QWEN_API_KEY")


def _prompt_sort_key(path: Path) -> tuple[int, str]:
    stem = path.stem
    head = stem.split("_", 1)[0]
    return (0, f"{int(head):012d}") if head.isdigit() else (1, stem)


def _extract_sql(text: str) -> str | None:
    cdata_blocks = re.findall(
        r"<result>\s*<!\[CDATA\[(.*?)\]\]>\s*</result>",
        text,
        re.DOTALL | re.IGNORECASE,
    )
    if cdata_blocks:
        return cdata_blocks[-1].strip()

    xml_blocks = re.findall(
        r"<result>\s*(.*?)\s*</result>",
        text,
        re.DOTALL | re.IGNORECASE,
    )
    if xml_blocks:
        return xml_blocks[-1].strip()

    blocks = re.findall(
        r"```(?:postgresql|sql)\s*\n(.*?)```", text, re.DOTALL | re.IGNORECASE
    )
    if blocks:
        return blocks[-1].strip()

    stripped = text.strip()
    if re.match(r"^(select|with)\b", stripped, re.IGNORECASE | re.DOTALL):
        return stripped
    return None


def _load_prompt_id_filter(prompt_ids: str | None, prompt_ids_file: str | None) -> set[str] | None:
    selected: set[str] = set()
    if prompt_ids:
        selected.update(x.strip() for x in prompt_ids.split(",") if x.strip())
    if prompt_ids_file:
        with open(prompt_ids_file, encoding="utf-8") as f:
            for raw in f:
                text = raw.strip()
                if text:
                    selected.add(text)
    return selected or None


def _resolve_api_key(explicit: str | None) -> str | None:
    if explicit:
        return explicit
    for key in DEFAULT_API_KEY_ENV:
        value = os.getenv(key)
        if value:
            return value
    return None


def _normalize_message_content(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text") or item.get("content") or ""
                if text:
                    parts.append(str(text))
            elif item:
                parts.append(str(item))
        return "\n".join(parts).strip()
    return str(content or "")


def _call_qwen_chat_completions(
    *,
    endpoint: str,
    api_key: str,
    model: str,
    prompt_text: str,
    temperature: float | None,
    enable_thinking: bool,
    thinking_budget: int | None,
    timeout_s: float,
    max_retries: int,
    retry_delay_s: float,
) -> tuple[str, dict]:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt_text}],
        "enable_thinking": enable_thinking,
    }
    if thinking_budget is not None:
        payload["enable_thinking"] = True
        payload["thinking_budget"] = thinking_budget
    if temperature is not None:
        payload["temperature"] = temperature
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    last_error: str | None = None
    for attempt in range(1, max_retries + 1):
        req = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                raw = resp.read().decode("utf-8")
            data = json.loads(raw)
            choices = data.get("choices") or []
            if not choices:
                raise RuntimeError(f"empty choices in response: {raw[:500]}")
            message = choices[0].get("message") or {}
            content = _normalize_message_content(message.get("content"))
            if not content:
                reasoning = _normalize_message_content(message.get("reasoning_content"))
                if reasoning:
                    content = reasoning
            if not content:
                raise RuntimeError(f"empty message content in response: {raw[:500]}")
            usage = data.get("usage") or {}
            return content, usage
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            last_error = f"http {exc.code}: {detail[:500]}"
            if exc.code in {429, 500, 502, 503, 504} and attempt < max_retries:
                time.sleep(retry_delay_s * attempt)
                continue
            break
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            if attempt < max_retries:
                time.sleep(retry_delay_s * attempt)
                continue
            break
    raise RuntimeError(last_error or "unknown Qwen API error")


async def run_prompt(
    prompt_path: Path,
    output_path: Path,
    semaphore: asyncio.Semaphore,
    *,
    endpoint: str,
    api_key: str,
    model: str,
    temperature: float | None,
    enable_thinking: bool,
    thinking_budget: int | None,
    timeout_s: float,
    max_retries: int,
    retry_delay_s: float,
) -> dict:
    async with semaphore:
        qid = prompt_path.stem
        if output_path.is_file() and output_path.stat().st_size > 0:
            print(f"  SKIP {qid} (already exists)")
            return {"qid": qid, "status": "skipped"}

        prompt_text = prompt_path.read_text(encoding="utf-8")
        try:
            text, usage = await asyncio.to_thread(
                _call_qwen_chat_completions,
                endpoint=endpoint,
                api_key=api_key,
                model=model,
                prompt_text=prompt_text,
                temperature=temperature,
                enable_thinking=enable_thinking,
                thinking_budget=thinking_budget,
                timeout_s=timeout_s,
                max_retries=max_retries,
                retry_delay_s=retry_delay_s,
            )
            output_path.write_text(text, encoding="utf-8")
            sql = _extract_sql(text)
            details = usage.get("completion_tokens_details") or usage.get("output_tokens_details") or {}
            reasoning_tokens = usage.get("reasoning_tokens", details.get("reasoning_tokens", 0))
            print(f"  OK  {qid} sql={'ok' if sql else 'MISSING'} ({len(text)} chars)")
            return {
                "qid": qid,
                "status": "ok",
                "sql": sql,
                "usage": {
                    "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
                    "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
                    "thinking_tokens": int(reasoning_tokens or 0),
                    "total_tokens": int(usage.get("total_tokens", 0) or 0),
                },
            }
        except Exception as exc:  # noqa: BLE001
            print(f"  ERR {qid}: {exc}")
            return {"qid": qid, "status": "error", "error": str(exc)}


async def main_async(
    *,
    trace_dir: Path,
    prompt_dir: str,
    output_dir: str,
    output_suffix: str,
    shard_id: int,
    num_shards: int,
    max_concurrent: int,
    prompt_filter: set[str] | None,
    endpoint: str,
    api_key: str,
    model: str,
    temperature: float | None,
    enable_thinking: bool,
    thinking_budget: int | None,
    timeout_s: float,
    max_retries: int,
    retry_delay_s: float,
    usage_jsonl: Path | None,
) -> None:
    src_dir = trace_dir / prompt_dir
    dst_dir = trace_dir / output_dir
    dst_dir.mkdir(parents=True, exist_ok=True)

    prompts = sorted(src_dir.glob("*.txt"), key=_prompt_sort_key)
    if prompt_filter is not None:
        prompts = [p for p in prompts if p.stem in prompt_filter]
    shard_prompts = prompts[shard_id::num_shards]
    print(
        f"Shard {shard_id}/{num_shards}: {len(shard_prompts)} prompts, "
        f"max_concurrent={max_concurrent}, output_dir={output_dir}, model={model}, "
        f"temperature={temperature}, enable_thinking={enable_thinking}, thinking_budget={thinking_budget}"
    )

    semaphore = asyncio.Semaphore(max_concurrent)
    coros = []
    for prompt_path in shard_prompts:
        output_path = dst_dir / f"{prompt_path.stem}{output_suffix}.txt"
        coros.append(
            run_prompt(
                prompt_path,
                output_path,
                semaphore,
                endpoint=endpoint,
                api_key=api_key,
                model=model,
                temperature=temperature,
                enable_thinking=enable_thinking,
                thinking_budget=thinking_budget,
                timeout_s=timeout_s,
                max_retries=max_retries,
                retry_delay_s=retry_delay_s,
            )
        )

    start = time.time()
    results = await asyncio.gather(*coros)
    if usage_jsonl is not None:
        usage_jsonl.parent.mkdir(parents=True, exist_ok=True)
        usage_by_prompt: dict[str, dict] = {}
        if usage_jsonl.exists():
            for raw in usage_jsonl.read_text(encoding="utf-8", errors="ignore").splitlines():
                if not raw.strip():
                    continue
                try:
                    previous = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                prompt_id = str(previous.get("prompt_id") or "")
                if prompt_id:
                    usage_by_prompt[prompt_id] = previous
        for row in results:
            if row.get("status") != "ok":
                continue
            usage = row.get("usage") or {}
            if not usage:
                continue
            payload = {
                "prompt_id": row["qid"],
                "status": row["status"],
                "provider": "qwen",
                "model": model,
                "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
                "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
                "thinking_tokens": int(usage.get("thinking_tokens", 0) or 0),
                "total_tokens": int(usage.get("total_tokens", 0) or 0),
            }
            usage_by_prompt[str(row["qid"])] = payload
        with usage_jsonl.open("w", encoding="utf-8") as f:
            for _, payload in sorted(usage_by_prompt.items()):
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    elapsed = time.time() - start
    print(f"Shard {shard_id} done in {elapsed:.1f}s")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trace-dir", required=True)
    parser.add_argument("--prompt-dir", default="sql_prompts")
    parser.add_argument("--output-dir", default="gen_results_qwen-api")
    parser.add_argument("--output-suffix", default="_direct")
    parser.add_argument("--shard-id", type=int, default=0)
    parser.add_argument("--num-shards", type=int, default=1)
    parser.add_argument("--max-concurrent", type=int, default=1)
    parser.add_argument("--model", default="qwen3.6-plus")
    parser.add_argument("--temperature", type=float, default=None)
    parser.add_argument("--endpoint", default=QWEN_BEIJING_CHAT_COMPLETIONS)
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--enable-thinking", action="store_true")
    parser.add_argument(
        "--thinking-budget",
        type=int,
        default=None,
        help="Optional Qwen thinking_budget. When set, thinking is enabled and capped at this budget.",
    )
    parser.add_argument("--timeout-seconds", type=float, default=180.0)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--retry-delay-seconds", type=float, default=2.0)
    parser.add_argument("--prompt-ids", default=None)
    parser.add_argument("--prompt-ids-file", default=None)
    parser.add_argument("--usage-jsonl", default=None)
    args = parser.parse_args()

    api_key = _resolve_api_key(args.api_key)
    if not api_key:
        raise SystemExit(
            "Missing API key. Set DASHSCOPE_API_KEY or pass --api-key for Bailian/Qwen API access."
        )

    prompt_filter = _load_prompt_id_filter(args.prompt_ids, args.prompt_ids_file)
    asyncio.run(
        main_async(
            trace_dir=Path(args.trace_dir),
            prompt_dir=args.prompt_dir,
            output_dir=args.output_dir,
            output_suffix=args.output_suffix,
            shard_id=args.shard_id,
            num_shards=args.num_shards,
            max_concurrent=args.max_concurrent,
            prompt_filter=prompt_filter,
            endpoint=args.endpoint,
            api_key=api_key,
            model=args.model,
            temperature=args.temperature,
            enable_thinking=args.enable_thinking,
            thinking_budget=args.thinking_budget,
            timeout_s=args.timeout_seconds,
            max_retries=args.max_retries,
            retry_delay_s=args.retry_delay_seconds,
            usage_jsonl=Path(args.usage_jsonl).resolve() if args.usage_jsonl else None,
        )
    )


if __name__ == "__main__":
    main()
