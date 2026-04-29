#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
FR="$ROOT/field_recall_standalone"

: "${TEST_JSON:?Set TEST_JSON to BIRD test.json.}"
: "${TEST_TABLES:?Set TEST_TABLES to BIRD test_tables.json.}"
: "${TEST_DATABASES:?Set TEST_DATABASES to BIRD test_databases directory.}"
: "${GEMINI_API_KEY:?Set GEMINI_API_KEY.}"
if [[ -z "${DASHSCOPE_API_KEY:-}" ]]; then
  : "${QWEN_API_KEY:?Set DASHSCOPE_API_KEY or QWEN_API_KEY.}"
  export DASHSCOPE_API_KEY="$QWEN_API_KEY"
fi

OUTPUT_DIR="${OUTPUT_DIR:-$ROOT/output/bird_submission_test_run}"
QWEN_MODEL="${QWEN_MODEL:-qwen3.6-plus}"
GEMINI_FLASH_MODEL="${GEMINI_FLASH_MODEL:-gemini-3-flash-preview}"
GEMINI_REPAIR_MODEL="${GEMINI_REPAIR_MODEL:-gemini-3.1-pro-preview}"
GEMINI_DRAFT_THINKING_BUDGET="${GEMINI_DRAFT_THINKING_BUDGET:-none}"
GEMINI_FINAL_THINKING_BUDGET="${GEMINI_FINAL_THINKING_BUDGET:-128}"
GEMINI_REPAIR_THINKING_BUDGET="${GEMINI_REPAIR_THINKING_BUDGET:-128}"
GEMINI_DRAFT_THINKING_LEVEL="${GEMINI_DRAFT_THINKING_LEVEL:-none}"
GEMINI_FINAL_THINKING_LEVEL="${GEMINI_FINAL_THINKING_LEVEL:-none}"
GEMINI_REPAIR_THINKING_LEVEL="${GEMINI_REPAIR_THINKING_LEVEL:-none}"
MAX_CONCURRENT="${MAX_CONCURRENT:-4}"
GEMINI_MAX_CONCURRENT="${GEMINI_MAX_CONCURRENT:-$MAX_CONCURRENT}"
QWEN_MAX_CONCURRENT="${QWEN_MAX_CONCURRENT:-$MAX_CONCURRENT}"
EMBED_BATCH_SIZE="${EMBED_BATCH_SIZE:-32}"
QWEN_EMBED_BATCH_SIZE="${QWEN_EMBED_BATCH_SIZE:-10}"
STAGE4_EMBEDDING_MODE="${STAGE4_EMBEDDING_MODE:-fallback}"
QWEN_DRAFT_ENABLE_THINKING="${QWEN_DRAFT_ENABLE_THINKING:-0}"
QWEN_DRAFT_THINKING_BUDGET="${QWEN_DRAFT_THINKING_BUDGET:-none}"
QWEN_FINAL_ENABLE_THINKING="${QWEN_FINAL_ENABLE_THINKING:-1}"
QWEN_FINAL_THINKING_BUDGET="${QWEN_FINAL_THINKING_BUDGET:-128}"
FINAL_GEMINI_DIR_PARALLEL="${FINAL_GEMINI_DIR_PARALLEL:-3}"
FINAL_QWEN_DIR_PARALLEL="${FINAL_QWEN_DIR_PARALLEL:-2}"
SELECTOR_MAX_WORKERS="${SELECTOR_MAX_WORKERS:-4}"

if (( FINAL_GEMINI_DIR_PARALLEL < 1 )); then FINAL_GEMINI_DIR_PARALLEL=1; fi
if (( FINAL_QWEN_DIR_PARALLEL < 1 )); then FINAL_QWEN_DIR_PARALLEL=1; fi
if (( SELECTOR_MAX_WORKERS < 1 )); then SELECTOR_MAX_WORKERS=1; fi

if [[ -z "${FINAL_GEMINI_PER_DIR_CONCURRENT:-}" ]]; then
  FINAL_GEMINI_PER_DIR_CONCURRENT=$(( GEMINI_MAX_CONCURRENT / FINAL_GEMINI_DIR_PARALLEL ))
  if (( FINAL_GEMINI_PER_DIR_CONCURRENT < 1 )); then FINAL_GEMINI_PER_DIR_CONCURRENT=1; fi
fi
if [[ -z "${FINAL_QWEN_PER_DIR_CONCURRENT:-}" ]]; then
  FINAL_QWEN_PER_DIR_CONCURRENT=$(( QWEN_MAX_CONCURRENT / FINAL_QWEN_DIR_PARALLEL ))
  if (( FINAL_QWEN_PER_DIR_CONCURRENT < 1 )); then FINAL_QWEN_PER_DIR_CONCURRENT=1; fi
fi
if (( FINAL_GEMINI_PER_DIR_CONCURRENT < 1 )); then FINAL_GEMINI_PER_DIR_CONCURRENT=1; fi
if (( FINAL_QWEN_PER_DIR_CONCURRENT < 1 )); then FINAL_QWEN_PER_DIR_CONCURRENT=1; fi

GEMINI_DRAFT_THINKING_ARGS=(--thinking-level "$GEMINI_DRAFT_THINKING_LEVEL")
GEMINI_FINAL_THINKING_ARGS=(--thinking-level "$GEMINI_FINAL_THINKING_LEVEL")
GEMINI_REPAIR_THINKING_ARGS=(--thinking-level "$GEMINI_REPAIR_THINKING_LEVEL")
QWEN_DRAFT_THINKING_ARGS=()
QWEN_FINAL_THINKING_ARGS=()
if [[ -n "${GEMINI_DRAFT_THINKING_BUDGET:-}" && "${GEMINI_DRAFT_THINKING_BUDGET:-}" != "none" ]]; then
  GEMINI_DRAFT_THINKING_ARGS=(--thinking-budget "$GEMINI_DRAFT_THINKING_BUDGET")
fi
if [[ -n "${GEMINI_FINAL_THINKING_BUDGET:-}" && "${GEMINI_FINAL_THINKING_BUDGET:-}" != "none" ]]; then
  GEMINI_FINAL_THINKING_ARGS=(--thinking-budget "$GEMINI_FINAL_THINKING_BUDGET")
fi
if [[ -n "${GEMINI_REPAIR_THINKING_BUDGET:-}" && "${GEMINI_REPAIR_THINKING_BUDGET:-}" != "none" ]]; then
  GEMINI_REPAIR_THINKING_ARGS=(--thinking-budget "$GEMINI_REPAIR_THINKING_BUDGET")
fi
if [[ "$QWEN_DRAFT_ENABLE_THINKING" == "1" ]]; then
  QWEN_DRAFT_THINKING_ARGS+=(--enable-thinking)
  if [[ -n "${QWEN_DRAFT_THINKING_BUDGET:-}" && "${QWEN_DRAFT_THINKING_BUDGET:-}" != "none" ]]; then
    QWEN_DRAFT_THINKING_ARGS+=(--thinking-budget "$QWEN_DRAFT_THINKING_BUDGET")
  fi
fi
if [[ "$QWEN_FINAL_ENABLE_THINKING" == "1" ]]; then
  QWEN_FINAL_THINKING_ARGS+=(--enable-thinking)
  if [[ -n "${QWEN_FINAL_THINKING_BUDGET:-}" && "${QWEN_FINAL_THINKING_BUDGET:-}" != "none" ]]; then
    QWEN_FINAL_THINKING_ARGS+=(--thinking-budget "$QWEN_FINAL_THINKING_BUDGET")
  fi
fi

if [[ -z "${GEMINI_PROXY:-}" ]]; then
  GEMINI_PROXY="none"
  if command -v scutil >/dev/null 2>&1; then
    SYSTEM_PROXY="$(
      scutil --proxy | awk '
        /HTTPSEnable : 1/ { enabled=1 }
        /HTTPSProxy : / { host=$3 }
        /HTTPSPort : / { port=$3 }
        END { if (enabled && host && port) print "http://" host ":" port }
      '
    )"
    if [[ -n "$SYSTEM_PROXY" ]]; then
      GEMINI_PROXY="$SYSTEM_PROXY"
    fi
  fi
fi

RUN="$OUTPUT_DIR"
DATASET="$RUN/test_dataset"
MANIFEST="$RUN/meta/test_manifest.jsonl"

mkdir -p \
  "$RUN/meta" \
  "$RUN/prompts" \
  "$RUN/draft_raw" \
  "$RUN/stages" \
  "$RUN/draft_hints" \
  "$RUN/final_prompts" \
  "$RUN/final_raw" \
  "$RUN/repair" \
  "$RUN/pairwise_schema" \
  "$RUN/selected" \
  "$RUN/candidates" \
  "$RUN/reports" \
  "$RUN/usage" \
  "$RUN/embeddings" \
  "$RUN/logs"

echo "[1/13] Stage official test dataset"
STAGE_ARGS=(
  --test-json "$TEST_JSON"
  --test-tables "$TEST_TABLES"
  --test-databases "$TEST_DATABASES"
  --output-root "$DATASET"
)
if [[ -n "${COLUMN_MEANING:-}" ]]; then
  STAGE_ARGS+=(--column-meaning "$COLUMN_MEANING")
fi
if [[ "${COPY_DATABASES:-0}" == "1" ]]; then
  STAGE_ARGS+=(--copy-databases)
fi
python "$SCRIPT_DIR/stage_test_dataset.py" "${STAGE_ARGS[@]}"

echo "[2/13] Build gold-free manifest; force moderate route for all test rows"
python "$SCRIPT_DIR/build_test_manifest.py" \
  --test-json "$DATASET/test.json" \
  --difficulty moderate \
  --output "$MANIFEST" \
  --qids-output "$RUN/meta/test_qids.txt" \
  --dbids-output "$RUN/meta/test_dbids.txt"

echo "[3/13] Render safe stage prompts with column_meaning and no sampled values"
python "$FR/scripts/render_stage_prompts.py" \
  --manifest "$MANIFEST" \
  --dataset-root "$DATASET" \
  --output-root "$RUN/prompts" \
  --no-sample-values
python "$SCRIPT_DIR/audit_prompts_no_db_values.py" --prompt-root "$RUN/prompts"

echo "[4/13] Run Stage1/2/3 table extraction with Qwen3.6-plus, thinking disabled"
python "$SCRIPT_DIR/run_prompt_dir_qwen_api_batch.py" \
  --trace-dir "$ROOT" \
  --prompt-dir "$RUN/prompts/stage1_rewrite" \
  --prompt-ids-file "$RUN/meta/test_qids.txt" \
  --output-dir "$RUN/draft_raw/stage1_rewrite_qwen36plus_t03" \
  --output-suffix "" \
  --model "$QWEN_MODEL" \
  --temperature 0.3 \
  --max-concurrent "$QWEN_MAX_CONCURRENT" \
  --usage-jsonl "$RUN/usage/stage1_rewrite_qwen36plus_t03.jsonl"

python "$SCRIPT_DIR/run_prompt_dir_qwen_api_batch.py" \
  --trace-dir "$ROOT" \
  --prompt-dir "$RUN/prompts/stage2_field_rewrite" \
  --prompt-ids-file "$RUN/meta/test_dbids.txt" \
  --output-dir "$RUN/draft_raw/stage2_field_rewrite_qwen36plus_t03" \
  --output-suffix "" \
  --model "$QWEN_MODEL" \
  --temperature 0.3 \
  --max-concurrent "$QWEN_MAX_CONCURRENT" \
  --usage-jsonl "$RUN/usage/stage2_field_rewrite_qwen36plus_t03.jsonl"

python "$SCRIPT_DIR/run_prompt_dir_qwen_api_batch.py" \
  --trace-dir "$ROOT" \
  --prompt-dir "$RUN/prompts/stage3_tables_columns" \
  --prompt-ids-file "$RUN/meta/test_qids.txt" \
  --output-dir "$RUN/draft_raw/stage3_tables_columns_qwen36plus_t03" \
  --output-suffix "" \
  --model "$QWEN_MODEL" \
  --temperature 0.3 \
  --max-concurrent "$QWEN_MAX_CONCURRENT" \
  --usage-jsonl "$RUN/usage/stage3_tables_columns_qwen36plus_t03.jsonl"

echo "[5/13] Collect Stage1/2/3 outputs and run Gemini embedding field match"
python "$SCRIPT_DIR/collect_stage_outputs.py" \
  --raw-dir "$RUN/draft_raw/stage1_rewrite_qwen36plus_t03" \
  --stage stage1_rewrite \
  --output "$RUN/stages/stage1_rewrite.jsonl"
python "$SCRIPT_DIR/collect_stage_outputs.py" \
  --raw-dir "$RUN/draft_raw/stage2_field_rewrite_qwen36plus_t03" \
  --stage stage2_field_rewrite \
  --output "$RUN/stages/stage2_field_rewrite.jsonl"
python "$SCRIPT_DIR/collect_stage_outputs.py" \
  --raw-dir "$RUN/draft_raw/stage3_tables_columns_qwen36plus_t03" \
  --stage stage3_tables_columns \
  --output "$RUN/stages/stage3_tables_columns.jsonl"

python "$SCRIPT_DIR/run_stage4_field_match_gemini_embedding.py" \
  --manifest "$MANIFEST" \
  --dataset-root "$DATASET" \
  --rewrite-jsonl "$RUN/stages/stage1_rewrite.jsonl" \
  --field-rewrite-jsonl "$RUN/stages/stage2_field_rewrite.jsonl" \
  --embedding-model gemini-embedding-001 \
  --fallback-embedding-provider qwen \
  --qwen-embedding-model "${QWEN_EMBEDDING_MODEL:-text-embedding-v4}" \
  --qwen-batch-size "$QWEN_EMBED_BATCH_SIZE" \
  --batch-size "$EMBED_BATCH_SIZE" \
  --cache-dir "$RUN/embeddings/gemini_embedding_001" \
  --proxy "$GEMINI_PROXY" \
  --timeout-seconds "${EMBED_TIMEOUT_SECONDS:-45}" \
  --max-retries "${EMBED_MAX_RETRIES:-1}" \
  $([[ "$STAGE4_EMBEDDING_MODE" == "cache_only" ]] && printf '%s' '--cache-only' || printf '%s' '--fallback-on-embedding-error') \
  --usage-jsonl "$RUN/usage/stage4_gemini_embedding_001.jsonl" \
  --output "$RUN/stages/stage4_field_match.jsonl"

echo "[6/13] Generate Stage3 draft SQL hints with thinking disabled"
python "$SCRIPT_DIR/run_prompt_dir_gemini_api_batch.py" \
  --trace-dir "$ROOT" \
  --prompt-dir "$RUN/prompts/stage3_draft_sql" \
  --prompt-ids-file "$RUN/meta/test_qids.txt" \
  --output-dir "$RUN/draft_raw/stage3_draft_sql_gemini3flash_nothink_t10" \
  --output-suffix "" \
  --model "$GEMINI_FLASH_MODEL" \
  --proxy "$GEMINI_PROXY" \
  --temperature 1.0 \
  "${GEMINI_DRAFT_THINKING_ARGS[@]}" \
  --max-concurrent "$GEMINI_MAX_CONCURRENT" \
  --usage-jsonl "$RUN/usage/stage3_draft_sql_gemini3flash_nothink_t10.jsonl"

python "$SCRIPT_DIR/run_prompt_dir_qwen_api_batch.py" \
  --trace-dir "$ROOT" \
  --prompt-dir "$RUN/prompts/stage3_draft_sql" \
  --prompt-ids-file "$RUN/meta/test_qids.txt" \
  --output-dir "$RUN/draft_raw/stage3_draft_sql_qwen36plus_nothink_t10" \
  --output-suffix "" \
  --model "$QWEN_MODEL" \
  --temperature 1.0 \
  ${QWEN_DRAFT_THINKING_ARGS[@]+"${QWEN_DRAFT_THINKING_ARGS[@]}"} \
  --max-concurrent "$QWEN_MAX_CONCURRENT" \
  --usage-jsonl "$RUN/usage/stage3_draft_sql_qwen36plus_nothink_t10.jsonl"

python "$SCRIPT_DIR/collect_stage_outputs.py" \
  --raw-dir "$RUN/draft_raw/stage3_draft_sql_gemini3flash_nothink_t10" \
  --stage stage3_draft_sql \
  --output "$RUN/stages/stage3_draft_sql_gemini3flash.jsonl"
python "$SCRIPT_DIR/collect_stage_outputs.py" \
  --raw-dir "$RUN/draft_raw/stage3_draft_sql_qwen36plus_nothink_t10" \
  --stage stage3_draft_sql \
  --output "$RUN/stages/stage3_draft_sql_qwen36plus.jsonl"

echo "[7/13] Build cross-model draft hints with SQL only, no row previews"
python "$SCRIPT_DIR/build_cross_model_stage3_hints.py" \
  --manifest "$MANIFEST" \
  --dataset-root "$DATASET" \
  --stage3-raw-dir "$RUN/draft_raw/stage3_draft_sql_gemini3flash_nothink_t10" \
  --source-label gemini3flash_draft_t10 \
  --output-jsonl "$RUN/draft_hints/gemini3flash_draft_hints.jsonl" \
  --artifacts-dir "$RUN/draft_hints/artifacts/gemini3flash"

python "$SCRIPT_DIR/build_cross_model_stage3_hints.py" \
  --manifest "$MANIFEST" \
  --dataset-root "$DATASET" \
  --stage3-raw-dir "$RUN/draft_raw/stage3_draft_sql_qwen36plus_nothink_t10" \
  --source-label qwen36plus_draft_t10 \
  --output-jsonl "$RUN/draft_hints/qwen36plus_draft_hints.jsonl" \
  --artifacts-dir "$RUN/draft_hints/artifacts/qwen36plus"

python "$SCRIPT_DIR/merge_field_candidates.py" \
  --manifest "$MANIFEST" \
  --dataset-root "$DATASET" \
  --stage-jsonl "$RUN/stages/stage3_tables_columns.jsonl" \
  --stage-jsonl "$RUN/stages/stage3_draft_sql_gemini3flash.jsonl" \
  --stage-jsonl "$RUN/stages/stage3_draft_sql_qwen36plus.jsonl" \
  --stage-jsonl "$RUN/stages/stage4_field_match.jsonl" \
  --filter-stage4 \
  --inject-table-pks \
  --inject-fk-neighbor-keys \
  --inject-california-companions \
  --inject-implicit-id-bridges \
  --output "$RUN/stages/final_fields.jsonl"

echo "[8/13] Build final prompts; Gemini sees Qwen draft, Qwen sees Gemini draft"
python "$SCRIPT_DIR/build_final_prompt_dirs_with_draft_hints.py" \
  --manifest "$MANIFEST" \
  --dataset-root "$DATASET" \
  --base-template-root "$ROOT/templates" \
  --final-fields-jsonl "$RUN/stages/final_fields.jsonl" \
  --field-rewrites-jsonl "$RUN/stages/stage2_field_rewrite.jsonl" \
  --stage-jsonl "$RUN/stages/stage3_tables_columns.jsonl" \
  --stage-jsonl "$RUN/stages/stage4_field_match.jsonl" \
  --draft-hint-jsonl "$RUN/draft_hints/qwen36plus_draft_hints.jsonl" \
  --prompt-scope modchall \
  --target-final-model gemini \
  --no-value-examples \
  --output-root "$RUN/final_prompts"

python "$SCRIPT_DIR/build_final_prompt_dirs_with_draft_hints.py" \
  --manifest "$MANIFEST" \
  --dataset-root "$DATASET" \
  --base-template-root "$ROOT/templates" \
  --final-fields-jsonl "$RUN/stages/final_fields.jsonl" \
  --field-rewrites-jsonl "$RUN/stages/stage2_field_rewrite.jsonl" \
  --stage-jsonl "$RUN/stages/stage3_tables_columns.jsonl" \
  --stage-jsonl "$RUN/stages/stage4_field_match.jsonl" \
  --draft-hint-jsonl "$RUN/draft_hints/gemini3flash_draft_hints.jsonl" \
  --prompt-scope modchall \
  --target-final-model qwen \
  --no-value-examples \
  --output-root "$RUN/final_prompts"

python "$SCRIPT_DIR/audit_prompts_no_db_values.py" --prompt-root "$RUN/final_prompts"

_wait_final_pids() {
  local status=0
  local pid
  for pid in "$@"; do
    wait "$pid" || status=1
  done
  return "$status"
}

_run_final_candidate() {
  local provider="$1"
  local prompt_name="$2"
  local temperature="$3"
  local temp_label="$4"
  local per_dir_concurrent="$5"
  local base_name
  local out_name

  if [[ "$provider" == "gemini" ]]; then
    base_name="${prompt_name#modchall_for_gemini_}"
    if [[ "$base_name" != "v12_hint" ]]; then
      base_name="${base_name%_hint}"
    fi
    out_name="${base_name}_gemini_${temp_label}"
    python "$SCRIPT_DIR/run_prompt_dir_gemini_api_batch.py" \
      --trace-dir "$ROOT" \
      --prompt-dir "$RUN/final_prompts/$prompt_name" \
      --output-dir "$RUN/final_raw/modchall_$out_name" \
      --output-suffix "" \
      --model "$GEMINI_FLASH_MODEL" \
      --proxy "$GEMINI_PROXY" \
      --temperature "$temperature" \
      "${GEMINI_FINAL_THINKING_ARGS[@]}" \
      --max-concurrent "$per_dir_concurrent" \
      --usage-jsonl "$RUN/usage/final_modchall_$out_name.jsonl"
  else
    base_name="${prompt_name#modchall_for_qwen_}"
    if [[ "$base_name" != "v12_hint" ]]; then
      base_name="${base_name%_hint}"
    fi
    out_name="${base_name}_qwen_${temp_label}"
    if [[ "$temp_label" == "t18" ]]; then
      # Qwen @1.8 is retained, but thinking stays disabled because it produced unstable output.
      python "$SCRIPT_DIR/run_prompt_dir_qwen_api_batch.py" \
        --trace-dir "$ROOT" \
        --prompt-dir "$RUN/final_prompts/$prompt_name" \
        --output-dir "$RUN/final_raw/modchall_$out_name" \
        --output-suffix "" \
        --model "$QWEN_MODEL" \
        --temperature "$temperature" \
        --max-concurrent "$per_dir_concurrent" \
        --usage-jsonl "$RUN/usage/final_modchall_$out_name.jsonl"
    else
      python "$SCRIPT_DIR/run_prompt_dir_qwen_api_batch.py" \
        --trace-dir "$ROOT" \
        --prompt-dir "$RUN/final_prompts/$prompt_name" \
        --output-dir "$RUN/final_raw/modchall_$out_name" \
        --output-suffix "" \
        --model "$QWEN_MODEL" \
        --temperature "$temperature" \
        ${QWEN_FINAL_THINKING_ARGS[@]+"${QWEN_FINAL_THINKING_ARGS[@]}"} \
        --max-concurrent "$per_dir_concurrent" \
        --usage-jsonl "$RUN/usage/final_modchall_$out_name.jsonl"
    fi
  fi
}

_run_final_provider_queue() {
  local provider="$1"
  local temperature="$2"
  local temp_label="$3"
  local dir_parallel="$4"
  local per_dir_concurrent="$5"
  shift 5

  local pids=()
  local status=0
  local prompt_name
  echo "  provider=$provider temp=$temperature dirs_parallel=$dir_parallel per_dir_concurrent=$per_dir_concurrent"
  for prompt_name in "$@"; do
    _run_final_candidate "$provider" "$prompt_name" "$temperature" "$temp_label" "$per_dir_concurrent" &
    pids+=("$!")
    if (( ${#pids[@]} >= dir_parallel )); then
      _wait_final_pids ${pids[@]+"${pids[@]}"} || status=1
      pids=()
    fi
  done
  if (( ${#pids[@]} > 0 )); then
    _wait_final_pids ${pids[@]+"${pids[@]}"} || status=1
  fi
  return "$status"
}

_run_final_batch() {
  local temp_label="$1"
  local temperature="$2"
  local status=0
  local gemini_pid
  local qwen_pid

  _run_final_provider_queue \
    gemini "$temperature" "$temp_label" "$FINAL_GEMINI_DIR_PARALLEL" "$FINAL_GEMINI_PER_DIR_CONCURRENT" \
    modchall_for_gemini_v12_hint \
    modchall_for_gemini_v13_dc_sqlonly_hint \
    modchall_for_gemini_v13_skeleton_sqlonly_hint &
  gemini_pid="$!"

  _run_final_provider_queue \
    qwen "$temperature" "$temp_label" "$FINAL_QWEN_DIR_PARALLEL" "$FINAL_QWEN_PER_DIR_CONCURRENT" \
    modchall_for_qwen_v12_hint \
    modchall_for_qwen_v13_dc_sqlonly_hint \
    modchall_for_qwen_v13_skeleton_sqlonly_hint &
  qwen_pid="$!"

  wait "$gemini_pid" || status=1
  wait "$qwen_pid" || status=1
  if [[ "$status" != "0" ]]; then
    echo "Final candidate batch $temp_label failed" >&2
    exit "$status"
  fi
}

echo "[9/13] Generate 6 final candidates at T=0.6 with provider-level queues"
_run_final_batch t06 0.6

echo "[10/13] Generate 6 exploratory final candidates at T=1.8 with provider-level queues"
_run_final_batch t18 1.8

echo "[11/13] Repair non-executable candidates locally without gold SQL"
for candidate in \
  modchall_v12_hint_gemini_t06 \
  modchall_v13_dc_sqlonly_gemini_t06 \
  modchall_v13_skeleton_sqlonly_gemini_t06 \
  modchall_v12_hint_qwen_t06 \
  modchall_v13_dc_sqlonly_qwen_t06 \
  modchall_v13_skeleton_sqlonly_qwen_t06 \
  modchall_v12_hint_gemini_t18 \
  modchall_v13_dc_sqlonly_gemini_t18 \
  modchall_v13_skeleton_sqlonly_gemini_t18 \
  modchall_v12_hint_qwen_t18 \
  modchall_v13_dc_sqlonly_qwen_t18 \
  modchall_v13_skeleton_sqlonly_qwen_t18
do
  python "$SCRIPT_DIR/repair_nonexec_local.py" \
    --manifest "$MANIFEST" \
    --dataset-root "$DATASET" \
    --source-dir "$RUN/final_raw/$candidate" \
    --output-dir "$RUN/repair/$candidate" \
    --model "$GEMINI_REPAIR_MODEL" \
    --temperature 0.2 \
    "${GEMINI_REPAIR_THINKING_ARGS[@]}" \
    --sampling-budget 1 \
    --proxy "$GEMINI_PROXY" \
    --usage-jsonl "$RUN/usage/repair_${candidate}_gemini31pro_t02.jsonl"
done

echo "[12/13] Build pairwise schema blocks and select best candidate per question"
python "$SCRIPT_DIR/build_pairwise_schema_blocks.py" \
  --manifest "$MANIFEST" \
  --dataset-root "$DATASET" \
  --final-fields-jsonl "$RUN/stages/final_fields.jsonl" \
  --field-rewrites-jsonl "$RUN/stages/stage2_field_rewrite.jsonl" \
  --output-dir "$RUN/pairwise_schema"

python "$SCRIPT_DIR/select_candidates_from_dirs.py" \
  --manifest "$MANIFEST" \
  --dataset-root "$DATASET" \
  --selector-mode hybrid \
  --pairwise-model "$GEMINI_REPAIR_MODEL" \
  --pairwise-proxy "$GEMINI_PROXY" \
  --pairwise-threshold 0.55 \
  --pairwise-schema-dir "$RUN/pairwise_schema" \
  --candidate-dir "v12_hint_gemini_t06=$RUN/repair/modchall_v12_hint_gemini_t06/merged_results" \
  --candidate-dir "v13_dc_gemini_t06=$RUN/repair/modchall_v13_dc_sqlonly_gemini_t06/merged_results" \
  --candidate-dir "v13_skeleton_gemini_t06=$RUN/repair/modchall_v13_skeleton_sqlonly_gemini_t06/merged_results" \
  --candidate-dir "v12_hint_qwen_t06=$RUN/repair/modchall_v12_hint_qwen_t06/merged_results" \
  --candidate-dir "v13_dc_qwen_t06=$RUN/repair/modchall_v13_dc_sqlonly_qwen_t06/merged_results" \
  --candidate-dir "v13_skeleton_qwen_t06=$RUN/repair/modchall_v13_skeleton_sqlonly_qwen_t06/merged_results" \
  --candidate-dir "v12_hint_gemini_t18=$RUN/repair/modchall_v12_hint_gemini_t18/merged_results" \
  --candidate-dir "v13_dc_gemini_t18=$RUN/repair/modchall_v13_dc_sqlonly_gemini_t18/merged_results" \
  --candidate-dir "v13_skeleton_gemini_t18=$RUN/repair/modchall_v13_skeleton_sqlonly_gemini_t18/merged_results" \
  --candidate-dir "v12_hint_qwen_t18=$RUN/repair/modchall_v12_hint_qwen_t18/merged_results" \
  --candidate-dir "v13_dc_qwen_t18=$RUN/repair/modchall_v13_dc_sqlonly_qwen_t18/merged_results" \
  --candidate-dir "v13_skeleton_qwen_t18=$RUN/repair/modchall_v13_skeleton_sqlonly_qwen_t18/merged_results" \
  --output-dir "$RUN/selected/final_results" \
  --metadata-dir "$RUN/candidates/per_qid_json" \
  --usage-jsonl "$RUN/usage/selector_pairwise.jsonl" \
  --max-workers "$SELECTOR_MAX_WORKERS"

echo "[13/13] Write predict_test.json and usage summary"
python "$SCRIPT_DIR/convert_results_to_predict_json.py" \
  --manifest "$MANIFEST" \
  --result-dir "$RUN/selected/final_results" \
  --output "$RUN/predict_test.json" \
  --key-mode source_index

python "$SCRIPT_DIR/aggregate_usage.py" \
  --run-root "$RUN" \
  --output-json "$RUN/reports/usage_summary.json" \
  --output-md "$RUN/reports/usage_summary.md"

echo "Finished."
echo "Prediction JSON: $RUN/predict_test.json"
echo "Usage summary:   $RUN/reports/usage_summary.md"
