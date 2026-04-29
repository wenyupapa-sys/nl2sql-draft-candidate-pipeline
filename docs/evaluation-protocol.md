# BIRD Evaluation Protocol

This document defines the measurement semantics for `bird_agentsm`.

It is adapted from the main `agentsm` project so that BIRD runs use the same
high-level reporting discipline while accounting for SQLite- and BIRD-specific
execution.

## Scope

This protocol applies to BIRD Query-only runs under `bird_agentsm/`.

For the current expanded metrics panel, including structural metrics such as
`JoinTypedF1`, `AggExact`, `GrainAcc`, and `OrderAcc`, see the metrics notes in
the main project documentation.

Primary outputs:
- `accuracy`
- `execution_errors`
- `assertion_errors`
- `table_recall`
- `table_precision`
- `table_f1`
- `explicit_column_recall`
- `join_connectivity`
- `per_question_input_tokens`
- `per_question_output_tokens`
- `total_input_tokens`
- `total_output_tokens`

## Experiment Governance

Strategy-changing benchmark runs are governed by a **pending experiment log**
mechanism.

Before running `scripts/run_sonnet_benchmark.py`, the operator must record a
pending experiment log with:

- `title`
- `purpose`
- `hypothesis`
- at least one `change`
- `result_summary` placeholder

The logger also records a **strategy fingerprint** computed from the current
strategy-related files, including retrieval code, prompt templates, runtime
configuration, and generation orchestration.

Runner behavior:

1. compute current strategy fingerprint
2. compare it with the latest logged fingerprint
3. if the fingerprint changed and no matching pending experiment log exists,
   fail closed before the benchmark starts
4. after a successful run, bind the pending experiment log to the trace and
   append it to the global experiment log

This prevents silent strategy changes from entering the benchmark history
without an explicit purpose / hypothesis / change record.

For autonomous iteration without git commits, keep an explicit iteration log and
bind each benchmark run to the configuration that produced it.

## Accuracy Semantics

Accuracy is execution accuracy (`EX`):

```text
accuracy = success / total
```

Per item:
- `success`: predicted SQL executes on the correct SQLite database and its
  result set matches the gold SQL result set
- `exec_error`: predicted SQL fails to execute, or is evaluated against the
  wrong database
- `assertion_error`: predicted SQL executes successfully but returns a result
  set different from the gold SQL

The canonical report should always include:
- `Overall Accuracy`
- `Execution Errors`
- `Assertion Errors`

## Retrieval Metrics

Retrieval metrics are computed against gold SQL from BIRD `dev.json` / gold SQL
files, not against prompt hints.

### `table_recall`
- gold tables are parsed from the GT SQL
- pseudo-tables / function-like tokens must be filtered

### `table_precision`
- precision of linked tables against gold tables

### `table_f1`
- harmonic mean of table recall and table precision

### `explicit_column_recall`
- computed only from explicit `table.column` references in GT SQL
- narrower than full semantic necessity
- this is the official column metric for current BIRD runs

### `join_connectivity`
- computed from GT join predicates
- measures recovered GT join coverage, not generic graph connectedness

## Token Semantics

Unless a backend returns real provider usage, token counts are reported as
estimated tokens:

```text
estimated_tokens = round(len(text) / 3.5)
```

This applies to:
- prompt input tokens
- model output tokens

For BIRD runs we report:
- `per_question_input_tokens`
- `per_question_output_tokens`
- `total_input_tokens`
- `total_output_tokens`

If pricing is unavailable for a model, report token totals but do not claim a
real billed cost.

## Measurement Modes

### `actual`
- derived from a real trace on disk
- uses actual retrieval outputs, prompts, generated SQL, and evaluation results

### `simulation`
- replay or policy analysis over existing artifacts
- must not be mixed with actual EX claims

All benchmark summaries must explicitly state whether they are `actual` or
`simulation`.

## BIRD Subset Evaluation

When evaluating a filtered subset (for example 100 questions), the run must:
1. keep internal `predictions.jsonl`
2. export subset-specific `predict_subset.json`
3. export matching `dev_subset.json`
4. export matching `dev_subset_gold.sql`
5. run evaluation against those subset assets only

Do not report full-dev accuracy from a subset run.

## Reporting Rules

Every BIRD benchmark summary should state:
1. total number of evaluated questions
2. whether the run is full-dev or subset
3. whether token numbers are estimated or provider-reported
4. whether retrieval metrics use explicit GT SQL column recall
5. whether pseudo-table/function tokens were filtered from table evaluation

## Recommended Output Bundle

For each run trace, produce:
- `eval_output/report.txt`
- `eval_output/output_with_status.jsonl`
- `eval_output/retrieval_metrics.md`
- `eval_output/retrieval_budget.json`
- `eval_output/retrieval_budget.md`
- `token_estimation.json`
- `token_estimation.md`
- a final benchmark summary file tying the metrics together
