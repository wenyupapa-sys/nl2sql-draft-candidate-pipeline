#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from field_recall.evaluator import evaluate_manifest, write_report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--candidate-jsonl", required=True)
    parser.add_argument("--out-json", required=True)
    parser.add_argument("--out-md", required=True)
    args = parser.parse_args()

    per_question, summary = evaluate_manifest(args.manifest, args.candidate_jsonl)
    write_report(per_question, summary, args.out_json, args.out_md)
    print(f"average_field_recall={summary['average_field_recall']:.4f}")
    print(f"candidate_precision={summary['candidate_precision']:.4f}")
    print(f"candidate_set_size={summary['candidate_set_size']:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
