from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable


def _first_present(row: dict, keys: list[str], fallback):
    for key in keys:
        if key in row and row[key] is not None:
            return row[key]
    return fallback


def load_jsonl(path: str | Path) -> list[dict]:
    rows: list[dict] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def write_jsonl(rows: Iterable[dict], path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def normalize_question_row(row: dict, fallback_qid: int) -> dict:
    qid = int(_first_present(row, ["qid", "question_id", "instance_id"], fallback_qid))
    question = (row.get("question") or row.get("query") or "").strip()
    evidence = (row.get("evidence") or row.get("external_knowledge") or "").strip()
    gold_sql = (row.get("gold_sql") or row.get("SQL") or "").strip()
    db_id = str(row.get("db_id") or row.get("selected_database") or "").strip()
    difficulty = str(row.get("difficulty") or row.get("difficulty_tier") or "unknown").strip().lower()
    return {
        "qid": qid,
        "db_id": db_id,
        "question": question,
        "evidence": evidence,
        "gold_sql": gold_sql,
        "difficulty": difficulty,
    }


def load_bird_dev_json(path: str | Path) -> list[dict]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return [normalize_question_row(row, i) for i, row in enumerate(raw, start=1)]


def load_generic_questions(path: str | Path) -> list[dict]:
    path = Path(path)
    if path.suffix == ".jsonl":
        rows = load_jsonl(path)
    elif path.suffix == ".json":
        rows = json.loads(path.read_text(encoding="utf-8"))
    else:
        raise ValueError(f"Unsupported question file format: {path}")
    return [normalize_question_row(row, i) for i, row in enumerate(rows, start=1)]
