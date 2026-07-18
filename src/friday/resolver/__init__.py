"""Capability Resolver (Milestone 9.3).

The ONLY layer permitted to map a Task to a Worker. Deterministic capability
matching only — no execution, no LLM, no repository access, no worker
invocation. Execution (Runtime) and scheduling (Scheduler) are future
milestones; the Resolver decides eligibility and assigns, nothing more.
"""

from __future__ import annotations

from .confidence import ConfidenceInputs, derive_confidence
from .engine import CapabilityResolver, ResolveResult
from .models import (
    Assignment,
    ResolutionResult,
    ResolutionStatus,
    SCHEMA_VERSION,
    ScoreBreakdown,
    SelectionStrategy,
)
from .resolver import rank_workers, score_worker, select_assignment

__all__ = [
    "CapabilityResolver",
    "ResolveResult",
    "Assignment",
    "ResolutionResult",
    "ResolutionStatus",
    "SelectionStrategy",
    "ScoreBreakdown",
    "SCHEMA_VERSION",
    "ConfidenceInputs",
    "derive_confidence",
    "rank_workers",
    "score_worker",
    "select_assignment",
]
