"""Runnable-state derivation (Milestone 9.4).

Pure logic that decides a task's INITIAL scheduler state from dependency
presence, capability assignment, and worker availability. The Scheduler only
creates this initial state; the future Runtime advances states forward.

Rules (no exceptions):
  - Missing assignment (no resolver row, or status != assigned)
      -> BLOCKED (reason: no assignment)
  - Assigned worker is not active (disabled)
      -> BLOCKED (reason: worker disabled)
  - Has predecessors (dependency_count > 0)
      -> NOT_READY (predecessors incomplete)
  - No predecessors, valid assignment, active worker
      -> READY
"""

from __future__ import annotations

from typing import List, Optional, Set

from .models import TaskState


def compute_initial_state(
    task,
    worker_id: Optional[str],
    assignment_status: Optional[str],
    active_workers: Set[str],
) -> tuple[TaskState, str]:
    """Return (initial_state, blocked_reason).

    `task` must expose `.dependencies` (list) and `.id`.
    """
    # Rule: assignment must exist and be assigned.
    if assignment_status != "assigned" or not worker_id:
        return TaskState.BLOCKED, "no capability assignment"

    # Rule: assigned worker must be active.
    if worker_id not in active_workers:
        return TaskState.BLOCKED, f"assigned worker disabled: {worker_id}"

    # Rule: predecessors must complete before runnable.
    if task.dependencies:
        return TaskState.NOT_READY, "predecessors incomplete"

    return TaskState.READY, ""


def is_runnable(state: TaskState) -> bool:
    """A task the Scheduler marks READY is immediately runnable by the Runtime."""
    return state == TaskState.READY


def terminal_runtime_states() -> Set[TaskState]:
    """States only the Runtime may set (Scheduler never initializes these)."""
    return {TaskState.SCHEDULED, TaskState.COMPLETE,
            TaskState.FAILED, TaskState.CANCELLED}
