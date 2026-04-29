#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


def _extract_sql(text: str) -> str:
    cdata = re.findall(r"<result>\s*<!\[CDATA\[(.*?)\]\]>\s*</result>", text, re.DOTALL | re.I)
    if cdata:
        return cdata[-1].strip()
    xml = re.findall(r"<result>\s*(.*?)\s*</result>", text, re.DOTALL | re.I)
    if xml:
        return xml[-1].strip()
    blocks = re.findall(r"```(?:postgresql|sql)?\s*\n(.*?)```", text, re.DOTALL | re.I)
    if blocks:
        return blocks[-1].strip()
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def _load_manifest(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _find_result(result_dir: Path, qid: int) -> Path | None:
    for name in (f"{qid}.txt", f"{qid}_direct.txt", f"{qid}_direct_gemini.txt"):
        path = result_dir / name
        if path.exists() and path.stat().st_size > 0:
            return path
    matches = sorted(result_dir.glob(f"{qid}_*.txt"))
    return matches[0] if matches else None


def main() -> int:
    parser = argparse.ArgumentParser(description="Convert selected {qid}.txt SQL files to BIRD prediction JSON.")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--result-dir", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--key-mode", choices=["source_index", "qid"], default="source_index")
    args = parser.parse_args()

    result_dir = Path(args.result_dir)
    predictions: dict[str, str] = {}
    missing: list[int] = []
    for order_idx, row in enumerate(_load_manifest(Path(args.manifest))):
        qid = int(row["qid"])
        key = str(row.get("source_index", order_idx) if args.key_mode == "source_index" else qid)
        path = _find_result(result_dir, qid)
        if path is None:
            predictions[key] = ""
            missing.append(qid)
            continue
        predictions[key] = _extract_sql(path.read_text(encoding="utf-8", errors="ignore"))

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(predictions, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(predictions)} predictions to {output}; missing={len(missing)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
