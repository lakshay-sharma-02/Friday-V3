"""Deterministic confidence for a Capability Resolver assignment (M9.3).

Confidence is DERIVED, never guessed. It combines:
  - Capability coverage: fraction of required capabilities the worker actually has.
  - Task coverage: whether the worker supports the task's task_type.
  - Historical compatibility: prior successful assignments of this worker
    (read from resolver_history — append-only, never an LLM).
  - Worker confidence: the registry-assigned profile confidence.

No LLM, no randomness, no time-dependent heuristics. Same inputs -> same band.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

# Confidence bands, highest to lowest. Used for stable to_text() ordering.
BANDS = ("high", "medium", "low")


@dataclass
class ConfidenceInputs:
    """Everything needed to derive an assignment confidence band."""

    capability_coverage: float          # 0.0..1.0 (matched / required)
    task_supported: bool                # worker supports the task_type
    plan_supported: bool                # worker supports the plan_type
    worker_confidence: str              # registry 'high' | 'medium' | 'low'
    successful_history: int = 0         # prior resolved assignments for worker
    required_count: int = 0             # number of required capabilities


def _band_rank(band: str) -> int:
    return BANDS.index(band) if band in BANDS else len(BANDS)


def derive_confidence(inp: ConfidenceInputs) -> str:
    """Return 'high' | 'medium' | 'low' deterministically from inputs.

    Rules (deterministic, documented):
      - If no capabilities were required, confidence is driven by task/plan
        support and worker confidence.
      - high: full capability coverage AND task supported AND worker confidence
        is 'high', OR (full coverage AND task supported AND >=3 successful
        history).
      - low: any mandatory capability missing, OR task unsupported with no
        capability coverage.
      - otherwise medium.
    """
    # A missing mandatory capability is the strongest negative signal.
    if inp.required_count > 0 and inp.capability_coverage < 1.0:
        # Partial coverage of mandatory caps -> at most medium, lower if poor.
        if inp.capability_coverage == 0.0:
            return "low"
        return "low" if not inp.task_supported else "medium"

    if inp.required_count == 0:
        # No mandatory caps: confidence from task/plan fit + worker profile.
        if inp.task_supported and inp.worker_confidence == "high":
            return "high"
        if inp.task_supported or inp.plan_supported:
            return "medium" if inp.worker_confidence != "low" else "low"
        return "low"

    # Full mandatory capability coverage.
    if inp.task_supported and inp.worker_confidence == "high":
        return "high"
    if inp.task_supported and inp.successful_history >= 3:
        return "high"
    if inp.task_supported or inp.plan_supported:
        return "medium"
    return "medium" if inp.worker_confidence != "low" else "low"


def confidence_at_least(a: str, b: str) -> bool:
    """True iff band `a` is at least as strong as band `b`."""
    return _band_rank(a) <= _band_rank(b)
