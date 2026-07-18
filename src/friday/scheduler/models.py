"""Scheduler models (Milestone 9.4).

The Scheduler is the ONLY layer that computes execution *ordering* (waves,
dependency depth, critical path, priority, runnable state) from a validated
Task Graph + Capability Assignments. It executes NOTHING, invokes no worker,
calls no LLM, and never recalculates assignment (the Resolver owns that).

These dataclasses are pure data + (de)serialization. All scheduling math lives
in `scheduler.py`; all persistence lives in `engine.py`; the runnable-state
machine lives in `state.py`.

Contract version (Law 24): every persisted schedule row carries `schema_version`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional

# Contract version. Bump only on a breaking change to the schedule shape.
SCHEMA_VERSION = "1.0"


class TaskState(str, Enum):
    """Runnable lifecycle state of one scheduled task.

    The Scheduler only CREATES the initial state (from dependency/assignment
    analysis). The future Runtime mutates states forward (SCHEDULED -> COMPLETE
    / FAILED / CANCELLED). The Scheduler never sets a post-initial state.
    """

    NOT_READY = "not_ready"    # predecessors incomplete
    READY = "ready"            # predecessors done, worker present+active
    BLOCKED = "blocked"        # missing assignment, disabled worker, or cycle
    SCHEDULED = "scheduled"    # placed on the timeline by the Scheduler
    COMPLETE = "complete"      # Runtime-only
    FAILED = "failed"          # Runtime-only
    CANCELLED = "cancelled"    # Runtime-only

    @classmethod
    def from_str(cls, s: str) -> "TaskState":
        s = (s or "").strip().lower()
        for k in cls:
            if k.value == s:
                return k
        raise ValueError(f"{cls.__name__} has no member {s!r}")


@dataclass
class ScheduledTask:
    """One task placed on the execution schedule.

    A first-class node combining graph position, resolver assignment, and the
    scheduler's computed ordering. This object is the SOLE input the future
    Runtime consumes — it never recomputes dependencies or waves.
    """

    schedule_id: str            # {graph_id}:{task_id}
    graph_id: str
    assignment_id: str
    task_id: str
    worker_id: Optional[str]    # None when BLOCKED / unresolved
    phase: str                  # e.g. "wave-1", "wave-2"
    wave: int                   # parallel-execution wave (1-based)
    status: TaskState
    priority: int               # computed scheduler priority
    dependency_count: int       # number of predecessors
    dependencies: List[str] = field(default_factory=list)
    estimated_start: Optional[int] = None    # wave index (relative order)
    estimated_finish: Optional[int] = None   # wave index (relative order)
    blocked_reason: str = ""    # populated when status == BLOCKED
    confidence: str = "low"     # carried from the resolver assignment
    selection_strategy: str = "single"
    schema_version: str = SCHEMA_VERSION
    created_at: str = ""
    updated_at: str = ""

    def to_row(self) -> dict:
        import json
        return {
            "schedule_id": self.schedule_id,
            "graph_id": self.graph_id,
            "assignment_id": self.assignment_id,
            "task_id": self.task_id,
            "worker_id": self.worker_id,
            "phase": self.phase,
            "status": self.status.value,
            "priority": self.priority,
            "wave": self.wave,
            "dependency_count": self.dependency_count,
            "estimated_start": self.estimated_start,
            "estimated_finish": self.estimated_finish,
            "blocked_reason": self.blocked_reason,
            "confidence": self.confidence,
            "selection_strategy": self.selection_strategy,
            "schema_version": self.schema_version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def to_dict(self) -> dict:
        return {
            "schedule_id": self.schedule_id,
            "graph_id": self.graph_id,
            "assignment_id": self.assignment_id,
            "task_id": self.task_id,
            "worker_id": self.worker_id,
            "phase": self.phase,
            "wave": self.wave,
            "status": self.status.value,
            "priority": self.priority,
            "dependency_count": self.dependency_count,
            "dependencies": list(self.dependencies),
            "estimated_start": self.estimated_start,
            "estimated_finish": self.estimated_finish,
            "blocked_reason": self.blocked_reason,
            "confidence": self.confidence,
            "selection_strategy": self.selection_strategy,
            "schema_version": self.schema_version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class ExecutionSchedule:
    """The first-class schedule object — the sole input to the M9.5 Runtime.

    Carries waves, dependency metadata, critical path, and per-task runnable
    state. The Runtime consumes this verbatim; it never recalculates ordering.
    """

    schedule_id: str            # == graph_id (one schedule per graph)
    graph_id: str
    goal: str = ""
    task_count: int = 0
    wave_count: int = 0
    critical_path: List[str] = field(default_factory=list)
    critical_path_length: int = 0
    max_parallelism: int = 0
    worker_utilization: dict = field(default_factory=dict)  # worker_id -> task count
    tasks: List[ScheduledTask] = field(default_factory=list)
    status: str = "scheduled"   # schedule-level status (initial)
    schema_version: str = SCHEMA_VERSION
    created_at: str = ""
    updated_at: str = ""

    def task_by_id(self, task_id: str) -> Optional[ScheduledTask]:
        for t in self.tasks:
            if t.task_id == task_id:
                return t
        return None

    def waves(self) -> List[List[str]]:
        """Task ids grouped by wave, wave 1 first."""
        out: List[List[str]] = []
        for w in range(1, self.wave_count + 1):
            out.append([t.task_id for t in self.tasks if t.wave == w])
        return out

    def to_dict(self) -> dict:
        return {
            "schedule_id": self.schedule_id,
            "graph_id": self.graph_id,
            "goal": self.goal,
            "task_count": self.task_count,
            "wave_count": self.wave_count,
            "critical_path": list(self.critical_path),
            "critical_path_length": self.critical_path_length,
            "max_parallelism": self.max_parallelism,
            "worker_utilization": dict(self.worker_utilization),
            "waves": self.waves(),
            "status": self.status,
            "schema_version": self.schema_version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "tasks": [t.to_dict() for t in self.tasks],
        }
