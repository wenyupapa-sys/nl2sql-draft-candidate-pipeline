#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any


def _iter_usage_files(paths: list[str], run_root: str | None) -> list[Path]:
    out: list[Path] = []
    for item in paths:
        path = Path(item)
        if path.is_dir():
            out.extend(sorted(path.rglob("*.jsonl")))
        elif path.exists():
            out.append(path)
    if run_root:
        out.extend(sorted(Path(run_root).rglob("usage/**/*.jsonl")))
        out.extend(sorted((Path(run_root) / "usage").rglob("*.jsonl")) if (Path(run_root) / "usage").exists() else [])
    deduped: list[Path] = []
    seen: set[Path] = set()
    for path in out:
        resolved = path.resolve()
        if resolved not in seen and path.exists():
            seen.add(resolved)
            deduped.append(path)
    return deduped


def _int(row: dict[str, Any], key: str) -> int:
    try:
        return int(row.get(key, 0) or 0)
    except Exception:
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Aggregate model usage JSONL files.")
    parser.add_argument("--usage-path", action="append", default=[], help="Usage JSONL file or directory. Can be repeated.")
    parser.add_argument("--run-root", help="Run root containing a usage/ directory.")
    parser.add_argument("--output-json", required=True)
    parser.add_argument("--output-md", required=True)
    args = parser.parse_args()

    files = _iter_usage_files(args.usage_path, args.run_root)
    by_model: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "calls": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "thinking_tokens": 0,
            "total_tokens": 0,
            "estimated_input_tokens": 0,
            "embedding_items": 0,
            "input_chars": 0,
        }
    )
    by_file: dict[str, dict[str, int]] = {}
    skipped = 0

    for file_path in files:
        file_totals = {
            "rows": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "thinking_tokens": 0,
            "total_tokens": 0,
            "estimated_input_tokens": 0,
        }
        for raw in file_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            text = raw.strip()
            if not text:
                continue
            try:
                row = json.loads(text)
            except json.JSONDecodeError:
                skipped += 1
                continue
            provider = str(row.get("provider") or "unknown")
            model = str(row.get("model") or "unknown")
            key = f"{provider}/{model}"
            prompt_tokens = _int(row, "prompt_tokens")
            estimated_input_tokens = _int(row, "estimated_input_tokens")
            completion_tokens = _int(row, "completion_tokens")
            thinking_tokens = _int(row, "thinking_tokens")
            total_tokens = _int(row, "total_tokens")
            if not total_tokens:
                total_tokens = prompt_tokens + completion_tokens + thinking_tokens

            bucket = by_model[key]
            bucket["provider"] = provider
            bucket["model"] = model
            bucket["calls"] += 1
            bucket["prompt_tokens"] += prompt_tokens
            bucket["completion_tokens"] += completion_tokens
            bucket["thinking_tokens"] += thinking_tokens
            bucket["total_tokens"] += total_tokens
            bucket["estimated_input_tokens"] += estimated_input_tokens
            bucket["embedding_items"] += _int(row, "embedding_items")
            bucket["input_chars"] += _int(row, "input_chars")

            file_totals["rows"] += 1
            file_totals["prompt_tokens"] += prompt_tokens
            file_totals["completion_tokens"] += completion_tokens
            file_totals["thinking_tokens"] += thinking_tokens
            file_totals["total_tokens"] += total_tokens
            file_totals["estimated_input_tokens"] += estimated_input_tokens
        by_file[str(file_path)] = file_totals

    totals = {
        "calls": sum(row["calls"] for row in by_model.values()),
        "prompt_tokens": sum(row["prompt_tokens"] for row in by_model.values()),
        "completion_tokens": sum(row["completion_tokens"] for row in by_model.values()),
        "thinking_tokens": sum(row["thinking_tokens"] for row in by_model.values()),
        "total_tokens": sum(row["total_tokens"] for row in by_model.values()),
        "estimated_embedding_input_tokens": sum(row["estimated_input_tokens"] for row in by_model.values()),
        "embedding_items": sum(row["embedding_items"] for row in by_model.values()),
        "input_chars": sum(row["input_chars"] for row in by_model.values()),
    }
    payload = {
        "usage_files": [str(path) for path in files],
        "skipped_bad_lines": skipped,
        "totals": totals,
        "by_model": dict(sorted(by_model.items())),
        "by_file": by_file,
    }

    output_json = Path(args.output_json)
    output_md = Path(args.output_md)
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_md.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# Usage Summary",
        "",
        f"- usage_files: `{len(files)}`",
        f"- calls: `{totals['calls']}`",
        f"- prompt_tokens: `{totals['prompt_tokens']}`",
        f"- completion_tokens: `{totals['completion_tokens']}`",
        f"- thinking_tokens: `{totals['thinking_tokens']}`",
        f"- total_tokens: `{totals['total_tokens']}`",
        f"- estimated_embedding_input_tokens: `{totals['estimated_embedding_input_tokens']}`",
        "",
        "| provider/model | calls | prompt | completion | thinking | total | embed_est_input |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for key, row in sorted(by_model.items()):
        lines.append(
            "| "
            + key
            + f" | {row['calls']} | {row['prompt_tokens']} | {row['completion_tokens']} | "
            + f"{row['thinking_tokens']} | {row['total_tokens']} | {row['estimated_input_tokens']} |"
        )
    output_md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {output_json}")
    print(f"Wrote {output_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
