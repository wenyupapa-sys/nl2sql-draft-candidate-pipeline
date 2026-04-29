#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FR_ROOT = PROJECT_ROOT / "field_recall_standalone"
sys.path.insert(0, str(FR_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT))

from field_recall.dataset import load_jsonl
from field_recall.schema_assets import SchemaAssets


TYPE_MAP = {
    "text": "TEXT",
    "integer": "INTEGER",
    "number": "REAL",
    "real": "REAL",
    "float": "REAL",
    "double": "REAL",
    "boolean": "INTEGER",
    "bool": "INTEGER",
    "date": "TEXT",
    "time": "TEXT",
    "datetime": "TEXT",
    "year": "INTEGER",
}


def _type_name(raw_type: object) -> str:
    return TYPE_MAP.get(str(raw_type).lower(), str(raw_type).upper())


def _md_escape(value: object) -> str:
    text = str(value if value is not None else "").replace("\n", " ").strip()
    return text.replace("|", "\\|")


def _load_final_fields(path: Path) -> dict[int, set[tuple[str, str]]]:
    out: dict[int, set[tuple[str, str]]] = {}
    for row in load_jsonl(path):
        try:
            qid = int(row["qid"])
        except Exception:
            continue
        fields = row.get("final_fields") or row.get("fields") or []
        selected: set[tuple[str, str]] = set()
        for item in fields:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                selected.add((str(item[0]).lower(), str(item[1]).lower()))
            elif isinstance(item, dict):
                table = item.get("table") or item.get("table_name")
                column = item.get("column") or item.get("column_name")
                if table and column:
                    selected.add((str(table).lower(), str(column).lower()))
        out[qid] = selected
    return out


def _load_stage2_maps(path: Path | None) -> tuple[dict[str, dict], dict[str, dict[str, str]]]:
    field_rewrites_by_db: dict[str, dict] = {}
    table_descs_by_db: dict[str, dict[str, str]] = {}
    if path is None or not path.exists():
        return field_rewrites_by_db, table_descs_by_db
    for row in load_jsonl(path):
        db_id = str(row.get("db_id") or "").strip()
        if not db_id:
            continue
        if row.get("field_rewrites"):
            field_rewrites_by_db[db_id] = row["field_rewrites"]
        if row.get("table_descriptions"):
            table_descs_by_db[db_id] = row["table_descriptions"]
    return field_rewrites_by_db, table_descs_by_db


def _column_records(meta: dict[str, Any]) -> list[dict[str, Any]]:
    records = []
    for idx, ((table_idx, col_name), (_, readable_name), col_type) in enumerate(
        zip(meta["column_names_original"], meta["column_names"], meta["column_types"])
    ):
        if int(table_idx) < 0:
            continue
        table_name = str(meta["table_names_original"][table_idx])
        records.append(
            {
                "global_idx": idx,
                "table_idx": int(table_idx),
                "table_name": table_name,
                "table_lc": table_name.lower(),
                "column_name": str(col_name),
                "column_lc": str(col_name).lower(),
                "readable_name": str(readable_name or ""),
                "column_type": col_type,
            }
        )
    return records


def _table_desc_fallback(schema_assets: SchemaAssets, db_id: str, table_name: str) -> str:
    text = schema_assets.load_db_descriptions(db_id).get(table_name, "").strip()
    return text.split("\n\n", 1)[0].strip() if text else ""


def _lookup_rewrite(field_rewrites: dict | None, table_name: str, column_name: str) -> str:
    if not field_rewrites:
        return ""
    table_keys = [table_name, table_name.lower()]
    column_keys = [column_name, column_name.lower()]
    values = []
    for table_key in table_keys:
        table_payload = field_rewrites.get(table_key)
        if not isinstance(table_payload, dict):
            continue
        for column_key in column_keys:
            raw = table_payload.get(column_key)
            if isinstance(raw, list):
                values.extend(str(x).strip() for x in raw if str(x).strip())
            elif raw:
                values.append(str(raw).strip())
    return " ; ".join(dict.fromkeys(values))


def _render_schema(
    *,
    schema_assets: SchemaAssets,
    db_id: str,
    selected_fields: set[tuple[str, str]],
    field_rewrites: dict | None,
    table_descriptions: dict[str, str] | None,
    include_value_examples: bool,
) -> str:
    meta = schema_assets.load_tables_meta(db_id)
    llm_defs = schema_assets.load_schema_defs(db_id).get("tables", {})
    meanings = schema_assets.load_column_meanings_map(db_id)
    table_names = [str(x) for x in meta["table_names_original"]]
    table_lookup = {name.lower(): name for name in table_names}
    table_order = {name.lower(): i for i, name in enumerate(table_names)}
    records = _column_records(meta)
    records_by_table: dict[str, list[dict[str, Any]]] = defaultdict(list)
    record_by_idx = {int(row["global_idx"]): row for row in records}
    for row in records:
        records_by_table[row["table_lc"]].append(row)

    selected_by_table: dict[str, set[str]] = defaultdict(set)
    if selected_fields:
        for table, column in selected_fields:
            selected_by_table[str(table).lower()].add(str(column).lower())
    else:
        for row in records:
            selected_by_table[row["table_lc"]].add(row["column_lc"])

    pk_by_table: dict[str, set[str]] = defaultdict(set)
    for pk_idx in meta.get("primary_keys", []):
        row = record_by_idx.get(int(pk_idx))
        if row:
            pk_by_table[row["table_lc"]].add(row["column_lc"])

    fk_edges = []
    for from_idx, to_idx in meta.get("foreign_keys", []):
        from_row = record_by_idx.get(int(from_idx))
        to_row = record_by_idx.get(int(to_idx))
        if from_row and to_row:
            fk_edges.append((from_row, to_row))

    for table_lc in list(selected_by_table):
        selected_by_table[table_lc].update(pk_by_table.get(table_lc, set()))
    for from_row, to_row in fk_edges:
        if from_row["table_lc"] in selected_by_table and to_row["table_lc"] in selected_by_table:
            selected_by_table[from_row["table_lc"]].add(from_row["column_lc"])
            selected_by_table[to_row["table_lc"]].add(to_row["column_lc"])

    lines = ["# Database Schema", ""]
    for table_lc in sorted(selected_by_table, key=lambda x: table_order.get(x, 10**9)):
        table_name = table_lookup.get(table_lc, table_lc)
        lines.append(f"## Table: {table_name}")
        lines.append("### Table description")
        table_desc = (
            (table_descriptions or {}).get(table_name, "").strip()
            or (table_descriptions or {}).get(table_lc, "").strip()
            or llm_defs.get(table_name, {}).get("table_description", "").strip()
            or _table_desc_fallback(schema_assets, db_id, table_name)
            or f"The {table_name} table stores records related to {table_name}."
        )
        lines.append(table_desc)
        lines.append("### Column information")
        if include_value_examples:
            lines.append("| column_name | column_type | column_description | value_examples |")
            lines.append("|-------------|-------------|-------------------|----------------|")
        else:
            lines.append("| column_name | column_type | column_description |")
            lines.append("|-------------|-------------|-------------------|")

        table_records = [
            row
            for row in records_by_table.get(table_lc, [])
            if row["column_lc"] in selected_by_table[table_lc]
        ]
        table_records.sort(key=lambda row: row["global_idx"])
        column_name_lookup = {row["column_lc"]: row["column_name"] for row in table_records}
        for row in table_records:
            col_name = row["column_name"]
            col_type = _type_name(row["column_type"])
            meaning = meanings.get(f"{table_name}.{col_name}") or meanings.get(
                f"{table_lc}.{row['column_lc']}"
            )
            rewrite = _lookup_rewrite(field_rewrites, table_name, col_name)
            rendered_desc = meaning or rewrite or row["readable_name"] or col_name
            if include_value_examples:
                samples = schema_assets.sample_values(db_id, table_name, col_name, limit=3)
                sample_text = "[" + ", ".join(repr(x) for x in samples) + "]" if samples else "[]"
                lines.append(
                    f"| {_md_escape(col_name)} | {_md_escape(col_type)} | {_md_escape(rendered_desc)} | {_md_escape(sample_text)} |"
                )
            else:
                lines.append(
                    f"| {_md_escape(col_name)} | {_md_escape(col_type)} | {_md_escape(rendered_desc)} |"
                )

        lines.append("### Primary keys")
        table_pks = sorted(pk_by_table.get(table_lc, set()), key=lambda x: column_name_lookup.get(x, x))
        if table_pks:
            for col_lc in table_pks:
                lines.append(f"- `{column_name_lookup.get(col_lc, col_lc)}`")
        else:
            lines.append("- none")

        lines.append("### Foreign keys")
        scoped_edges = [
            (from_row, to_row)
            for from_row, to_row in fk_edges
            if from_row["table_lc"] == table_lc and to_row["table_lc"] in selected_by_table
        ]
        if scoped_edges:
            for from_row, to_row in scoped_edges:
                lines.append(
                    f"- `{from_row['column_name']}` -> `{to_row['table_name']}.{to_row['column_name']}`"
                )
        else:
            lines.append("- none")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--dataset-root", required=True)
    parser.add_argument("--final-fields-jsonl", required=True)
    parser.add_argument("--field-rewrites-jsonl", default=None)
    parser.add_argument("--include-value-examples", action="store_true")
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    manifest_rows = [json.loads(line) for line in Path(args.manifest).read_text(encoding="utf-8").splitlines() if line.strip()]
    final_fields = _load_final_fields(Path(args.final_fields_jsonl))
    field_rewrites_by_db, table_descs_by_db = _load_stage2_maps(
        Path(args.field_rewrites_jsonl) if args.field_rewrites_jsonl else None
    )
    schema_assets = SchemaAssets(args.dataset_root)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    written = 0
    for row in manifest_rows:
        qid = int(row["qid"])
        db_id = str(row["db_id"])
        schema_text = _render_schema(
            schema_assets=schema_assets,
            db_id=db_id,
            selected_fields=final_fields.get(qid, set()),
            field_rewrites=field_rewrites_by_db.get(db_id),
            table_descriptions=table_descs_by_db.get(db_id),
            include_value_examples=args.include_value_examples,
        )
        (output_dir / f"{qid}.md").write_text(schema_text, encoding="utf-8")
        written += 1

    print(f"Wrote {written} pairwise schema blocks to {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
