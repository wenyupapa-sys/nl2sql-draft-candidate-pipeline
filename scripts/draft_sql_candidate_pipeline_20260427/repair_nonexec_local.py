#!/usr/bin/env python3
"""Repair non-executable SQL files without using gold SQL.

This is the official-test safe repair path. It only checks whether a generated
SQL can be executed on the local SQLite database, then runs CheckerPipeline on
the non-executable cases. It never reads gold SQL or an evaluation report.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

FIELD_RECALL_ROOT = PROJECT_ROOT / "field_recall_standalone"
if str(FIELD_RECALL_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(FIELD_RECALL_ROOT / "src"))

from checker_pipeline import CheckerPipeline
from field_recall.schema_assets import SchemaAssets


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
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    if re.match(r"^(select|with)\b", text, re.I | re.S):
        return text
    tail = re.findall(r"((?:SELECT|WITH)\b[\s\S]+)", text, re.I)
    return tail[-1].strip() if tail else None


def _render_result(sql: str) -> str:
    return f"<result><![CDATA[\n{sql.strip()}\n]]></result>\n"


def _load_manifest(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _find_result_file(result_dir: Path, qid: int) -> Path | None:
    for name in (f"{qid}.txt", f"{qid}_direct.txt", f"{qid}_direct_gemini.txt"):
        path = result_dir / name
        if path.exists() and path.stat().st_size > 0:
            return path
    matches = sorted(result_dir.glob(f"{qid}_*.txt"))
    return matches[0] if matches else None


def _sqlite_db_path(dataset_root: Path, db_id: str) -> Path:
    for rel in ("test_databases", "dev_databases", "databases"):
        path = dataset_root / rel / db_id / f"{db_id}.sqlite"
        if path.exists():
            return path
    raise FileNotFoundError(f"SQLite database not found for db_id={db_id} under {dataset_root}")


def _execute_sql(sql: str | None, db_path: Path, timeout_s: float) -> dict[str, Any]:
    if not sql or not sql.strip():
        return {"ok": False, "error": "empty sql"}
    clean = sql.strip().rstrip(";")
    if not clean:
        return {"ok": False, "error": "empty sql"}
    conn: sqlite3.Connection | None = None
    start = time.monotonic()
    timed_out = {"value": False}
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.text_factory = lambda b: b.decode("utf-8", errors="replace")

        def _progress_handler() -> int:
            if timeout_s > 0 and time.monotonic() - start > timeout_s:
                timed_out["value"] = True
                return 1
            return 0

        conn.set_progress_handler(_progress_handler, 10_000)
        cur = conn.execute(clean)
        rows = cur.fetchmany(1)
        columns = [d[0] for d in (cur.description or [])]
        return {"ok": True, "error": None, "row_preview_count": len(rows), "columns": columns}
    except Exception as exc:  # noqa: BLE001
        err = f"sqlite timeout after {timeout_s}s" if timed_out["value"] else str(exc)
        return {"ok": False, "error": err}
    finally:
        if conn is not None:
            try:
                conn.set_progress_handler(None, 0)
            except Exception:
                pass
            conn.close()


def _resolve_proxy(explicit: str | None) -> str:
    if explicit is not None:
        text = explicit.strip()
        return "" if text.lower() in {"", "none", "off", "false"} else text
    for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        value = os.getenv(key)
        if value:
            return value
    try:
        raw = subprocess.check_output(
            ["scutil", "--proxy"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
    except Exception:
        return ""
    enabled = False
    host = None
    port = None
    for line in raw.splitlines():
        text = line.strip()
        if text == "HTTPSEnable : 1":
            enabled = True
        elif text.startswith("HTTPSProxy : "):
            host = text.split(" : ", 1)[1].strip()
        elif text.startswith("HTTPSPort : "):
            port = text.split(" : ", 1)[1].strip()
    return f"http://{host}:{port}" if enabled and host and port else ""


def _trace_usage_rows(qid: int, trace: dict[str, Any], *, model: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for step in trace.get("steps") or []:
        if not isinstance(step, dict):
            continue
        meta = step.get("llm_meta") or {}
        usage = meta.get("usage") or {}
        if not usage:
            continue
        rows.append(
            {
                "prompt_id": qid,
                "status": "ok",
                "provider": "gemini",
                "model": model,
                "checker": step.get("checker"),
                "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
                "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
                "total_tokens": int(usage.get("total_tokens", 0) or 0),
            }
        )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Gold-free non-executable SQL repair.")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--dataset-root", required=True)
    parser.add_argument("--source-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--model", default="gemini-3.1-pro-preview")
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--thinking-level", default="none")
    parser.add_argument("--thinking-budget", type=int, default=128)
    parser.add_argument("--sampling-budget", type=int, default=1)
    parser.add_argument("--max-output-tokens", type=int, default=8192)
    parser.add_argument("--timeout-s", type=float, default=120.0)
    parser.add_argument("--exec-timeout", type=float, default=30.0)
    parser.add_argument("--max-retries", type=int, default=4)
    parser.add_argument("--retry-delay-s", type=float, default=4.0)
    parser.add_argument("--proxy", default=None)
    parser.add_argument("--usage-jsonl")
    args = parser.parse_args()

    manifest = _load_manifest(Path(args.manifest).resolve())
    dataset_root = Path(args.dataset_root).resolve()
    source_dir = Path(args.source_dir).resolve()
    output_dir = Path(args.output_dir).resolve()
    merged_dir = output_dir / "merged_results"
    cases_dir = output_dir / "cases"
    output_dir.mkdir(parents=True, exist_ok=True)
    cases_dir.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source_dir, merged_dir, dirs_exist_ok=True)

    schema_assets = SchemaAssets(dataset_root)
    pipeline: CheckerPipeline | None = None
    case_rows: list[dict[str, Any]] = []
    usage_rows: list[dict[str, Any]] = []

    for row in manifest:
        qid = int(row["qid"])
        db_id = str(row["db_id"])
        result_path = _find_result_file(source_dir, qid)
        if result_path is None:
            case = {"qid": qid, "db_id": db_id, "status": "missing", "repaired": False}
            case_rows.append(case)
            (cases_dir / f"{qid}.json").write_text(json.dumps(case, ensure_ascii=False, indent=2), encoding="utf-8")
            continue

        raw = result_path.read_text(encoding="utf-8", errors="ignore")
        sql = _extract_sql(raw)
        db_path = _sqlite_db_path(dataset_root, db_id)
        before = _execute_sql(sql, db_path, timeout_s=args.exec_timeout)
        if before["ok"]:
            case = {
                "qid": qid,
                "db_id": db_id,
                "status": "already_executable",
                "source_path": str(result_path),
                "repaired": False,
                "before": before,
            }
            case_rows.append(case)
            (cases_dir / f"{qid}.json").write_text(json.dumps(case, ensure_ascii=False, indent=2), encoding="utf-8")
            continue

        if pipeline is None:
            pipeline = CheckerPipeline(
                model=args.model,
                temperature=args.temperature,
                thinking_level=args.thinking_level,
                thinking_budget=args.thinking_budget,
                api_keys=None,
                proxy=_resolve_proxy(args.proxy),
                checker_sampling_budget=args.sampling_budget,
                max_output_tokens=args.max_output_tokens,
                timeout_s=args.timeout_s,
                max_retries=args.max_retries,
                retry_delay_s=args.retry_delay_s,
            )

        baseline_sql = sql or ""
        revised_sql, trace = pipeline.revise_with_trace(
            sql=baseline_sql,
            question=str(row.get("question") or ""),
            schema=schema_assets.load_schema_sql(db_id),
            evidence=str(row.get("evidence") or ""),
            db_path=str(db_path),
        )
        after = _execute_sql(revised_sql, db_path, timeout_s=args.exec_timeout)
        out_path = merged_dir / result_path.name
        out_path.write_text(_render_result(revised_sql), encoding="utf-8")

        usage_rows.extend(_trace_usage_rows(qid, trace, model=args.model))
        case = {
            "qid": qid,
            "db_id": db_id,
            "status": "repaired" if after["ok"] else "repair_failed",
            "source_path": str(result_path),
            "output_path": str(out_path),
            "repaired": True,
            "before": before,
            "after": after,
            "trace": trace,
        }
        case_rows.append(case)
        (cases_dir / f"{qid}.json").write_text(json.dumps(case, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"qid={qid} before_exec=0 after_exec={1 if after['ok'] else 0}")

    summary = {
        "source_dir": str(source_dir),
        "merged_dir": str(merged_dir),
        "total": len(case_rows),
        "missing": sum(1 for row in case_rows if row["status"] == "missing"),
        "already_executable": sum(1 for row in case_rows if row["status"] == "already_executable"),
        "repaired": sum(1 for row in case_rows if row["status"] == "repaired"),
        "repair_failed": sum(1 for row in case_rows if row["status"] == "repair_failed"),
        "nonexec_before": sum(1 for row in case_rows if row["status"] in {"repaired", "repair_failed"}),
        "executable_after": sum(1 for row in case_rows if row["status"] in {"already_executable", "repaired"}),
        "cases": case_rows,
    }
    (output_dir / "repair_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# Local Non-Executable Repair",
        "",
        f"- source_dir: `{source_dir}`",
        f"- merged_dir: `{merged_dir}`",
        f"- total: `{summary['total']}`",
        f"- missing: `{summary['missing']}`",
        f"- nonexec_before: `{summary['nonexec_before']}`",
        f"- repaired: `{summary['repaired']}`",
        f"- repair_failed: `{summary['repair_failed']}`",
        f"- executable_after: `{summary['executable_after']}`",
    ]
    (output_dir / "repair_summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    if args.usage_jsonl:
        usage_path = Path(args.usage_jsonl).resolve()
        usage_path.parent.mkdir(parents=True, exist_ok=True)
        with usage_path.open("w", encoding="utf-8") as f:
            for row in usage_rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    print(json.dumps({k: v for k, v in summary.items() if k != "cases"}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
