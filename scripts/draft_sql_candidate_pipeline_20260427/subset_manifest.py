#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path


def _read_ids(value: str | None, path: str | None) -> set[str]:
    out: set[str] = set()
    if value:
        out.update(x.strip() for x in value.split(",") if x.strip())
    if path:
        for raw in Path(path).read_text(encoding="utf-8").splitlines():
            text = raw.strip()
            if text:
                out.add(text)
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--qid", action="append", default=[])
    parser.add_argument("--qid-file")
    parser.add_argument("--difficulty", choices=["simple", "moderate", "challenging"])
    parser.add_argument("--limit", type=int)
    parser.add_argument("--output", required=True)
    parser.add_argument("--qids-output")
    parser.add_argument("--dbids-output")
    args = parser.parse_args()

    wanted_qids = _read_ids(",".join(args.qid), args.qid_file)
    rows: list[dict] = []
    for raw in Path(args.manifest).read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        row = json.loads(raw)
        if wanted_qids and str(row["qid"]) not in wanted_qids:
            continue
        if args.difficulty and str(row.get("difficulty", "")).lower() != args.difficulty:
            continue
        rows.append(row)
    if args.limit is not None:
        rows = rows[: args.limit]

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    if args.qids_output:
        qids_path = Path(args.qids_output)
        qids_path.parent.mkdir(parents=True, exist_ok=True)
        qids_path.write_text("\n".join(str(row["qid"]) for row in rows) + "\n", encoding="utf-8")
    if args.dbids_output:
        dbids = sorted({str(row["db_id"]) for row in rows})
        dbids_path = Path(args.dbids_output)
        dbids_path.parent.mkdir(parents=True, exist_ok=True)
        dbids_path.write_text("\n".join(dbids) + "\n", encoding="utf-8")

    print(f"Wrote {len(rows)} rows to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

