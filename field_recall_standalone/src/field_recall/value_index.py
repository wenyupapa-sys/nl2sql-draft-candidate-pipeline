from __future__ import annotations

import os
import re
import sqlite3
from typing import Dict, List

import jieba.posseg as pseg


STOP_WORDS = {
    "what", "which", "how", "many", "much", "where", "when", "who", "list", "show", "find",
    "table", "column", "all", "each", "every", "total", "count", "average", "sum", "maximum",
    "minimum", "most", "least", "more", "less", "than", "between", "from", "to", "in", "on",
    "at", "of", "for", "with", "by", "that", "this", "those", "these", "result", "results",
}
KEEP_POS = {"n", "nr", "ns", "nt", "nz", "vn", "m", "eng"}
SKIP_COL_PATTERNS = re.compile(r"(id|url|email|image)", re.IGNORECASE)


class KeywordExtractor:
    def extract(self, question: str, evidence: str = "") -> List[str]:
        combined = f"{question} {evidence}".strip()
        seen = set()
        keywords = []

        def _add(kw: str):
            s = kw.strip()
            if s and s.lower() not in seen and s.lower() not in STOP_WORDS and len(s) > 1:
                seen.add(s.lower())
                keywords.append(s)

        for m in re.finditer(r"""['"'\u2018\u2019\u201c\u201d]([^'"'\u2018\u2019\u201c\u201d]+)['"'\u2018\u2019\u201c\u201d]""", combined):
            _add(m.group(1))
        for m in re.finditer(r"\b(\d{4,})\b", combined):
            _add(m.group(1))
        for m in re.finditer(r"(\w+)\s*=\s*'([^']+)'", evidence):
            _add(m.group(2))
        for word, flag in pseg.cut(combined):
            if flag in KEEP_POS:
                _add(word)
        return keywords[:8]


class TextValueRetriever:
    def __init__(self, threshold: float = 0.8, chroma_path: str = "data/chroma_cell_index"):
        self.threshold = threshold
        self.chroma_path = chroma_path
        self.extractor = KeywordExtractor()
        self._client = None
        self._ef = None

    def _init(self):
        if self._client is not None:
            return
        import chromadb
        from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

        self._client = chromadb.PersistentClient(path=self.chroma_path)
        self._ef = SentenceTransformerEmbeddingFunction(model_name="all-MiniLM-L6-v2")

    def retrieve(self, question: str, evidence: str, db_id: str, top_k: int = 5) -> List[Dict]:
        self._init()
        keywords = self.extractor.extract(question, evidence)
        return self.retrieve_terms(keywords, db_id, top_k=top_k, threshold=self.threshold)

    def retrieve_terms(
        self,
        terms: List[str],
        db_id: str,
        top_k: int = 5,
        threshold: float | None = None,
    ) -> List[Dict]:
        self._init()
        threshold = self.threshold if threshold is None else threshold
        try:
            collection = self._client.get_collection(name=f"cell_{db_id}", embedding_function=self._ef)
        except Exception:
            return []
        results = []
        seen = set()
        for kw in terms:
            try:
                resp = collection.query(query_texts=[kw], n_results=top_k)
            except Exception:
                continue
            if not resp["distances"] or not resp["distances"][0]:
                continue
            for dist, meta in zip(resp["distances"][0], resp["metadatas"][0]):
                similarity = 1.0 - dist
                if similarity < threshold:
                    continue
                key = (meta["table"], meta["column"], meta["value"])
                if key in seen:
                    continue
                seen.add(key)
                results.append(
                    {
                        "keyword": kw,
                        "table": meta["table"],
                        "column": meta["column"],
                        "value": meta["value"],
                        "similarity": round(similarity, 4),
                    }
                )
        return results


def extract_text_values_from_sqlite(sqlite_path: str) -> list[dict]:
    conn = sqlite3.connect(sqlite_path)
    conn.text_factory = lambda b: b.decode("utf-8", errors="replace")
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
    tables = [row[0] for row in cur.fetchall()]
    docs = []
    for table in tables:
        cur.execute(f'PRAGMA table_info("{table}")')
        for col in cur.fetchall():
            col_name = col[1]
            col_type = (col[2] or "").upper()
            if not any(t in col_type for t in ("TEXT", "CHAR", "VARCHAR")):
                continue
            if SKIP_COL_PATTERNS.search(col_name):
                continue
            try:
                cur.execute(
                    f'SELECT DISTINCT "{col_name}" FROM "{table}" WHERE "{col_name}" IS NOT NULL '
                    f'AND length("{col_name}") <= 256 LIMIT 500'
                )
            except Exception:
                continue
            for row in cur.fetchall():
                value = str(row[0]).strip()
                if value:
                    docs.append({"table": table, "column": col_name, "value": value})
    conn.close()
    return docs
