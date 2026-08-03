"""Mission models — the persistent goal data structures (Wave 9).

``Mission`` / ``MissionStep`` mirror the V4 ``missions`` / ``mission_steps``
DB tables but as typed objects, with a defined *payload contract* that
bridges to the execution layer:

    step.payload = {
        "action_type": "testing" | "shell" | "git" | "file" | "python",
        "command": "tests/",            # args for the executor
        "cwd": "/path/to/repo",         # optional working directory
    }

When ``action_type`` is present, the step can be executed through
:func:`friday_v4.execution.execute`. A step without one is a *manual*
step (operator completes it) — Friday never invents an action it
doesn't have an executor for.

Statuses match the DB column comments exactly.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class MissionStatus(str, Enum):
    PLANNED = "planned"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class MissionStep:
    """One executable-or-manual step of a mission."""

    id: str
    mission_id: str
    title: str
    status: StepStatus = StepStatus.PENDING
    position: int = 0
    payload: dict = field(default_factory=dict)
    result: str = ""
    created_at: str = ""
    updated_at: str = ""

    @property
    def action_type(self) -> Optional[str]:
        """The executor to use (None = manual step)."""
        return self.payload.get("action_type")

    @property
    def command(self) -> str:
        return self.payload.get("command", "")

    @property
    def cwd(self) -> Optional[str]:
        return self.payload.get("cwd")

    @property
    def is_executable(self) -> bool:
        return bool(self.action_type)

    @classmethod
    def from_row(cls, row: dict) -> "MissionStep":
        return cls(
            id=row["id"],
            mission_id=row["mission_id"],
            title=row["title"],
            status=StepStatus(row["status"]),
            position=int(row["position"]),
            payload=_loads_payload(row.get("payload", "{}")),
            result=row.get("result", "") or "",
            created_at=row.get("created_at", "") or "",
            updated_at=row.get("updated_at", "") or "",
        )


@dataclass
class Mission:
    """A persistent goal with ordered, schedulable steps."""

    id: str
    title: str
    description: str = ""
    status: MissionStatus = MissionStatus.PLANNED
    priority: str = "medium"
    steps: list[MissionStep] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""

    @property
    def pending_steps(self) -> list[MissionStep]:
        return [s for s in self.steps if s.status == StepStatus.PENDING]

    @property
    def completed_steps(self) -> list[MissionStep]:
        return [s for s in self.steps if s.status == StepStatus.COMPLETED]

    @property
    def next_step(self) -> Optional[MissionStep]:
        pending = self.pending_steps
        return pending[0] if pending else None

    @property
    def progress(self) -> float:
        """Fraction of steps completed (0.0–1.0). Empty mission = 1.0."""
        if not self.steps:
            return 1.0
        return len(self.completed_steps) / len(self.steps)

    @classmethod
    def from_row(cls, row: dict,
                 steps: Optional[list[MissionStep]] = None) -> "Mission":
        return cls(
            id=row["id"],
            title=row["title"],
            description=row.get("description", "") or "",
            status=MissionStatus(row["status"]),
            priority=row.get("priority", "medium") or "medium",
            steps=steps or [],
            created_at=row.get("created_at", "") or "",
            updated_at=row.get("updated_at", "") or "",
        )


def _loads_payload(raw: str) -> dict:
    try:
        data = json.loads(raw or "{}")
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def make_step_payload(action_type: Optional[str], command: str = "",
                      cwd: Optional[str] = None) -> dict:
    """Build a step payload per the execution contract."""
    payload: dict = {"command": command or ""}
    if action_type:
        payload["action_type"] = action_type
    if cwd:
        payload["cwd"] = str(cwd)
    return payload


__all__ = ["Mission", "MissionStep", "MissionStatus", "StepStatus",
           "make_step_payload"]
