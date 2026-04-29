#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--simple-output", required=True)
    parser.add_argument("--modchall-output", required=True)
    parser.add_argument("--moderate-output")
    parser.add_argument("--challenging-output")
    args = parser.parse_args()

    buckets = {"simple": [], "moderate": [], "challenging": []}
    for raw in Path(args.manifest).read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        row = json.loads(raw)
        diff = str(row.get("difficulty", "")).lower()
        if diff in buckets:
            buckets[diff].append(str(row["qid"]))

    def write(path: str, ids: list[str]) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("\n".join(ids) + ("\n" if ids else ""), encoding="utf-8")

    write(args.simple_output, buckets["simple"])
    write(args.modchall_output, buckets["moderate"] + buckets["challenging"])
    if args.moderate_output:
        write(args.moderate_output, buckets["moderate"])
    if args.challenging_output:
        write(args.challenging_output, buckets["challenging"])

    print(
        f"simple={len(buckets['simple'])} moderate={len(buckets['moderate'])} "
        f"challenging={len(buckets['challenging'])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

