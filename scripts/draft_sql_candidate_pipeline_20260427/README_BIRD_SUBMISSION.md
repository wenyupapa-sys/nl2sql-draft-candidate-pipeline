# BIRD Submission README

## Submission Type

This submission is **Category 4: Combined Models**. It uses closed-source API models only:

- Gemini 3 Flash for draft/final SQL generation.
- Qwen3.6-plus for rewrite, field recall, draft SQL, and final SQL candidates.
- Gemini embedding (`gemini-embedding-001`) for semantic field matching, with Qwen `text-embedding-v4` as automatic fallback.
- Gemini 3.1 Pro for non-executable SQL repair only.

No GPU is required.

## Inputs

The runner expects the official BIRD test files:

- `test.json`
- `test_tables.json`
- `test_databases/`
- `column_meaning.json` optional but recommended

`test.json` may contain an empty `SQL` field. The pipeline does not read or require gold SQL on test.

## Environment

Install Python dependencies from the repository root:

```bash
pip install -r requirements.txt
```

Set API keys through environment variables:

```bash
export GEMINI_API_KEY="..."
export DASHSCOPE_API_KEY="..."
```

`QWEN_API_KEY` can be used instead of `DASHSCOPE_API_KEY`.

Optional variables:

```bash
export GEMINI_PROXY="none"          # or http://host:port
export OUTPUT_DIR="./bird_test_run"
export MAX_CONCURRENT=4
export FINAL_GEMINI_DIR_PARALLEL=3
export FINAL_QWEN_DIR_PARALLEL=2
export SELECTOR_MAX_WORKERS=4
export STAGE4_EMBEDDING_MODE=fallback   # fallback or cache_only
export QWEN_EMBEDDING_MODEL=text-embedding-v4
export QWEN_EMBED_BATCH_SIZE=10
export QWEN_DRAFT_ENABLE_THINKING=0
export QWEN_DRAFT_THINKING_BUDGET=none
export QWEN_FINAL_ENABLE_THINKING=1
export QWEN_FINAL_THINKING_BUDGET=128
export GEMINI_DRAFT_THINKING_BUDGET=none
export GEMINI_FINAL_THINKING_BUDGET=128
export GEMINI_REPAIR_THINKING_BUDGET=128
```

By default the runner connects to Gemini directly. It only uses a proxy when `GEMINI_PROXY`, `HTTPS_PROXY`, `HTTP_PROXY`, or a macOS system HTTPS proxy is explicitly configured.

## Run Command

From this script directory:

```bash
TEST_JSON=/path/to/test.json \
TEST_TABLES=/path/to/test_tables.json \
TEST_DATABASES=/path/to/test_databases \
COLUMN_MEANING=/path/to/column_meaning.json \
OUTPUT_DIR=/path/to/output_run \
./run_test.sh
```

The final prediction file is:

```text
$OUTPUT_DIR/predict_test.json
```

The prediction JSON is keyed by the original `test.json` row index:

```json
{
  "0": "SELECT ...",
  "1": "SELECT ..."
}
```

## Development Predictions

If included, `predict_dev.json` at the package root contains the development-set SQL predictions in the same key format as `predict_test.json`. It is provided only for reproducibility and follow-up; the test runner does not read it.

## Data Safety

For official test, prompts are rendered with:

- no sampled database values
- no SQL result row previews
- `column_meaning.json` as the preferred column description source

The runner audits generated prompt files before API calls. It fails if known value-leakage markers such as `sample_values=`, `samples=[...]`, `first_rows:`, or value-example instructions are found.

`column_meaning.json` is read by schema rendering first. If a column meaning is missing, the fallback order is:

1. LLM field rewrite from Stage2
2. readable column name from `test_tables.json`
3. original column name

## Resume And Logging

API runners skip existing non-empty output files, so a failed run can be restarted with the same `OUTPUT_DIR`.

Important outputs:

- `$OUTPUT_DIR/prompts/`: generated prompts
- `$OUTPUT_DIR/draft_raw/`: Stage1/2/3 raw model outputs
- `$OUTPUT_DIR/stages/`: parsed intermediate JSONL files
- `$OUTPUT_DIR/final_prompts/`: final candidate prompts
- `$OUTPUT_DIR/final_raw/`: final candidate SQL outputs
- `$OUTPUT_DIR/repair/`: gold-free non-executable repair outputs
- `$OUTPUT_DIR/pairwise_schema/`: per-question schema blocks used only by the pairwise selector; no value examples are included by default
- `$OUTPUT_DIR/selected/final_results/`: selected per-question SQL files
- `$OUTPUT_DIR/predict_test.json`: final BIRD prediction file
- `$OUTPUT_DIR/reports/usage_summary.md`: token usage summary

Stage4 embedding is fail-open. In `fallback` mode, a failed embedding batch falls back to lexical-only matching for that batch and still writes `stage4_field_match.jsonl`. In `cache_only` mode, only existing embedding cache is used and missing vectors are skipped, which is useful for smoke tests or unstable networks.

## Token Usage

Usage JSONL files are written under `$OUTPUT_DIR/usage/`. A combined summary is generated at:

```text
$OUTPUT_DIR/reports/usage_summary.json
$OUTPUT_DIR/reports/usage_summary.md
```

This summary reports prompt, completion, thinking, total, and estimated embedding input tokens by model.

## Packaging

Build a concise evaluator zip from the repository root with:

```bash
scripts/draft_sql_candidate_pipeline_20260427/make_bird_submission_zip.sh
```

The bundle intentionally excludes development data, output directories, gold-SQL evaluation scripts, and historical experiment files.

## Route Used On Test

The official test set does not provide difficulty labels, so all test questions are forced through the moderate/challenging route:

1. Qwen3.6-plus Stage1 rewrite, Stage2 field rewrite, Stage3 tables/columns, thinking disabled, temperature `0.3`.
2. Routed semantic field matching:
   - primary: Gemini `gemini-embedding-001`
   - fallback: Qwen `text-embedding-v4`
   - query embeddings are batched per database, so 100 questions over 2 databases need about 2 query embedding calls instead of 100 single-question calls
   - if both providers fail and `STAGE4_EMBEDDING_MODE=fallback`, Stage4 degrades to lexical-only matching rather than stopping the whole run
3. Stage3 draft SQL:
   - Gemini 3 Flash, thinking suppressed by default via `thinkingLevel=minimal`, temperature `1.0`
   - Qwen3.6-plus, thinking disabled by default, temperature `1.0`
4. Final prompt construction with cross-model draft hints:
   - Gemini final prompts receive Qwen draft SQL hints.
   - Qwen final prompts receive Gemini draft SQL hints.
5. Twelve final candidates:
   - `v12_hint`, `v13_dc_sqlonly`, `v13_skeleton_sqlonly`
   - generated by Gemini 3 Flash and Qwen3.6-plus at temperatures `0.6` and `1.8`
   - final candidate directories run through provider-level queues: Gemini and Qwen queues can run concurrently, while each provider caps directory-level and per-directory concurrency separately
   - T=0.6 final generation uses low-budget thinking (`thinkingBudget=128`) by default
   - Gemini T=1.8 also uses `thinkingBudget=128`; Qwen T=1.8 disables thinking for output stability
6. Gemini 3.1 Pro repairs non-executable SQL only, `thinkingBudget=128` by default.
7. Hybrid selector:
   - execute all repaired candidates and cluster by result hash
   - qids are independent and can be processed in parallel with `SELECTOR_MAX_WORKERS`
   - if the top cluster consistency is at least `0.55`, select the fastest candidate in that cluster
   - otherwise compare cluster representatives with Gemini 3.1 Pro pairwise judging, using only the question, evidence, scoped schema, and two SQLs
   - pairwise judging uses `thinkingLevel=low` and `maxOutputTokens=128`, because Gemini 3.1 Pro does not support `minimal` and low thinking consumes output budget
   - if the pairwise API is unavailable or ambiguous, keep the majority/fastest champion
