from __future__ import annotations

from collections import defaultdict

from .sql_analysis import extract_physical_table_columns


def extract_gold_table_columns(sql: str) -> tuple[set[str], set[tuple[str, str]]]:
    return extract_physical_table_columns(sql)


def recall_precision(candidate_columns: set[tuple[str, str]], gold_columns: set[tuple[str, str]]) -> tuple[float, float]:
    if not gold_columns:
        recall = 1.0
    else:
        recall = len(candidate_columns & gold_columns) / len(gold_columns)
    precision = 0.0 if not candidate_columns else len(candidate_columns & gold_columns) / len(candidate_columns)
    return recall, precision


def aggregate_metrics(rows: list[dict]) -> dict:
    n = len(rows) or 1
    avg_recall = sum(r["recall"] for r in rows) / n
    avg_precision = sum(r["precision"] for r in rows) / n
    avg_candidate_set_size = sum(r["candidate_set_size"] for r in rows) / n
    full_coverage_rate = sum(1 for r in rows if r["recall"] == 1.0) / n
    by_diff = defaultdict(list)
    for row in rows:
        by_diff[row["difficulty"]].append(row)
    difficulty_breakdown = {}
    for diff, subset in by_diff.items():
        m = len(subset)
        difficulty_breakdown[diff] = {
            "count": m,
            "avg_recall": sum(r["recall"] for r in subset) / m,
            "avg_precision": sum(r["precision"] for r in subset) / m,
            "avg_candidate_set_size": sum(r["candidate_set_size"] for r in subset) / m,
            "full_coverage_rate": sum(1 for r in subset if r["recall"] == 1.0) / m,
        }
    return {
        "count": len(rows),
        "average_field_recall": avg_recall,
        "candidate_precision": avg_precision,
        "candidate_set_size": avg_candidate_set_size,
        "full_coverage_rate": full_coverage_rate,
        "per_difficulty": difficulty_breakdown,
    }
