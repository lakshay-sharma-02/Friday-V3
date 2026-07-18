"""Execution timeline derivation (Milestone 9.4).

Deterministic construction of the parallel-wave timeline from a schedule. The
Scheduler never executes; it only arranges tasks into waves and records the
relative start/finish order (wave indices), including serialized same-worker
sub-ordering computed in `scheduler.serialize_worker_conflicts`.

Pure functions — no I/O, no LLM, no time dependence.
"""

from __future__ import annotations

from typing import Dict, List

from .models import ExecutionSchedule, ScheduledTask, TaskState


def order_tasks(scheduled: List[ScheduledTask]) -> List[ScheduledTask]:
    """Deterministic global ordering: wave asc, priority desc, task id asc.

    This is the canonical execution order the Runtime walks. Within a wave,
    higher-priority tasks come first; ties break on task id.
    """
    return sorted(
        scheduled,
        key=lambda t: (t.wave, -t.priority, t.task_id),
    )


def build_timeline(schedule: ExecutionSchedule) -> List[dict]:
    """Return a flat, ordered list of runnable steps for the timeline view.

    Each entry: {order, wave, task_id, worker_id, status, priority,
    estimated_start, estimated_finish}. Blocked tasks are included (status
    reveals why) so the timeline is complete and transparent.
    """
    ordered = order_tasks(schedule.tasks)
    out: List[dict] = []
    for i, t in enumerate(ordered, start=1):
        out.append({
            "order": i,
            "wave": t.wave,
            "task_id": t.task_id,
            "worker_id": t.worker_id,
            "status": t.status.value,
            "priority": t.priority,
            "estimated_start": t.estimated_start,
            "estimated_finish": t.estimated_finish,
            "blocked_reason": t.blocked_reason,
        })
    return out


def wave_summary(schedule: ExecutionSchedule) -> List[dict]:
    """One entry per wave: tasks and their workers."""
    summary: List[dict] = []
    for w in range(1, schedule.wave_count + 1):
        members = [t for t in schedule.tasks if t.wave == w]
        members.sort(key=lambda t: (t.estimated_start or 0, t.task_id))
        summary.append({
            "wave": w,
            "task_ids": [t.task_id for t in members],
            "worker_ids": [t.worker_id for t in members],
            "count": len(members),
        })
    return summary


def max_parallelism(schedule: ExecutionSchedule) -> int:
    """Largest number of tasks in any single wave (peak concurrency)."""
    if schedule.wave_count == 0:
        return 0
    return max(
        (sum(1 for t in schedule.tasks if t.wave == w)
         for w in range(1, schedule.wave_count + 1)),
        default=0,
    )


def critical_path_status(schedule: ExecutionSchedule) -> Dict[str, object]:
    """Report critical-path coverage and whether any CP task is blocked."""
    blocked_on_cp = [
        t.task_id for t in schedule.tasks
        if t.task_id in schedule.critical_path
        and t.status == TaskState.BLOCKED
    ]
    return {
        "critical_path": list(schedule.critical_path),
        "critical_path_length": schedule.critical_path_length,
        "blocked_on_critical_path": blocked_on_cp,
    }
