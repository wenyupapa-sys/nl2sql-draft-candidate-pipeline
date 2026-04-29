#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path
from typing import Any


def _load_manifest(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _extract_sql(text: str) -> str | None:
    cdata = re.findall(r"<result>\s*<!\[CDATA\[(.*?)\]\]>\s*</result>", text, re.DOTALL | re.I)
    if cdata:
        return cdata[-1].strip() or None
    xml = re.findall(r"<result>\s*(.*?)\s*</result>", text, re.DOTALL | re.I)
    if xml:
        return xml[-1].strip() or None
    blocks = re.findall(r"```(?:postgresql|sql)?\s*\n(.*?)```", text, re.DOTALL | re.I)
    for block in reversed(blocks):
        sql = block.strip()
        if re.match(r"^(select|with)\b", sql, re.I | re.S):
            return sql
    stripped = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    if re.match(r"^(select|with)\b", stripped, re.I | re.S):
        return stripped
    tail = re.findall(r"((?:SELECT|WITH)\b[\s\S]+)", text, re.I)
    return tail[-1].strip() if tail else None


def _find_result(result_dir: Path, qid: int) -> Path | None:
    for name in (f"{qid}.txt", f"{qid}_direct.txt", f"{qid}_direct_gemini.txt"):
        path = result_dir / name
        if path.exists() and path.stat().st_size > 0:
            return path
    matches = sorted(result_dir.glob(f"{qid}_*.txt"))
    return matches[0] if matches else None


def _render_result(sql: str) -> str:
    return f"<result><![CDATA[\n{sql.strip()}\n]]></result>\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Fill missing result files from a backup result directory.")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--primary-dir", required=True)
    parser.add_argument("--backup-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--metadata-path")
    parser.add_argument("--source-label", default="backup")
    args = parser.parse_args()

    manifest = _load_manifest(Path(args.manifest))
    primary_dir = Path(args.primary_dir)
    backup_dir = Path(args.backup_dir)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, Any]] = []
    for row in manifest:
        qid = int(row["qid"])
        out_path = output_dir / f"{qid}.txt"
        primary_path = _find_result(primary_dir, qid)
        if primary_path is not None:
            shutil.copyfile(primary_path, out_path)
            rows.append({"qid": qid, "source": "primary", "path": str(primary_path), "status": "copied"})
            continue
        backup_path = _find_result(backup_dir, qid)
        if backup_path is None:
            rows.append({"qid": qid, "source": args.source_label, "path": None, "status": "missing"})
            continue
        sql = _extract_sql(backup_path.read_text(encoding="utf-8", errors="ignore"))
        if not sql:
            rows.append({"qid": qid, "source": args.source_label, "path": str(backup_path), "status": "missing_sql"})
            continue
        out_path.write_text(_render_result(sql), encoding="utf-8")
        rows.append({"qid": qid, "source": args.source_label, "path": str(backup_path), "status": "filled"})

    if args.metadata_path:
        meta_path = Path(args.metadata_path)
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "primary_dir": str(primary_dir),
            "backup_dir": str(backup_dir),
            "output_dir": str(output_dir),
            "total": len(rows),
            "primary": sum(1 for row in rows if row["source"] == "primary"),
            "filled": sum(1 for row in rows if row["status"] == "filled"),
            "missing": sum(1 for row in rows if row["status"] in {"missing", "missing_sql"}),
            "rows": rows,
        }
        meta_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "output_dir": str(output_dir),
                "total": len(rows),
                "primary": sum(1 for row in rows if row["source"] == "primary"),
                "filled": sum(1 for row in rows if row["status"] == "filled"),
                "missing": sum(1 for row in rows if row["status"] in {"missing", "missing_sql"}),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
