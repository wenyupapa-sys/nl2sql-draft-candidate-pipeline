#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from field_recall.dataset import load_jsonl
from field_recall.evaluator import evaluate_manifest
from field_recall.metrics import aggregate_metrics
from field_recall.stage_io import load_stage_columns


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--stage", action="append", required=True, help="name=path")
    parser.add_argument("--out-json", required=True)
    parser.add_argument("--out-md", required=True)
    args = parser.parse_args()

    manifest_rows = load_jsonl(args.manifest)
    stage_specs = []
    for spec in args.stage:
        name, path = spec.split("=", 1)
        stage_specs.append((name, path))

    cumulative: dict[int, set[tuple[str, str]]] = {}
    history = []
    tmp_dir = Path(args.out_json).parent
    tmp_dir.mkdir(parents=True, exist_ok=True)
    for idx, (name, path) in enumerate(stage_specs, start=1):
        stage_map = load_stage_columns(path)
        for qid, cols in stage_map.items():
            cumulative.setdefault(qid, set()).update(cols)
        tmp_jsonl = tmp_dir / f".tmp_stage_gain_{idx}.jsonl"
        with tmp_jsonl.open("w", encoding="utf-8") as f:
            for row in manifest_rows:
                cols = cumulative.get(int(row["qid"]), set())
                f.write(json.dumps({"qid": row["qid"], "final_fields": sorted([[t, c] for t, c in cols])}, ensure_ascii=False) + "\n")
        per_question, summary = evaluate_manifest(args.manifest, tmp_jsonl)
        history.append({"stage": name, "summary": summary})

    prev_recall = 0.0
    prev_size = 0.0
    lines = ["# Stage Gain Report", ""]
    for item in history:
        summary = item["summary"]
        gain = summary["average_field_recall"] - prev_recall
        size_gain = summary["candidate_set_size"] - prev_size
        lines.extend(
            [
                f"## {item['stage']}",
                f"- average_field_recall: `{summary['average_field_recall']:.4f}`",
                f"- recall_gain_vs_prev: `{gain:+.4f}`",
                f"- candidate_precision: `{summary['candidate_precision']:.4f}`",
                f"- candidate_set_size: `{summary['candidate_set_size']:.2f}`",
                f"- candidate_set_size_gain_vs_prev: `{size_gain:+.2f}`",
                "",
            ]
        )
        prev_recall = summary["average_field_recall"]
        prev_size = summary["candidate_set_size"]

    Path(args.out_md).write_text("\n".join(lines), encoding="utf-8")
    Path(args.out_json).write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote stage gain report to {args.out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
