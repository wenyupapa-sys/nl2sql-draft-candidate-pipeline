from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .dataset import load_jsonl
from .schema_assets import SchemaAssets


TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")


def tokenize(text: str) -> list[str]:
    return [tok.lower() for tok in TOKEN_RE.findall(text or "")]


def normalize_name(name: str) -> str:
    return re.sub(r"[_\W]+", " ", name).strip().lower()


@dataclass
class CandidateField:
    table: str
    column: str
    texts: list[str]


class FieldMatcher:
    def __init__(self, dataset_root: str | Path, semantic_model: str = "all-MiniLM-L6-v2", enable_semantic: bool = True):
        self.schema_assets = SchemaAssets(dataset_root)
        self.semantic_model_name = semantic_model
        self._semantic_model = None
        self.enable_semantic = enable_semantic

    def _get_semantic_model(self):
        if not self.enable_semantic:
            return None
        if self._semantic_model is None:
            try:
                from sentence_transformers import SentenceTransformer

                self._semantic_model = SentenceTransformer(self.semantic_model_name)
            except Exception:
                self._semantic_model = None
        return self._semantic_model

    def _build_candidates(
        self,
        db_id: str,
        field_rewrites: dict[str, dict[str, list[str]]] | None = None,
    ) -> list[CandidateField]:
        meta = self.schema_assets.load_tables_meta(db_id)
        table_names = meta["table_names_original"]
        rows = self.schema_assets._column_records(meta)
        meanings = self.schema_assets.load_column_meanings_map(db_id)

        candidates: list[CandidateField] = []
        for row in rows:
            if row["table_idx"] < 0:
                continue
            table = table_names[row["table_idx"]].lower()
            column = row["column_name"].lower()
            texts = [
                normalize_name(table),
                normalize_name(column),
                f"{normalize_name(table)} {normalize_name(column)}",
                normalize_name(meanings.get(f"{table}.{row['column_name']}", "")),
            ]
            if field_rewrites:
                aliases = (
                    field_rewrites.get(table, {}).get(row["column_name"], [])
                    or field_rewrites.get(table, {}).get(column, [])
                )
                texts.extend(normalize_name(alias) for alias in aliases if alias)
            dedup = []
            seen = set()
            for text in texts:
                if text and text not in seen:
                    seen.add(text)
                    dedup.append(text)
            candidates.append(CandidateField(table=table, column=column, texts=dedup))
        return candidates

    def _lexical_score(self, source_texts: list[str], candidate: CandidateField) -> tuple[float, bool]:
        regex_hit = False
        score = 0.0
        source_blob = " ".join(source_texts).lower()
        source_tokens = set(tokenize(source_blob))
        for text in candidate.texts:
            if not text:
                continue
            if text in source_blob:
                regex_hit = True
                score = max(score, 1.0)
            cand_tokens = set(tokenize(text))
            if not cand_tokens:
                continue
            overlap = len(source_tokens & cand_tokens) / len(cand_tokens)
            score = max(score, overlap * 0.8)
        return score, regex_hit

    def _semantic_scores(self, query_text: str, candidates: list[CandidateField]) -> dict[tuple[str, str], float]:
        if not query_text.strip():
            return {}
        model = self._get_semantic_model()
        if model is None:
            return {}
        candidate_texts = [" ; ".join(c.texts) for c in candidates]
        q_emb = model.encode([query_text], normalize_embeddings=True)
        c_emb = model.encode(candidate_texts, normalize_embeddings=True)
        scores = (c_emb @ q_emb[0]).tolist()
        return {(c.table, c.column): float(s) for c, s in zip(candidates, scores)}

    def match(
        self,
        qid: int,
        db_id: str,
        question: str,
        evidence: str,
        rewritten_query: str = "",
        rewritten_evidence: str = "",
        field_rewrites: dict[str, dict[str, list[str]]] | None = None,
        semantic_threshold: float = 0.42,
        lexical_threshold: float = 0.45,
    ) -> dict:
        candidates = self._build_candidates(db_id, field_rewrites=field_rewrites)
        source_texts = [question, evidence, rewritten_query, rewritten_evidence]
        semantic_query = " ".join(text for text in source_texts if text)
        semantic_scores = self._semantic_scores(semantic_query, candidates)

        regex_hits = []
        semantic_hits = []
        final_columns = set()
        for cand in candidates:
            lexical_score, regex_hit = self._lexical_score(source_texts, cand)
            semantic_score = semantic_scores.get((cand.table, cand.column), 0.0)
            if regex_hit or lexical_score >= lexical_threshold:
                regex_hits.append(
                    {
                        "table": cand.table,
                        "column": cand.column,
                        "score": round(max(lexical_score, semantic_score), 4),
                    }
                )
                final_columns.add((cand.table, cand.column))
            elif semantic_score >= semantic_threshold:
                semantic_hits.append(
                    {
                        "table": cand.table,
                        "column": cand.column,
                        "score": round(semantic_score, 4),
                    }
                )
                final_columns.add((cand.table, cand.column))
        return {
            "qid": qid,
            "regex_hits": regex_hits,
            "semantic_hits": semantic_hits,
            "columns": sorted([[t, c] for t, c in final_columns]),
            "metadata": {
                "semantic_threshold": semantic_threshold,
                "lexical_threshold": lexical_threshold,
            },
        }


def load_rewrite_results(path: str | Path) -> dict[int, dict]:
    path = Path(path)
    if not path.exists():
        return {}
    out = {}
    for row in load_jsonl(path):
        out[int(row["qid"])] = row
    return out


def load_field_rewrites(path: str | Path) -> dict[str, dict[str, list[str]]]:
    path = Path(path)
    if not path.exists():
        return {}
    out: dict[str, dict[str, list[str]]] = {}
    for row in load_jsonl(path):
        db_id = row.get("db_id")
        payload = row.get("field_rewrites") or {}
        if db_id and isinstance(payload, dict):
            out[str(db_id)] = payload
    return out
