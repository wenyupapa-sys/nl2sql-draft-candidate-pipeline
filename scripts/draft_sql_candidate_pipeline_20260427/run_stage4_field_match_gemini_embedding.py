#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FIELD_RECALL_ROOT = PROJECT_ROOT / "field_recall_standalone"
sys.path.insert(0, str(FIELD_RECALL_ROOT / "src"))

from field_recall.dataset import load_jsonl, write_jsonl
from field_recall.field_match import FieldMatcher, load_field_rewrites, load_rewrite_results

GEMINI_NATIVE_URL = "https://generativelanguage.googleapis.com/v1beta/models"
QWEN_EMBEDDINGS_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings"
DISABLE_PROXY_VALUES = {"", "none", "off", "false", "0", "direct"}

_HTTP_POST_CHILD = r"""
import json
import sys
import urllib.error
import urllib.request

payload = json.loads(sys.stdin.read())
proxy = payload.get("proxy")
opener = urllib.request.build_opener(
    urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {})
)
body = json.dumps(payload["body"], ensure_ascii=False).encode("utf-8")
req = urllib.request.Request(
    payload["url"],
    data=body,
    headers=payload["headers"],
    method="POST",
)
try:
    with opener.open(req, timeout=float(payload["socket_timeout"])) as resp:
        raw = resp.read().decode("utf-8")
    print(json.dumps({"ok": True, "status": getattr(resp, "status", 200), "body": raw}))
except urllib.error.HTTPError as exc:
    detail = exc.read().decode("utf-8", errors="ignore")
    print(json.dumps({"ok": False, "status": exc.code, "body": detail}))
except Exception as exc:
    print(json.dumps({"ok": False, "status": None, "body": str(exc)}))
"""


def _resolve_api_key(explicit: str | None) -> str:
    if explicit:
        return explicit
    for key in ("GEMINI_API_KEY", "GEMINI_API_KEYS"):
        raw = os.getenv(key, "")
        if raw:
            return raw.split(",", 1)[0].strip()
    raise SystemExit("Missing Gemini API key. Set GEMINI_API_KEY or pass --api-key.")


def _resolve_qwen_api_key(explicit: str | None) -> str | None:
    if explicit:
        return explicit
    for key in ("DASHSCOPE_API_KEY", "QWEN_API_KEY"):
        raw = os.getenv(key, "")
        if raw:
            return raw.strip()
    return None


def _resolve_proxy(explicit: str | None) -> str | None:
    if explicit is not None:
        text = explicit.strip()
        return None if text.lower() in DISABLE_PROXY_VALUES else text
    for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        value = os.getenv(key)
        if value and value.strip().lower() not in DISABLE_PROXY_VALUES:
            return value
    try:
        raw = subprocess.check_output(
            ["scutil", "--proxy"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
    except Exception:
        return None
    enabled = False
    host = None
    port = None
    for line in raw.splitlines():
        text = line.strip()
        if text == "HTTPSEnable : 1":
            enabled = True
        elif text.startswith("HTTPSProxy : "):
            host = text.split(" : ", 1)[1].strip()
        elif text.startswith("HTTPSPort : "):
            port = text.split(" : ", 1)[1].strip()
    if enabled and host and port:
        return f"http://{host}:{port}"
    return None


def _norm(vec: list[float]) -> list[float]:
    denom = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / denom for x in vec]


def _dot(a: list[float], b: list[float]) -> float:
    return float(sum(x * y for x, y in zip(a, b)))


def _post_json_hard_timeout(
    *,
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    proxy: str | None,
    timeout_s: float,
) -> dict[str, Any]:
    """POST JSON in a child process so stuck sockets are killed at timeout_s."""
    child_input = json.dumps(
        {
            "url": url,
            "headers": headers,
            "body": body,
            "proxy": proxy,
            "socket_timeout": max(1.0, min(float(timeout_s), 30.0)),
        },
        ensure_ascii=False,
    )
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _HTTP_POST_CHILD],
            input=child_input,
            text=True,
            capture_output=True,
            timeout=max(1.0, float(timeout_s)),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"hard timeout after {timeout_s:.1f}s") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"http child failed rc={proc.returncode}: {detail[:500]}")
    try:
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"invalid http child response: {proc.stdout[:500]}") from exc
    if not payload.get("ok"):
        status = payload.get("status")
        detail = str(payload.get("body") or "")
        raise RuntimeError(f"http {status}: {detail[:500]}")
    return json.loads(str(payload.get("body") or "{}"))


class RoutedEmbedder:
    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        fallback_provider: str,
        qwen_model: str,
        qwen_api_key: str | None,
        qwen_endpoint: str,
        qwen_batch_size: int,
        proxy: str | None,
        cache_dir: Path | None,
        batch_size: int,
        timeout_s: float,
        max_retries: int,
        fallback_on_error: bool,
        cache_only: bool,
    ) -> None:
        self.model = model.replace("models/", "")
        self.api_key = api_key
        self.fallback_provider = fallback_provider
        self.qwen_model = qwen_model
        self.qwen_api_key = qwen_api_key
        self.qwen_endpoint = qwen_endpoint
        self.qwen_batch_size = qwen_batch_size
        self.batch_size = batch_size
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.fallback_on_error = fallback_on_error
        self.cache_only = cache_only
        self.active_provider = "gemini"
        self.last_provider = "gemini"
        self.errors: list[dict[str, Any]] = []
        self.cache_dir = cache_dir
        self.proxy = proxy
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _provider_model(self, provider: str) -> str:
        return self.model if provider == "gemini" else self.qwen_model

    def _cache_path(self, text: str, task_type: str, provider: str) -> Path | None:
        if not self.cache_dir:
            return None
        model = self._provider_model(provider)
        cache_model = model if provider == "gemini" else f"qwen:{model}"
        digest = hashlib.sha256(f"{cache_model}\0{task_type}\0{text}".encode("utf-8")).hexdigest()
        return self.cache_dir / f"{digest}.json"

    def _request_gemini(self, texts: list[str], task_type: str) -> list[list[float]]:
        body = {
            "requests": [
                {
                    "model": f"models/{self.model}",
                    "content": {"parts": [{"text": text}]},
                    "taskType": task_type,
                }
                for text in texts
            ]
        }
        data = _post_json_hard_timeout(
            url=f"{GEMINI_NATIVE_URL}/{self.model}:batchEmbedContents",
            headers={"Content-Type": "application/json", "x-goog-api-key": self.api_key},
            body=body,
            proxy=self.proxy,
            timeout_s=self.timeout_s,
        )
        embeddings = data.get("embeddings") or []
        if len(embeddings) != len(texts):
            raise RuntimeError(f"embedding count mismatch: {json.dumps(data)[:500]}")
        return [_norm([float(x) for x in emb["values"]]) for emb in embeddings]

    def _request_qwen(self, texts: list[str], _task_type: str) -> list[list[float]]:
        if not self.qwen_api_key:
            raise RuntimeError("missing Qwen embedding API key")
        data = _post_json_hard_timeout(
            url=self.qwen_endpoint,
            headers={
                "Authorization": f"Bearer {self.qwen_api_key}",
                "Content-Type": "application/json",
            },
            body={"model": self.qwen_model, "input": texts, "encoding_format": "float"},
            proxy=None,
            timeout_s=self.timeout_s,
        )
        raw_items = data.get("data")
        if raw_items is None:
            raw_items = (data.get("output") or {}).get("embeddings")
        items = raw_items or []
        if len(items) != len(texts):
            raise RuntimeError(f"qwen embedding count mismatch: {json.dumps(data)[:500]}")
        ordered: list[Any] = [None] * len(texts)
        for pos, item in enumerate(items):
            idx = item.get("index", item.get("text_index", pos))
            ordered[int(idx)] = item
        vectors = []
        for item in ordered:
            if item is None:
                raise RuntimeError(f"missing qwen embedding item: {json.dumps(data)[:500]}")
            vec = item.get("embedding")
            if vec is None:
                vec = item.get("values")
            vectors.append(_norm([float(x) for x in vec]))
        return vectors

    def _request_provider(self, provider: str, texts: list[str], task_type: str) -> list[list[float]]:
        if provider == "gemini":
            return self._request_gemini(texts, task_type)
        if provider == "qwen":
            return self._request_qwen(texts, task_type)
        raise RuntimeError(f"unknown embedding provider: {provider}")

    def _batch_size_for_provider(self, provider: str) -> int:
        return self.batch_size if provider == "gemini" else self.qwen_batch_size

    def _embed_many_with_provider(
        self,
        provider: str,
        texts: list[str],
        task_type: str,
        *,
        label: str,
    ) -> list[list[float]]:
        self.last_provider = provider
        out: list[list[float] | None] = [None] * len(texts)
        missing_idx: list[int] = []
        missing_texts: list[str] = []
        for idx, text in enumerate(texts):
            cache_path = self._cache_path(text, task_type, provider)
            if cache_path and cache_path.exists():
                out[idx] = [float(x) for x in json.loads(cache_path.read_text(encoding="utf-8"))["values"]]
            else:
                missing_idx.append(idx)
                missing_texts.append(text)
        batch_size = self._batch_size_for_provider(provider)
        cached = len(texts) - len(missing_texts)
        print(
            json.dumps(
                {
                    "event": "embedding_start",
                    "provider": provider,
                    "model": self._provider_model(provider),
                    "label": label,
                    "task_type": task_type,
                    "total": len(texts),
                    "cached": cached,
                    "missing": len(missing_texts),
                    "batch_size": batch_size,
                    "cache_only": self.cache_only,
                    "fallback_on_error": self.fallback_on_error,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        if self.cache_only:
            for idx in missing_idx:
                out[idx] = []
            if missing_idx:
                self.errors.append(
                    {
                        "provider": provider,
                        "model": self._provider_model(provider),
                        "label": label,
                        "task_type": task_type,
                        "fallback": "cache_only",
                        "missing": len(missing_idx),
                    }
                )
            return [vec if vec is not None else [] for vec in out]
        last_error: str | None = None
        for start in range(0, len(missing_texts), batch_size):
            batch_texts = missing_texts[start : start + batch_size]
            batch_no = start // batch_size + 1
            batch_total = (len(missing_texts) + batch_size - 1) // batch_size
            print(
                json.dumps(
                    {
                        "event": "embedding_batch_request",
                        "provider": provider,
                        "model": self._provider_model(provider),
                        "label": label,
                        "task_type": task_type,
                        "batch": batch_no,
                        "batch_total": batch_total,
                        "size": len(batch_texts),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            batch_vecs: list[list[float]]
            try:
                for attempt in range(1, self.max_retries + 1):
                    try:
                        batch_vecs = self._request_provider(provider, batch_texts, task_type)
                        break
                    except Exception as exc:  # noqa: BLE001
                        last_error = str(exc)
                        if attempt < self.max_retries:
                            time.sleep(attempt * 2)
                            continue
                        raise
            except Exception as exc:  # noqa: BLE001
                error = {
                    "provider": provider,
                    "model": self._provider_model(provider),
                    "label": label,
                    "task_type": task_type,
                    "batch": batch_no,
                    "size": len(batch_texts),
                    "error": str(exc),
                }
                self.errors.append(error)
                print(json.dumps({"event": "embedding_batch_error", **error}, ensure_ascii=False), flush=True)
                if provider == "gemini" and self.fallback_provider == "qwen":
                    raise
                if not self.fallback_on_error:
                    raise
                batch_vecs = [[] for _ in batch_texts]
            for offset, vec in enumerate(batch_vecs):
                idx = missing_idx[start + offset]
                out[idx] = vec
                cache_path = self._cache_path(texts[idx], task_type, provider)
                if cache_path and vec:
                    cache_path.write_text(json.dumps({"values": vec}), encoding="utf-8")
            print(
                json.dumps(
                    {
                        "event": "embedding_batch_done",
                        "provider": provider,
                        "model": self._provider_model(provider),
                        "label": label,
                        "task_type": task_type,
                        "batch": batch_no,
                        "batch_total": batch_total,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        return [vec if vec is not None else [] for vec in out]

    def embed_many(self, texts: list[str], task_type: str, *, label: str = "") -> list[list[float]]:
        provider = self.active_provider
        try:
            return self._embed_many_with_provider(provider, texts, task_type, label=label)
        except Exception as exc:  # noqa: BLE001
            if provider == "gemini" and self.fallback_provider == "qwen":
                error = {
                    "provider": "gemini",
                    "model": self.model,
                    "label": label,
                    "task_type": task_type,
                    "fallback": "qwen",
                    "error": str(exc),
                }
                self.errors.append(error)
                print(json.dumps({"event": "embedding_provider_fallback", **error}, ensure_ascii=False), flush=True)
                self.active_provider = "qwen"
                return self._embed_many_with_provider("qwen", texts, task_type, label=label)
            if self.fallback_on_error:
                self.errors.append(
                    {
                        "provider": provider,
                        "model": self._provider_model(provider),
                        "label": label,
                        "task_type": task_type,
                        "fallback": "lexical_only",
                        "error": str(exc),
                    }
                )
                return [[] for _ in texts]
            raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--dataset-root", required=True)
    parser.add_argument("--rewrite-jsonl")
    parser.add_argument("--field-rewrite-jsonl")
    parser.add_argument("--embedding-model", default="gemini-embedding-001")
    parser.add_argument("--fallback-embedding-provider", choices=["none", "qwen"], default="qwen")
    parser.add_argument("--qwen-embedding-model", default="text-embedding-v4")
    parser.add_argument("--qwen-embedding-endpoint", default=QWEN_EMBEDDINGS_URL)
    parser.add_argument("--qwen-api-key")
    parser.add_argument("--qwen-batch-size", type=int, default=10)
    parser.add_argument("--semantic-threshold", type=float, default=0.42)
    parser.add_argument("--lexical-threshold", type=float, default=0.45)
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--cache-dir")
    parser.add_argument("--api-key")
    parser.add_argument("--proxy", default=None)
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    parser.add_argument("--max-retries", type=int, default=5)
    parser.add_argument("--fallback-on-embedding-error", action="store_true", help="Continue with lexical-only matches when an embedding batch fails.")
    parser.add_argument("--cache-only", action="store_true", help="Use existing embedding cache only; missing vectors become lexical-only fallbacks.")
    parser.add_argument("--usage-jsonl", default=None)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    rows = load_jsonl(args.manifest)
    rewrites = load_rewrite_results(args.rewrite_jsonl) if args.rewrite_jsonl else {}
    field_rewrites = load_field_rewrites(args.field_rewrite_jsonl) if args.field_rewrite_jsonl else {}
    matcher = FieldMatcher(args.dataset_root, enable_semantic=False)
    qwen_api_key = _resolve_qwen_api_key(args.qwen_api_key)
    embedder = RoutedEmbedder(
        model=args.embedding_model,
        api_key=_resolve_api_key(args.api_key),
        fallback_provider=args.fallback_embedding_provider,
        qwen_model=args.qwen_embedding_model,
        qwen_api_key=qwen_api_key,
        qwen_endpoint=args.qwen_embedding_endpoint,
        qwen_batch_size=args.qwen_batch_size,
        proxy=_resolve_proxy(args.proxy),
        cache_dir=Path(args.cache_dir) if args.cache_dir else None,
        batch_size=args.batch_size,
        timeout_s=args.timeout_seconds,
        max_retries=args.max_retries,
        fallback_on_error=args.fallback_on_embedding_error,
        cache_only=args.cache_only,
    )

    def _rewrite_digest(db_id: str) -> str:
        return hashlib.sha1(
            json.dumps(field_rewrites.get(db_id, {}), sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()

    def _candidate_cache_key(db_id: str, provider: str) -> str:
        return f"{db_id}:{provider}:{_rewrite_digest(db_id)}"

    candidate_cache: dict[str, tuple[list[Any], list[list[float]], str]] = {}
    output_by_qid: dict[int, dict[str, Any]] = {}
    usage_rows: list[dict[str, Any]] = []
    row_contexts: list[dict[str, Any]] = []
    rows_by_db: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for row in rows:
        qid = int(row["qid"])
        db_id = str(row["db_id"])
        rewrite = rewrites.get(qid, {})
        source_texts = [
            str(row.get("question", "")),
            str(row.get("evidence", "")),
            str(rewrite.get("rewritten_query", "")),
            str(rewrite.get("rewritten_evidence", "")),
        ]
        query_text = " ".join(text for text in source_texts if text).strip()
        context = {
            "row": row,
            "qid": qid,
            "db_id": db_id,
            "source_texts": source_texts,
            "query_text": query_text,
        }
        row_contexts.append(context)
        rows_by_db[db_id].append(context)

    def _ensure_candidate_vectors(db_id: str) -> tuple[list[Any], list[list[float]], str]:
        provider = embedder.active_provider
        cache_key = _candidate_cache_key(db_id, provider)
        if cache_key in candidate_cache:
            return candidate_cache[cache_key]

        candidates = matcher._build_candidates(db_id, field_rewrites=field_rewrites.get(db_id))
        candidate_texts = [" ; ".join(c.texts) for c in candidates]
        candidate_provider_before = embedder.active_provider
        candidate_vecs = (
            embedder.embed_many(
                candidate_texts,
                "RETRIEVAL_DOCUMENT",
                label=f"{db_id}:candidate_fields",
            )
            if candidate_texts
            else []
        )
        candidate_provider = embedder.last_provider
        actual_cache_key = _candidate_cache_key(db_id, candidate_provider)
        candidate_cache[actual_cache_key] = (candidates, candidate_vecs, candidate_provider)
        usage_rows.append(
            {
                "prompt_id": f"{db_id}:candidate_fields",
                "status": "ok",
                "provider": candidate_provider,
                "model": embedder._provider_model(candidate_provider),
                "provider_switched": candidate_provider_before != candidate_provider,
                "embedding_items": len(candidate_texts),
                "input_chars": sum(len(text) for text in candidate_texts),
                "estimated_input_tokens": sum((len(text) + 3) // 4 for text in candidate_texts),
            }
        )
        return candidate_cache[actual_cache_key]

    for db_id, db_contexts in rows_by_db.items():
        candidates, candidate_vecs, candidate_provider = _ensure_candidate_vectors(db_id)

        query_contexts = [ctx for ctx in db_contexts if ctx["query_text"]]
        query_vec_by_qid: dict[int, list[float]] = {}
        if query_contexts:
            query_texts = [ctx["query_text"] for ctx in query_contexts]
            query_provider_before = embedder.active_provider
            query_vecs = embedder.embed_many(
                query_texts,
                "RETRIEVAL_QUERY",
                label=f"{db_id}:queries",
            )
            query_provider = embedder.last_provider
            usage_rows.append(
                {
                    "prompt_id": f"{db_id}:queries",
                    "status": "ok",
                    "provider": query_provider,
                    "model": embedder._provider_model(query_provider),
                    "provider_switched": query_provider_before != query_provider,
                    "embedding_items": len(query_texts),
                    "input_chars": sum(len(text) for text in query_texts),
                    "estimated_input_tokens": sum((len(text) + 3) // 4 for text in query_texts),
                }
            )
            if query_provider != candidate_provider:
                # Query fallback changed providers. Recompute candidate vectors
                # under the query provider so cosine similarity never mixes dimensions.
                candidates, candidate_vecs, candidate_provider = _ensure_candidate_vectors(db_id)
            query_vec_by_qid = {
                int(ctx["qid"]): vec
                for ctx, vec in zip(query_contexts, query_vecs)
            }

        for context in db_contexts:
            qid = int(context["qid"])
            source_texts = context["source_texts"]
            query_vec = query_vec_by_qid.get(qid, [])
            regex_hits = []
            semantic_hits = []
            final_columns = set()
            for cand, cand_vec in zip(candidates, candidate_vecs):
                lexical_score, regex_hit = matcher._lexical_score(source_texts, cand)
                semantic_score = _dot(query_vec, cand_vec) if query_vec else 0.0
                if regex_hit or lexical_score >= args.lexical_threshold:
                    regex_hits.append(
                        {
                            "table": cand.table,
                            "column": cand.column,
                            "score": round(max(lexical_score, semantic_score), 4),
                        }
                    )
                    final_columns.add((cand.table, cand.column))
                elif semantic_score >= args.semantic_threshold:
                    semantic_hits.append(
                        {"table": cand.table, "column": cand.column, "score": round(semantic_score, 4)}
                    )
                    final_columns.add((cand.table, cand.column))
            output_by_qid[qid] = {
                "qid": qid,
                "regex_hits": regex_hits,
                "semantic_hits": semantic_hits,
                "columns": sorted([[t, c] for t, c in final_columns]),
                "metadata": {
                    "semantic_threshold": args.semantic_threshold,
                    "lexical_threshold": args.lexical_threshold,
                    "embedding_provider": candidate_provider,
                    "embedding_model": embedder._provider_model(candidate_provider),
                    "primary_embedding_provider": "gemini",
                    "fallback_embedding_provider": args.fallback_embedding_provider,
                    "embedding_cache_only": args.cache_only,
                    "embedding_fallback_on_error": args.fallback_on_embedding_error,
                    "embedding_error_count": len(embedder.errors),
                    "query_embedding_batched": True,
                },
            }

    outputs = [output_by_qid[int(context["qid"])] for context in row_contexts]
    write_jsonl(outputs, args.output)
    if embedder.errors:
        error_path = Path(str(args.output) + ".embedding_errors.jsonl")
        write_jsonl(embedder.errors, error_path)
        print(f"Wrote {len(embedder.errors)} embedding fallback errors to {error_path}")
    if args.usage_jsonl:
        usage_path = Path(args.usage_jsonl)
        usage_path.parent.mkdir(parents=True, exist_ok=True)
        write_jsonl(usage_rows, usage_path)
    print(f"Wrote {len(outputs)} routed-embedding stage4 rows to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
