from __future__ import annotations

from collections import defaultdict
import math


DEFAULT_SOURCE_WEIGHTS = {
    "stage3_tables_columns": 2.0,
    "stage3_draft_sql": 1.8,
    "stage3_tables_columns_qwen": 1.9,
    "stage3_draft_sql_qwen": 1.7,
    "stage4_regex": 0.9,
    "stage4_semantic": 0.55,
}


def merge_columns(*column_sets: set[tuple[str, str]]) -> set[tuple[str, str]]:
    merged: set[tuple[str, str]] = set()
    for cols in column_sets:
        merged |= cols
    return merged


def fk_closure(linked_columns: set[tuple[str, str]], fk_edges: list[tuple[str, str, str, str]]) -> set[tuple[str, str]]:
    tables = {t for t, _ in linked_columns}
    augmented = set(linked_columns)
    adjacency = defaultdict(list)
    for ft, fc, tt, tc in fk_edges:
        adjacency[ft].append((tt, fc, tc))
        adjacency[tt].append((ft, tc, fc))
    for table in list(tables):
        for neighbor, own_fk, other_fk in adjacency.get(table, []):
            if neighbor in tables:
                augmented.add((table, own_fk))
                augmented.add((neighbor, other_fk))
    return augmented


def rrf_merge_ranked_lists(
    ranked_lists: list[tuple[str, list[tuple[str, str]]]],
    fk_edges: list[tuple[str, str, str, str]] | None = None,
    source_weights: dict[str, float] | None = None,
    rrf_k: int = 60,
    score_threshold: float | None = None,
) -> list[dict]:
    weights = source_weights or DEFAULT_SOURCE_WEIGHTS
    scores: dict[tuple[str, str], float] = defaultdict(float)
    sources: dict[tuple[str, str], list[str]] = defaultdict(list)

    for source_name, ranked in ranked_lists:
        if not ranked:
            continue
        weight = weights.get(source_name, 1.0)
        seen_in_source: set[tuple[str, str]] = set()
        for rank, key in enumerate(ranked):
            if key in seen_in_source:
                continue
            seen_in_source.add(key)
            scores[key] += weight / (rrf_k + rank + 1)
            sources[key].append(source_name)

    if not scores:
        return []

    max_score = max(scores.values())
    if max_score > 0:
        for key in list(scores):
            scores[key] /= max_score

    if fk_edges:
        fk_ref_count: dict[tuple[str, str], int] = defaultdict(int)
        for ft, fc, tt, tc in fk_edges:
            fk_ref_count[(ft, fc)] += 1
            fk_ref_count[(tt, tc)] += 1
        for key in list(scores):
            ref_count = fk_ref_count.get(key, 0)
            if ref_count > 0:
                scores[key] *= 1.0 + 0.05 * math.log(1 + ref_count)

    results = [
        {
            "table": table,
            "column": column,
            "score": score,
            "sources": sorted(set(sources[(table, column)])),
            "source_count": len(set(sources[(table, column)])),
        }
        for (table, column), score in scores.items()
    ]
    results.sort(key=lambda row: (-row["score"], row["table"], row["column"]))
    if score_threshold is not None:
        results = [row for row in results if row["score"] >= score_threshold]
    return results
