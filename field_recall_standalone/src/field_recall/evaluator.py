from __future__ import annotations

import json
from pathlib import Path

from .dataset import load_jsonl
from .metrics import aggregate_metrics, extract_gold_table_columns, recall_precision


def load_candidate_jsonl(path: str | Path) -> dict[int, set[tuple[str, str]]]:
    rows = load_jsonl(path)
    out: dict[int, set[tuple[str, str]]] = {}
    for row in rows:
        qid = int(row["qid"])
        cols = row.get("final_fields") or row.get("columns") or []
        out[qid] = {(str(t).lower(), str(c).lower()) for t, c in cols}
    return out


def evaluate_manifest(manifest_path: str | Path, candidate_jsonl_path: str | Path) -> tuple[list[dict], dict]:
    manifest = load_jsonl(manifest_path)
    candidates = load_candidate_jsonl(candidate_jsonl_path)
    per_question = []
    for row in manifest:
        qid = int(row["qid"])
        _, gold_columns = extract_gold_table_columns(row["gold_sql"])
        candidate_columns = candidates.get(qid, set())
        recall, precision = recall_precision(candidate_columns, gold_columns)
        per_question.append(
            {
                "qid": qid,
                "db_id": row["db_id"],
                "difficulty": row["difficulty"],
                "gold_column_count": len(gold_columns),
                "candidate_set_size": len(candidate_columns),
                "recall": recall,
                "precision": precision,
                "gold_columns": sorted([[t, c] for t, c in gold_columns]),
                "candidate_columns": sorted([[t, c] for t, c in candidate_columns]),
            }
        )
    summary = aggregate_metrics(per_question)
    return per_question, summary


def write_report(per_question: list[dict], summary: dict, out_json: str | Path, out_md: str | Path) -> None:
    Path(out_json).parent.mkdir(parents=True, exist_ok=True)
    Path(out_md).parent.mkdir(parents=True, exist_ok=True)
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump({"summary": summary, "questions": per_question}, f, ensure_ascii=False, indent=2)
    lines = [
        "# Field Recall Evaluation",
        "",
        f"- count: `{summary['count']}`",
        f"- average_field_recall: `{summary['average_field_recall']:.4f}`",
        f"- candidate_precision: `{summary['candidate_precision']:.4f}`",
        f"- candidate_set_size: `{summary['candidate_set_size']:.2f}`",
        f"- full_coverage_rate: `{summary['full_coverage_rate']:.4f}`",
        "",
        "## Per Difficulty",
        "",
    ]
    for diff, stats in sorted(summary["per_difficulty"].items()):
        lines.extend(
            [
                f"### {diff}",
                f"- count: `{stats['count']}`",
                f"- avg_recall: `{stats['avg_recall']:.4f}`",
                f"- avg_precision: `{stats['avg_precision']:.4f}`",
                f"- avg_candidate_set_size: `{stats['avg_candidate_set_size']:.2f}`",
                f"- full_coverage_rate: `{stats['full_coverage_rate']:.4f}`",
                "",
            ]
        )
    Path(out_md).write_text("\n".join(lines), encoding="utf-8")
