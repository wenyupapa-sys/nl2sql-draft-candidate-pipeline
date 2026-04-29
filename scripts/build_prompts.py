#!/usr/bin/env python3
"""Build BIRD prompts for schema linking and SQL generation."""

from __future__ import annotations

import json
import os
import re
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

try:
    import config
except Exception:  # pragma: no cover - official submission can omit dev config.
    class _PromptConfig:
        PROMPT_COMPACT_COLUMN_MEANINGS = False
        PROMPT_COLUMN_MEANING_MAX_CHARS = 0

    config = _PromptConfig()


def load_template(name: str) -> str:
    path = os.path.join(PROJECT_ROOT, "templates", name)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _compact_text(text: str, max_chars: int) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(text) <= max_chars:
        return text
    clipped = text[: max(0, max_chars - 3)].rstrip()
    if " " in clipped:
        clipped = clipped.rsplit(" ", 1)[0]
    return clipped + "..."


def compact_column_meanings_for_prompt(meanings_text: str) -> str:
    if not config.PROMPT_COMPACT_COLUMN_MEANINGS:
        return meanings_text
    try:
        obj = json.loads(meanings_text)
    except Exception:
        return meanings_text
    compacted = {}
    for key, value in obj.items():
        compacted[key] = _compact_text(value, config.PROMPT_COLUMN_MEANING_MAX_CHARS)
    return json.dumps(compacted, ensure_ascii=False, indent=2)


def select_error_patterns(item: dict) -> str:
    return ""


def build_prompt(item: dict, template_text: str, decomposed_suffix: str | None = None) -> str:
    prompt = template_text
    prompt = prompt.replace("{{DATABASE}}", item.get("database", ""))
    # If schema_with_annotations is provided (V9+), use it; otherwise fallback
    prompt = prompt.replace("{{SCHEMA}}", item.get("schema_with_annotations", item.get("schema", "")))
    prompt = prompt.replace(
        "{{COLUMN_MEANINGS}}",
        compact_column_meanings_for_prompt(item.get("column_meanings", "")),
    )
    prompt = prompt.replace("{{EXTERNAL_KNOWLEDGE}}", item.get("external_knowledge", ""))
    prompt = prompt.replace("{{TABLE_DESCRIPTIONS}}", item.get("table_descriptions", ""))
    prompt = prompt.replace("{{SKELETON_ICL_EXAMPLES}}", item.get("skeleton_icl_examples", ""))
    prompt = prompt.replace("{{QUERY_UNDERSTANDING}}", item.get("query_understanding", ""))
    prompt = prompt.replace("{{QUESTION}}", item.get("question", ""))
    prompt = prompt.replace("{{VALUE_GROUNDING_HINT}}", item.get("value_grounding_hint", ""))
    prompt = prompt.replace("{{CROSS_MODEL_SQL_HINT}}", item.get("cross_model_sql_hint", ""))
    prompt = prompt.replace("{{MANAGEMENT_CONTRACT}}", "")
    prompt = prompt.replace("{{MANAGEMENT_DB_HINTS}}", "")

    if decomposed_suffix:
        marker = "## 输出要求"
        idx = prompt.find(marker)
        if idx != -1:
            prompt = prompt[:idx] + decomposed_suffix
        else:
            prompt += "\n\n" + decomposed_suffix
    return prompt


def build_schema_linking_prompt(item: dict, template_text: str) -> str:
    prompt = template_text
    prompt = prompt.replace("{{SCHEMA}}", item.get("schema", ""))
    prompt = prompt.replace(
        "{{COLUMN_MEANINGS}}",
        compact_column_meanings_for_prompt(item.get("column_meanings", "")),
    )
    prompt = prompt.replace("{{EXTERNAL_KNOWLEDGE}}", item.get("external_knowledge_raw_text", ""))
    prompt = prompt.replace("{{QUESTION}}", item.get("question", ""))
    return prompt


def load_table_descriptions(db_id: str) -> str:
    """Load LLM-generated table descriptions for a database."""
    import os
    desc_dir = os.path.join(PROJECT_ROOT, "artifacts", "db_descriptions", db_id)
    if not os.path.isdir(desc_dir):
        return ""
    parts = []
    for f in sorted(os.listdir(desc_dir)):
        if not f.endswith(".md"):
            continue
        path = os.path.join(desc_dir, f)
        with open(path, "r", encoding="utf-8") as fh:
            content = fh.read().strip()
        if content and "TODO: Replace" not in content:
            parts.append(content)
    if not parts:
        return ""
    return "## Table Descriptions\n\n" + "\n\n---\n\n".join(parts)
