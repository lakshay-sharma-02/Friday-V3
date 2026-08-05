"""Mission progress — the feed surfaces consume (Wave 9).

:class:`ProgressReport` is the per-mission view (percent, next step,
status) that voice briefings, the web dashboard, and desktop
notifications render. :func:`progress_feed` returns the active
missions newest-first; :func:`summary` gives status rollups.

Deterministic, never raises (missing missions → empty structures).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from .. import db
from .engine import MissionEngine
from .models import Mission, MissionStatus

logger = logging.getLogger("friday_v6.missions.progress")


@dataclass
class ProgressReport:
    """One mission's progress view for a surface."""

    mission_id: str
    title: str
    status: str
    priority: str = "medium"
    percent: float = 0.0
    completed_steps: int = 0
    total_steps: int = 0
    next_step: Optional[str] = None
    updated_at: str = ""

    def to_dict(self) -> dict:
        return {
            "mission_id": self.mission_id,
            "title": self.title,
            "status": self.status,
            "priority": self.priority,
            "percent": round(self.percent * 100, 1),
            "completed_steps": self.completed_steps,
            "total_steps": self.total_steps,
            "next_step": self.next_step,
            "updated_at": self.updated_at,
        }


def report(mission: Mission) -> ProgressReport:
    """Build a ProgressReport from a Mission (never raises)."""
    next_step = mission.next_step
    return ProgressReport(
        mission_id=mission.id,
        title=mission.title,
        status=mission.status.value,
        priority=mission.priority,
        percent=mission.progress,
        completed_steps=len(mission.completed_steps),
        total_steps=len(mission.steps),
        next_step=next_step.title if next_step else None,
        updated_at=mission.updated_at,
    )


def progress_feed(conn, limit: int = 20,
                  statuses: Optional[tuple[str, ...]] = None) -> list[dict]:
    """Active missions as ProgressReport dicts (newest first).

    Default statuses: active + planned (things in flight). Never raises.
    """
    if statuses is None:
        statuses = (MissionStatus.ACTIVE.value, MissionStatus.PLANNED.value)
    engine = MissionEngine(conn)
    rows = db.list_missions(conn, limit=limit)
    reports = []
    for row in rows:
        if row.get("status") not in statuses:
            continue
        mission = engine.get(row["id"])
        if mission:
            reports.append(report(mission).to_dict())
    return reports


def summary(conn) -> dict:
    """Status rollup across all missions (never raises)."""
    counts = {s.value: 0 for s in MissionStatus}
    total_steps = 0
    completed_steps = 0
    try:
        for row in db.list_missions(conn, limit=10000):
            status = row.get("status")
            if status in counts:
                counts[status] += 1
        engine = MissionEngine(conn)
        for row in db.list_missions(conn, limit=10000):
            mission = engine.get(row["id"])
            if mission:
                total_steps += len(mission.steps)
                completed_steps += len(mission.completed_steps)
    except Exception as exc:  # defensive — never crash
        logger.debug(f"progress summary failed: {exc}")
    return {
        "by_status": counts,
        "total_steps": total_steps,
        "completed_steps": completed_steps,
        "overall_percent": round(completed_steps / total_steps, 3)
        if total_steps else 0.0,
    }


__all__ = ["ProgressReport", "report", "progress_feed", "summary"]
