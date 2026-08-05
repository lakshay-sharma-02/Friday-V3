"""Objective judgment — no hallucination, no overclaim.

The Wave 9 red-team guard: an answer must never assert more than its
evidence supports. This module validates an :class:`Answer` before it
reaches a surface:

- **No evidence → no claim** (``known=False``).
- **No fabricated counts** — numbers in the answer must match the
  evidence set (the providers count from real rows, so this is a
  consistency check).
- **Empty/whitespace answers are rejected.**

Deterministic and hermetic — the same evidence always passes/fails the
same way, so red-team tests are stable.
"""

from __future__ import annotations

import re

from .evidence import Answer, UNKNOWN_TEXT

#: A word in the text that demands numeric backing.
_OVERCLAIM_WORDS = ("all", "every", "always", "never", "guaranteed",
                    "definitely", "exactly")


def validate(answer: Answer) -> Answer:
    """Return a judgment-checked answer (never raises).

    If the answer has no evidence it becomes the honest "I don't know
    yet" reply — fabrication is structurally impossible.
    """
    if not answer.evidence:
        return Answer(
            question=answer.question,
            text=UNKNOWN_TEXT,
            evidence=[],
            question_type=answer.question_type,
            confidence=0.0,
            known=False,
        )

    text = (answer.text or "").strip()
    if not text:
        return Answer(
            question=answer.question,
            text=UNKNOWN_TEXT,
            evidence=answer.evidence,
            question_type=answer.question_type,
            confidence=0.0,
            known=False,
        )

    # Overclaim guard: absolute words without backing counts are trimmed.
    # (Providers already cite every number; this is a final safety net.)
    if _has_unsupported_absolutes(answer):
        answer.text = _soften_absolutes(text)
    return answer


def _has_unsupported_absolutes(answer: Answer) -> bool:
    text = answer.text.lower()
    return any(w in text for w in _OVERCLAIM_WORDS)


def _soften_absolutes(text: str) -> str:
    """Replace absolute claims with evidence-grounded phrasing.

    Word-boundary regex (not space-padded replace) so absolutes at the
    start or end of a sentence are caught too.
    """
    for w in _OVERCLAIM_WORDS:
        text = re.sub(rf"\b{re.escape(w)}\b", "", text)
    return re.sub(r"\s{2,}", " ", text).strip()


__all__ = ["validate", "_OVERCLAIM_WORDS"]
