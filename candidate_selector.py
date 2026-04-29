"""
Execution-based hash clustering candidate selector.

For each moderate/challenging question, we generate multiple SQL candidates.
This module executes each candidate, clusters by result hash, and picks the best.

Reference: DeepEye-SQL sql_selection.py + Agentar consolidation.
"""

import hashlib
import re
import sqlite3
import threading
import time
from collections import defaultdict


def _execute_with_timeout(sql: str, db_path: str, timeout_sec: int):
    """
    Execute SQL in a separate thread with timeout protection.
    Uses threading + join (not signal.alarm) to avoid pipe conflicts.

    Returns:
        (rows, error): rows is list of tuples or None; error is str or None.
    """
    result = [None]
    error = [None]

    def worker():
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            cur = conn.cursor()
            cur.execute(sql)
            result[0] = cur.fetchall()
            conn.close()
        except Exception as e:
            error[0] = str(e)

    t = threading.Thread(target=worker)
    t.start()
    t.join(timeout=timeout_sec)
    if t.is_alive():
        return None, "TIMEOUT"
    return result[0], error[0]


def hash_result(rows) -> str:
    """
    Produce a stable hash of a result set (order-independent).

    1. Convert each row to its str representation.
    2. Sort all row strings.
    3. MD5 the joined result.
    """
    normalized = sorted(str(row) for row in rows)
    return hashlib.md5("\n".join(normalized).encode()).hexdigest()


def execute_and_hash(sql: str, db_path: str, timeout: int = 30) -> dict:
    """
    Execute a SQL query and hash its result set.

    Returns:
        {
            "success": bool,
            "result_hash": str | None,
            "row_count": int,
            "exec_time_ms": int,
            "error": str | None,
        }
    """
    start = time.time()
    rows, err = _execute_with_timeout(sql, db_path, timeout)
    elapsed_ms = int((time.time() - start) * 1000)

    if err is not None:
        return {
            "success": False,
            "result_hash": None,
            "row_count": 0,
            "exec_time_ms": elapsed_ms,
            "error": err,
        }

    if rows is None:
        # Should not happen if err is None, but be defensive
        return {
            "success": False,
            "result_hash": None,
            "row_count": 0,
            "exec_time_ms": elapsed_ms,
            "error": "No result returned",
        }

    return {
        "success": True,
        "result_hash": hash_result(rows),
        "row_count": len(rows),
        "exec_time_ms": elapsed_ms,
        "error": None,
    }


class SimpleMajoritySelector:
    """
    Execute all candidate SQLs -> cluster by result hash -> largest cluster wins.

    Tie-breaking: shortest execution time.
    All failed: fallback to first candidate.
    """

    def select(self, candidates: list[dict], db_path: str, timeout: int = 30) -> dict:
        """
        Select the best candidate via majority vote on execution results.

        Args:
            candidates: [{"sql": str, "template": str, "temperature": float}, ...]
            db_path: Path to the SQLite database.
            timeout: Per-query timeout in seconds.

        Returns:
            The selected candidate dict augmented with selection metadata.
        """
        if not candidates:
            raise ValueError("candidates list is empty")

        # Single candidate - no need to vote
        if len(candidates) == 1:
            result = execute_and_hash(candidates[0]["sql"], db_path, timeout)
            return {
                **candidates[0],
                **result,
                "selection_method": "single_candidate",
                "cluster_size": 1 if result["success"] else 0,
                "total_valid": 1 if result["success"] else 0,
                "total_candidates": 1,
                "consistency_score": 1.0 if result["success"] else 0.0,
            }

        # 1. Execute every candidate
        exec_results = []
        for c in candidates:
            result = execute_and_hash(c["sql"], db_path, timeout)
            exec_results.append({**c, **result})

        # 2. Filter successfully executed candidates
        valid = [r for r in exec_results if r["success"] and r["result_hash"]]

        if not valid:
            # All failed -> fallback to first candidate
            return {
                **candidates[0],
                "success": False,
                "result_hash": None,
                "row_count": 0,
                "exec_time_ms": 0,
                "error": "all_candidates_failed",
                "selection_method": "fallback_first",
                "cluster_size": 0,
                "total_valid": 0,
                "total_candidates": len(candidates),
                "consistency_score": 0.0,
            }

        # 3. Cluster by result_hash
        clusters = defaultdict(list)
        for r in valid:
            clusters[r["result_hash"]].append(r)

        # 4. Rank: cluster_size DESC, min_exec_time ASC
        ranked = sorted(
            clusters.items(),
            key=lambda x: (-len(x[1]), min(r["exec_time_ms"] for r in x[1])),
        )

        # 5. Pick the fastest candidate from the largest cluster
        best_cluster = ranked[0][1]
        best = min(best_cluster, key=lambda r: r["exec_time_ms"])

        return {
            **best,
            "selection_method": "majority_vote",
            "cluster_size": len(best_cluster),
            "total_valid": len(valid),
            "total_candidates": len(candidates),
            "consistency_score": len(best_cluster) / len(valid),
        }

    def select_with_report(
        self, candidates: list[dict], db_path: str, timeout: int = 30
    ) -> dict:
        """select() with a printed summary report."""
        result = self.select(candidates, db_path, timeout)
        print(f"  Candidates: {result['total_candidates']}")
        print(f"  Valid executions: {result['total_valid']}")
        print(f"  Cluster size: {result['cluster_size']}")
        print(f"  Consistency: {result.get('consistency_score', 0):.2f}")
        print(f"  Method: {result['selection_method']}")
        print(f"  Selected template: {result.get('template', '?')}")
        return result


class HybridPairwiseSelector:
    """
    Execute candidates, use result-hash clusters when stable, otherwise run
    champion-defense pairwise comparisons over cluster representatives.
    """

    def __init__(
        self,
        *,
        pairwise_caller=None,
        usage_writer=None,
        model: str = "gemini-3.1-pro-preview",
        threshold: float = 0.55,
        include_result_preview: bool = False,
    ):
        self.pairwise_caller = pairwise_caller
        self.usage_writer = usage_writer
        self.model = model
        self.threshold = threshold
        self.include_result_preview = include_result_preview

    def select(
        self,
        candidates: list[dict],
        db_path: str,
        timeout: int = 30,
        *,
        qid=None,
        question: str = "",
        evidence: str = "",
        schema: str = "",
    ) -> dict:
        if not candidates:
            raise ValueError("candidates list is empty")

        if len(candidates) == 1:
            result = execute_and_hash(candidates[0]["sql"], db_path, timeout)
            clusters = self._cluster_summaries([dict(candidates[0], **result)])
            return {
                **candidates[0],
                **result,
                "selection_method": "single_candidate",
                "cluster_size": 1 if result["success"] else 0,
                "total_valid": 1 if result["success"] else 0,
                "total_candidates": 1,
                "consistency_score": 1.0 if result["success"] else 0.0,
                "clusters": clusters,
                "pairwise_log": [],
            }

        exec_results = []
        for candidate in candidates:
            result = execute_and_hash(candidate["sql"], db_path, timeout)
            exec_results.append({**candidate, **result})

        valid = [r for r in exec_results if r["success"] and r["result_hash"]]
        if not valid:
            return {
                **candidates[0],
                "success": False,
                "result_hash": None,
                "row_count": 0,
                "exec_time_ms": 0,
                "error": "all_candidates_failed",
                "selection_method": "fallback_first",
                "cluster_size": 0,
                "total_valid": 0,
                "total_candidates": len(candidates),
                "consistency_score": 0.0,
                "clusters": [],
                "pairwise_log": [],
            }

        clusters_by_hash = defaultdict(list)
        for row in valid:
            clusters_by_hash[row["result_hash"]].append(row)

        ranked_clusters = sorted(
            clusters_by_hash.items(),
            key=lambda item: (-len(item[1]), min(r["exec_time_ms"] for r in item[1])),
        )
        cluster_summaries = self._cluster_summaries(valid)
        best_hash, best_cluster = ranked_clusters[0]
        best = min(best_cluster, key=lambda r: r["exec_time_ms"])
        consistency = len(best_cluster) / len(valid)

        if consistency >= self.threshold:
            return {
                **best,
                "selection_method": "high_confidence_vote",
                "cluster_size": len(best_cluster),
                "total_valid": len(valid),
                "total_candidates": len(candidates),
                "consistency_score": consistency,
                "clusters": cluster_summaries,
                "pairwise_log": [
                    {
                        "reason": "top_cluster_consistency_above_threshold",
                        "result_hash": best_hash,
                        "consistency_score": consistency,
                        "threshold": self.threshold,
                    }
                ],
            }

        representatives = [
            min(cluster, key=lambda r: r["exec_time_ms"])
            for _, cluster in ranked_clusters
        ]
        if not self.pairwise_caller:
            return {
                **best,
                "selection_method": "no_api_key_majority_fallback",
                "cluster_size": len(best_cluster),
                "total_valid": len(valid),
                "total_candidates": len(candidates),
                "consistency_score": consistency,
                "clusters": cluster_summaries,
                "pairwise_log": [
                    {
                        "reason": "pairwise_caller_unavailable",
                        "selected_hash": best_hash,
                    }
                ],
            }

        champion = representatives[0]
        pairwise_log = []
        for challenger in representatives[1:]:
            winner, log_row = self._compare_pair(
                champion=champion,
                challenger=challenger,
                qid=qid,
                question=question,
                evidence=evidence,
                schema=schema,
            )
            pairwise_log.append(log_row)
            if winner == "B":
                champion = challenger

        champion_cluster_size = len(clusters_by_hash[champion["result_hash"]])
        return {
            **champion,
            "selection_method": "pairwise_tournament",
            "cluster_size": champion_cluster_size,
            "total_valid": len(valid),
            "total_candidates": len(candidates),
            "consistency_score": consistency,
            "clusters": cluster_summaries,
            "pairwise_log": pairwise_log,
        }

    def _cluster_summaries(self, rows: list[dict]) -> list[dict]:
        clusters = defaultdict(list)
        for row in rows:
            if row.get("success") and row.get("result_hash"):
                clusters[row["result_hash"]].append(row)
        summaries = []
        for result_hash, cluster in sorted(
            clusters.items(),
            key=lambda item: (-len(item[1]), min(r["exec_time_ms"] for r in item[1])),
        ):
            summaries.append(
                {
                    "result_hash": result_hash,
                    "size": len(cluster),
                    "templates": [str(r.get("template", "")) for r in cluster],
                    "row_count": cluster[0].get("row_count", 0),
                    "min_exec_time_ms": min(r.get("exec_time_ms", 0) for r in cluster),
                }
            )
        return summaries

    def _compare_pair(
        self,
        *,
        champion: dict,
        challenger: dict,
        qid,
        question: str,
        evidence: str,
        schema: str,
    ) -> tuple[str, dict]:
        prompt = self._build_pairwise_prompt(
            question=question,
            evidence=evidence,
            schema=schema,
            sql_a=str(champion.get("sql", "")),
            sql_b=str(challenger.get("sql", "")),
        )
        prompt_id = f"{qid or 'unknown'}:{champion.get('template')}:{challenger.get('template')}"
        base_log = {
            "qid": qid,
            "champion_template": champion.get("template"),
            "challenger_template": challenger.get("template"),
            "champion_hash": champion.get("result_hash"),
            "challenger_hash": challenger.get("result_hash"),
        }
        try:
            content, usage = self.pairwise_caller(prompt, prompt_id)
            winner = self._parse_winner(content)
            status = "ok" if winner else "ambiguous"
            if not winner:
                winner = "A"
            self._write_usage(prompt_id, "ok", usage)
            return winner, {
                **base_log,
                "winner": winner,
                "status": status,
                "reason": "pairwise_llm_compare" if status == "ok" else "ambiguous_keep_champion",
                "raw_response": str(content).strip()[:200],
            }
        except Exception as exc:  # noqa: BLE001
            self._write_usage(prompt_id, "error", {}, error=str(exc))
            return "A", {
                **base_log,
                "winner": "A",
                "status": "error",
                "reason": "pairwise_error_keep_champion",
                "error": str(exc)[:500],
            }

    def _write_usage(self, prompt_id: str, status: str, usage=None, error=None):
        if not self.usage_writer:
            return
        usage = usage or {}
        row = {
            "provider": "gemini",
            "model": self.model,
            "prompt_id": prompt_id,
            "status": status,
            "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
            "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
            "thinking_tokens": int(usage.get("thinking_tokens", 0) or 0),
            "total_tokens": int(usage.get("total_tokens", 0) or 0),
        }
        if error:
            row["error"] = error[:500]
        self.usage_writer(row)

    def _parse_winner(self, content: str):
        text = str(content or "").strip().upper()
        if text in {"A", "B"}:
            return text
        letters = set(re.findall(r"\b([AB])\b", text))
        if letters == {"A"}:
            return "A"
        if letters == {"B"}:
            return "B"
        return None

    def _build_pairwise_prompt(
        self,
        *,
        question: str,
        evidence: str,
        schema: str,
        sql_a: str,
        sql_b: str,
    ) -> str:
        preview_note = ""
        if self.include_result_preview:
            preview_note = "\nExecution results are already clustered by hash; compare semantic SQL correctness, not style."
        return f"""You are selecting the better SQL answer for a BIRD text-to-SQL question.
Return exactly one character: A or B.

Question:
{question}

Evidence:
{evidence}

{schema}

SQL A:
```sql
{sql_a}
```

SQL B:
```sql
{sql_b}
```
{preview_note}

Choose the SQL that is more likely to answer the question correctly under the given schema.
Return only A or B."""


if __name__ == "__main__":
    selector = SimpleMajoritySelector()

    # Simulated 6 candidates: 3 same + 2 same (different value) + 1 invalid SQL
    test_candidates = [
        {
            "sql": "SELECT COUNT(*) FROM schools WHERE County = 'Alameda'",
            "template": "v12",
            "temperature": 0.5,
        },
        {
            "sql": "SELECT COUNT(*) FROM schools WHERE County = 'Alameda'",
            "template": "v13_dc",
            "temperature": 0.5,
        },
        {
            "sql": "SELECT COUNT(*) FROM schools WHERE County = 'Alameda'",
            "template": "v13_skel",
            "temperature": 1.0,
        },
        {
            "sql": "SELECT COUNT(*) FROM schools WHERE County = 'Alameda County'",
            "template": "v12",
            "temperature": 1.0,
        },
        {
            "sql": "SELEC invalid sql here",
            "template": "v13_dc",
            "temperature": 1.0,
        },
        {
            "sql": "SELECT COUNT(*) FROM schools WHERE County = 'Alameda County'",
            "template": "v13_skel",
            "temperature": 0.5,
        },
    ]

    db_path = "data/dev_databases/california_schools/california_schools.sqlite"
    print("=== SimpleMajoritySelector Test ===")
    result = selector.select_with_report(test_candidates, db_path)
    print(f"\nSelected SQL: {result['sql'][:100]}")
