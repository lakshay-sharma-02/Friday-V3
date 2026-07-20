"""Convert runtime execution results to CLI view models."""
from __future__ import annotations

from ..models import (
    MissionView, MissionPhase, WorkerView, WorkerStatus,
    TimelineEventView, SummaryView, ProgressView,
)

_STATUS_MAP = {
    "success": WorkerStatus.COMPLETED,
    "failed": WorkerStatus.FAILED,
    "running": WorkerStatus.RUNNING,
    "pending": WorkerStatus.READY,
    "cancelled": WorkerStatus.WAITING,
}

_PHASE_MAP = {
    "planning": MissionPhase.PLANNING,
    "discovery": MissionPhase.DISCOVERY,
    "analysis": MissionPhase.ANALYSIS,
    "implementation": MissionPhase.IMPLEMENTATION,
    "verification": MissionPhase.VERIFICATION,
    "summary": MissionPhase.SUMMARY,
    "complete": MissionPhase.COMPLETE,
}


def execution_result_to_view(
    mission_id: str,
    goal: str,
    phase: str,
    progress: float,
    workers: list[WorkerView] | None = None,
    timeline: list[TimelineEventView] | None = None,
    summary: SummaryView | None = None,
    elapsed_seconds: int = 0,
) -> MissionView:
    """Build a MissionView from execution state."""
    phase_enum = _PHASE_MAP.get(phase.lower(), MissionPhase.IMPLEMENTATION)
    return MissionView(
        id=mission_id,
        goal=goal,
        phase=phase_enum,
        progress=progress,
        workers=workers or [],
        timeline=timeline or [],
        summary=summary or SummaryView(),
        elapsed_seconds=elapsed_seconds or _estimate_elapsed(mission_id),
    )


def task_to_worker_view(
    worker_id: str,
    name: str,
    status: str,
    current_task: str,
    current: int = 0,
    total: int | None = None,
    findings: list[str] | None = None,
) -> WorkerView:
    """Convert a task's state to a WorkerView."""
    ws = _STATUS_MAP.get(status.lower(), WorkerStatus.SPAWNED)
    progress = ProgressView(current=current, total=total) if current > 0 else None
    return WorkerView(
        id=worker_id,
        name=name,
        status=ws,
        current_task=current_task,
        progress=progress,
        findings=findings or [],
    )


def _estimate_elapsed(mission_id: str) -> int:
    """Rough elapsed from mission id timestamp if available (sess:hex format)."""
    import time
    try:
        created = int(mission_id.split(":")[1][:8], 16)
        return int(time.time()) - created
    except (ValueError, IndexError):
        return 0
