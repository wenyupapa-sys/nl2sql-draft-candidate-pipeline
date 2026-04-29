#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from field_recall.dataset import load_jsonl
from field_recall.prompts import PromptLibrary
from field_recall.schema_assets import SchemaAssets


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--dataset-root", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--field-rewrites", default=None, help="Path to stage2_field_rewrite.jsonl (optional)")
    parser.add_argument("--include-stage3-conditions", action="store_true", help="Render stage3_conditions prompts (off by default)")
    parser.add_argument("--no-sample-values", action="store_true", help="Do not include sampled database values in prompts.")
    args = parser.parse_args()

    manifest = load_jsonl(args.manifest)
    prompts = PromptLibrary(ROOT / "prompts")
    schema_assets = SchemaAssets(args.dataset_root)
    output_root = Path(args.output_root)

    # Load field rewrites if available
    field_rewrites_by_db: dict[str, dict] = {}
    if args.field_rewrites and Path(args.field_rewrites).exists():
        for row in load_jsonl(args.field_rewrites):
            if row.get("db_id") and row.get("field_rewrites"):
                field_rewrites_by_db[row["db_id"]] = row["field_rewrites"]
        print(f"Loaded field rewrites for {len(field_rewrites_by_db)} databases")

    # Cache rich schema per DB to avoid re-rendering
    rich_schema_cache: dict[str, str] = {}

    # Stage 2 is DB-level, render once per DB.
    seen_dbs = set()
    for row in manifest:
        qid = int(row["qid"])
        db_id = row["db_id"]

        # Get or build rich schema for this DB
        if db_id not in rich_schema_cache:
            rewrites = field_rewrites_by_db.get(db_id)
            rich_schema_cache[db_id] = schema_assets.render_rich_schema(
                db_id,
                field_rewrites=rewrites,
                include_samples=not args.no_sample_values,
            )
        schema_summary = rich_schema_cache[db_id]
        schema_sql = schema_assets.render_prompt_schema_sql(db_id)
        column_meanings = schema_assets.render_column_meanings_text(db_id)

        write_text(
            output_root / "stage1_rewrite" / f"{qid}.txt",
            prompts.render("stage1_rewrite",
                question=row["question"],
                evidence=row["evidence"] or "(none)",
                schema_summary=schema_summary,
                schema=schema_sql,
                column_meanings=column_meanings),
        )
        render_ctx = {
            "question": row["question"],
            "evidence": row["evidence"] or "(none)",
            "schema_summary": schema_summary,
            "schema": schema_sql,
            "column_meanings": column_meanings,
        }
        write_text(output_root / "stage3_tables_columns" / f"{qid}.txt", prompts.render("stage3_tables_columns", **render_ctx))
        write_text(output_root / "stage3_draft_sql" / f"{qid}.txt", prompts.render("stage3_draft_sql", **render_ctx))
        if args.include_stage3_conditions:
            write_text(output_root / "stage3_conditions" / f"{qid}.txt", prompts.render("stage3_conditions", **render_ctx))

        if db_id not in seen_dbs:
            seen_dbs.add(db_id)
            summary = schema_assets.render_schema_summary(db_id, include_samples=not args.no_sample_values)
            write_text(output_root / "stage2_field_rewrite" / f"{db_id}.txt", prompts.render("stage2_field_rewrite", schema_summary=summary))

    metadata = {
        "manifest": str(Path(args.manifest).resolve()),
        "dataset_root": str(Path(args.dataset_root).resolve()),
        "count": len(manifest),
        "db_count": len(seen_dbs),
    }
    write_text(output_root / "render_metadata.json", json.dumps(metadata, ensure_ascii=False, indent=2))
    print(f"Rendered prompts for {len(manifest)} questions and {len(seen_dbs)} databases into {output_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
