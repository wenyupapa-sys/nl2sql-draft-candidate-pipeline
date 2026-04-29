#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
STAMP="$(date +%Y%m%d_%H%M%S)"
BUNDLE_DIR="${BUNDLE_DIR:-$ROOT/output/bird_submission_bundle_$STAMP}"
ZIP_PATH="${ZIP_PATH:-$BUNDLE_DIR.zip}"
DEV_PREDICT_JSON="${DEV_PREDICT_JSON:-}"

rm -rf "$BUNDLE_DIR" "$ZIP_PATH"
mkdir -p \
  "$BUNDLE_DIR/scripts/draft_sql_candidate_pipeline_20260427" \
  "$BUNDLE_DIR/field_recall_standalone/scripts" \
  "$BUNDLE_DIR/field_recall_standalone/src" \
  "$BUNDLE_DIR/templates" \
  "$BUNDLE_DIR/scripts"

cp "$ROOT/requirements.txt" "$BUNDLE_DIR/requirements.txt"
cp "$ROOT/checker_pipeline.py" "$BUNDLE_DIR/checker_pipeline.py"
cp "$ROOT/candidate_selector.py" "$BUNDLE_DIR/candidate_selector.py"
cp "$ROOT/scripts/build_prompts.py" "$BUNDLE_DIR/scripts/build_prompts.py"
cp "$ROOT/scripts/run_prompt_dir_gemini_api_batch.py" "$BUNDLE_DIR/scripts/run_prompt_dir_gemini_api_batch.py"

cp "$ROOT/templates/query_prompt_v12_direct.md" "$BUNDLE_DIR/templates/query_prompt_v12_direct.md"
cp "$ROOT/templates/query_prompt_v13_dc_sqlonly.md" "$BUNDLE_DIR/templates/query_prompt_v13_dc_sqlonly.md"
cp "$ROOT/templates/query_prompt_v13_skeleton_sqlonly.md" "$BUNDLE_DIR/templates/query_prompt_v13_skeleton_sqlonly.md"

cp -R "$ROOT/field_recall_standalone/src/field_recall" "$BUNDLE_DIR/field_recall_standalone/src/field_recall"
cp -R "$ROOT/field_recall_standalone/prompts" "$BUNDLE_DIR/field_recall_standalone/prompts"
cp "$ROOT/field_recall_standalone/scripts/render_stage_prompts.py" "$BUNDLE_DIR/field_recall_standalone/scripts/render_stage_prompts.py"
cp "$ROOT/field_recall_standalone/scripts/generate_pipeline_scoped_prompts.py" "$BUNDLE_DIR/field_recall_standalone/scripts/generate_pipeline_scoped_prompts.py"

rm -f \
  "$BUNDLE_DIR/field_recall_standalone/src/field_recall/evaluator.py" \
  "$BUNDLE_DIR/field_recall_standalone/src/field_recall/metrics.py" \
  "$BUNDLE_DIR/field_recall_standalone/src/field_recall/value_index.py"

cat > "$BUNDLE_DIR/field_recall_standalone/src/field_recall/dataset.py" <<'PY'
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable


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
PY

SAFE_FILES=(
  README_BIRD_SUBMISSION.md
  run_test.sh
  stage_test_dataset.py
  build_test_manifest.py
  audit_prompts_no_db_values.py
  run_prompt_dir_qwen_api_batch.py
  run_prompt_dir_gemini_api_batch.py
  collect_stage_outputs.py
  run_stage4_field_match_gemini_embedding.py
  build_cross_model_stage3_hints.py
  merge_field_candidates.py
  build_final_prompt_dirs_with_draft_hints.py
  build_pairwise_schema_blocks.py
  fill_missing_results_from_backup.py
  repair_nonexec_local.py
  select_candidates_from_dirs.py
  convert_results_to_predict_json.py
  aggregate_usage.py
)

for file in "${SAFE_FILES[@]}"; do
  cp "$SCRIPT_DIR/$file" "$BUNDLE_DIR/scripts/draft_sql_candidate_pipeline_20260427/$file"
done
cp "$SCRIPT_DIR/README_BIRD_SUBMISSION.md" "$BUNDLE_DIR/README.md"
if [[ -n "$DEV_PREDICT_JSON" ]]; then
  cp "$DEV_PREDICT_JSON" "$BUNDLE_DIR/predict_dev.json"
fi

find "$BUNDLE_DIR" -name '__pycache__' -type d -prune -exec rm -rf {} +
find "$BUNDLE_DIR" -name '*.pyc' -delete

(
  cd "$BUNDLE_DIR"
  rg -n "AIza|sk-|dev_gold|BIRD_GOLD_SQL|gold_sql|eval_result_dir|repair_nonexec_result_dir" . && {
    echo "Compliance scan failed. Remove matched content before submitting." >&2
    exit 2
  } || true
)

(
  cd "$(dirname "$BUNDLE_DIR")"
  zip -qr "$(basename "$ZIP_PATH")" "$(basename "$BUNDLE_DIR")"
)

echo "Bundle directory: $BUNDLE_DIR"
echo "Bundle zip:       $ZIP_PATH"
