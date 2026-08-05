"""Reasoning Layer — evidence-cited answers (Wave 9).

The answer engine behind ASK intents: "what's the status of my
projects?" no longer degrades to a canned status line — it becomes a
real, evidence-cited answer built from V4 state (missions, actions,
memories) plus V3's read-only bridge when present.

Flow:
    parse (question.py)      → what kind of question + target
    providers (providers.py) → evidence per question type (registry)
    judgment (judgment.py)   → no evidence → "I don't know yet"
    engine (engine.py)       → answer() — the single entry point

Law: *every answer cites evidence; empty evidence → "I don't know
yet".* Fabrication is structurally impossible — the judgment pass
rewrites evidence-less answers before they reach a surface.

**Status:** Wave 9 — built (2026-08). Pure stdlib, deterministic,
hermetic tests. Imports stay guarded so importing this package never
crashes the rest of Friday V4.

Usage:
    from friday_v6.reasoning import answer
    a = answer("what's the status of my projects?", conn=conn)
    print(a.text)         # evidence-cited natural language
    print(a.citations)    # ["v4.missions — 3 missions — ..."]
"""

from __future__ import annotations

try:
    from .engine import answer, parse
    from .evidence import Answer, Evidence
    from .judgment import validate
    from .question import Question, QuestionType, classify
    from .providers import PROVIDERS, llm_provider
    _REASONING_AVAILABLE = True
except ImportError:  # pragma: no cover - defensive stub
    answer = None  # type: ignore
    parse = None  # type: ignore
    Answer = None  # type: ignore
    Evidence = None  # type: ignore
    validate = None  # type: ignore
    Question = None  # type: ignore
    QuestionType = None  # type: ignore
    classify = None  # type: ignore
    PROVIDERS = ()  # type: ignore
    llm_provider = None  # type: ignore
    _REASONING_AVAILABLE = False


def is_available() -> bool:
    """Whether the reasoning layer is implemented yet."""
    return _REASONING_AVAILABLE


__all__ = [
    "answer",
    "parse",
    "Answer",
    "Evidence",
    "validate",
    "Question",
    "QuestionType",
    "classify",
    "PROVIDERS",
    "llm_provider",
    "is_available",
    "_REASONING_AVAILABLE",
]
