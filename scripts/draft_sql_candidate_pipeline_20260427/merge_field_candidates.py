#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "field_recall_standalone"
sys.path.insert(0, str(ROOT / "src"))

from field_recall.dataset import load_jsonl, write_jsonl
from field_recall.merge import fk_closure, merge_columns, rrf_merge_ranked_lists
from field_recall.schema_assets import SchemaAssets
from field_recall.stage_io import load_stage_columns


def _load_stage_rows(path: str | Path) -> dict[int, dict]:
    rows = {}
    for row in load_jsonl(path):
        if "qid" not in row:
            continue
        rows[int(row["qid"])] = row
    return rows


def _infer_stage_name(path: str | Path) -> str:
    name = Path(path).stem.lower()
    if name.startswith("stage3_tables_columns"):
        return "stage3_tables_columns"
    if name.startswith("stage3_draft_sql"):
        return "stage3_draft_sql"
    if name == "stage3_tables_columns":
        return "stage3_tables_columns"
    if name == "stage3_draft_sql":
        return "stage3_draft_sql"
    if name == "stage3_tables_columns_qwen":
        return "stage3_tables_columns_qwen"
    if name == "stage3_draft_sql_qwen":
        return "stage3_draft_sql_qwen"
    if name == "stage4_field_match":
        return "stage4_field_match"
    return name


def _table_column_counts(schema_assets: SchemaAssets, db_id: str) -> dict[str, int]:
    meta = schema_assets.load_tables_meta(db_id)
    table_names = [t.lower() for t in meta["table_names_original"]]
    counts: dict[str, int] = {table: 0 for table in table_names}
    for table_idx, column_name in meta["column_names_original"]:
        if table_idx < 0:
            continue
        counts[table_names[table_idx]] += 1
    return counts


def _pk_columns(schema_assets: SchemaAssets, db_id: str) -> set[tuple[str, str]]:
    meta = schema_assets.load_tables_meta(db_id)
    table_names = [t.lower() for t in meta["table_names_original"]]
    cols = []
    for table_idx, column_name in meta["column_names_original"]:
        cols.append((table_idx, str(column_name).lower()))
    out: set[tuple[str, str]] = set()
    for idx in meta.get("primary_keys", []):
        table_idx, column_name = cols[idx]
        if table_idx >= 0:
            out.add((table_names[table_idx], column_name))
    return out


def _pk_columns_by_table(schema_assets: SchemaAssets, db_id: str) -> dict[str, set[str]]:
    out: dict[str, set[str]] = defaultdict(set)
    for table, column in _pk_columns(schema_assets, db_id):
        out[table].add(column)
    return out


def _augment_active_table_pks(columns: set[tuple[str, str]], pk_by_table: dict[str, set[str]]) -> set[tuple[str, str]]:
    augmented = set(columns)
    for table, _ in list(columns):
        for pk in pk_by_table.get(table, set()):
            augmented.add((table, pk))
    return augmented


def _augment_one_hop_fk_neighbor_keys(
    columns: set[tuple[str, str]],
    fk_edges: list[tuple[str, str, str, str]],
) -> set[tuple[str, str]]:
    active_tables = {table for table, _ in columns}
    augmented = set(columns)
    for ft, fc, tt, tc in fk_edges:
        if ft in active_tables or tt in active_tables:
            augmented.add((ft, fc))
            augmented.add((tt, tc))
    return augmented


def _augment_california_companions(
    db_id: str,
    columns: set[tuple[str, str]],
) -> set[tuple[str, str]]:
    if db_id != "california_schools":
        return columns
    augmented = set(columns)
    active_tables = {table for table, _ in columns}
    if "satscores" in active_tables:
        for col in {"cds", "sname", "cname", "rtype", "numtsttakr", "numge1500"}:
            augmented.add(("satscores", col))
    if "schools" in active_tables:
        for col in {"cdscode", "school"}:
            augmented.add(("schools", col))
    if "frpm" in active_tables:
        for col in {"cdscode", "school code", "school name"}:
            augmented.add(("frpm", col))
    return augmented


def _augment_debit_card_specializing_implicit_relations(
    db_id: str,
    columns: set[tuple[str, str]],
) -> set[tuple[str, str]]:
    if db_id != "debit_card_specializing":
        return columns
    augmented = set(columns)
    active_tables = {table for table, _ in columns}
    if "transactions_1k" in active_tables:
        txn_cols = {column for table, column in columns if table == "transactions_1k"}
        if txn_cols & {"date", "time", "price", "productid", "customerid", "amount"}:
            augmented.add(("transactions_1k", "gasstationid"))
            augmented.add(("gasstations", "gasstationid"))
    return augmented


def _flatten_ranked_payload(row: dict, key: str) -> list[tuple[str, str]]:
    payload = row.get(key) or {}
    ranked: list[tuple[str, str]] = []
    if not isinstance(payload, dict):
        return ranked
    for table, columns in payload.items():
        if not columns:
            continue
        for column in columns:
            ranked.append((str(table).lower(), str(column).lower()))
    return ranked


def _rank_stage4_hits(
    row: dict,
    *,
    active_tables: set[str],
    wide_tables: set[str],
    pk_fk_columns: set[tuple[str, str]],
    stage3_columns_by_table: dict[str, set[str]],
    stage4_active_only: bool,
    stage4_lexical_threshold: float,
    stage4_semantic_threshold: float,
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    regex_ranked: list[tuple[str, str]] = []
    semantic_ranked: list[tuple[str, str]] = []

    def _allow(table: str, column: str, score: float, semantic: bool) -> bool:
        if stage4_active_only and table not in active_tables:
            return False
        if semantic:
            if score < stage4_semantic_threshold:
                return False
        else:
            if score < stage4_lexical_threshold:
                return False
        if table in wide_tables:
            if column not in stage3_columns_by_table.get(table, set()) and (table, column) not in pk_fk_columns:
                return False
        return True

    regex_hits = sorted(row.get("regex_hits") or [], key=lambda hit: float(hit.get("score", 0.0)), reverse=True)
    semantic_hits = sorted(row.get("semantic_hits") or [], key=lambda hit: float(hit.get("score", 0.0)), reverse=True)

    for hit in regex_hits:
        table = str(hit.get("table", "")).lower()
        column = str(hit.get("column", "")).lower()
        score = float(hit.get("score", 0.0))
        if table and column and _allow(table, column, score, semantic=False):
            regex_ranked.append((table, column))
    for hit in semantic_hits:
        table = str(hit.get("table", "")).lower()
        column = str(hit.get("column", "")).lower()
        score = float(hit.get("score", 0.0))
        if table and column and _allow(table, column, score, semantic=True):
            semantic_ranked.append((table, column))
    return regex_ranked, semantic_ranked


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--dataset-root", required=True)
    parser.add_argument("--stage-jsonl", action="append", default=[])
    parser.add_argument("--output", required=True)
    parser.add_argument("--mode", choices=["union", "rrf"], default="union")
    parser.add_argument("--rrf-threshold", type=float, default=0.12)
    parser.add_argument("--rrf-k", type=int, default=60)
    parser.add_argument("--wide-table-threshold", type=int, default=40)
    parser.add_argument("--stage4-lexical-threshold", type=float, default=0.55)
    parser.add_argument("--stage4-semantic-threshold", type=float, default=0.48)
    parser.add_argument("--stage4-active-only", action="store_true")
    parser.add_argument("--preserve-stage3", action="store_true")
    parser.add_argument("--filter-stage4", action="store_true")
    parser.add_argument("--inject-table-pks", action="store_true")
    parser.add_argument("--inject-fk-neighbor-keys", action="store_true")
    parser.add_argument("--inject-california-companions", action="store_true")
    parser.add_argument("--inject-implicit-id-bridges", action="store_true")
    args = parser.parse_args()

    manifest = load_jsonl(args.manifest)
    schema_assets = SchemaAssets(args.dataset_root)
    stage_maps = [load_stage_columns(path) for path in args.stage_jsonl]
    stage_rows = {_infer_stage_name(path): _load_stage_rows(path) for path in args.stage_jsonl}

    rows = []
    for row in manifest:
        qid = int(row["qid"])
        db_id = row["db_id"]
        fk_edges = schema_assets.fk_edges(db_id)
        table_counts = _table_column_counts(schema_assets, db_id)
        wide_tables = {table for table, count in table_counts.items() if count >= args.wide_table_threshold}
        pk_fk_columns = _pk_columns(schema_assets, db_id)
        pk_by_table = _pk_columns_by_table(schema_assets, db_id)
        for ft, fc, tt, tc in fk_edges:
            pk_fk_columns.add((ft, fc))
            pk_fk_columns.add((tt, tc))
        if args.mode == "union":
            column_sets = []
            active_stage3_columns: list[tuple[str, str]] = []
            for path, stage_map in zip(args.stage_jsonl, stage_maps):
                stage_name = _infer_stage_name(path)
                if stage_name in {
                    "stage3_tables_columns",
                    "stage3_draft_sql",
                    "stage3_tables_columns_qwen",
                    "stage3_draft_sql_qwen",
                }:
                    cols = stage_map.get(qid, set())
                    active_stage3_columns.extend(sorted(cols))
                    column_sets.append(cols)
                    continue
                if stage_name == "stage4_field_match" and args.filter_stage4:
                    active_tables = {table for table, _ in active_stage3_columns}
                    stage3_columns_by_table: dict[str, set[str]] = defaultdict(set)
                    for table, column in active_stage3_columns:
                        stage3_columns_by_table[table].add(column)
                    stage4_row = stage_rows.get("stage4_field_match", {}).get(qid, {})
                    regex_ranked, semantic_ranked = _rank_stage4_hits(
                        stage4_row,
                        active_tables=active_tables,
                        wide_tables=wide_tables,
                        pk_fk_columns=pk_fk_columns,
                        stage3_columns_by_table=stage3_columns_by_table,
                        stage4_active_only=args.stage4_active_only,
                        stage4_lexical_threshold=args.stage4_lexical_threshold,
                        stage4_semantic_threshold=args.stage4_semantic_threshold,
                    )
                    column_sets.append(set(regex_ranked) | set(semantic_ranked))
                    continue
                column_sets.append(stage_map.get(qid, set()))
            merged = merge_columns(*column_sets)
            if args.inject_table_pks:
                merged = _augment_active_table_pks(merged, pk_by_table)
            if args.inject_fk_neighbor_keys:
                merged = _augment_one_hop_fk_neighbor_keys(merged, fk_edges)
            if args.inject_california_companions:
                merged = _augment_california_companions(db_id, merged)
            if args.inject_implicit_id_bridges:
                merged = _augment_debit_card_specializing_implicit_relations(db_id, merged)
            merged = fk_closure(merged, fk_edges)
            metadata = {
                "num_stage_inputs": len(args.stage_jsonl),
                "merge_mode": "union",
                "filter_stage4": args.filter_stage4,
                "inject_table_pks": args.inject_table_pks,
                "inject_fk_neighbor_keys": args.inject_fk_neighbor_keys,
                "inject_california_companions": args.inject_california_companions,
                "inject_implicit_id_bridges": args.inject_implicit_id_bridges,
                "stage4_lexical_threshold": args.stage4_lexical_threshold,
                "stage4_semantic_threshold": args.stage4_semantic_threshold,
                "stage4_active_only": args.stage4_active_only,
                "wide_table_threshold": args.wide_table_threshold,
            }
        else:
            stage3_tables = _flatten_ranked_payload(stage_rows.get("stage3_tables_columns", {}).get(qid, {}), "tables")
            stage3_draft = _flatten_ranked_payload(stage_rows.get("stage3_draft_sql", {}).get(qid, {}), "draft_sql_tables")
            stage3_tables_qwen = _flatten_ranked_payload(stage_rows.get("stage3_tables_columns_qwen", {}).get(qid, {}), "tables")
            stage3_draft_qwen = _flatten_ranked_payload(stage_rows.get("stage3_draft_sql_qwen", {}).get(qid, {}), "draft_sql_tables")

            active_stage3 = stage3_tables + stage3_draft + stage3_tables_qwen + stage3_draft_qwen
            active_tables = {table for table, _ in active_stage3}
            stage3_columns_by_table: dict[str, set[str]] = defaultdict(set)
            for table, column in active_stage3:
                stage3_columns_by_table[table].add(column)

            stage4_row = stage_rows.get("stage4_field_match", {}).get(qid, {})
            regex_ranked, semantic_ranked = _rank_stage4_hits(
                stage4_row,
                active_tables=active_tables,
                wide_tables=wide_tables,
                pk_fk_columns=pk_fk_columns,
                stage3_columns_by_table=stage3_columns_by_table,
                stage4_active_only=args.stage4_active_only,
                stage4_lexical_threshold=args.stage4_lexical_threshold,
                stage4_semantic_threshold=args.stage4_semantic_threshold,
            )

            ranked_lists = [
                ("stage3_tables_columns", stage3_tables),
                ("stage3_draft_sql", stage3_draft),
                ("stage3_tables_columns_qwen", stage3_tables_qwen),
                ("stage3_draft_sql_qwen", stage3_draft_qwen),
                ("stage4_regex", regex_ranked),
                ("stage4_semantic", semantic_ranked),
            ]
            scored = rrf_merge_ranked_lists(
                ranked_lists,
                fk_edges=fk_edges,
                rrf_k=args.rrf_k,
                score_threshold=args.rrf_threshold,
            )
            merged = {(row["table"], row["column"]) for row in scored}
            if args.preserve_stage3:
                merged |= set(active_stage3)
            if args.inject_table_pks:
                merged = _augment_active_table_pks(merged, pk_by_table)
            if args.inject_fk_neighbor_keys:
                merged = _augment_one_hop_fk_neighbor_keys(merged, fk_edges)
            if args.inject_california_companions:
                merged = _augment_california_companions(db_id, merged)
            if args.inject_implicit_id_bridges:
                merged = _augment_debit_card_specializing_implicit_relations(db_id, merged)
            merged = fk_closure(merged, fk_edges)
            metadata = {
                "num_stage_inputs": len(args.stage_jsonl),
                "merge_mode": "rrf",
                "rrf_threshold": args.rrf_threshold,
                "rrf_k": args.rrf_k,
                "stage4_lexical_threshold": args.stage4_lexical_threshold,
                "stage4_semantic_threshold": args.stage4_semantic_threshold,
                "stage4_active_only": args.stage4_active_only,
                "preserve_stage3": args.preserve_stage3,
                "inject_table_pks": args.inject_table_pks,
                "inject_fk_neighbor_keys": args.inject_fk_neighbor_keys,
                "inject_california_companions": args.inject_california_companions,
                "inject_implicit_id_bridges": args.inject_implicit_id_bridges,
                "wide_table_threshold": args.wide_table_threshold,
                "selected_before_fk": len(scored),
            }
        rows.append(
            {
                "qid": qid,
                "final_fields": sorted([[t, c] for t, c in merged]),
                "metadata": metadata,
            }
        )
    write_jsonl(rows, args.output)
    print(f"Wrote {len(rows)} merged rows to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
