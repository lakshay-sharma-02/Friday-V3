"""Hermetic tests for the Wave 9 execution layer (friday_v4.execution).

Covers the full safety pipeline:
- gate: permission classification (AUTO/CONFIRM/NEVER), command sniffing,
  force override, safe default for unknown types
- sandbox: path allowlists (escape → SandboxViolation), timeouts, env
  secret sanitization, structured results (never raise)
- audit: durable action rows, denied actions still recorded, no-DB no-op
- undo: restore_file / move_file / run payloads, unknown op, missing payload
- executors: single execute() dispatcher end-to-end (shell/file/testing),
  unknown action type fails closed

Every test is hermetic: tmp_path roots, tmp_path DB connections — never
the real ~/.friday.
"""

from __future__ import annotations

import os
import sys

import pytest

from friday_v4.execution import (
    AuditLogger,
    PermissionGate,
    PermissionLevel,
    Sandbox,
    SandboxViolation,
    UndoManager,
    execute,
)
from friday_v4 import db


# ==========================================================================
# Gate
# ==========================================================================


class TestGate:
    def test_auto_executes_without_confirm(self):
        gate = PermissionGate()
        assert gate.level_for("status") == PermissionLevel.AUTO
        assert gate.check(PermissionLevel.AUTO) is True

    def test_confirm_requires_confirm_fn(self):
        gate = PermissionGate()
        assert gate.level_for("shell") == PermissionLevel.CONFIRM
        # No confirm_fn → denied (safe default)
        assert gate.check(PermissionLevel.CONFIRM) is False

    def test_confirm_fn_decision(self):
        gate = PermissionGate()
        assert gate.check(PermissionLevel.CONFIRM,
                          confirm_fn=lambda d: True) is True
        assert gate.check(PermissionLevel.CONFIRM,
                          confirm_fn=lambda d: False) is False

    def test_never_blocked_unless_force(self):
        gate = PermissionGate()
        assert gate.check(PermissionLevel.NEVER) is False
        assert gate.check(PermissionLevel.NEVER,
                          confirm_fn=lambda d: True) is False
        assert gate.check(PermissionLevel.NEVER, force=True) is True

    def test_unknown_action_type_defaults_to_confirm(self):
        gate = PermissionGate()
        assert gate.level_for("quantum_flux") == PermissionLevel.CONFIRM

    def test_command_sniffing_escalates_to_never(self):
        gate = PermissionGate()
        # git push escalates even though 'git' defaults to CONFIRM
        assert gate.level_for("git", "push origin main") == PermissionLevel.NEVER
        # word-boundary: 'echo pushups' does NOT escalate
        assert gate.level_for("shell", "echo pushups") == PermissionLevel.CONFIRM

    def test_sniffing_never_downgrades(self):
        gate = PermissionGate()
        # read-looking command can't downgrade a write action type
        assert gate.level_for("git", "status") == PermissionLevel.AUTO
        assert gate.level_for("shell", "status") == PermissionLevel.CONFIRM


# ==========================================================================
# Sandbox
# ==========================================================================


class TestSandbox:
    def test_resolve_path_within_roots(self, tmp_path):
        sb = Sandbox(allowed_roots=[tmp_path])
        resolved = sb.resolve_path(tmp_path / "a" / "b.txt")
        assert resolved == (tmp_path / "a" / "b.txt").resolve()
        # relative path resolves against the first root
        assert sb.resolve_path("x.txt") == (tmp_path / "x.txt").resolve()

    def test_resolve_path_escape_raises(self, tmp_path):
        sb = Sandbox(allowed_roots=[tmp_path])
        with pytest.raises(SandboxViolation):
            sb.resolve_path(tmp_path.parent / "escape.txt")
        # '..' traversal from inside must not escape
        with pytest.raises(SandboxViolation):
            sb.resolve_path(tmp_path / ".." / "escape.txt")

    def test_is_allowed(self, tmp_path):
        sb = Sandbox(allowed_roots=[tmp_path])
        assert sb.is_allowed(tmp_path / "ok.txt")
        assert not sb.is_allowed(tmp_path.parent / "no.txt")

    def test_run_success(self, tmp_path):
        sb = Sandbox(allowed_roots=[tmp_path], timeout_seconds=30)
        res = sb.run([sys.executable, "-c", "print('hello')"])
        assert res.result_code == 0
        assert "hello" in res.output
        assert not res.timed_out

    def test_run_nonzero_exit(self, tmp_path):
        sb = Sandbox(allowed_roots=[tmp_path])
        res = sb.run([sys.executable, "-c", "import sys; sys.exit(3)"])
        assert res.result_code == 3
        assert res.output == ""

    def test_run_timeout(self, tmp_path):
        sb = Sandbox(allowed_roots=[tmp_path], timeout_seconds=1)
        code = "import time; time.sleep(10)"
        res = sb.run([sys.executable, "-c", code])
        assert res.timed_out is True
        assert res.result_code is None

    def test_run_launch_failure(self, tmp_path):
        sb = Sandbox(allowed_roots=[tmp_path])
        res = sb.run(["/nonexistent/binary-that-does-not-exist", "x"])
        assert res.result_code is None
        assert "failed to launch" in res.error

    def test_env_sanitizes_secrets(self, tmp_path):
        sb = Sandbox(allowed_roots=[tmp_path])
        env = sb.sanitized_env()
        assert "OPENAI_API_KEY" not in env
        assert "AWS_SECRET_ACCESS_KEY" not in env
        assert "MY_TOKEN" not in env
        # non-secret vars survive
        assert "PATH" in env

    def test_cwd_must_be_inside_roots(self, tmp_path):
        sb = Sandbox(allowed_roots=[tmp_path])
        res = sb.run([sys.executable, "-c", "print('x')"],
                     cwd=str(tmp_path.parent))
        assert res.error  # cwd escaped → structured failure, no raise

    def test_run_never_raises(self, tmp_path):
        sb = Sandbox(allowed_roots=[tmp_path])
        # garbage inputs must produce results, not exceptions
        res = sb.run([])
        assert res.error == "empty command"


# ==========================================================================
# Audit
# ==========================================================================


class TestAudit:
    def test_record_and_finish(self, tmp_path):
        conn = db.connect(tmp_path / "v4.db")
        try:
            audit = AuditLogger(conn)
            aid = audit.record("shell", goal="run tests",
                               command="pytest -q", permission_level="confirm")
            assert aid
            assert audit.finish(aid, "succeeded", result_code=0,
                                output="57 passed",
                                undo_payload={"op": "run"})
            rows = audit.recent()
            assert len(rows) == 1
            assert rows[0]["status"] == "succeeded"
            assert rows[0]["output"] == "57 passed"
            assert rows[0]["undo_payload"] == '{"op": "run"}'
        finally:
            conn.close()

    def test_denied_is_audited(self, tmp_path):
        conn = db.connect(tmp_path / "v4.db")
        try:
            audit = AuditLogger(conn)
            aid = audit.record("shell", command="rm x")
            assert audit.deny(aid, reason="operator said no")
            row = audit.get(aid)
            assert row["status"] == "denied"
            assert row["output"] == "operator said no"
        finally:
            conn.close()

    def test_no_conn_is_noop(self):
        audit = AuditLogger(None)
        assert audit.enabled is False
        assert audit.record("shell", command="x") is None
        assert audit.finish("nope", "succeeded") is False
        assert audit.recent() == []
        assert audit.get("nope") is None

    def test_get_missing(self, tmp_path):
        conn = db.connect(tmp_path / "v4.db")
        try:
            audit = AuditLogger(conn)
            assert audit.get("missing-id") is None
        finally:
            conn.close()


# ==========================================================================
# Undo
# ==========================================================================


class TestUndo:
    def test_restore_file_roundtrip(self, tmp_path):
        conn = db.connect(tmp_path / "v4.db")
        try:
            audit = AuditLogger(conn)
            sb = Sandbox(allowed_roots=[tmp_path])
            target = tmp_path / "notes.txt"
            target.write_text("v1 content", encoding="utf-8")

            # simulate the action that overwrote the file
            aid = audit.record("file", command=f"write {target}")
            audit.finish(aid, "succeeded",
                         undo_payload={"op": "restore_file",
                                       "path": str(target),
                                       "original": "v1 content"})

            target.write_text("v2 content", encoding="utf-8")  # mutate
            undo = UndoManager(sandbox=sb, audit=audit)
            result = undo.undo(aid)
            assert result.ok
            assert target.read_text(encoding="utf-8") == "v1 content"
        finally:
            conn.close()

    def test_restore_base64(self, tmp_path):
        import base64
        conn = db.connect(tmp_path / "v4.db")
        try:
            audit = AuditLogger(conn)
            sb = Sandbox(allowed_roots=[tmp_path])
            target = tmp_path / "data.bin"
            target.write_text("corrupted", encoding="utf-8")

            aid = audit.record("file", command=f"write {target}")
            audit.finish(aid, "succeeded",
                         undo_payload={"op": "restore_file",
                                       "path": str(target),
                                       "original": base64.b64encode(
                                           b"original bytes").decode(),
                                       "encoding": "base64"})
            undo = UndoManager(sandbox=sb, audit=audit)
            assert undo.undo(aid).ok
            assert target.read_text(encoding="utf-8") == "original bytes"
        finally:
            conn.close()

    def test_move_file_undo(self, tmp_path):
        conn = db.connect(tmp_path / "v4.db")
        try:
            audit = AuditLogger(conn)
            sb = Sandbox(allowed_roots=[tmp_path])
            src = tmp_path / "a.txt"
            dst = tmp_path / "sub" / "b.txt"
            src.write_text("moved", encoding="utf-8")

            aid = audit.record("file", command=f"move {src} {dst}")
            audit.finish(aid, "succeeded",
                         undo_payload={"op": "move_file",
                                       "from": str(dst), "to": str(src)})
            # perform the move, then undo it
            dst.parent.mkdir(parents=True, exist_ok=True)
            src.rename(dst)
            assert src.exists() is False

            undo = UndoManager(sandbox=sb, audit=audit)
            assert undo.undo(aid).ok
            assert src.read_text(encoding="utf-8") == "moved"
        finally:
            conn.close()

    def test_unknown_op(self, tmp_path):
        conn = db.connect(tmp_path / "v4.db")
        try:
            audit = AuditLogger(conn)
            sb = Sandbox(allowed_roots=[tmp_path])
            aid = audit.record("shell", command="x")
            audit.finish(aid, "succeeded", undo_payload={"op": "teleport"})
            undo = UndoManager(sandbox=sb, audit=audit)
            result = undo.undo(aid)
            assert result.ok is False
            assert "unknown undo op" in result.message
        finally:
            conn.close()

    def test_missing_payload(self, tmp_path):
        conn = db.connect(tmp_path / "v4.db")
        try:
            audit = AuditLogger(conn)
            sb = Sandbox(allowed_roots=[tmp_path])
            undo = UndoManager(sandbox=sb, audit=audit)
            result = undo.undo("no-such-action")
            assert result.ok is False
        finally:
            conn.close()


# ==========================================================================
# Executors (end-to-end through the single execute() dispatcher)
# ==========================================================================


class TestExecutors:
    def test_shell_auto_confirmed(self, tmp_path):
        conn = db.connect(tmp_path / "v4.db")
        try:
            result = execute(
                "shell", "echo hello from the sandbox",
                cwd=tmp_path, conn=conn, force=True, goal="smoke test")
            assert result.status == "succeeded"
            assert "hello from the sandbox" in result.output
            assert result.action_id is not None
        finally:
            conn.close()

    def test_shell_denied_without_confirm(self, tmp_path):
        conn = db.connect(tmp_path / "v4.db")
        try:
            result = execute(
                "shell", "echo nope", cwd=tmp_path, conn=conn,
                goal="should be denied")
            assert result.status == "denied"
            assert result.action_id is not None  # denial is audited
            rows = db.recent_actions(conn)
            assert rows[0]["status"] == "denied"
        finally:
            conn.close()

    def test_git_push_escalates_to_never(self, tmp_path):
        conn = db.connect(tmp_path / "v4.db")
        try:
            result = execute(
                "git", "push origin main", cwd=tmp_path, conn=conn,
                confirm_fn=lambda d: True,  # even a yes-confirm won't pass
                goal="deploy attempt")
            assert result.status == "denied"
            assert "never" in result.output
        finally:
            conn.close()

    def test_file_write_and_undo_via_execute(self, tmp_path):
        conn = db.connect(tmp_path / "v4.db")
        try:
            target = tmp_path / "file.txt"
            result = execute(
                "file", f"write {target} hello world",
                cwd=tmp_path, conn=conn, force=True, goal="write file")
            assert result.status == "succeeded"
            assert target.read_text(encoding="utf-8") == "hello world"

            # Brand-new file → undo deletes what the action created.
            undo = UndoManager(
                sandbox=Sandbox(allowed_roots=[tmp_path]),
                audit=AuditLogger(conn))
            ur = undo.undo(result.action_id)
            assert ur.ok
            assert not target.exists()
        finally:
            conn.close()

    def test_file_overwrite_undo_restores_original(self, tmp_path):
        """Undo of an overwrite must restore the *real* prior content —
        never a lossy empty placeholder."""
        conn = db.connect(tmp_path / "v4.db")
        try:
            target = tmp_path / "file.txt"
            target.write_text("original v1 content", encoding="utf-8")

            result = execute(
                "file", f"write {target} overwritten",
                cwd=tmp_path, conn=conn, force=True, goal="overwrite file")
            assert result.status == "succeeded"
            assert target.read_text(encoding="utf-8") == "overwritten"

            undo = UndoManager(
                sandbox=Sandbox(allowed_roots=[tmp_path]),
                audit=AuditLogger(conn))
            ur = undo.undo(result.action_id)
            assert ur.ok
            assert target.read_text(encoding="utf-8") == "original v1 content"
        finally:
            conn.close()

    def test_file_append_undo_restores_pre_append(self, tmp_path):
        conn = db.connect(tmp_path / "v4.db")
        try:
            target = tmp_path / "log.txt"
            target.write_text("line1\n", encoding="utf-8")

            # NB: the file-command grammar tokenizes via shlex, so a
            # trailing newline in the payload is stripped — append
            # "line2" yields "line1\nline2".
            result = execute(
                "file", f"append {target} line2",
                cwd=tmp_path, conn=conn, force=True, goal="append line")
            assert result.status == "succeeded"
            assert target.read_text(encoding="utf-8") == "line1\nline2"

            undo = UndoManager(
                sandbox=Sandbox(allowed_roots=[tmp_path]),
                audit=AuditLogger(conn))
            ur = undo.undo(result.action_id)
            assert ur.ok
            assert target.read_text(encoding="utf-8") == "line1\n"
        finally:
            conn.close()

    def test_unknown_action_type_fails_closed(self, tmp_path):
        result = execute("warp_drive", "engage", cwd=tmp_path)
        assert result.status == "failed"
        assert "unknown action type" in result.output

    def test_file_read_auto(self, tmp_path):
        conn = db.connect(tmp_path / "v4.db")
        try:
            f = tmp_path / "readme.txt"
            f.write_text("contents here", encoding="utf-8")
            result = execute("file", f"read {f}", cwd=tmp_path, conn=conn)
            assert result.status == "succeeded"
            assert result.output == "contents here"
        finally:
            conn.close()

    def test_testing_executor_missing_pytest(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PATH", tmp_path.as_posix())  # empty PATH
        # force=True: simulate an operator-approved run so the gate passes
        # and the executor reaches pytest discovery (which fails cleanly).
        result = execute("testing", "tests/", cwd=tmp_path, force=True)
        # pytest not discoverable → structured failure, not a crash
        assert result.status == "failed"
        assert "pytest" in result.output
