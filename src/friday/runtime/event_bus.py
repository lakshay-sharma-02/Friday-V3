"""In-process pub/sub event bus for live execution events.

Strongly typed events. No DB writes — this is for live UI consumption.
Terminal events (mission start/end) are still written to the DB via
runtime/events.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Callable, Type


class MissionPhase(str, Enum):
    PLANNING = "planning"
    DISCOVERY = "discovery"
    ANALYSIS = "analysis"
    IMPLEMENTATION = "implementation"
    VERIFICATION = "verification"
    SUMMARY = "summary"
    COMPLETE = "complete"


@dataclass(frozen=True)
class Event:
    mission_id: str
    timestamp: datetime


@dataclass(frozen=True)
class MissionStarted(Event):
    goal: str


@dataclass(frozen=True)
class MissionCompleted(Event):
    result: str  # "success" | "failed"
    summary: dict = field(default_factory=dict)
    duration_ms: int = 0


@dataclass(frozen=True)
class PhaseChanged(Event):
    previous: MissionPhase
    current: MissionPhase


@dataclass(frozen=True)
class WorkerSpawned(Event):
    worker_id: str
    name: str
    capability: str = ""


@dataclass(frozen=True)
class WorkerReady(Event):
    worker_id: str


@dataclass(frozen=True)
class WorkerStarted(Event):
    worker_id: str
    task_description: str


@dataclass(frozen=True)
class WorkerProgress(Event):
    worker_id: str
    current: int
    total: int | None
    message: str


@dataclass(frozen=True)
class WorkerWaiting(Event):
    worker_id: str
    reason: str = ""


@dataclass(frozen=True)
class WorkerCompleted(Event):
    worker_id: str
    success: bool
    findings: list = field(default_factory=list)


@dataclass(frozen=True)
class WorkerFailed(Event):
    worker_id: str
    error: str


@dataclass(frozen=True)
class ToolStarted(Event):
    tool_name: str
    args: str = ""


@dataclass(frozen=True)
class ToolCompleted(Event):
    tool_name: str
    exit_code: int


@dataclass(frozen=True)
class LogMessage(Event):
    level: str  # "info" | "warn" | "error"
    message: str


class EventBus:
    """In-process pub/sub. Subscribers are called synchronously on publish()."""

    def __init__(self):
        self._subscribers: dict[Type[Event], list[Callable]] = {}

    def subscribe(self, event_type: Type[Event], callback: Callable) -> None:
        """Register a callback for a specific event type."""
        self._subscribers.setdefault(event_type, []).append(callback)

    def publish(self, event: Event) -> None:
        """Deliver event to all subscribers of its exact type."""
        handlers = self._subscribers.get(type(event), [])
        for h in handlers:
            h(event)
