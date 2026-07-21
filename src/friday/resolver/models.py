"""Capability Resolver models (Milestone 9.3).

The Resolver is the ONLY layer that maps a Task to a Worker. It reads the
Task Graph (tasks + required capabilities) and the Worker Registry (capability
profiles) and produces deterministic Assignments. It executes NOTHING, calls no
LLM, touches no repository, and never invents workers.

These dataclasses are pure data + (de)serialization. All scoring/matching
lives in `resolver.py`; all persistence lives in `engine.py`.

Contract version (Law 24): every persisted assignment carries `schema_version`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional

# Contract version. Bump only on a breaking change to the assignment shape.
SCHEMA_VERSION = "1.0"


class SelectionStrategy(str, Enum):
    """How a task's workers are chosen. The Resolver decides ELIGIBILITY only;
    the future Scheduler decides timing. No execution here."""

    SINGLE = "single"        # exactly one best worker
    PARALLEL = "parallel"    # multiple eligible workers run concurrently
    SEQUENTIAL = "sequential"  # multiple eligible workers run in order

    @classmethod
    def from_str(cls, s: str) -> "SelectionStrategy":
        s = (s or "").strip().lower()
        for k in cls:
            if k.value == s:
                return k
        raise ValueError(f"{cls.__name__} has no member {s!r}")


class ResolutionStatus(str, Enum):
    """Outcome of resolving one task."""

    ASSIGNED = "assigned"
    UNRESOLVED = "unresolved"  # no eligible worker satisfied mandatory caps


@dataclass
class ScoreBreakdown:
    """Transparent, deterministic score components for one (task, worker) pair.

    Every field is a plain number so explanations and tests can assert exact
    values. No hidden logic — the formula is fully reproducible from inputs.
    """

    capability: int = 0
    language: int = 0
    task_type: int = 0
    plan_type: int = 0
    availability: int = 0
    confidence: int = 0
    penalty: int = 0
    # Executor-kind preference (deterministic vs AI). Kept separate from
    # `capability` so the capability explanation stays pure; folded into `total`
    # so ranking prefers deterministic executors without distorting diagnostics.
    executor_pref: int = 0

    @property
    def total(self) -> int:
        return (
            self.capability
            + self.language
            + self.task_type
            + self.plan_type
            + self.availability
            + self.confidence
            + self.executor_pref
            - self.penalty
        )

    def to_dict(self) -> dict:
        return {
            "capability": self.capability,
            "language": self.language,
            "task_type": self.task_type,
            "plan_type": self.plan_type,
            "availability": self.availability,
            "confidence": self.confidence,
            "penalty": self.penalty,
            "executor_pref": self.executor_pref,
            "total": self.total,
        }


@dataclass
class Assignment:
    """One Task -> Worker mapping produced by the Resolver.

    Append-only once persisted (resolver_assignments). `updated_at` may change
    on deterministic re-resolution; the prior state is preserved in
    resolver_history.
    """

    assignment_id: str
    graph_id: str
    task_id: str
    worker_id: Optional[str]          # None when UNRESOLVED
    status: ResolutionStatus
    confidence: str                    # derived, deterministic (e.g. 'high')
    reason: str
    matched_capabilities: List[str] = field(default_factory=list)
    missing_capabilities: List[str] = field(default_factory=list)
    selection_strategy: SelectionStrategy = SelectionStrategy.SINGLE
    schema_version: str = SCHEMA_VERSION
    created_at: str = ""
    updated_at: str = ""

    def to_row(self) -> dict:
        import json
        return {
            "assignment_id": self.assignment_id,
            "graph_id": self.graph_id,
            "task_id": self.task_id,
            "worker_id": self.worker_id,
            "status": self.status.value,
            "confidence": self.confidence,
            "reason": self.reason,
            "matched_capabilities": json.dumps(self.matched_capabilities),
            "missing_capabilities": json.dumps(self.missing_capabilities),
            "selection_strategy": self.selection_strategy.value,
            "schema_version": self.schema_version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def to_dict(self) -> dict:
        return {
            "assignment_id": self.assignment_id,
            "graph_id": self.graph_id,
            "task_id": self.task_id,
            "worker_id": self.worker_id,
            "status": self.status.value,
            "confidence": self.confidence,
            "reason": self.reason,
            "matched_capabilities": list(self.matched_capabilities),
            "missing_capabilities": list(self.missing_capabilities),
            "selection_strategy": self.selection_strategy.value,
            "schema_version": self.schema_version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class ResolutionResult:
    """Resolution of a single task against the worker pool."""

    task_id: str
    task_title: str
    required_capabilities: List[str] = field(default_factory=list)
    status: ResolutionStatus = ResolutionStatus.UNRESOLVED
    worker_id: Optional[str] = None
    worker_name: Optional[str] = None
    confidence: str = "low"
    reason: str = ""
    matched_capabilities: List[str] = field(default_factory=list)
    missing_capabilities: List[str] = field(default_factory=list)
    selection_strategy: SelectionStrategy = SelectionStrategy.SINGLE
    score: ScoreBreakdown = field(default_factory=ScoreBreakdown)
    candidates: List[str] = field(default_factory=list)   # eligible worker ids
    alternatives: List[dict] = field(default_factory=list)  # ranked runners-up
    expected_artifacts: List[str] = field(default_factory=list)  # explicit contract

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "task_title": self.task_title,
            "required_capabilities": list(self.required_capabilities),
            "status": self.status.value,
            "worker_id": self.worker_id,
            "worker_name": self.worker_name,
            "confidence": self.confidence,
            "reason": self.reason,
            "matched_capabilities": list(self.matched_capabilities),
            "missing_capabilities": list(self.missing_capabilities),
            "selection_strategy": self.selection_strategy.value,
            "score": self.score.to_dict(),
            "candidates": list(self.candidates),
            "alternatives": list(self.alternatives),
            "expected_artifacts": list(self.expected_artifacts),
        }
