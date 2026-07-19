"""Execution event log (Milestone 9.5).

Records the lifecycle of a session as append-only events. The Runtime emits
exactly: session_started, task_started, task_finished, task_failed,
session_finished. Events are never updated or deleted. This is a pure, ordered
log — no analysis is performed here.
"""

from __future__ import annotations

import uuid
from typing import List

from ..db import insert_runtime_event, get_runtime_events
from .models import RuntimeEvent


def _event_id() -> str:
    # uuid4 is used only for cross-process uniqueness of event ids; the ordered
    # `eid` surrogate PK in the DB gives deterministic in-session ordering.
    return f"evt:{uuid.uuid4().hex}"


def record(conn, session_id: str, kind: str, *,
           task_id: str = "", worker_id: str = "", detail: str = "",
           at: str = "") -> RuntimeEvent:
    ev = RuntimeEvent(
        event_id=_event_id(), session_id=session_id, kind=kind,
        task_id=task_id, worker_id=worker_id or "", detail=detail or "",
        at=at)
    insert_runtime_event(conn, ev.to_dict())
    return ev


def session_started(conn, session_id: str, at: str) -> RuntimeEvent:
    return record(conn, session_id, "session_started", at=at)


def task_started(conn, session_id: str, task_id: str, worker_id: str,
                 at: str) -> RuntimeEvent:
    return record(conn, session_id, "task_started", task_id=task_id,
                  worker_id=worker_id, at=at)


def task_finished(conn, session_id: str, task_id: str, worker_id: str,
                 at: str) -> RuntimeEvent:
    return record(conn, session_id, "task_finished", task_id=task_id,
                  worker_id=worker_id, at=at)


def task_failed(conn, session_id: str, task_id: str, worker_id: str,
               at: str) -> RuntimeEvent:
    return record(conn, session_id, "task_failed", task_id=task_id,
                  worker_id=worker_id, at=at)


def session_finished(conn, session_id: str, at: str) -> RuntimeEvent:
    return record(conn, session_id, "session_finished", at=at)


def load(conn, session_id: str) -> List[dict]:
    return get_runtime_events(conn, session_id)
