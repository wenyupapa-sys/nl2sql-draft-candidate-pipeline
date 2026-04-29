# Draft SQL Candidate Pipeline Scripts

This folder contains the runnable scripts for the 2026-04-27 draft-SQL candidate pipeline.

API keys are read from environment variables and are not stored in scripts:

```bash
export GEMINI_API_KEY="..."
export QWEN_API_KEY="..."
export GEMINI_PROXY="none"   # direct connection; set http://host:port only if required
```

Smoke test:

```bash
QID=1 ./run_smoke_one_qid.sh
```

Official BIRD test run:

```bash
TEST_JSON=/path/to/test.json \
TEST_TABLES=/path/to/test_tables.json \
TEST_DATABASES=/path/to/test_databases \
COLUMN_MEANING=/path/to/column_meaning.json \
OUTPUT_DIR=/path/to/output_run \
./run_test.sh
```

See `README_BIRD_SUBMISSION.md` for the evaluator-facing instructions.

Build a concise submission zip:

```bash
./make_bird_submission_zip.sh
```

Main scripts:

- `run_prompt_dir_qwen_api_batch.py`: Qwen3.6-plus prompt directory runner.
- `run_prompt_dir_gemini_api_batch.py`: Gemini prompt directory runner.
- `run_stage4_field_match_gemini_embedding.py`: routed embedding field matcher; Gemini embedding first, Qwen `text-embedding-v4` fallback.
- `build_test_manifest.py`: builds a gold-free manifest from official `test.json`.
- `stage_test_dataset.py`: stages official test files under a dataset root expected by schema helpers.
- `merge_field_candidates.py`: merges stage JSONL outputs into `final_fields.jsonl`; this replaces the older planned `merge_stage_jsonl.py` name.
- `build_cross_model_stage3_hints.py`: converts draft SQL outputs into executable draft hint JSONL.
- `build_final_prompt_dirs_with_draft_hints.py`: builds scoped final prompts with `final_fields.jsonl` and cross-model draft hints.
- `build_pairwise_schema_blocks.py`: renders selector-only schema blocks from `final_fields.jsonl`; value examples are off by default.
- `repair_nonexec_local.py`: repairs non-executable SQL using local SQLite execution only; it does not require gold SQL.
- `fill_missing_results_from_backup.py`: fills missing final outputs from a backup SQL directory so smoke runs can complete when a provider times out.
- `select_candidates_from_dirs.py`: executes candidates and selects by result-hash majority or hybrid vote + pairwise comparison.
- `convert_results_to_predict_json.py`: converts selected `{qid}.txt` files to `predict_test.json`.
- `aggregate_usage.py`: aggregates model usage JSONL files into token reports.
- `make_bird_submission_zip.sh`: creates a compact evaluator zip that excludes dev data and gold-SQL evaluation helpers.
- `eval_result_dir.py`: evaluates selected final SQL against the current manifest.

Important behavior:

- Stage1, Stage2, and Stage3 tables/columns use `qwen3.6-plus` with thinking disabled.
- Simple draft SQL uses `qwen3.6-plus` with thinking disabled at temperature `0.3`.
- Moderate/challenging draft SQL uses both Gemini 3 Flash and Qwen3.6-plus at temperature `1.0`, with draft thinking disabled. For Gemini 3 Flash this is implemented as `thinkingLevel=minimal`, because the current Gemini 3 API does not expose a true off switch.
- Stage4 semantic matching uses Gemini `gemini-embedding-001` first. If Gemini embedding times out or fails, it automatically switches the whole Stage4 run to Qwen `text-embedding-v4`, keeping candidate/query vectors from the same provider.
- Gemini final SQL and repair use low-budget thinking by default: `thinkingBudget=128`. Draft SQL sets `GEMINI_DRAFT_THINKING_BUDGET=none` and falls back to `GEMINI_DRAFT_THINKING_LEVEL=none`.
- Qwen thinking is used only for final SQL candidates by default. It is controlled by `QWEN_FINAL_ENABLE_THINKING=1` and `QWEN_FINAL_THINKING_BUDGET=128`; Stage1/2/3 table extraction, draft SQL, embedding, and repair do not use Qwen thinking.
- Moderate/challenging final prompts inject cross-model draft SQL only:
  - Gemini final prompts receive Qwen3.6-plus draft SQL.
  - Qwen final prompts receive Gemini 3 Flash draft SQL.
- Table/column candidates are used through `final_fields.jsonl`, not as free-text draft hints.
- Test-mode moderate/challenging routing now generates 12 final candidates: 3 templates, 2 models, and temperatures `0.6` plus `1.8`.
- Qwen T=1.8 final candidates intentionally disable thinking; Qwen T=0.6 keeps `thinking_budget=128`.
- The hybrid selector first trusts stable execution clusters; low-confidence cases use Gemini 3.1 Pro pairwise comparison over cluster representatives with `thinkingLevel=low`.
