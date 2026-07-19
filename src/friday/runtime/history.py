"""Execution history (Milestone 9.5).

Append-only per-task state snapshots across the session. Every state transition
writes a new `runtime_history` row; the `runtime_tasks` table holds only the
latest state. Combined with `runtime_evolution`, this gives a full, auditable
trail of the Runtime's deterministic decisions.
"""

from __future__ import annotations

from typing import List, Optional

from ..db import (
    get_runtime_history,
    insert_runtime_history,
)


def snapshot(conn, *, session_id: str, schedule_id: str, task_id: str,
             worker_id: Optional[str], status: str, attempt: int,
             at: str) -> None:
    insert_runtime_history(conn, {
        "session_id": session_id,
        "schedule_id": schedule_id,
        "task_id": task_id,
        "worker_id": worker_id,
        "status": status,
        "attempt": attempt,
        "at": at,
    })


def load(conn, session_id: Optional[str] = None) -> List[dict]:
    return get_runtime_history(conn, session_id)
