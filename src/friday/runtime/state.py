"""Execution state machine (Milestone 9.5).

The ONLY transitions the Runtime performs. Linear and irreversible: a task moves
PENDING -> RUNNING, then to exactly one terminal state. There is no retry, no
rollback, no repair — those are future concerns (Review / Repair Loop) that do
NOT exist in this milestone.

    PENDING -> RUNNING -> SUCCESS
                        -> FAILED
                        -> CANCELLED   (ancestor failed; never executed)

The Runtime creates the initial PENDING rows and drives them forward. It never
reasons about WHY a task failed (that is Review's job, later).
"""

from __future__ import annotations

from typing import Set

from .models import RunState


# Allowed transitions. Anything else is a programming error in the Runtime.
_TRANSITIONS = {
    RunState.PENDING: {RunState.RUNNING, RunState.CANCELLED},
    RunState.RUNNING: {RunState.SUCCESS, RunState.FAILED, RunState.CANCELLED},
    RunState.SUCCESS: set(),
    RunState.FAILED: set(),
    RunState.CANCELLED: set(),
}


def can_transition(frm: RunState, to: RunState) -> bool:
    return to in _TRANSITIONS.get(frm, set())


def next_state_for_result(frm: RunState, success: bool) -> RunState:
    """Map a worker result onto the next state. No exceptions, no retries."""
    if frm != RunState.RUNNING:
        raise ValueError(f"cannot finish task from state {frm.value}")
    return RunState.SUCCESS if success else RunState.FAILED


def mark_cancelled(frm: RunState) -> RunState:
    """A descendant of a failed task is CANCELLED (never executed)."""
    if frm.terminal and frm != RunState.PENDING:
        # Already decided; do not override a real outcome.
        return frm
    return RunState.CANCELLED


def blocked_descendants(task_id: str, dependents: dict) -> Set[str]:
    """All transitive descendants of a failed task (to be CANCELLED).

    `dependents` maps task_id -> [task_ids that depend on it].
    """
    out: Set[str] = set()
    stack = list(dependents.get(task_id, []))
    while stack:
        n = stack.pop()
        if n in out:
            continue
        out.add(n)
        stack.extend(dependents.get(n, []))
    return out
