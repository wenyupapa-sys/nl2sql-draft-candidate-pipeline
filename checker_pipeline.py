"""
Checker Pipeline aligned to DeepEye-SQL source.

Reference source:
https://github.com/HKUSTDial/DeepEye-SQL

This file keeps the local Gemini API adapter and tracing helpers, but the
checker order, checker responsibilities, and checker prompt split are aligned
to DeepEye-SQL's implementation:

1. SyntaxChecker          (execution-based, LLM revise)
2. JoinChecker            (regex-based, LLM revise)
3. OrderByLimitChecker    (regex-based, LLM revise)
4. TimeChecker            (regex-based, auto-fix only)
5. SelectChecker          (regex-based, LLM revise + string concat pre-clean)
6. MaxMinChecker          (regex-based, LLM revise)
7. OrderByNullChecker     (regex-based, LLM revise)
8. ResultChecker          (execution-based, LLM revise)
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from run_prompt_dir_gemini_api_batch import (  # type: ignore
    _call_gemini_sync,
    _resolve_api_keys,
    _resolve_proxy,
)


EXECUTION_CHECKER_PROMPT = """
# Task:
You are an SQL database expert tasked with correcting a SQL query. A previous attempt to run a query did not yield the correct results, either due to errors in execution or because the result returned was empty or unexpected. Your role is to analyze the error based on the provided database schema and the details of the failed execution, and then provide a corrected version of the SQL query.

# Instructions:
1. Review Database Schema:
    - Examine the database schema to understand the database structure.
2. Analyze Query Requirements:
    - Original Question: Consider what information the query is supposed to retrieve.
    - Hint: Use the provided hints to understand the relationships and conditions relevant to the query.
    - Executed SQL Query: Review the SQL query that was previously executed and led to an error or incorrect result.
    - Execution Result: Analyze the outcome of the executed query to identify why it failed (e.g., syntax errors, incorrect column references, logical mistakes).
3. Correct the Query:
    - Modify the SQL query to address the identified issues, ensuring it correctly fetches the requested data according to the database schema and query requirements.
    - Use the retrieved values to help write more accurate conditions when appropriate.

[IMPORTANT]
For key phrases mentioned in the question, we have provided the most similar values within the columns (TEXT-TYPE columns) denoted by "Value Examples". **This is a critical hint to identify the tables/columns that will be used in the SQL query.**

# Output Format:
Only output a single XML block in the following format:
<result>
    The final revised SQL query that answers the question and can be executed by SQLite directly, ensure there is not any SQLite comment and not any other explanation text in the SQL query.
    The SQL query must not include XML-specific characters (e.g., `&lt;`, `&gt;`, `&amp;`); only SQL-valid characters are allowed.
</result>

# Input:
## Database Schema:
{DATABASE_SCHEMA}

## Question:
{QUESTION}

## Hint:
{HINT}

## Previous SQL:
{QUERY}

## Execution Result:
{RESULT}

Based on the question, table schemas, the previous query, and the execution result, try to fix the query, and only output the XML code (<result>...</result>) as your response. Do not output <reasoning>, markdown, or any extra text.

# Output:
"""


COMMON_CHECKER_PROMPT = """
# Task:
You are an SQL database expert tasked with correcting a SQL query. An external SQL checker tool has checked the SQL query and provided some suggestions to correct. Your role is to analyze the suggestions from the checker tool, and then based on the provided database schema provide a corrected version of the SQL query.

# Instructions:
1. Review Database Schema:
    - Examine the database schema to understand the database structure.
2. Analyze Query Requirements:
    - Original Question: Consider what information the query is supposed to retrieve.
    - Hint: Use the provided hints to understand the relationships and conditions relevant to the query.
    - SQL Query: Review the SQL query that was previously checked.
    - Modification Suggestions: Review the suggestions provided by the external checker, and think how to modify the SQL to meet the suggestions.
3. Correct the Query:
    - Modify the SQL query based the given Modification Suggestions, ensuring it correctly meet the expected suggestions.

[IMPORTANT]
Your are NOT ALLOWED to do any other modifications which are not listed in given suggestions.

# Output Format:
Only output a single XML block in the following format:
<result>
    The final revised SQL query that answers the question and can be executed by SQLite directly, ensure there is not any SQLite comment and not any other explanation text in the SQL query.
    The SQL query must not include XML-specific characters (e.g., `&lt;`, `&gt;`, `&amp;`); only SQL-valid characters are allowed.
</result>

# Input:
## Database Schema:
{DATABASE_SCHEMA}

## Question:
{QUESTION}

## Hint:
{HINT}

## Previous SQL:
{QUERY}

## Modification Suggestions:
{SUGGESTIONS}

Based on the question, database schemas, previous SQL query and modification suggestions, try to fix the query, and only output the XML code (<result>...</result>) as your response. Do not output <reasoning>, markdown, or any extra text.

# Output:
"""


def _format_execution_checker_prompt(
    database_schema: str,
    question: str,
    hint: str,
    sql: str,
    execution_result: str,
) -> str:
    return EXECUTION_CHECKER_PROMPT.format(
        DATABASE_SCHEMA=database_schema,
        QUESTION=question,
        HINT=hint or "",
        QUERY=sql,
        RESULT=execution_result,
    )


def _format_common_checker_prompt(
    database_schema: str,
    question: str,
    hint: str,
    sql: str,
    suggestions: str,
) -> str:
    return COMMON_CHECKER_PROMPT.format(
        DATABASE_SCHEMA=database_schema,
        QUESTION=question,
        HINT=hint or "",
        QUERY=sql,
        SUGGESTIONS=suggestions,
    )


def _rows_all_null(rows: list[tuple[Any, ...]]) -> bool:
    return bool(rows) and all(all(cell is None for cell in row) for row in rows)


def _hash_result(rows: list[tuple[Any, ...]]) -> str:
    payload = repr(rows).encode("utf-8", errors="ignore")
    return hashlib.sha256(payload).hexdigest()


def _strip_sql_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```sql") and stripped.endswith("```"):
        return stripped[len("```sql") : -len("```")].strip()
    return stripped


def _format_rows_preview(
    rows: list[tuple[Any, ...]],
    columns: list[str],
    *,
    limit: int = 5,
) -> str:
    preview_rows = rows[:limit]
    lines = []
    if columns:
        lines.append("columns: " + " | ".join(columns))
    for row in preview_rows:
        lines.append(" | ".join("NULL" if cell is None else str(cell) for cell in row))
    if len(rows) > limit:
        lines.append(f"... ({len(rows)} rows total)")
    return "\n".join(lines).strip()


def _execute_sql(db_path: str, sql: str, *, timeout_s: float = 30.0) -> dict[str, Any]:
    conn: sqlite3.Connection | None = None
    timed_out = {"value": False}
    start_time = time.monotonic()
    try:
        conn = sqlite3.connect(db_path)
        if timeout_s > 0:
            def _progress_handler() -> int:
                if time.monotonic() - start_time > timeout_s:
                    timed_out["value"] = True
                    return 1
                return 0

            # Abort long-running SQLite scans inside the checker instead of hanging forever.
            conn.set_progress_handler(_progress_handler, 10_000)
        cursor = conn.cursor()
        cursor.execute(sql)
        rows = cursor.fetchall()
        columns = [desc[0] for desc in (cursor.description or [])]
        if not rows:
            result_type = "empty_result"
            result_table_str = "empty result"
        elif _rows_all_null(rows):
            result_type = "all_null_result"
            result_table_str = "all NULL result\n" + _format_rows_preview(rows, columns)
        else:
            result_type = "success"
            result_table_str = _format_rows_preview(rows, columns)
        return {
            "ok": True,
            "result_type": result_type,
            "rows": rows,
            "columns": columns,
            "error": None,
            "result_table_str": result_table_str,
        }
    except sqlite3.Error as exc:
        error_text = f"sqlite timeout after {timeout_s:.1f}s" if timed_out["value"] else str(exc)
        return {
            "ok": False,
            "result_type": "execution_error",
            "rows": [],
            "columns": [],
            "error": error_text,
            "result_table_str": f"sqlite error: {error_text}",
        }
    finally:
        if conn is not None:
            conn.close()


class BaseChecker:
    """DeepEye-like checker interface."""

    name = "BaseChecker"
    trigger_group = "rules"

    def check_and_revise(
        self,
        sql: str,
        *,
        question: str,
        schema: str,
        evidence: str,
        db_path: str,
        pipeline: "CheckerPipeline",
        sampling_budget: int,
    ) -> tuple[str, dict[str, Any]]:
        raise NotImplementedError

    @staticmethod
    def parse_llm_response(response: str) -> Optional[str]:
        try:
            answer_match = re.search(r"<result>(.*?)</result>", response, re.DOTALL | re.IGNORECASE)
            if not answer_match:
                blocks = re.findall(r"```(?:postgresql|sql)?\s*\n(.*?)```", response, re.DOTALL | re.IGNORECASE)
                if blocks:
                    candidate = blocks[-1].strip()
                    return candidate or None
                sql_matches = list(
                    re.finditer(r"(?is)\b(select|with)\b.*", response)
                )
                if sql_matches:
                    candidate = sql_matches[-1].group(0).strip()
                    return candidate or None
                return None
            answer_content = _strip_sql_fence(answer_match.group(1))
            if not answer_content or not answer_content.strip():
                return None
            return answer_content.strip()
        except Exception:  # noqa: BLE001
            return None


class CommonLLMChecker(BaseChecker):
    """Regex-based checker that uses DeepEye common checker prompt."""

    def detect(self, sql: str) -> Optional[str]:
        raise NotImplementedError

    def preprocess(self, sql: str) -> tuple[str, Optional[dict[str, Any]]]:
        return sql, None

    def check_and_revise(
        self,
        sql: str,
        *,
        question: str,
        schema: str,
        evidence: str,
        db_path: str,
        pipeline: "CheckerPipeline",
        sampling_budget: int,
    ) -> tuple[str, dict[str, Any]]:
        current_sql, preprocess_step = self.preprocess(sql)
        suggestion = self.detect(current_sql)

        step = {
            "checker": self.name,
            "trigger_group": self.trigger_group,
            "triggered": bool(suggestion),
            "called": False,
            "changed": current_sql.strip() != sql.strip(),
            "error_stage": None,
            "message": suggestion,
        }

        if preprocess_step:
            step["preprocess"] = preprocess_step

        if not suggestion:
            return current_sql, step

        prompt = _format_common_checker_prompt(
            database_schema=schema,
            question=question,
            hint=evidence,
            sql=current_sql,
            suggestions=suggestion,
        )
        candidates, meta = pipeline._generate_sql_candidates(prompt=prompt, n=1)
        step["called"] = True
        step["llm_meta"] = meta

        if not candidates:
            step["error_stage"] = meta.get("error_stage") or "extract_sql"
            return current_sql, step

        revised_sql = candidates[0]
        step["changed"] = revised_sql.strip() != sql.strip()
        return revised_sql, step


class SyntaxChecker(BaseChecker):
    name = "SyntaxChecker"
    trigger_group = "syntax"

    def check_and_revise(
        self,
        sql: str,
        *,
        question: str,
        schema: str,
        evidence: str,
        db_path: str,
        pipeline: "CheckerPipeline",
        sampling_budget: int,
    ) -> tuple[str, dict[str, Any]]:
        execution_result = _execute_sql(db_path, sql, timeout_s=pipeline.timeout_s)
        step = {
            "checker": self.name,
            "trigger_group": self.trigger_group,
            "triggered": execution_result["result_type"] not in {"success", "empty_result", "all_null_result"},
            "called": False,
            "changed": False,
            "error_stage": None,
            "result_type_before": execution_result["result_type"],
            "message": execution_result["result_table_str"],
        }
        if not step["triggered"]:
            return sql, step

        prompt = _format_execution_checker_prompt(
            database_schema=schema,
            question=question,
            hint=evidence,
            sql=sql,
            execution_result=execution_result["result_table_str"],
        )
        candidates, meta = pipeline._generate_sql_candidates(prompt=prompt, n=sampling_budget)
        step["called"] = True
        step["llm_meta"] = meta

        selected_sql, selected_meta = pipeline._select_candidate_by_execution(
            candidates=candidates,
            db_path=db_path,
            accept_types={"success", "empty_result", "all_null_result"},
            timeout_s=pipeline.timeout_s,
        )
        step["selection"] = selected_meta
        if selected_sql is None:
            step["error_stage"] = meta.get("error_stage") or "no_valid_candidate"
            return sql, step

        step["changed"] = selected_sql.strip() != sql.strip()
        step["result_type_after"] = _execute_sql(db_path, selected_sql, timeout_s=pipeline.timeout_s)["result_type"]
        return selected_sql, step


class ResultChecker(BaseChecker):
    name = "ResultChecker"
    trigger_group = "result"

    def check_and_revise(
        self,
        sql: str,
        *,
        question: str,
        schema: str,
        evidence: str,
        db_path: str,
        pipeline: "CheckerPipeline",
        sampling_budget: int,
    ) -> tuple[str, dict[str, Any]]:
        execution_result = _execute_sql(db_path, sql, timeout_s=pipeline.timeout_s)
        step = {
            "checker": self.name,
            "trigger_group": self.trigger_group,
            "triggered": execution_result["result_type"] != "success",
            "called": False,
            "changed": False,
            "error_stage": None,
            "result_type_before": execution_result["result_type"],
            "message": execution_result["result_table_str"],
        }
        if not step["triggered"]:
            return sql, step

        prompt = _format_execution_checker_prompt(
            database_schema=schema,
            question=question,
            hint=evidence,
            sql=sql,
            execution_result=execution_result["result_table_str"],
        )
        candidates, meta = pipeline._generate_sql_candidates(prompt=prompt, n=sampling_budget)
        step["called"] = True
        step["llm_meta"] = meta

        selected_sql, selected_meta = pipeline._select_candidate_by_execution(
            candidates=candidates,
            db_path=db_path,
            accept_types={"success"},
            timeout_s=pipeline.timeout_s,
        )
        step["selection"] = selected_meta
        if selected_sql is None:
            step["error_stage"] = meta.get("error_stage") or "no_valid_candidate"
            return sql, step

        step["changed"] = selected_sql.strip() != sql.strip()
        step["result_type_after"] = _execute_sql(db_path, selected_sql, timeout_s=pipeline.timeout_s)["result_type"]
        return selected_sql, step


class JoinChecker(CommonLLMChecker):
    name = "JoinChecker"

    def detect(self, sql: str) -> Optional[str]:
        identifier = r'(?:`[^`]+`|\[[^\]]+\]|"[^"]+"|[\w\.]+)'
        join_pattern = re.compile(
            rf"JOIN\s+{identifier}(\s+AS\s+{identifier}){{0,1}}\s+ON(\s+{identifier}\.{identifier}\s*(=\s*{identifier}\.{identifier}(?:\s+OR\s+{identifier}\.{identifier}\s*=\s*{identifier}\.{identifier})+|IN\s+\(.*?\)))",
            re.IGNORECASE | re.DOTALL,
        )
        if join_pattern.findall(sql):
            return (
                "The SQL uses the JOIN function incorrectly, due to using `JOIN table AS T ON Ta.column1 = Tb.column2 OR Ta.column1 = Tb.column3` or "
                "`JOIN table AS T ON Ta.column1 IN`, please only keep the highest priority group of `Ta.column = Tb.column` in `OR`."
            )
        return None


class OrderByLimitChecker(CommonLLMChecker):
    name = "OrderByLimitChecker"

    def detect(self, sql: str) -> Optional[str]:
        identifier = r'(?:`[^`]+`|\[[^\]]+\]|"[^"]+"|[\w\.]+)'
        order_by_pattern = re.compile(
            rf"ORDER BY ((MIN|MAX)\(\s*({identifier})\s*\)).*? LIMIT \d+",
            re.IGNORECASE | re.DOTALL,
        )
        res = order_by_pattern.search(sql)
        if res:
            return (
                f"The SQL uses the ORDER BY function incorrectly, using MIN/MAX in ORDER BY caluse is incrorrect (`{res.group()}`), "
                f"please correct the SQL. If the SQL contains GROUP BY, please judge whether the content of `{res.groups()[0]}` needs to use `SUM({res.groups()[2]})`."
            )
        return None


class TimeChecker(BaseChecker):
    name = "TimeChecker"
    trigger_group = "time_auto_fix"

    def check_and_revise(
        self,
        sql: str,
        *,
        question: str,
        schema: str,
        evidence: str,
        db_path: str,
        pipeline: "CheckerPipeline",
        sampling_budget: int,
    ) -> tuple[str, dict[str, Any]]:
        revised_sql = re.sub(
            r"(strftime *\([^\(]*?\) *[>=<]+ *)(\d{4,})",
            r"\1'\2'",
            sql,
        )
        changed = revised_sql != sql
        step = {
            "checker": self.name,
            "trigger_group": self.trigger_group,
            "triggered": changed,
            "called": False,
            "changed": changed,
            "error_stage": None,
            "message": "Auto-quoted numeric literal in strftime comparison." if changed else None,
        }
        return revised_sql, step

    def detect(self, sql: str) -> Optional[str]:
        revised_sql = re.sub(
            r"(strftime *\([^\(]*?\) *[>=<]+ *)(\d{4,})",
            r"\1'\2'",
            sql,
        )
        if revised_sql != sql:
            return "strftime() compared with unquoted integer."
        return None


class SelectChecker(CommonLLMChecker):
    name = "SelectChecker"

    def preprocess(self, sql: str) -> tuple[str, Optional[dict[str, Any]]]:
        cleaned = sql
        select = re.findall(r"^SELECT.*?\|\| ' ' \|\| .*?FROM", sql, re.IGNORECASE | re.DOTALL | re.MULTILINE)
        if select:
            cleaned = cleaned.replace("|| ' ' ||", ", ")
            cleaned = cleaned.replace("|| ', ' ||", ", ")
        if cleaned != sql:
            return cleaned, {"changed": True, "message": "Replaced string concatenation with comma-separated projection."}
        return sql, None

    def detect(self, sql: str) -> Optional[str]:
        identifier = r'(?:`[^`]+`|\[[^\]]+\]|"[^"]+"|[\w\.]+)'
        select_amb = re.findall(
            rf"^SELECT.*? ({identifier}\.\*).*?FROM",
            sql,
            re.IGNORECASE | re.DOTALL | re.MULTILINE,
        )
        if select_amb:
            suggestion = ""
            for idx, item in enumerate(select_amb, 1):
                suggestion += (
                    f"{idx}. We have specified that the ambiguous query is the corresponding id column, "
                    f"please replace {item} with the corresponding id column in the above SQL\n"
                )
            return suggestion
        return None


class MaxMinChecker(CommonLLMChecker):
    name = "MaxMinChecker"

    def detect(self, sql: str) -> Optional[str]:
        identifier = r'(?:`[^`]+`|\[[^\]]+\]|"[^"]+"|[\w\.]+)'
        max_min_pattern = re.compile(
            rf"=\s*\(\s*SELECT\s*(MAX|MIN)\s*\(\s*({identifier})\s*\)\s*FROM\s*({identifier})",
            re.IGNORECASE | re.DOTALL,
        )
        fun_amb = max_min_pattern.findall(sql)
        order_amb = set(re.findall(r"= (\(SELECT .* LIMIT \d\))", sql, re.IGNORECASE | re.DOTALL))
        select_amb_pattern = re.compile(
            rf"^SELECT[^\(\)]*? ((MIN|MAX)\(\s*{identifier}\s*\)).*?LIMIT 1",
            re.IGNORECASE | re.DOTALL | re.MULTILINE,
        )
        select_amb = set(select_amb_pattern.findall(sql))
        suggestions = []
        for fun in fun_amb:
            fuc, col, table = fun
            order = "DESC" if fuc == "MAX" else "ASC"
            suggestions.append(
                f"WHERE {col} = (SELECT {fuc}({col}) FROM {table}): Please use ORDER BY {table}.{col} {order} LIMIT 1 instead of nested SQL"
            )
        for fun in order_amb:
            suggestions.append(f"{fun}: Please use JOIN instead of nested SQL")
        for fun in select_amb:
            suggestions.append(f"{fun[0]}: {fun[1]} function is redundant due to LIMIT clause, please use ORDER BY + LIMIT instead")
        if suggestions:
            return "\n".join(f"{idx + 1}. {item}" for idx, item in enumerate(suggestions))
        return None


class OrderByNullChecker(CommonLLMChecker):
    name = "OrderByNullChecker"

    def detect(self, sql: str) -> Optional[str]:
        inn = re.findall(r"ORDER BY .*?(?<!DESC )LIMIT +\d+;{0,1}", sql)
        if not inn:
            return None
        for item in inn:
            if re.findall(r"SUM\(|COUNT\(", item):
                return None
        suggestion = ""
        for item in inn:
            suggestion += f"Please add `IS NOT NULL` condition **in the WHERE clause** for the ORDER BY column: {item}\n"
        return suggestion


class CheckerPipeline:
    def __init__(
        self,
        *,
        model: str = "gemini-3-flash-preview",
        temperature: float = 0.1,
        max_output_tokens: int = 8192,
        thinking_level: str = "none",
        thinking_budget: int | None = None,
        timeout_s: float = 120.0,
        max_retries: int = 4,
        retry_delay_s: float = 4.0,
        checker_sampling_budget: int = 3,
        api_keys: list[str] | None = None,
        proxy: str | None = None,
    ):
        self.model = model
        self.temperature = temperature
        self.max_output_tokens = max_output_tokens
        self.thinking_level = thinking_level
        self.thinking_budget = thinking_budget
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.retry_delay_s = retry_delay_s
        self.checker_sampling_budget = max(1, checker_sampling_budget)
        self.api_keys = api_keys if api_keys is not None else _resolve_api_keys(None)
        self.proxy = proxy if proxy is not None else _resolve_proxy(None)
        self.checkers: list[BaseChecker] = [
            SyntaxChecker(),
            JoinChecker(),
            OrderByLimitChecker(),
            TimeChecker(),
            SelectChecker(),
            MaxMinChecker(),
            OrderByNullChecker(),
            ResultChecker(),
        ]

    def check_syntax(self, sql: str, db_path: str) -> tuple[bool, str]:
        result = _execute_sql(db_path, sql, timeout_s=self.timeout_s)
        ok = result["result_type"] in {"success", "empty_result", "all_null_result"}
        return ok, "" if ok else result["result_table_str"]

    def check_result(self, sql: str, db_path: str) -> tuple[bool, str]:
        result = _execute_sql(db_path, sql, timeout_s=self.timeout_s)
        ok = result["result_type"] == "success"
        return ok, "" if ok else result["result_table_str"]

    def check_rules(self, sql: str) -> list[str]:
        suggestions: list[str] = []
        for checker in self.checkers:
            if isinstance(checker, (SyntaxChecker, ResultChecker)):
                continue
            if isinstance(checker, TimeChecker):
                suggestion = checker.detect(sql)
            elif isinstance(checker, CommonLLMChecker):
                preprocessed_sql, _ = checker.preprocess(sql)
                suggestion = checker.detect(preprocessed_sql)
            else:
                suggestion = None
            if suggestion:
                suggestions.append(f"[{checker.name}] {suggestion}")
        return suggestions

    def revise(
        self,
        sql: str,
        question: str,
        schema: str,
        evidence: str,
        db_path: str,
    ) -> str:
        revised_sql, _trace = self.revise_with_trace(
            sql=sql,
            question=question,
            schema=schema,
            evidence=evidence,
            db_path=db_path,
        )
        return revised_sql

    def revise_with_trace(
        self,
        sql: str,
        question: str,
        schema: str,
        evidence: str,
        db_path: str,
    ) -> tuple[str, dict[str, Any]]:
        current_sql = sql
        trace: dict[str, Any] = {
            "triggered": False,
            "trigger_sources": [],
            "steps": [],
            "rolled_back": False,
            "error_stage": None,
        }

        for checker in self.checkers:
            next_sql, step = checker.check_and_revise(
                current_sql,
                question=question,
                schema=schema,
                evidence=evidence,
                db_path=db_path,
                pipeline=self,
                sampling_budget=self.checker_sampling_budget,
            )
            trace["steps"].append(step)
            if step.get("triggered"):
                trace["triggered"] = True
                trace["trigger_sources"].append(step["trigger_group"])
            if step.get("error_stage") and not trace["error_stage"]:
                trace["error_stage"] = step["error_stage"]
            current_sql = next_sql

        trace["trigger_sources"] = list(dict.fromkeys(trace["trigger_sources"]))
        trace["changed"] = current_sql.strip() != sql.strip()
        trace["final_executable"] = _execute_sql(db_path, current_sql, timeout_s=self.timeout_s)["result_type"] != "execution_error"
        return current_sql, trace

    def _generate_sql_candidates(self, *, prompt: str, n: int) -> tuple[list[str], dict[str, Any]]:
        meta: dict[str, Any] = {
            "attempted": n,
            "responses": 0,
            "extracted": 0,
            "error_stage": None,
            "errors": [],
        }
        if not self.api_keys:
            meta["error_stage"] = "no_api_key"
            return [], meta

        candidates: list[str] = []
        prompt_tokens = 0
        completion_tokens = 0
        total_tokens = 0

        for _ in range(max(1, n)):
            try:
                content, _thinking, usage = _call_gemini_sync(
                    prompt_text=prompt,
                    model=self.model,
                    api_keys=self.api_keys,
                    proxy=self.proxy,
                    temperature=self.temperature,
                    max_output_tokens=self.max_output_tokens,
                    thinking_level=self.thinking_level,
                    thinking_budget=self.thinking_budget,
                    timeout_s=self.timeout_s,
                    max_retries=self.max_retries,
                    retry_delay_s=self.retry_delay_s,
                )
                meta["responses"] += 1
                prompt_tokens += usage.get("prompt_tokens", 0)
                completion_tokens += usage.get("completion_tokens", 0)
                total_tokens += usage.get("total_tokens", 0)
            except Exception as exc:  # noqa: BLE001
                meta["errors"].append(str(exc))
                if meta["error_stage"] is None:
                    meta["error_stage"] = "gemini_call"
                continue

            revised_sql = BaseChecker.parse_llm_response(content)
            if not revised_sql:
                meta["errors"].append("extract_sql")
                if meta["error_stage"] is None:
                    meta["error_stage"] = "extract_sql"
                continue
            candidates.append(revised_sql)
            meta["extracted"] += 1

        meta["usage"] = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        }
        return candidates, meta

    @staticmethod
    def _select_candidate_by_execution(
        *,
        candidates: list[str],
        db_path: str,
        accept_types: set[str],
        timeout_s: float = 30.0,
    ) -> tuple[str | None, dict[str, Any]]:
        valid_candidates: list[tuple[str, str]] = []
        per_candidate: list[dict[str, Any]] = []
        for candidate in candidates:
            execution_result = _execute_sql(db_path, candidate, timeout_s=timeout_s)
            candidate_meta = {
                "sql": candidate,
                "result_type": execution_result["result_type"],
            }
            if execution_result["result_type"] in accept_types:
                candidate_hash = _hash_result(execution_result["rows"])
                valid_candidates.append((candidate, candidate_hash))
                candidate_meta["result_hash"] = candidate_hash
            per_candidate.append(candidate_meta)

        if not valid_candidates:
            return None, {"valid_candidates": 0, "candidates": per_candidate}

        counts: dict[str, int] = {}
        for _, result_hash in valid_candidates:
            counts[result_hash] = counts.get(result_hash, 0) + 1
        selected_sql, selected_hash = max(valid_candidates, key=lambda item: counts[item[1]])
        return selected_sql, {
            "valid_candidates": len(valid_candidates),
            "selected_hash": selected_hash,
            "candidates": per_candidate,
        }


if __name__ == "__main__":
    pipeline = CheckerPipeline(api_keys=[], proxy=None)
    test_cases = [
        ("MaxMin anti-pattern", "SELECT name FROM students WHERE score = (SELECT MAX(score) FROM students)"),
        ("JOIN OR anti-pattern", "SELECT * FROM A JOIN B ON A.x = B.x OR A.x = B.y"),
        ("Normal SQL", "SELECT COUNT(*) FROM orders WHERE status = 'shipped'"),
        ("Syntax error", "SELEC * FROM orders"),
    ]

    for label, sql in test_cases:
        print(f"\n{'=' * 60}")
        print(f"Test: {label}")
        print(f"SQL:  {sql}")
        print("-" * 60)
        suggestions = pipeline.check_rules(sql)
        if suggestions:
            for item in suggestions:
                print(f"  TRIGGERED: {item}")
        else:
            print("  No rule checker triggered.")
        ok, err = pipeline.check_syntax(sql, ":memory:")
        if not ok:
            print(f"  SYNTAX FAIL: {err}")
        else:
            print("  SYNTAX OK")

    print(f"\n{'=' * 60}")
    print("All tests complete.")
