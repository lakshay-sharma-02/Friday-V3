"""EnsembleReasoner — thin wrapper around services/llm._call().

Originally this module fired 2-3 models in parallel and tried to measure
agreement as a confidence proxy. That approach was:

  1. Expensive — 3 parallel API calls, 25s latency, for every ask()
  2. Redundant — services/llm.py already fires all providers concurrently
     via _parallel_call() and returns the fastest response.
  3. Fake confidence — word-overlap Jaccard similarity between responses
     from different models is NOT a real confidence signal.
  4. Duplicate HTTP — _call_one() reimplemented urllib logic that
     services/llm.py already had.

Now EnsembleReasoner.reason() delegates directly to services/llm._call(),
which fires all configured providers concurrently (in _parallel_call()),
returns the first successful response, and has an LRU cache.

The EnsembleResult dataclass is preserved for backward compat with
cli.py's ``friday reason`` command and any tests. Confidence defaults to
1.0 / "high" since the parallel probe gives us the best available answer
— no word-overlap pseudo-confidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# Agreement levels (preserved for backward compat)
# ---------------------------------------------------------------------------


class AgreementLevel(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


# ---------------------------------------------------------------------------
# Result type (preserved for backward compat with cli.py / ask.py)
# ---------------------------------------------------------------------------


@dataclass
class EnsembleResult:
    """The output of a single reasoning pass.

    Backward-compatible with the original EnsembleResult shape, but
    ``confidence`` is always 1.0 (not a word-overlap proxy) and
    ``agreement`` is always ``high``.

    ``all_responses`` and ``primary_model`` contain the single model's
    info since we now delegate to the fastest available model (via
    services/llm._parallel_call) rather than firing multiple models.
    """

    text: str = ""
    confidence: float = 1.0
    agreement: str = "high"
    all_responses: dict[str, str] = field(default_factory=dict)
    response_count: int = 1
    agreement_score: float = 1.0
    primary_model: str = "llm"


# ---------------------------------------------------------------------------
# EnsembleReasoner — now a thin wrapper around services/llm._call()
# ---------------------------------------------------------------------------


class EnsembleReasoner:
    """Call the LLM with a prompt and return the result.

    Previously this fired 2-3 models in parallel and measured agreement.
    Now it delegates to ``services/llm._call()``, which fires all
    configured providers concurrently (``_parallel_call``), returns the
    first successful response, and caches repeated calls.

    Usage::

        er = EnsembleReasoner()
        result = er.reason(
            system="You are a helpful assistant.",
            user="What should I work on next?",
        )
        print(f"Answer: {result.text}")
    """

    def __init__(self, timeout_per_model: int = 30):
        self._timeout = timeout_per_model

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def reason(
        self,
        system: str,
        user: str,
        response_type: str = "free",
    ) -> EnsembleResult:
        """Call the LLM and return the result.

        Delegates to ``services/llm._call()``, which fires all configured
        providers concurrently via ``_parallel_call()`` and returns the
        first successful response.

        Args:
            system: The system prompt (instructions, persona, constraints).
            user: The user message / question.
            response_type: Ignored (preserved for backward compat).

        Returns:
            An ``EnsembleResult`` with the answer, confidence=1.0, and
            agreement="high" (since we return the best available answer
            rather than fabricating pseudo-confidence from word overlap).
        """
        from ..services.llm import _call

        text = _call(system, user)
        if text is None:
            return EnsembleResult(
                text="",
                confidence=0.0,
                agreement="low",
                response_count=0,
                agreement_score=0.0,
                primary_model="",
            )
        return EnsembleResult(
            text=text,
            confidence=1.0,
            agreement="high",
            all_responses={"llm": text},
            response_count=1,
            agreement_score=1.0,
            primary_model="llm",
        )

    def reason_structured(
        self,
        system: str,
        user: str,
        expected_fields: Optional[list[str]] = None,
    ) -> EnsembleResult:
        """Convenience wrapper for JSON-structured responses.

        Preserved for backward compat. Just calls ``reason()`` since
        the underlying ``services/llm._call()`` already handles any
        response format the model produces.
        """
        return self.reason(system, user, response_type="structured")

    def confidence_label(self, confidence: float) -> str:
        """Return a human-readable confidence label for an answer.

        These map directly to the FRIDAY ideology's calibrated language:
            > 0.8  — "I'd bet on this"
            0.6-0.8 — "I'm fairly sure"
            0.4-0.6 — "My best guess"
            < 0.4   — "I'm not confident enough to say"
        """
        if confidence >= 0.8:
            return "I'd bet on this"
        if confidence >= 0.6:
            return "I'm fairly sure"
        if confidence >= 0.4:
            return "My best guess"
        return "I'm not confident enough to say"
