#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

def _load_manifest(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if text:
            rows.append(json.loads(text))
    return rows


def _sqlite_db_path(dataset_root: Path, db_id: str) -> Path:
    for rel in ("dev_databases", "test_databases", "databases"):
        candidate = dataset_root / rel / db_id / f"{db_id}.sqlite"
        if candidate.exists():
            return candidate
    raise FileNotFoundError(f"SQLite database not found for db_id={db_id} under {dataset_root}")


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


def _normalize_cell(value: Any, *, max_chars: int) -> Any:
    if value is None:
        return None
    if isinstance(value, (int, float, bool)):
        return value
    text = str(value)
    if len(text) > max_chars:
        return text[: max_chars - 3] + "..."
    return text


def _execute_preview(
    *,
    db_path: Path,
    sql: str,
    timeout_s: float,
    preview_rows: int,
    max_cell_chars: int,
) -> tuple[bool, list[str], list[list[Any]], str | None]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    timed_out = {"value": False}
    start = time.monotonic()
    cur = conn.cursor()
    try:
        def _progress_handler() -> int:
            if timeout_s > 0 and time.monotonic() - start > timeout_s:
                timed_out["value"] = True
                return 1
            return 0

        conn.set_progress_handler(_progress_handler, 10_000)
        clean = sql.strip().rstrip(";")
        if not clean:
            return False, [], [], "empty sql"
        wrapped = f"SELECT * FROM ({clean}) AS _preview LIMIT {preview_rows}"
        try:
            cur.execute(wrapped)
        except Exception:
            cur.execute(clean)
        columns = [d[0] for d in (cur.description or [])]
        rows = [
            [_normalize_cell(cell, max_chars=max_cell_chars) for cell in row]
            for row in cur.fetchmany(preview_rows)
        ]
        return True, columns, rows, None
    except Exception as exc:  # noqa: BLE001
        if timed_out["value"]:
            return False, [], [], f"sqlite timeout after {timeout_s}s"
        return False, [], [], str(exc)
    finally:
        try:
            conn.set_progress_handler(None, 0)
        except Exception:
            pass
        cur.close()
        conn.close()


def _format_hint_text(
    *,
    source_label: str,
    sql: str,
    columns: list[str],
    rows: list[list[Any]],
    include_preview_rows: bool,
) -> str:
    label_lower = source_label.lower()
    if "gemini" in label_lower:
        source_title = "Gemini 3 Flash draft SQL"
    elif "qwen" in label_lower:
        source_title = "Qwen3.6-plus draft SQL"
    else:
        source_title = f"{source_label} draft SQL"
    columns_json = json.dumps(columns, ensure_ascii=False)
    parts = [
        f"### {source_title}\n"
        "The following draft SQL was generated earlier by another model and executed successfully "
        "on the same database. The SQL and its returned data may still be semantically incomplete "
        "or partially wrong. Use it only as a reference hint, not as ground truth.\n\n"
        f"Source draft model: {source_label}\n\n"
        "Previously tested draft SQL:\n"
        "```sql\n"
        f"{sql.strip()}\n"
        "```\n\n"
        "Observed execution metadata from that SQL:\n"
        f"- columns: {columns_json}\n"
    ]
    if include_preview_rows:
        rows_json = json.dumps(rows, ensure_ascii=False, indent=2)
        parts.append(f"- first_rows: {rows_json}\n")
    else:
        parts.append("- row preview disabled for submission data-safety\n")
    return "".join(parts)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--dataset-root", required=True)
    parser.add_argument("--stage3-raw-dir", required=True)
    parser.add_argument("--source-label", required=True)
    parser.add_argument("--output-jsonl", required=True)
    parser.add_argument("--artifacts-dir", required=True)
    parser.add_argument("--preview-rows", type=int, default=0)
    parser.add_argument("--include-preview-rows", action="store_true", help="Include SQL result row previews in hint text. Keep disabled for official submission.")
    parser.add_argument("--max-cell-chars", type=int, default=120)
    parser.add_argument("--exec-timeout", type=float, default=3.0)
    args = parser.parse_args()

    manifest_path = Path(args.manifest).resolve()
    dataset_root = Path(args.dataset_root).resolve()
    stage3_raw_dir = Path(args.stage3_raw_dir).resolve()
    output_jsonl = Path(args.output_jsonl).resolve()
    artifacts_dir = Path(args.artifacts_dir).resolve()
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    rows = _load_manifest(manifest_path)
    qid_to_db = {int(row["qid"]): str(row["db_id"]) for row in rows}
    out_rows: list[dict[str, Any]] = []

    for qid, db_id in sorted(qid_to_db.items()):
        raw_path = stage3_raw_dir / f"{qid}.txt"
        if not raw_path.exists() or raw_path.stat().st_size == 0:
            continue
        raw_text = raw_path.read_text(encoding="utf-8", errors="ignore")
        sql = _extract_sql(raw_text)
        if not sql:
            continue
        ok, columns, preview, error = _execute_preview(
            db_path=_sqlite_db_path(dataset_root, db_id),
            sql=sql,
            timeout_s=args.exec_timeout,
            preview_rows=args.preview_rows if args.include_preview_rows else 0,
            max_cell_chars=args.max_cell_chars,
        )
        row = {
            "qid": qid,
            "db_id": db_id,
            "source_label": args.source_label,
            "raw_path": str(raw_path),
            "sql": sql,
            "executable": ok,
            "preview_columns": columns,
            "preview_rows": preview,
            "error": error,
        }
        (artifacts_dir / f"{qid}.json").write_text(
            json.dumps(row, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        if not ok:
            continue
        row["hint_text"] = _format_hint_text(
            source_label=args.source_label,
            sql=sql,
            columns=columns,
            rows=preview,
            include_preview_rows=args.include_preview_rows,
        )
        out_rows.append(row)

    output_jsonl.parent.mkdir(parents=True, exist_ok=True)
    with output_jsonl.open("w", encoding="utf-8") as f:
        for row in out_rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    print(
        json.dumps(
            {
                "source_label": args.source_label,
                "stage3_raw_dir": str(stage3_raw_dir),
                "manifest": str(manifest_path),
                "output_jsonl": str(output_jsonl),
                "artifacts_dir": str(artifacts_dir),
                "executable_hints": len(out_rows),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
