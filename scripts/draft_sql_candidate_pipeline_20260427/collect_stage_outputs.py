#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "field_recall_standalone"
sys.path.insert(0, str(ROOT / "src"))

from field_recall.dataset import write_jsonl
from field_recall.parsers import (
    extract_table_descriptions,
    parse_field_rewrite_json,
    parse_conditions_json,
    parse_direct_tables_columns,
    parse_draft_sql_tables_columns,
    parse_rewrite_markdown,
)

def _base_stem(path: Path) -> str:
    stem = path.stem
    for suffix in ("_direct_gemini", "_direct", "_gemini", "_qwen"):
        if stem.endswith(suffix):
            return stem[: -len(suffix)]
    return stem


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw-dir", required=True)
    parser.add_argument(
        "--stage",
        choices=["stage1_rewrite", "stage2_field_rewrite", "stage3_tables_columns", "stage3_draft_sql", "stage3_conditions"],
        required=True,
    )
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    raw_dir = Path(args.raw_dir)
    rows = []
    for path in sorted(raw_dir.glob("*.txt")):
        text = path.read_text(encoding="utf-8", errors="replace")
        if args.stage == "stage1_rewrite":
            qid = int(_base_stem(path))
            parsed = parse_rewrite_markdown(text)
        elif args.stage == "stage2_field_rewrite":
            parsed = {
                "field_rewrites": parse_field_rewrite_json(text),
                "table_descriptions": extract_table_descriptions(text),
                "db_id": _base_stem(path),
            }
        elif args.stage == "stage3_tables_columns":
            qid = int(_base_stem(path))
            parsed = {"tables": parse_direct_tables_columns(text)}
        elif args.stage == "stage3_draft_sql":
            qid = int(_base_stem(path))
            parsed = {"draft_sql_tables": {k: sorted(v) for k, v in parse_draft_sql_tables_columns(text).items()}}
        else:
            qid = int(_base_stem(path))
            parsed = {"conditions": parse_conditions_json(text)}
        if args.stage != "stage2_field_rewrite":
            parsed["qid"] = qid
        rows.append(parsed)
    write_jsonl(rows, args.output)
    print(f"Collected {len(rows)} rows into {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
