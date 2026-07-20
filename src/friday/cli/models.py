"""Immutable view models. Renderers consume these, never domain objects."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class MissionPhase(str, Enum):
    PLANNING = "planning"
    DISCOVERY = "discovery"
    ANALYSIS = "analysis"
    IMPLEMENTATION = "implementation"
    VERIFICATION = "verification"
    SUMMARY = "summary"
    COMPLETE = "complete"


class WorkerStatus(str, Enum):
    SPAWNED = "spawned"
    READY = "ready"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True)
class ProgressView:
    current: int
    total: Optional[int] = None


@dataclass(frozen=True)
class WorkerView:
    id: str
    name: str
    status: WorkerStatus
    current_task: str
    progress: Optional[ProgressView] = None
    findings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class TimelineEventView:
    id: str
    timestamp: str
    kind: str  # "phase" | "worker" | "info" | "error"
    message: str


@dataclass(frozen=True)
class SummaryView:
    files_modified: int = 0
    tests_passed: int = 0
    warnings: int = 0


@dataclass(frozen=True)
class MissionView:
    id: str
    goal: str
    phase: MissionPhase
    progress: float  # 0.0–1.0
    workers: list[WorkerView]
    timeline: list[TimelineEventView]
    summary: SummaryView
    elapsed_seconds: int
