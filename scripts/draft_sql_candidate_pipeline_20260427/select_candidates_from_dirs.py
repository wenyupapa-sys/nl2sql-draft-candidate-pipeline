#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import nullcontext
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from candidate_selector import HybridPairwiseSelector, SimpleMajoritySelector
from run_prompt_dir_gemini_api_batch import (
    _call_gemini_sync,
    _resolve_api_keys,
    _resolve_proxy,
)


def _extract_sql(text: str) -> str:
    cdata = re.findall(r"<result>\s*<!\[CDATA\[(.*?)\]\]>\s*</result>", text, re.DOTALL | re.I)
    if cdata:
        return cdata[-1].strip()
    xml = re.findall(r"<result>\s*(.*?)\s*</result>", text, re.DOTALL | re.I)
    if xml:
        return xml[-1].strip()
    blocks = re.findall(r"```(?:postgresql|sql)?\s*\n(.*?)```", text, re.DOTALL | re.I)
    if blocks:
        return blocks[-1].strip()
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    return text


def _load_manifest(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _sqlite_db_path(dataset_root: Path, db_id: str) -> Path:
    for rel in ("dev_databases", "test_databases", "databases"):
        candidate = dataset_root / rel / db_id / f"{db_id}.sqlite"
        if candidate.exists():
            return candidate
    raise FileNotFoundError(f"SQLite database not found for {db_id} under {dataset_root}")


def _find_result_file(result_dir: Path, qid: int) -> Path | None:
    for name in (f"{qid}.txt", f"{qid}_direct.txt", f"{qid}_direct_gemini.txt"):
        path = result_dir / name
        if path.exists() and path.stat().st_size > 0:
            return path
    matches = sorted(result_dir.glob(f"{qid}_*.txt"))
    return matches[0] if matches else None


def _parse_candidate_dir(value: str) -> tuple[str, Path]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("--candidate-dir must be name=/path")
    name, path = value.split("=", 1)
    return name.strip(), Path(path).resolve()


def _load_pairwise_schema(schema_dir: Path | None, qid: int) -> str:
    if schema_dir is None:
        return ""
    path = schema_dir / f"{qid}.md"
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="ignore").strip()


def _default_usage_jsonl(output_dir: Path) -> Path:
    run_root = output_dir.parents[1] if len(output_dir.parents) > 1 else output_dir.parent
    return run_root / "usage" / "selector_pairwise.jsonl"


def _build_pairwise_caller(args: argparse.Namespace):
    api_keys = _resolve_api_keys(args.pairwise_api_keys)
    if not api_keys:
        return None
    proxy = _resolve_proxy(args.pairwise_proxy)

    def _caller(prompt: str, prompt_id: str) -> tuple[str, dict]:
        content, _thinking, usage = _call_gemini_sync(
            prompt_text=prompt,
            model=args.pairwise_model,
            api_keys=api_keys,
            proxy=proxy,
            temperature=0.0,
            max_output_tokens=args.pairwise_max_output_tokens,
            thinking_level=args.pairwise_thinking_level,
            thinking_budget=None,
            include_thoughts=False,
            timeout_s=60.0,
            max_retries=3,
            retry_delay_s=3.0,
        )
        return content, usage

    return _caller


def _usage_writer(handle, lock: threading.Lock | None = None):
    def _write(row: dict[str, Any]) -> None:
        if lock:
            with lock:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
                handle.flush()
        else:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            handle.flush()

    return _write


def _process_row(
    row: dict[str, Any],
    *,
    args: argparse.Namespace,
    selector: Any,
    output_dir: Path,
    metadata_dir: Path,
    simple_dir: Path | None,
    pairwise_schema_dir: Path | None,
    dataset_root: Path,
) -> int:
    qid = int(row["qid"])
    output_path = output_dir / f"{qid}.txt"
    metadata_path = metadata_dir / f"{qid}.json"
    if not args.force and output_path.exists() and output_path.stat().st_size > 0 and metadata_path.exists():
        return qid

    difficulty = str(row.get("difficulty", "")).lower()
    db_path = _sqlite_db_path(dataset_root, str(row["db_id"]))
    metadata: dict[str, Any] = {
        "qid": qid,
        "difficulty": difficulty,
        "selector_mode": args.selector_mode,
        "candidates": [],
    }

    if difficulty == "simple" and simple_dir is not None:
        src = _find_result_file(simple_dir, qid)
        if src is None:
            metadata["selected"] = None
            metadata["error"] = "missing_simple_result"
        else:
            shutil.copyfile(src, output_path)
            metadata["selected"] = {"source": "simple", "path": str(src)}
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        return qid

    candidates = []
    for name, result_dir in args.candidate_dir:
        path = _find_result_file(result_dir, qid)
        if path is None:
            metadata["candidates"].append({"name": name, "path": None, "status": "missing"})
            continue
        raw = path.read_text(encoding="utf-8", errors="ignore")
        sql = _extract_sql(raw)
        candidates.append({"sql": sql, "template": name, "path": str(path)})
        metadata["candidates"].append({"name": name, "path": str(path), "status": "loaded"})
    if not candidates:
        metadata["selected"] = None
        metadata["error"] = "no_candidates"
    else:
        if args.selector_mode == "hybrid":
            selected = selector.select(
                candidates,
                str(db_path),
                timeout=args.timeout,
                qid=qid,
                question=str(row.get("question") or row.get("query") or ""),
                evidence=str(row.get("evidence") or ""),
                schema=_load_pairwise_schema(pairwise_schema_dir, qid),
            )
        else:
            selected = selector.select(candidates, str(db_path), timeout=args.timeout)
        output_path.write_text(selected.get("sql", ""), encoding="utf-8")
        metadata["selected"] = selected
        metadata["fallback_policy"] = args.fallback_policy
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    return qid


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--dataset-root", required=True)
    parser.add_argument("--simple-result-dir")
    parser.add_argument("--candidate-dir", action="append", default=[], type=_parse_candidate_dir)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--metadata-dir", required=True)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--fallback-policy", default="selector_by_db")
    parser.add_argument("--selector-mode", choices=["majority", "hybrid"], default="majority")
    parser.add_argument("--pairwise-model", default="gemini-3.1-pro-preview")
    parser.add_argument("--pairwise-proxy", default=None)
    parser.add_argument("--pairwise-api-keys", default=None)
    parser.add_argument("--pairwise-threshold", type=float, default=0.55)
    parser.add_argument("--pairwise-thinking-level", default="low")
    parser.add_argument("--pairwise-max-output-tokens", type=int, default=128)
    parser.add_argument("--pairwise-schema-dir", default=None)
    parser.add_argument("--include-result-preview", action="store_true")
    parser.add_argument("--usage-jsonl", default=None)
    parser.add_argument("--max-workers", type=int, default=1, help="Parallel qid workers for independent candidate selection.")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    rows = _load_manifest(Path(args.manifest))
    dataset_root = Path(args.dataset_root)
    output_dir = Path(args.output_dir)
    metadata_dir = Path(args.metadata_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    metadata_dir.mkdir(parents=True, exist_ok=True)
    usage_path = (
        Path(args.usage_jsonl).resolve()
        if args.usage_jsonl
        else _default_usage_jsonl(output_dir)
    )
    usage_cm = nullcontext(None)
    pairwise_caller = None
    if args.selector_mode == "hybrid":
        usage_path.parent.mkdir(parents=True, exist_ok=True)
        usage_cm = usage_path.open("a", encoding="utf-8")
        pairwise_caller = _build_pairwise_caller(args)
    simple_dir = Path(args.simple_result_dir).resolve() if args.simple_result_dir else None
    pairwise_schema_dir = Path(args.pairwise_schema_dir).resolve() if args.pairwise_schema_dir else None

    with usage_cm as usage_handle:
        usage_lock = threading.Lock() if usage_handle and args.max_workers > 1 else None
        selector = (
            HybridPairwiseSelector(
                pairwise_caller=pairwise_caller,
                usage_writer=_usage_writer(usage_handle, usage_lock) if usage_handle else None,
                model=args.pairwise_model,
                threshold=args.pairwise_threshold,
                include_result_preview=args.include_result_preview,
            )
            if args.selector_mode == "hybrid"
            else SimpleMajoritySelector()
        )

        worker_count = max(1, int(args.max_workers or 1))
        if worker_count == 1:
            for row in rows:
                _process_row(
                    row,
                    args=args,
                    selector=selector,
                    output_dir=output_dir,
                    metadata_dir=metadata_dir,
                    simple_dir=simple_dir,
                    pairwise_schema_dir=pairwise_schema_dir,
                    dataset_root=dataset_root,
                )
        else:
            print(f"Selecting {len(rows)} qids with max_workers={worker_count}", flush=True)
            with ThreadPoolExecutor(max_workers=worker_count) as pool:
                futures = [
                    pool.submit(
                        _process_row,
                        row,
                        args=args,
                        selector=selector,
                        output_dir=output_dir,
                        metadata_dir=metadata_dir,
                        simple_dir=simple_dir,
                        pairwise_schema_dir=pairwise_schema_dir,
                        dataset_root=dataset_root,
                    )
                    for row in rows
                ]
                done = 0
                for future in as_completed(futures):
                    future.result()
                    done += 1
                    if done % 25 == 0 or done == len(futures):
                        print(f"  selected {done}/{len(futures)}", flush=True)

    print(f"Wrote selected results to {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
