#!/usr/bin/env python3
"""Run Gemini native API on a directory of prompt files with sharding support.

Usage:
    python scripts/run_prompt_dir_gemini_api_batch.py \
        --trace-dir output/run_20260415_v12_direct \
        --prompt-dir sql_prompts \
        --output-dir gen_results_gemini \
        --shard-id 0 --num-shards 4 \
        --model gemini-3.1-pro-preview
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


GEMINI_NATIVE_URL = "https://generativelanguage.googleapis.com/v1beta/models"
DEFAULT_API_KEY_ENV = ("GEMINI_API_KEYS", "GEMINI_API_KEY")
DISABLE_PROXY_VALUES = {"", "none", "off", "false", "0", "direct"}


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
    return blocks[-1].strip() if blocks else None


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


def _parse_hardcoded_keys_from_parallel_script() -> list[str]:
    script_path = Path(__file__).with_name("run_gen_gemini_parallel.sh")
    if not script_path.is_file():
        return []
    text = script_path.read_text(encoding="utf-8")
    keys = []
    for key_name in ("KEY1", "KEY2"):
        match = re.search(rf'{key_name}="([^"]+)"', text)
        if match:
            keys.append(match.group(1).strip())
    return [k for k in keys if k]


def _parse_proxy_from_parallel_script() -> str | None:
    script_path = Path(__file__).with_name("run_gen_gemini_parallel.sh")
    if not script_path.is_file():
        return None
    text = script_path.read_text(encoding="utf-8")
    match = re.search(r'PROXY="([^"]+)"', text)
    if match:
        return match.group(1).strip()
    return None


def _parse_macos_system_https_proxy() -> str | None:
    try:
        raw = subprocess.check_output(
            ["scutil", "--proxy"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
    except Exception:
        return None
    enabled = False
    host = None
    port = None
    for line in raw.splitlines():
        text = line.strip()
        if text == "HTTPSEnable : 1":
            enabled = True
        elif text.startswith("HTTPSProxy : "):
            host = text.split(" : ", 1)[1].strip()
        elif text.startswith("HTTPSPort : "):
            port = text.split(" : ", 1)[1].strip()
    if enabled and host and port:
        return f"http://{host}:{port}"
    return None


def _resolve_api_keys(explicit: str | None) -> list[str]:
    values: list[str] = []
    if explicit:
        values.extend(k.strip() for k in explicit.split(",") if k.strip())
    if not values:
        for key in DEFAULT_API_KEY_ENV:
            raw = os.getenv(key, "")
            if raw:
                values.extend(k.strip() for k in raw.split(",") if k.strip())
    if not values:
        values.extend(_parse_hardcoded_keys_from_parallel_script())
    deduped: list[str] = []
    seen = set()
    for value in values:
        if value and value not in seen:
            deduped.append(value)
            seen.add(value)
    return deduped


def _resolve_proxy(explicit: str | None) -> str | None:
    if explicit is not None:
        text = explicit.strip()
        return None if text.lower() in DISABLE_PROXY_VALUES else text
    for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        value = os.getenv(key)
        if value and value.strip().lower() not in DISABLE_PROXY_VALUES:
            return value
    return _parse_macos_system_https_proxy()


def _build_thinking_config(
    *,
    model: str,
    thinking_level: str,
    thinking_budget: int | None,
    include_thoughts: bool,
) -> dict | None:
    """Build Gemini thinkingConfig for both Gemini 3 and 2.5-style controls."""
    config: dict = {}
    if thinking_budget is not None:
        config["thinkingBudget"] = thinking_budget
    else:
        level = thinking_level.strip().lower()
        if level in DISABLE_PROXY_VALUES | {"disabled"}:
            if model.startswith("gemini-3"):
                # Gemini 3 Flash does not expose true off in the current API;
                # minimal is the closest low-budget setting.
                config["thinkingLevel"] = "minimal"
        else:
            config["thinkingLevel"] = level
    if include_thoughts:
        config["includeThoughts"] = True
    return config or None


def _call_gemini_sync(
    *,
    prompt_text: str,
    model: str,
    api_keys: list[str],
    proxy: str | None,
    temperature: float,
    max_output_tokens: int,
    thinking_level: str,
    thinking_budget: int | None,
    include_thoughts: bool,
    timeout_s: float,
    max_retries: int,
    retry_delay_s: float,
) -> tuple[str, str, dict]:
    generation_config = {
        "temperature": temperature,
        "maxOutputTokens": max_output_tokens,
    }
    thinking_config = _build_thinking_config(
        model=model,
        thinking_level=thinking_level,
        thinking_budget=thinking_budget,
        include_thoughts=include_thoughts,
    )
    if thinking_config:
        generation_config["thinkingConfig"] = thinking_config

    payload = json.dumps(
        {
            "contents": [{"role": "user", "parts": [{"text": prompt_text}]}],
            "generationConfig": generation_config,
        }
    ).encode("utf-8")

    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {})
    )
    key_cycle = itertools.cycle(api_keys)
    last_error: str | None = None

    for attempt in range(1, max_retries + 1):
        api_key = next(key_cycle)
        req = urllib.request.Request(
            f"{GEMINI_NATIVE_URL}/{model}:generateContent?key={api_key}",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with opener.open(req, timeout=timeout_s) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            candidates = data.get("candidates") or []
            if not candidates:
                raise RuntimeError(f"empty candidates in response: {json.dumps(data)[:500]}")

            parts = candidates[0].get("content", {}).get("parts", [])
            content_parts = []
            thinking_parts = []
            for part in parts:
                text = part.get("text", "")
                if not text:
                    continue
                if part.get("thought"):
                    thinking_parts.append(text)
                else:
                    content_parts.append(text)

            content = "\n".join(content_parts).strip()
            thinking = "\n".join(thinking_parts).strip()
            if not content and thinking:
                content = thinking
            if not content:
                raise RuntimeError(f"empty Gemini content: {json.dumps(data)[:500]}")

            usage = data.get("usageMetadata", {})
            normalized_usage = {
                "prompt_tokens": usage.get("promptTokenCount", 0),
                "completion_tokens": usage.get("candidatesTokenCount", 0),
                "total_tokens": usage.get("totalTokenCount", 0),
                "thinking_tokens": usage.get("thoughtsTokenCount", 0),
            }
            return content, thinking, normalized_usage
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

    raise RuntimeError(last_error or "unknown Gemini API error")


async def run_prompt(
    prompt_path: Path,
    output_path: Path,
    semaphore: asyncio.Semaphore,
    *,
    api_keys: list[str],
    model: str,
    proxy: str | None,
    temperature: float,
    max_output_tokens: int,
    thinking_level: str,
    thinking_budget: int | None,
    include_thoughts: bool,
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
            content, thinking, usage = await asyncio.to_thread(
                _call_gemini_sync,
                prompt_text=prompt_text,
                model=model,
                api_keys=api_keys,
                proxy=proxy,
                temperature=temperature,
                max_output_tokens=max_output_tokens,
                thinking_level=thinking_level,
                thinking_budget=thinking_budget,
                include_thoughts=include_thoughts,
                timeout_s=timeout_s,
                max_retries=max_retries,
                retry_delay_s=retry_delay_s,
            )
            full_text = (
                f"[THINKING]\n{thinking}\n\n[RESPONSE]\n{content}" if thinking else content
            )
            output_path.write_text(full_text, encoding="utf-8")
            sql = _extract_sql(content)
            print(
                f"  OK  {qid} sql={'ok' if sql else 'MISSING'} "
                f"tokens={usage.get('total_tokens', 0)} ({len(content)} chars)"
            )
            return {
                "qid": qid,
                "status": "ok",
                "model": model,
                "provider": "gemini",
                "sql": bool(sql),
                "usage": usage,
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
    api_keys: list[str],
    model: str,
    proxy: str | None,
    temperature: float,
    max_output_tokens: int,
    thinking_level: str,
    thinking_budget: int | None,
    include_thoughts: bool,
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
        f"thinking_level={thinking_level}, thinking_budget={thinking_budget}, "
        f"proxy={'on' if proxy else 'none'}"
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
                api_keys=api_keys,
                model=model,
                proxy=proxy,
                temperature=temperature,
                max_output_tokens=max_output_tokens,
                thinking_level=thinking_level,
                thinking_budget=thinking_budget,
                include_thoughts=include_thoughts,
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
                "provider": "gemini",
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
    parser.add_argument("--output-dir", default="gen_results_gemini")
    parser.add_argument("--output-suffix", default="_direct_gemini")
    parser.add_argument("--shard-id", type=int, default=0)
    parser.add_argument("--num-shards", type=int, default=1)
    parser.add_argument("--max-concurrent", type=int, default=1)
    parser.add_argument("--model", default="gemini-3.1-pro-preview")
    parser.add_argument("--api-keys", default=None)
    parser.add_argument("--proxy", default=None)
    parser.add_argument("--temperature", type=float, default=1.0)
    parser.add_argument("--max-output-tokens", type=int, default=16384)
    parser.add_argument("--thinking-level", default="none")
    parser.add_argument(
        "--thinking-budget",
        type=int,
        default=None,
        help="Optional thinkingBudget override. When set, it takes precedence over --thinking-level.",
    )
    parser.add_argument("--include-thoughts", action="store_true")
    parser.add_argument("--timeout-seconds", type=float, default=180.0)
    parser.add_argument("--max-retries", type=int, default=5)
    parser.add_argument("--retry-delay-seconds", type=float, default=5.0)
    parser.add_argument("--prompt-ids", default=None)
    parser.add_argument("--prompt-ids-file", default=None)
    parser.add_argument("--usage-jsonl", default=None)
    args = parser.parse_args()

    api_keys = _resolve_api_keys(args.api_keys)
    if not api_keys:
        raise SystemExit(
            "Missing Gemini API keys. Set GEMINI_API_KEYS/GEMINI_API_KEY or pass --api-keys."
        )

    prompt_filter = _load_prompt_id_filter(args.prompt_ids, args.prompt_ids_file)
    proxy = _resolve_proxy(args.proxy)
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
            api_keys=api_keys,
            model=args.model,
            proxy=proxy,
            temperature=args.temperature,
            max_output_tokens=args.max_output_tokens,
            thinking_level=args.thinking_level,
            thinking_budget=args.thinking_budget,
            include_thoughts=args.include_thoughts,
            timeout_s=args.timeout_seconds,
            max_retries=args.max_retries,
            retry_delay_s=args.retry_delay_seconds,
            usage_jsonl=Path(args.usage_jsonl).resolve() if args.usage_jsonl else None,
        )
    )


if __name__ == "__main__":
    main()
