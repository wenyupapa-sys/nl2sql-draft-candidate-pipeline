from __future__ import annotations

import json
import re
from collections import defaultdict

import sqlglot
from sqlglot import exp

from .sql_analysis import extract_physical_table_columns, group_columns_by_table


def _extract_json_objects(text: str) -> list[str]:
    stack = []
    start = None
    chunks: list[str] = []
    for i, ch in enumerate(text):
        if ch == "{":
            if not stack:
                start = i
            stack.append(ch)
        elif ch == "}":
            if stack:
                stack.pop()
                if not stack and start is not None:
                    chunks.append(text[start:i + 1])
                    start = None
    return chunks


def parse_rewrite_markdown(text: str) -> dict:
    text = text.strip()

    # New preferred format: JSON
    cleaned = re.sub(r"```(?:json)?\s*", "", text)
    cleaned = re.sub(r"```", "", cleaned).strip()
    for obj_text in _extract_json_objects(cleaned):
        try:
            parsed = json.loads(obj_text)
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, dict):
            continue
        rewritten = str(parsed.get("rewritten_query", "")).strip()
        keywords = parsed.get("keywords", [])
        if not isinstance(keywords, list):
            keywords = []
        keywords = [str(k).strip() for k in keywords if str(k).strip()]
        if rewritten or keywords:
            return {"rewritten_query": rewritten, "keywords": keywords}

    # Backward-compatible format: Markdown
    rq = ""
    keywords: list[str] = []
    m = re.search(r"##\s*Rewritten Query\s*(.+?)(?:\n##\s*Keywords|\Z)", text, flags=re.S | re.I)
    if m:
        rq = m.group(1).strip()
    km = re.search(r"##\s*Keywords\s*(.+)$", text, flags=re.S | re.I)
    if km:
        for line in km.group(1).splitlines():
            line = line.strip()
            if line.startswith("-"):
                keywords.append(line[1:].strip())
    return {"rewritten_query": rq, "keywords": keywords}


def parse_field_rewrite_payload(text: str) -> dict:
    text = text.strip()
    if "[RESPONSE]" in text:
        text = text.split("[RESPONSE]", 1)[1].strip()
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = re.sub(r"```", "", text).strip()

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        return parsed

    for obj_text in _extract_json_objects(text):
        try:
            parsed = json.loads(obj_text)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


def parse_field_rewrite_json(text: str) -> dict[str, dict[str, list[str]]]:
    parsed = parse_field_rewrite_payload(text)
    out: dict[str, dict[str, list[str]]] = {}
    for table, payload in parsed.items():
        if not isinstance(payload, dict):
            continue
        if "columns" in payload and isinstance(payload["columns"], dict):
            cols = payload["columns"]
        else:
            cols = payload
        normalized: dict[str, list[str]] = {}
        for column, aliases in cols.items():
            if isinstance(aliases, list):
                normalized[str(column)] = [str(a).strip() for a in aliases if str(a).strip()]
            elif isinstance(aliases, str) and aliases.strip():
                normalized[str(column)] = [aliases.strip()]
        if normalized:
            out[str(table)] = normalized
    return out


def extract_table_descriptions(text: str) -> dict[str, str]:
    parsed = parse_field_rewrite_payload(text)
    out: dict[str, str] = {}
    for table, payload in parsed.items():
        if not isinstance(payload, dict):
            continue
        desc = str(payload.get("table_description", "")).strip()
        if desc:
            out[str(table)] = desc
    return out


def parse_direct_tables_columns(text: str) -> dict[str, list[str]]:
    text = re.sub(r"```(?:json)?\s*", "", text.strip())
    text = re.sub(r"```", "", text)
    for obj_text in _extract_json_objects(text):
        try:
            parsed = json.loads(obj_text)
        except json.JSONDecodeError:
            continue
        tables = parsed.get("tables", parsed)
        if not isinstance(tables, dict):
            continue
        out = {}
        for table, cols in tables.items():
            if isinstance(cols, list):
                out[str(table).lower()] = [str(c).lower() for c in cols]
            elif isinstance(cols, str):
                out[str(table).lower()] = [cols.lower()]
        if out:
            return out
    return {}


def parse_conditions_json(text: str) -> list[dict]:
    text = re.sub(r"```(?:json)?\s*", "", text.strip())
    text = re.sub(r"```", "", text)
    for obj_text in _extract_json_objects(text):
        try:
            parsed = json.loads(obj_text)
        except json.JSONDecodeError:
            continue
        conditions = parsed.get("conditions", [])
        if isinstance(conditions, list):
            return conditions
    return []


def _fallback_parse_sql_tables_columns(sql: str) -> dict[str, set[str]]:
    alias_to_tables: dict[str, set[str]] = defaultdict(set)
    out: dict[str, set[str]] = defaultdict(set)

    table_pat = re.compile(
        r"\b(?:FROM|JOIN)\s+['\"`]?(?P<table>[A-Za-z_][A-Za-z0-9_]*)['\"`]?"
        r"(?:\s+(?:AS\s+)?(?P<alias>[A-Za-z_][A-Za-z0-9_]*))?",
        flags=re.I,
    )
    for match in table_pat.finditer(sql):
        table = match.group("table").lower()
        alias = (match.group("alias") or table).lower()
        alias_to_tables[alias].add(table)

    qualified_pat = re.compile(r"\b(?P<alias>[A-Za-z_][A-Za-z0-9_]*)\.(?P<column>[A-Za-z_][A-Za-z0-9_]*)\b")
    for match in qualified_pat.finditer(sql):
        alias = match.group("alias").lower()
        column = match.group("column").lower()
        for table in alias_to_tables.get(alias, set()):
            out[table].add(column)

    return out


def parse_draft_sql_tables_columns(text: str) -> dict[str, set[str]]:
    sql = re.sub(r"```(?:sql)?\s*", "", text.strip())
    sql = re.sub(r"```", "", sql).strip()
    if not any(kw in sql.upper() for kw in ("SELECT", "FROM", "WHERE", "JOIN")):
        return {}
    try:
        _, columns = extract_physical_table_columns(sql)
        return group_columns_by_table(columns)
    except Exception:
        return _fallback_parse_sql_tables_columns(sql)
