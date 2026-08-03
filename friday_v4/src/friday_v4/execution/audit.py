"""Audit log — every execution recorded, never silent.

Wave 9 rule: *"every action logged: what, when, result, undo payload."*
:class:`AuditLogger` wraps the V4 ``actions`` table (``friday_v4.db``)
so every gate decision and execution attempt leaves a durable trace the
operator can inspect with ``friday4 db status`` / ``friday4 actions``.

Design laws:
- Never crash: a missing/invalid DB connection degrades to a no-op
  logger (all methods return safe defaults) — the daemon law.
- Hermetic: tests pass a ``tmp_path`` connection.
- Undo payloads are stored as JSON in the ``actions`` row so undo can
  run after a restart (persistent audit trail).

Usage:
    audit = AuditLogger(conn)
    aid = audit.record("shell", goal="run tests", command="pytest -q",
                       permission_level="confirm")
    audit.finish(aid, "succeeded", result_code=0, output="57 passed",
                 undo_payload={"op": "run", "args": ["pytest", "-q"]})
"""

from __future__ import annotations

import logging
from typing import Optional

from .. import db

logger = logging.getLogger("friday_v4.execution.audit")


class AuditLogger:
    """Durable audit trail over the V4 ``actions`` table.

    A ``conn`` of ``None`` yields a no-op logger — every call returns a
    safe default so the rest of Friday never crashes on a missing DB.
    """

    def __init__(self, conn=None) -> None:
        self._conn = conn

    @property
    def enabled(self) -> bool:
        return self._conn is not None

    def record(self, action_type: str, goal: str = "",
               command: str = "", cwd: str = "",
               permission_level: str = "confirm") -> Optional[str]:
        """Record an execution attempt; returns the action id (None if
        no DB). The row starts in ``pending`` status."""
        if self._conn is None:
            return None
        try:
            return db.record_action(
                self._conn,
                action_type,
                goal=goal,
                status="pending",
                permission_level=permission_level,
                command=command,
                cwd=cwd,
            )
        except Exception as exc:  # defensive — never crash the caller
            logger.debug(f"audit.record failed: {exc}")
            return None

    def finish(self, action_id: Optional[str], status: str,
               result_code: Optional[int] = None, output: str = "",
               undo_payload: Optional[dict] = None) -> bool:
        """Finalize an action row with its outcome + undo payload."""
        if self._conn is None or not action_id:
            return False
        try:
            return bool(db.finish_action(
                self._conn, action_id, status,
                result_code=result_code, output=output,
                undo_payload=undo_payload))
        except Exception as exc:  # defensive
            logger.debug(f"audit.finish failed: {exc}")
            return False

    def deny(self, action_id: Optional[str], reason: str = "") -> bool:
        """Mark an action as denied by the gate (still audited!)."""
        return self.finish(action_id, "denied",
                           output=reason or "denied by confirmation gate")

    def recent(self, limit: int = 50,
               action_type: Optional[str] = None) -> list[dict]:
        """Recent actions from the audit trail ([] when no DB)."""
        if self._conn is None:
            return []
        try:
            return db.recent_actions(self._conn, limit=limit,
                                     action_type=action_type)
        except Exception as exc:  # defensive
            logger.debug(f"audit.recent failed: {exc}")
            return []

    def get(self, action_id: str) -> Optional[dict]:
        """Fetch one action row (None when no DB / not found)."""
        if self._conn is None:
            return None
        try:
            cur = self._conn.execute(
                "SELECT * FROM actions WHERE id = ?", (action_id,))
            row = cur.fetchone()
            return dict(row) if row is not None else None
        except Exception as exc:  # defensive
            logger.debug(f"audit.get failed: {exc}")
            return None


__all__ = ["AuditLogger"]
