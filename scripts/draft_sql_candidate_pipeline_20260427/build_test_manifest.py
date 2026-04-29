#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path


def _load_json(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise SystemExit(f"Expected a JSON list in {path}")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a gold-free manifest from BIRD test.json.")
    parser.add_argument("--test-json", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--qids-output")
    parser.add_argument("--dbids-output")
    parser.add_argument("--difficulty", default="moderate", help="Route label for test rows. Use moderate to force M/C path.")
    args = parser.parse_args()

    rows = _load_json(Path(args.test_json))
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    qids: list[str] = []
    dbids: list[str] = []
    seen_dbids: set[str] = set()

    with out_path.open("w", encoding="utf-8") as f:
        for idx, row in enumerate(rows):
            db_id = str(row.get("db_id") or row.get("database") or "").strip()
            question = str(row.get("question") or row.get("query") or "").strip()
            if not db_id or not question:
                raise SystemExit(f"Missing db_id/question at test index {idx}")
            qid = int(row.get("question_id", idx)) if str(row.get("question_id", idx)).isdigit() else idx
            item = {
                "qid": qid,
                "source_index": idx,
                "db_id": db_id,
                "question": question,
                "evidence": str(row.get("evidence") or "").strip(),
                "difficulty": args.difficulty,
            }
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
            qids.append(str(qid))
            if db_id not in seen_dbids:
                seen_dbids.add(db_id)
                dbids.append(db_id)

    if args.qids_output:
        Path(args.qids_output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.qids_output).write_text("\n".join(qids) + "\n", encoding="utf-8")
    if args.dbids_output:
        Path(args.dbids_output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.dbids_output).write_text("\n".join(dbids) + "\n", encoding="utf-8")
    print(f"Wrote {len(rows)} manifest rows to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
