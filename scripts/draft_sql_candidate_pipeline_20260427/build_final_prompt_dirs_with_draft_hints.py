#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import types
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FIELD_RECALL_ROOT = PROJECT_ROOT / "field_recall_standalone"
sys.path.insert(0, str(FIELD_RECALL_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT))

from field_recall.dataset import load_jsonl
from field_recall.schema_assets import SchemaAssets


BUILD_PROMPTS_PATH = PROJECT_ROOT / "scripts" / "build_prompts.py"
BUILD_SPEC = importlib.util.spec_from_file_location("build_prompts", BUILD_PROMPTS_PATH)
if BUILD_SPEC is None or BUILD_SPEC.loader is None:
    raise RuntimeError(f"Cannot load {BUILD_PROMPTS_PATH}")
BUILD_PROMPTS = importlib.util.module_from_spec(BUILD_SPEC)
BUILD_SPEC.loader.exec_module(BUILD_PROMPTS)
build_prompt = BUILD_PROMPTS.build_prompt
SCRIPTS_PACKAGE = types.ModuleType("scripts")
SCRIPTS_PACKAGE.__path__ = [str(PROJECT_ROOT / "scripts")]
sys.modules["scripts"] = SCRIPTS_PACKAGE
sys.modules["scripts.build_prompts"] = BUILD_PROMPTS

GEN_PATH = FIELD_RECALL_ROOT / "scripts" / "generate_pipeline_scoped_prompts.py"
SPEC = importlib.util.spec_from_file_location("generate_pipeline_scoped_prompts", GEN_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Cannot load {GEN_PATH}")
GEN = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GEN)

TEMPLATE_SPECS = [
    ("query_prompt_v12_direct.md", "v12_hint"),
    ("query_prompt_v13_dc_sqlonly.md", "v13_dc_sqlonly_hint"),
    ("query_prompt_v13_skeleton_sqlonly.md", "v13_skeleton_sqlonly_hint"),
]


def _read_id_file(path: str | None) -> set[int] | None:
    if not path:
        return None
    ids = {int(x.strip()) for x in Path(path).read_text(encoding="utf-8").splitlines() if x.strip()}
    return ids


def _load_hints(paths: list[str]) -> dict[int, str]:
    qid_to_parts: dict[int, list[str]] = {}
    for path_str in paths:
        path = Path(path_str)
        if not path.exists():
            continue
        for row in load_jsonl(path):
            try:
                qid = int(row["qid"])
            except Exception:
                continue
            hint = str(row.get("hint_text") or "").strip()
            if hint:
                qid_to_parts.setdefault(qid, []).append(hint)
    return {qid: "\n\n".join(parts) for qid, parts in qid_to_parts.items()}


def _hint_block(raw_hint: str, *, prompt_scope: str) -> str:
    if not raw_hint.strip():
        if prompt_scope == "simple":
            return "## Draft SQL Hints\nNo executable draft SQL hint is available for this question."
        return "## Draft SQL Hints\nNo executable cross-model draft SQL hint is available for this question."
    if prompt_scope == "simple":
        lead = "The following draft may be wrong. Use it only as a hint for tables, joins, filters, and aggregation."
    else:
        lead = "The following cross-model draft may be wrong. Use it only as a hint for tables, joins, filters, and aggregation."
    return f"## Draft SQL Hints\n{lead}\n\n{raw_hint.strip()}"


def _inject_hint_if_needed(rendered: str, template_text: str, hint_block: str) -> str:
    if "{{CROSS_MODEL_SQL_HINT}}" in template_text:
        return rendered
    if not hint_block.strip():
        return rendered
    for marker in ("\n# Final Reminder", "\n# Output"):
        idx = rendered.find(marker)
        if idx != -1:
            return rendered[:idx].rstrip() + "\n\n" + hint_block.strip() + "\n" + rendered[idx:]
    return rendered.rstrip() + "\n\n" + hint_block.strip() + "\n"


def _sanitize_value_example_mentions(rendered: str) -> str:
    replacements = {
        "**Rule 4 \u2014 TEXT date columns: check value_examples first**": "**Rule 4 \u2014 TEXT date columns: check column descriptions first**",
        "**Rule 4 - TEXT date columns: check value_examples first**": "**Rule 4 - TEXT date columns: check column descriptions first**",
        "Always check `value_examples` before writing comparisons.": "Use column descriptions and evidence before writing comparisons.",
        "Always check `value_examples` first.": "Use column descriptions and evidence first.",
        "12. **Value Examples:**": "12. **Column Descriptions:**",
        "For key phrases mentioned in the question, we have provided the most similar values within the columns (TEXT-TYPE columns) denoted by \"Value Examples\".": "For key phrases mentioned in the question, use the provided column descriptions, schema, evidence, and draft SQL hints. No sampled database values are included in this prompt.",
        "`value_examples`": "`column_descriptions`",
        "value_examples": "column descriptions",
    }
    for old, new in replacements.items():
        rendered = rendered.replace(old, new)
    return rendered


def _template_output_specs(prompt_scope: str, target_final_model: str) -> list[tuple[str, str]]:
    if prompt_scope == "simple":
        return [("query_prompt_v12_direct.md", "simple_v12_hint")]
    prefix = f"modchall_for_{target_final_model}"
    return [(template, f"{prefix}_{slug}") for template, slug in TEMPLATE_SPECS]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--dataset-root", default=str(FIELD_RECALL_ROOT / "data" / "bird_dev"))
    parser.add_argument("--base-template-root", default=str(PROJECT_ROOT / "templates"))
    parser.add_argument("--final-fields-jsonl", required=True)
    parser.add_argument("--field-rewrites-jsonl", required=True)
    parser.add_argument("--stage-jsonl", action="append", default=[])  # Accepted for plan compatibility.
    parser.add_argument("--draft-hint-jsonl", action="append", default=[])
    parser.add_argument("--prompt-scope", choices=["simple", "modchall"], required=True)
    parser.add_argument("--target-final-model", choices=["gemini", "qwen"], default="gemini")
    parser.add_argument("--qid-file")
    parser.add_argument("--no-value-examples", action="store_true", help="Do not include sampled DB values in final prompts.")
    parser.add_argument("--output-root", required=True)
    args = parser.parse_args()

    qid_filter = _read_id_file(args.qid_file)
    manifest_rows = load_jsonl(args.manifest)
    if args.prompt_scope == "simple":
        manifest_rows = [r for r in manifest_rows if str(r.get("difficulty", "")).lower() == "simple"]
    else:
        manifest_rows = [
            r for r in manifest_rows
            if str(r.get("difficulty", "")).lower() in {"moderate", "challenging"}
        ]
    if qid_filter is not None:
        manifest_rows = [r for r in manifest_rows if int(r["qid"]) in qid_filter]

    qid_to_fields = GEN._load_final_fields(Path(args.final_fields_jsonl))
    field_rewrites_by_db, table_descs_by_db = GEN._load_stage2_maps(Path(args.field_rewrites_jsonl))
    qid_to_hint = _load_hints(args.draft_hint_jsonl)
    schema_assets = SchemaAssets(args.dataset_root)
    output_root = Path(args.output_root)
    output_root.mkdir(parents=True, exist_ok=True)

    scoped_schema_cache: dict[tuple[int, str, str], str] = {}
    for template_name, out_slug in _template_output_specs(args.prompt_scope, args.target_final_model):
        template_path = Path(args.base_template_root) / template_name
        template_text = template_path.read_text(encoding="utf-8")
        out_dir = output_root / out_slug
        out_dir.mkdir(parents=True, exist_ok=True)
        count = 0
        template_stem = Path(template_name).stem
        for row in manifest_rows:
            qid = int(row["qid"])
            db_id = str(row["db_id"])
            cache_key = (qid, db_id, template_stem)
            if cache_key not in scoped_schema_cache:
                if template_stem == "query_prompt_v12_direct":
                    scoped_schema_cache[cache_key] = GEN._render_v12_legacy_schema(
                        schema_assets=schema_assets,
                        db_id=db_id,
                        selected_fields=qid_to_fields.get(qid, set()),
                        table_descriptions=table_descs_by_db.get(db_id),
                        field_rewrites=field_rewrites_by_db.get(db_id),
                        include_value_examples=not args.no_value_examples,
                    )
                else:
                    scoped_schema_cache[cache_key] = GEN._render_scoped_schema(
                        schema_assets=schema_assets,
                        db_id=db_id,
                        selected_fields=qid_to_fields.get(qid, set()),
                        field_rewrites=field_rewrites_by_db.get(db_id),
                        table_descriptions=table_descs_by_db.get(db_id),
                        include_value_examples=not args.no_value_examples,
                    )
            hint_block = _hint_block(qid_to_hint.get(qid, ""), prompt_scope=args.prompt_scope)
            item = {
                "database": db_id,
                "schema": scoped_schema_cache[cache_key],
                "schema_with_annotations": scoped_schema_cache[cache_key],
                "question": row["question"],
                "external_knowledge": row.get("evidence", "") or "",
                "cross_model_sql_hint": hint_block,
            }
            rendered = build_prompt(item, template_text)
            rendered = _inject_hint_if_needed(rendered, template_text, hint_block)
            if args.no_value_examples:
                rendered = _sanitize_value_example_mentions(rendered)
            (out_dir / f"{qid}.txt").write_text(rendered, encoding="utf-8")
            count += 1
        print(json.dumps({"template": template_name, "out_dir": str(out_dir), "count": count}, ensure_ascii=False))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
