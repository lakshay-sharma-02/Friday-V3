"""Answer engine — evidence-scoped Q&A (the Wave 9 reasoning entry point).

:func:`answer` is the single entry point the surfaces call (voice
router, ``friday6 ask``, web chat). Flow:

    parse (question.py) → classify type + target
    providers (providers.py) → gather evidence (V4 state + V3 bridge)
    judgment (judgment.py) → no evidence → "I don't know yet"

The result is always an :class:`Answer` — never an exception. An answer
without evidence is *impossible*: judgment rewrites it to the honest
unknown reply. That is the wave's no-hallucination guarantee.
"""

from __future__ import annotations

import logging
from typing import Optional

from .evidence import Answer
from .judgment import validate
from .providers import PROVIDERS, llm_provider
from .question import Question, QuestionType, parse

logger = logging.getLogger("friday_v6.reasoning.engine")


def answer(question_text: str, conn=None,
           providers: Optional[tuple] = None,
           history: Optional[list[dict]] = None,
           llm: Optional[object] = None) -> Answer:
    """Answer ``question_text`` with evidence (never raises).

    Args:
        question_text: The ASK utterance, e.g. "what's the status of my
            projects?".
        conn: V4 DB connection. ``None`` is allowed — providers just
            find no evidence and the honest unknown answer is returned.
        providers: Optional provider override (tests inject fakes).
        history: Recent conversation exchanges (oldest first) for
            follow-up context — threaded into the Wave 13 LLM synthesis
            prompt (``friday6 ask`` is conversation-capable).
        llm: Optional LLM client for Wave 13 synthesis. ``None`` consults
            the ``FRIDAY_V4_LLM`` env opt-in; an injected client is an
            explicit opt-in (tests and surfaces pass their own).

    Returns:
        An :class:`Answer`. ``known=True`` only when evidence backs it.
        With the LLM enabled, ``text`` may be a conversational synthesis
        across the same evidence — citations are never dropped.
    """
    question: Question = parse(question_text or "")
    if question.type == QuestionType.UNKNOWN:
        return Answer(question_text or "", _unknown_text(),
                      question_type=QuestionType.UNKNOWN, confidence=0.0,
                      known=False)

    chain = providers if providers is not None else PROVIDERS
    best: Optional[Answer] = None
    for provider in chain:
        try:
            candidate = provider(question, conn)
        except Exception as exc:
            logger.debug(f"provider {getattr(provider, '__name__', '?')} "
                         f"failed: {exc}")
            candidate = None
        if candidate is None:
            continue
        if best is None or candidate.confidence > best.confidence:
            best = candidate

    if best is None:
        return Answer(question_text or "", _unknown_text(),
                      question_type=question.type, confidence=0.0,
                      known=False)

    # Wave 13 — LLM synthesis over the deterministic best (Law 6:
    # enhances, never gates). Any failure keeps the deterministic floor;
    # the enhanced answer always carries the same evidence.
    try:
        enhanced = llm_provider(question, conn, best=best,
                                history=history, llm=llm)
        if enhanced is not None:
            best = enhanced
    except Exception as exc:
        logger.debug(f"llm enhancement failed: {exc}")

    return validate(best)


def _unknown_text() -> str:
    return ("I don't know yet — I don't have evidence about that. "
            "Ask me about project status, recent activity, mission "
            "progress, or what I remember.")


__all__ = ["Answer", "Question", "QuestionType", "answer", "parse"]
