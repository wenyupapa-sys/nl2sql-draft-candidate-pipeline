#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = ROOT.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT))

from field_recall.dataset import load_jsonl
from field_recall.schema_assets import SchemaAssets
from scripts.build_prompts import build_prompt, load_template


def _slug(template_name: str) -> str:
    return Path(template_name).stem


def _load_stage2_maps(path: Path) -> tuple[dict[str, dict], dict[str, dict[str, str]]]:
    field_rewrites_by_db: dict[str, dict] = {}
    table_descs_by_db: dict[str, dict[str, str]] = {}
    if not path.exists():
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


def _load_final_fields(path: Path) -> dict[int, set[tuple[str, str]]]:
    qid_to_fields: dict[int, set[tuple[str, str]]] = {}
    for row in load_jsonl(path):
        qid = int(row["qid"])
        fields: set[tuple[str, str]] = set()
        for table, column in row.get("final_fields") or []:
            fields.add((str(table).lower(), str(column).lower()))
        qid_to_fields[qid] = fields
    return qid_to_fields


def _load_cross_model_hints(path: Path | None) -> dict[int, str]:
    if path is None or not path.exists():
        return {}
    qid_to_hint: dict[int, str] = {}
    for row in load_jsonl(path):
        try:
            qid = int(row["qid"])
        except Exception:
            continue
        hint = str(row.get("hint_text") or "").strip()
        if hint:
            qid_to_hint[qid] = hint
    return qid_to_hint


def _table_order(meta: dict) -> dict[str, int]:
    return {str(name).lower(): i for i, name in enumerate(meta["table_names_original"])}


def _type_name(raw_type: str) -> str:
    mapping = {
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
    return mapping.get(str(raw_type).lower(), str(raw_type).upper())


def _md_escape(value: object) -> str:
    text = str(value if value is not None else "").replace("\n", " ").strip()
    return text.replace("|", "\\|")


def _format_samples(samples: list[str]) -> str:
    if not samples:
        return ""
    return ", ".join(_md_escape(s) for s in samples)


def _table_desc_fallback(schema_assets: SchemaAssets, db_id: str, table_name: str) -> str:
    md = schema_assets.load_db_descriptions(db_id).get(table_name, "").strip()
    if not md:
        return ""
    return md.split("\n\n", 1)[0].strip()


def _column_records_by_table(meta: dict) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    for idx, ((table_idx, col_name), (_, readable_name), col_type) in enumerate(
        zip(meta["column_names_original"], meta["column_names"], meta["column_types"])
    ):
        if table_idx < 0:
            continue
        row = {
            "global_idx": idx,
            "table_idx": table_idx,
            "column_name": col_name,
            "readable_name": readable_name,
            "column_type": col_type,
        }
        table_name = meta["table_names_original"][table_idx]
        out[str(table_name).lower()].append(row)
    return out


def _render_scoped_schema(
    schema_assets: SchemaAssets,
    db_id: str,
    selected_fields: set[tuple[str, str]],
    field_rewrites: dict[str, dict[str, list[str]]] | None,
    table_descriptions: dict[str, str] | None,
    include_value_examples: bool = True,
) -> str:
    meta = schema_assets.load_tables_meta(db_id)
    llm_defs = schema_assets.load_schema_defs(db_id).get("tables", {})
    meanings = schema_assets.load_column_meanings_map(db_id)
    table_names = meta["table_names_original"]
    table_lookup = {str(name).lower(): str(name) for name in table_names}
    col_records = _column_records_by_table(meta)
    selected_by_table: dict[str, set[str]] = defaultdict(set)
    for table, column in selected_fields:
        selected_by_table[table].add(column)

    fk_edges = schema_assets.fk_edges(db_id)
    table_sort = _table_order(meta)
    out: list[str] = []

    for table_lc in sorted(selected_by_table, key=lambda t: table_sort.get(t, 10**9)):
        table_name = table_lookup.get(table_lc, table_lc)
        out.append(f"## Table: {table_name}")
        stage2_desc = (table_descriptions or {}).get(table_name, "").strip()
        llm_desc = llm_defs.get(table_name, {}).get("table_description", "").strip()
        table_desc = stage2_desc or llm_desc or _table_desc_fallback(schema_assets, db_id, table_name)
        if table_desc:
            out.append("### Table description")
            out.append(table_desc)
            out.append("")
        if include_value_examples:
            out.append("| column_name | column_type | column_description | value_examples |")
            out.append("| --- | --- | --- | --- |")
        else:
            out.append("| column_name | column_type | column_description |")
            out.append("| --- | --- | --- |")

        record_lookup = {str(r["column_name"]).lower(): r for r in col_records.get(table_lc, [])}
        ordered_cols = [
            r for r in col_records.get(table_lc, [])
            if str(r["column_name"]).lower() in selected_by_table[table_lc]
        ]
        ordered_cols.sort(key=lambda r: r["global_idx"])

        # Keep unknown columns if they somehow exist in final_fields but not in metadata.
        known = {str(r["column_name"]).lower() for r in ordered_cols}
        unknown = sorted(selected_by_table[table_lc] - known)

        for row in ordered_cols:
            col_name = str(row["column_name"])
            col_type = _type_name(str(row["column_type"]))
            desc = (
                llm_defs.get(table_name, {})
                .get("columns", {})
                .get(col_name, "")
                .strip()
            )
            meaning = meanings.get(f"{table_name}.{col_name}") or meanings.get(
                f"{table_name.lower()}.{str(col_name).lower()}"
            )
            rewrites = []
            if field_rewrites:
                rewrites = (
                    field_rewrites.get(table_name, {}).get(col_name, [])
                    or field_rewrites.get(table_lc, {}).get(col_name, [])
                    or field_rewrites.get(table_lc, {}).get(str(col_name).lower(), [])
                )
            rewrite_desc = " ; ".join(str(x).strip() for x in rewrites if str(x).strip())
            rendered_desc = meaning or desc or rewrite_desc or row["readable_name"] or col_name
            samples = schema_assets.sample_values(db_id, table_name, col_name, limit=3) if include_value_examples else []
            cells = [
                _md_escape(col_name),
                _md_escape(col_type),
                _md_escape(rendered_desc),
            ]
            if include_value_examples:
                cells.append(_format_samples(samples))
            out.append(
                "| "
                + " | ".join(cells)
                + " |"
            )

        for col_lc in unknown:
            row = record_lookup.get(col_lc)
            col_name = row["column_name"] if row else col_lc
            if include_value_examples:
                out.append(f"| {_md_escape(col_name)} |  |  |  |")
            else:
                out.append(f"| {_md_escape(col_name)} |  |  |")

        scoped_edges = []
        for from_t, from_c, to_t, to_c in fk_edges:
            if from_t == table_lc and to_t in selected_by_table:
                if from_c in selected_by_table[from_t] or to_c in selected_by_table[to_t]:
                    scoped_edges.append((table_lookup.get(from_t, from_t), from_c, table_lookup.get(to_t, to_t), to_c))
        if scoped_edges:
            out.append("")
            out.append("Foreign Keys:")
            for from_t, from_c, to_t, to_c in scoped_edges:
                out.append(f"- {from_t}.{from_c} -> {to_t}.{to_c}")
        out.append("")

    return "\n".join(out).strip()


def _render_v12_legacy_schema(
    schema_assets: SchemaAssets,
    db_id: str,
    selected_fields: set[tuple[str, str]],
    table_descriptions: dict[str, str] | None,
    field_rewrites: dict[str, dict[str, list[str]]] | None = None,
    include_value_examples: bool = True,
) -> str:
    meta = schema_assets.load_tables_meta(db_id)
    llm_defs = schema_assets.load_schema_defs(db_id).get("tables", {})
    meanings = schema_assets.load_column_meanings_map(db_id)
    table_names = meta["table_names_original"]
    table_lookup = {str(name).lower(): str(name) for name in table_names}
    col_records = _column_records_by_table(meta)
    selected_by_table: dict[str, set[str]] = defaultdict(set)
    for table, column in selected_fields:
        selected_by_table[table].add(column)

    table_sort = _table_order(meta)
    rows = []

    pk_cols = set()
    fk_edges = []
    columns_flat = []
    for idx, ((table_idx, col_name), _, _) in enumerate(
        zip(meta["column_names_original"], meta["column_names"], meta["column_types"])
    ):
        columns_flat.append((idx, table_idx, col_name))
    for pk_idx in meta.get("primary_keys", []):
        _, table_idx, col_name = columns_flat[pk_idx]
        pk_cols.add((str(table_names[table_idx]).lower(), str(col_name).lower()))
    for from_idx, to_idx in meta.get("foreign_keys", []):
        _, from_tidx, from_col = columns_flat[from_idx]
        _, to_tidx, to_col = columns_flat[to_idx]
        fk_edges.append(
            (
                str(table_names[from_tidx]).lower(),
                str(from_col).lower(),
                str(table_names[to_tidx]).lower(),
                str(to_col).lower(),
            )
        )

    for table_lc in sorted(selected_by_table, key=lambda t: table_sort.get(t, 10**9)):
        table_name = table_lookup.get(table_lc, table_lc)
        rows.append(f"## Table: {table_name}")
        rows.append("### Table description")
        table_desc = (
            (table_descriptions or {}).get(table_name, "").strip()
            or llm_defs.get(table_name, {}).get("table_description", "").strip()
            or _table_desc_fallback(schema_assets, db_id, table_name)
            or f"The {table_name} table stores records related to {table_name}."
        )
        rows.append(table_desc)
        rows.append("### Column information")
        if include_value_examples:
            rows.append("| column_name | column_type | column_description | value_examples |")
            rows.append("|-------------|-------------|-------------------|----------------|")
        else:
            rows.append("| column_name | column_type | column_description |")
            rows.append("|-------------|-------------|-------------------|")

        ordered_cols = [
            r for r in col_records.get(table_lc, [])
            if str(r["column_name"]).lower() in selected_by_table[table_lc]
        ]
        ordered_cols.sort(key=lambda r: r["global_idx"])
        for row in ordered_cols:
            col_name = str(row["column_name"])
            col_type = _type_name(str(row["column_type"]))
            desc = (
                llm_defs.get(table_name, {})
                .get("columns", {})
                .get(col_name, "")
                .strip()
            )
            meaning = meanings.get(f"{table_name}.{col_name}") or meanings.get(
                f"{table_name.lower()}.{str(col_name).lower()}"
            )
            rewrites = []
            if field_rewrites:
                rewrites = (
                    field_rewrites.get(table_name, {}).get(col_name, [])
                    or field_rewrites.get(table_lc, {}).get(col_name, [])
                    or field_rewrites.get(table_lc, {}).get(str(col_name).lower(), [])
                )
            rewrite_desc = " ; ".join(str(x).strip() for x in rewrites if str(x).strip())
            rendered_desc = meaning or desc or rewrite_desc or row["readable_name"] or col_name
            if include_value_examples:
                samples = schema_assets.sample_values(db_id, table_name, col_name, limit=3)
                sample_text = "[" + ", ".join(repr(s) for s in samples) + "]" if samples else "[]"
                rows.append(
                    f"| {_md_escape(col_name)} | {_md_escape(col_type)} | {_md_escape(rendered_desc)} | {_md_escape(sample_text)} |"
                )
            else:
                rows.append(
                    f"| {_md_escape(col_name)} | {_md_escape(col_type)} | {_md_escape(rendered_desc)} |"
                )

        table_pks = [
            col for tbl, col in sorted(pk_cols)
            if tbl == table_lc and col in selected_by_table[table_lc]
        ]
        rows.append("### Primary keys")
        if table_pks:
            name_lookup = {str(r["column_name"]).lower(): str(r["column_name"]) for r in ordered_cols}
            for col in table_pks:
                rows.append(f"- `{name_lookup.get(col, col)}`")
        else:
            rows.append("- none")

        scoped_fks = [
            (from_col, to_tbl, to_col)
            for from_tbl, from_col, to_tbl, to_col in fk_edges
            if from_tbl == table_lc and from_col in selected_by_table[table_lc] and to_tbl in selected_by_table
        ]
        rows.append("### Foreign keys")
        if scoped_fks:
            name_lookup = {str(r["column_name"]).lower(): str(r["column_name"]) for r in ordered_cols}
            for from_col, to_tbl, to_col in scoped_fks:
                rows.append(
                    f"- `{name_lookup.get(from_col, from_col)}` -> `{table_lookup.get(to_tbl, to_tbl)}.{to_col}`"
                )
        else:
            rows.append("- none")
        rows.append("")

    return "\n".join(rows).strip()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--templates",
        nargs="+",
        required=True,
        help="Template filenames under templates/.",
    )
    parser.add_argument(
        "--manifest",
        default=str(ROOT / "data" / "manifests" / "bird_dev_questions_unique.jsonl"),
    )
    parser.add_argument(
        "--final-fields",
        default=str(ROOT / "output" / "full_union_gemini_qwen_t06" / "final_fields_coverage_best.jsonl"),
    )
    parser.add_argument(
        "--dataset-root",
        default=str(ROOT / "data" / "bird_dev"),
    )
    parser.add_argument(
        "--field-rewrites",
        default=str(ROOT / "output" / "full_current_gemini_baseline" / "stages" / "stage2_field_rewrite.jsonl"),
    )
    parser.add_argument(
        "--output-root",
        default=str(ROOT / "output" / "generated_prompts_from_pipeline_3_templates_20260422"),
    )
    parser.add_argument("--cross-model-hints-jsonl", default=None)
    parser.add_argument("--no-value-examples", action="store_true", help="Do not include sampled DB values in scoped final prompts.")
    parser.add_argument("--database", default=None)
    parser.add_argument("--difficulty", default=None)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    manifest_rows = load_jsonl(args.manifest)
    if args.database:
        manifest_rows = [row for row in manifest_rows if row["db_id"] == args.database]
    if args.difficulty:
        wanted = str(args.difficulty).strip().lower()
        manifest_rows = [row for row in manifest_rows if str(row.get("difficulty", "")).lower() == wanted]
    if args.limit:
        manifest_rows = manifest_rows[: args.limit]

    qid_to_fields = _load_final_fields(Path(args.final_fields))
    field_rewrites_by_db, table_descs_by_db = _load_stage2_maps(Path(args.field_rewrites))
    cross_model_hints = _load_cross_model_hints(
        Path(args.cross_model_hints_jsonl) if args.cross_model_hints_jsonl else None
    )
    schema_assets = SchemaAssets(args.dataset_root)

    output_root = Path(args.output_root)
    output_root.mkdir(parents=True, exist_ok=True)

    scoped_schema_cache: dict[tuple[int, str], str] = {}

    for template_name in args.templates:
        template_text = load_template(template_name)
        out_dir = output_root / _slug(template_name)
        out_dir.mkdir(parents=True, exist_ok=True)

        count = 0
        template_slug = _slug(template_name)
        for row in manifest_rows:
            qid = int(row["qid"])
            db_id = row["db_id"]
            cache_key = (qid, db_id, template_slug)
            if cache_key not in scoped_schema_cache:
                if template_slug == "query_prompt_v12_direct":
                    scoped_schema_cache[cache_key] = _render_v12_legacy_schema(
                        schema_assets=schema_assets,
                        db_id=db_id,
                        selected_fields=qid_to_fields.get(qid, set()),
                        table_descriptions=table_descs_by_db.get(db_id),
                        field_rewrites=field_rewrites_by_db.get(db_id),
                        include_value_examples=not args.no_value_examples,
                    )
                else:
                    scoped_schema_cache[cache_key] = _render_scoped_schema(
                        schema_assets=schema_assets,
                        db_id=db_id,
                        selected_fields=qid_to_fields.get(qid, set()),
                        field_rewrites=field_rewrites_by_db.get(db_id),
                        table_descriptions=table_descs_by_db.get(db_id),
                        include_value_examples=not args.no_value_examples,
                    )
            item = {
                "database": db_id,
                "schema": scoped_schema_cache[cache_key],
                "schema_with_annotations": scoped_schema_cache[cache_key],
                "question": row["question"],
                "external_knowledge": row.get("evidence", "") or "",
                "cross_model_sql_hint": cross_model_hints.get(
                    qid,
                    "No executable cross-model draft SQL hint is available for this question."
                    if cross_model_hints
                    else "",
                ),
            }
            rendered = build_prompt(item, template_text)
            (out_dir / f"{qid}.txt").write_text(rendered, encoding="utf-8")
            count += 1

        print(f"{template_name}\t{out_dir}\t{count}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
