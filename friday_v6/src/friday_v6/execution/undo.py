"""Undo — reversible actions for Wave 9 executors.

Wave 9 rule: *"reversible actions where possible."* :class:`UndoManager`
executes undo payloads stored in the audit trail.    Undo payloads are plain JSON dicts with an ``op`` discriminator:

    {"op": "restore_file", "path": "...", "original": "<base64 or raw>"}
    {"op": "delete_file", "path": "..."}          # undo a file creation
    {"op": "move_file", "from": "...", "to": "..."}
    {"op": "run", "args": [...], "cwd": "..."}      # undo command
    {"op": "none"}                                  # no undo available

Design laws:
- Never crash: unknown/absent undo payloads return a result with
  ``ok=False`` and a message — the caller decides how to surface it.
- Hermetic: undo runs through a :class:`Sandbox` for path containment,
  so an undo payload can never escape the sandbox roots.
- ``undo(action_id)`` requires an :class:`AuditLogger` (or a raw
  ``conn``) to look up the stored payload — audit and undo share the
  same durable trail.

Usage:
    undo = UndoManager(sandbox=sb, audit=audit)
    result = undo.undo(action_id)
    if not result.ok:
        print(f"undo failed: {result.message}")
"""

from __future__ import annotations

import base64
import logging
import os
from dataclasses import dataclass
from typing import Optional

from .sandbox import Sandbox, SandboxViolation

logger = logging.getLogger("friday_v6.execution.undo")


@dataclass
class UndoResult:
    """Outcome of an undo attempt — never raises."""

    ok: bool
    message: str = ""
    action_id: Optional[str] = None


class UndoManager:
    """Applies undo payloads from the audit trail inside a sandbox."""

    def __init__(self, sandbox: Optional[Sandbox] = None,
                 audit=None, conn=None) -> None:
        #: Sandbox enforces path containment for undo file ops.
        self.sandbox = sandbox or Sandbox()
        #: Accept either an AuditLogger or a raw DB connection.
        self.audit = audit
        self._conn = conn

    def _payload_for(self, action_id: str) -> Optional[dict]:
        """Fetch the undo_payload for an action from the audit trail."""
        try:
            if self.audit is not None and hasattr(self.audit, "get"):
                row = self.audit.get(action_id)
            else:
                if self._conn is None:
                    return None
                cur = self._conn.execute(
                    "SELECT undo_payload FROM actions WHERE id = ?",
                    (action_id,))
                row = cur.fetchone()
                row = dict(row) if row is not None else None
        except Exception as exc:  # defensive — never crash
            logger.debug(f"undo payload lookup failed: {exc}")
            return None

        if row is None:
            return None
        import json
        try:
            payload = json.loads(row.get("undo_payload") or "{}")
            return payload if isinstance(payload, dict) else None
        except (json.JSONDecodeError, TypeError) as exc:
            logger.debug(f"undo payload unparsable: {exc}")
            return None

    def undo(self, action_id: str) -> UndoResult:
        """Undo a previously audited action by its payload.

        Returns :class:`UndoResult` — ``ok=True`` only when the payload
        was found, recognized, and applied without error.
        """
        payload = self._payload_for(action_id)
        if not payload:
            return UndoResult(False, "no undo payload found "
                                     f"for action {action_id}", action_id)

        op = payload.get("op")
        try:
            if op == "restore_file":
                return self._undo_restore_file(payload, action_id)
            if op == "delete_file":
                return self._undo_delete_file(payload, action_id)
            if op == "move_file":
                return self._undo_move_file(payload, action_id)
            if op == "run":
                return self._undo_run(payload, action_id)
            if op == "none":
                return UndoResult(False, "action is not undoable", action_id)
            return UndoResult(False, f"unknown undo op: {op!r}", action_id)
        except Exception as exc:  # defensive — never crash the caller
            logger.warning(f"undo of {action_id} failed: {exc}")
            return UndoResult(False, str(exc), action_id)

    # ── Op handlers ───────────────────────────────────────────────────

    def _undo_restore_file(self, payload: dict, action_id: str) -> UndoResult:
        path = payload.get("path")
        original = payload.get("original")
        if not path or original is None:
            return UndoResult(False, "restore_file payload incomplete",
                              action_id)
        try:
            target = self.sandbox.resolve_path(path)
            data = original
            if payload.get("encoding") == "base64":
                data = base64.b64decode(original).decode("utf-8")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(data, encoding="utf-8")
            return UndoResult(True, f"restored {target}", action_id)
        except (SandboxViolation, OSError) as exc:
            return UndoResult(False, str(exc), action_id)

    def _undo_delete_file(self, payload: dict, action_id: str) -> UndoResult:
        """Undo a file *creation* by deleting what the action created.

        Only ever removes paths the sandbox allows; a missing file is a
        success (nothing left to clean up).
        """
        path = payload.get("path")
        if not path:
            return UndoResult(False, "delete_file payload incomplete",
                              action_id)
        try:
            target = self.sandbox.resolve_path(path)
        except SandboxViolation as exc:
            return UndoResult(False, str(exc), action_id)
        try:
            if target.exists() and target.is_file():
                target.unlink()
                return UndoResult(True, f"deleted {target}", action_id)
            return UndoResult(True, "nothing to clean up (file already gone)",
                              action_id)
        except OSError as exc:
            return UndoResult(False, str(exc), action_id)

    def _undo_move_file(self, payload: dict, action_id: str) -> UndoResult:
        src = payload.get("from")
        dst = payload.get("to")
        if not src or not dst:
            return UndoResult(False, "move_file payload incomplete", action_id)
        try:
            src_path = self.sandbox.resolve_path(src)
            dst_path = self.sandbox.resolve_path(dst)
            if not src_path.exists():
                return UndoResult(False, f"cannot undo move — {src_path} "
                                         f"missing", action_id)
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(src_path, dst_path)
            return UndoResult(True, f"moved {src_path} back to {dst_path}",
                              action_id)
        except (SandboxViolation, OSError) as exc:
            return UndoResult(False, str(exc), action_id)

    def _undo_run(self, payload: dict, action_id: str) -> UndoResult:
        args = payload.get("args") or []
        cwd = payload.get("cwd")
        if not args:
            return UndoResult(False, "run payload has no args", action_id)
        try:
            res = self.sandbox.run([str(a) for a in args], cwd=cwd)
        except SandboxViolation as exc:
            return UndoResult(False, str(exc), action_id)
        if res.result_code == 0:
            return UndoResult(True, res.output.strip() or "undo command ok",
                              action_id)
        return UndoResult(False,
                          f"undo command failed (rc={res.result_code}): "
                          f"{res.output.strip()}", action_id)


__all__ = ["UndoManager", "UndoResult"]
