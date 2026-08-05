"""Evidence models — the reasoning layer's ground truth contract.

Wave 9 law: *"every answer cites evidence; empty evidence → 'I don't
know yet' (never fabrication)."* :class:`Evidence` is one citable fact
(source + claim + when), :class:`Answer` is the final response with the
evidence that backs it.

A citation is just the evidence rendered: ``"source: claim (when)"`` —
so a surface (voice, CLI, web) can quote exactly where a claim came
from, and nothing is ever asserted without a backing row.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .question import QuestionType


@dataclass(frozen=True)
class Evidence:
    """One citable fact backing an answer.

    ``source`` is the provider/table (e.g. ``v4.actions``), ``claim`` is
    the human-readable fact, ``when`` is an ISO timestamp when known.
    """

    source: str
    claim: str
    when: str = ""

    def cite(self) -> str:
        """Render the citation line (used in answers and CLI output)."""
        if self.when:
            return f"{self.source} — {self.claim} ({self.when[:16]})"
        return f"{self.source} — {self.claim}"


@dataclass
class Answer:
    """A complete, evidence-cited response to a question."""

    question: str
    text: str
    evidence: list[Evidence] = field(default_factory=list)
    question_type: QuestionType = QuestionType.UNKNOWN
    confidence: float = 0.0
    known: bool = True

    @property
    def citations(self) -> list[str]:
        return [e.cite() for e in self.evidence]

    def to_dict(self) -> dict:
        return {
            "question": self.question,
            "text": self.text,
            "evidence": [e.cite() for e in self.evidence],
            "question_type": self.question_type.value,
            "confidence": self.confidence,
            "known": self.known,
        }


#: Rendered when no provider has evidence — the honest answer.
UNKNOWN_TEXT = "I don't know yet — I don't have evidence about that."


__all__ = ["Answer", "Evidence", "UNKNOWN_TEXT"]
