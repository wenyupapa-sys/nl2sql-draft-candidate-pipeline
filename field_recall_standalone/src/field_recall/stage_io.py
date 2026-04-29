from __future__ import annotations

from pathlib import Path

from .dataset import load_jsonl


def extract_columns_from_row(row: dict) -> set[tuple[str, str]]:
    cols: set[tuple[str, str]] = set()
    if "columns" in row:
        for table, column in row.get("columns") or []:
            cols.add((str(table).lower(), str(column).lower()))
    if "final_fields" in row:
        for table, column in row.get("final_fields") or []:
            cols.add((str(table).lower(), str(column).lower()))
    for key in ("tables", "draft_sql_tables"):
        payload = row.get(key) or {}
        if isinstance(payload, dict):
            for table, columns in payload.items():
                if not columns:
                    continue
                for column in columns:
                    cols.add((str(table).lower(), str(column).lower()))
    if "condition_value_hits" in row:
        for hit in row.get("condition_value_hits") or []:
            table = hit.get("table")
            column = hit.get("column")
            if table and column:
                cols.add((str(table).lower(), str(column).lower()))
    if "regex_hits" in row:
        for hit in row.get("regex_hits") or []:
            table = hit.get("table")
            column = hit.get("column")
            if table and column:
                cols.add((str(table).lower(), str(column).lower()))
    if "semantic_hits" in row:
        for hit in row.get("semantic_hits") or []:
            table = hit.get("table")
            column = hit.get("column")
            if table and column:
                cols.add((str(table).lower(), str(column).lower()))
    return cols


def load_stage_columns(path: str | Path) -> dict[int, set[tuple[str, str]]]:
    path = Path(path)
    if not path.exists():
        return {}
    out: dict[int, set[tuple[str, str]]] = {}
    for row in load_jsonl(path):
        if "qid" not in row:
            continue
        qid = int(row["qid"])
        out[qid] = extract_columns_from_row(row)
    return out
