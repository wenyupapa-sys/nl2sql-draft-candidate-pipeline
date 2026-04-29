from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class StageInput:
    qid: int
    question: str
    evidence: str
    db_id: str
    difficulty: str
    schema_text: str
    column_meanings: dict[str, str]
    prior_columns: set[tuple[str, str]] = field(default_factory=set)


@dataclass
class StageOutput:
    qid: int
    provenance: str
    columns: set[tuple[str, str]] = field(default_factory=set)
    scores: dict[tuple[str, str], float] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "qid": self.qid,
            "provenance": self.provenance,
            "columns": [[t, c] for t, c in sorted(self.columns)],
            "scores": {f"{t}.{c}": s for (t, c), s in sorted(self.scores.items())},
            "metadata": self.metadata,
        }
