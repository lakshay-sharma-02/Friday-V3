"""Confidence — ambiguity handling for the ONE NLU point (Wave 13a).

The LLM already reports ``needs_clarification`` + a clarification text;
the deterministic fallback needs its own ambiguity check. Both produce
an :class:`Assessment` consumed by ``resolver.resolve()`` — the
"did you mean…" path is shared.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from .intent import Intent, IntentResult


@dataclass
class Assessment:
    """Ambiguity verdict for one utterance."""

    needs_clarification: bool = False
    clarification: str = ""


def assess(result: IntentResult,
           llm_clarification: Optional[str] = None) -> Assessment:
    """Ambiguity from the LLM, else the rules fallback heuristic.

    The LLM path passes its ``needs_clarification`` straight through
    (with its clarification text). The fallback heuristic only flags
    EXECUTE without a resolvable action type or an empty UNKNOWN.
    """
    if llm_clarification:
        return Assessment(needs_clarification=True,
                          clarification=llm_clarification)
    if result.intent == Intent.UNKNOWN:
        return Assessment(needs_clarification=True,
                          clarification="I didn't catch that — "
                                        "could you rephrase it?")
    if result.intent == Intent.EXECUTE and not result.action_type:
        return Assessment(needs_clarification=True,
                          clarification="What would you like me to run?")
    return Assessment()


__all__ = ["Assessment", "assess"]
