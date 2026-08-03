"""Hermetic tests for `friday4 execute` / `friday4 actions` (Wave 9 CLI).

Covers the command handlers directly (no subprocess, no real ~/.friday):
- execute: AUTO runs silently, CONFIRM prompts (y/N via injected fn),
  NEVER requires --force, unknown action type fails closed, --json output
- actions: audit listing reads the real V4 DB trail, empty trail,
  --json output, --type filter
- exit codes: 0 success / 2 denied / 1 failed / 3 usage
"""

from __future__ import annotations

import json

import pytest

from friday_v4 import db
from friday_v4.cli_execute import (
    EXIT_DENIED,
    EXIT_FAILED,
    EXIT_OK,
    EXIT_USAGE,
    cmd_actions,
    cmd_execute,
)


def _args(**kw):
    """A minimal argparse.Namespace stand-in for command handlers."""
    import types
    defaults = dict(
        action_type=None, command=None, cwd=None, db=None, goal="",
        force=False, yes=False, json=False, limit=25,
    )
    defaults.update(kw)
    return types.SimpleNamespace(**defaults)


def _capture(capsys, fn, *args, **kw):
    """Run a command handler; returns (exit_code, stdout, stderr)."""
    code = fn(*args, **kw)
    out, err = capsys.readouterr()
    return code, out, err


# ==========================================================================
# friday4 execute
# ==========================================================================


class TestCmdExecute:
    def test_shell_echo_success(self, tmp_path, capsys):
        conn = db.connect(tmp_path / "v4.db")
        try:
            code, out, _ = _capture(
                capsys, cmd_execute,
                _args(action_type="shell", command=["echo", "hello cli"],
                      cwd=tmp_path, db=tmp_path / "v4.db", force=True))
            assert code == EXIT_OK
            assert "hello cli" in out
            assert "succeeded" in out
        finally:
            conn.close()

    def test_json_output(self, tmp_path, capsys):
        conn = db.connect(tmp_path / "v4.db")
        try:
            code, out, _ = _capture(
                capsys, cmd_execute,
                _args(action_type="shell", command=["echo", "hi"],
                      cwd=tmp_path, db=tmp_path / "v4.db", force=True,
                      json=True))
            assert code == EXIT_OK
            data = json.loads(out)
            assert data["status"] == "succeeded"
            assert data["action_type"] == "shell"
        finally:
            conn.close()

    def test_confirm_prompt_approve(self, tmp_path, capsys, monkeypatch):
        conn = db.connect(tmp_path / "v4.db")
        try:
            monkeypatch.setattr("builtins.input", lambda _: "y")
            code, out, _ = _capture(
                capsys, cmd_execute,
                _args(action_type="shell", command=["echo", "yep"],
                      cwd=tmp_path, db=tmp_path / "v4.db"))
            assert code == EXIT_OK
        finally:
            conn.close()

    def test_confirm_prompt_decline_is_denied(self, tmp_path, capsys,
                                              monkeypatch):
        conn = db.connect(tmp_path / "v4.db")
        try:
            monkeypatch.setattr("builtins.input", lambda _: "n")
            code, out, _ = _capture(
                capsys, cmd_execute,
                _args(action_type="shell", command=["echo", "nope"],
                      cwd=tmp_path, db=tmp_path / "v4.db"))
            assert code == EXIT_DENIED
            assert "denied" in out
        finally:
            conn.close()

    def test_yes_approves_confirm_actions(self, tmp_path, capsys):
        """--yes is the non-interactive alias for --force: a CONFIRM
        action must run without prompting (regression guard for the
        original bug where --yes denied every CONFIRM action)."""
        conn = db.connect(tmp_path / "v4.db")
        try:
            code, out, _ = _capture(
                capsys, cmd_execute,
                _args(action_type="shell", command=["echo", "yes-magic"],
                      cwd=tmp_path, db=tmp_path / "v4.db", yes=True))
            assert code == EXIT_OK
            assert "yes-magic" in out
            assert "succeeded" in out
        finally:
            conn.close()

    def test_json_mode_fails_closed_without_override(self, tmp_path, capsys):
        """JSON mode must never print an interactive prompt into the
        machine-readable document — a CONFIRM action without --force
        fails closed with pure JSON + EXIT_DENIED."""
        conn = db.connect(tmp_path / "v4.db")
        try:
            code, out, _ = _capture(
                capsys, cmd_execute,
                _args(action_type="shell", command=["echo", "hi"],
                      cwd=tmp_path, db=tmp_path / "v4.db", json=True))
            assert code == EXIT_DENIED
            data = json.loads(out)  # pure JSON — no prompt text polluted it
            assert data["status"] == "denied"
        finally:
            conn.close()

    def test_never_blocked_without_override(self, tmp_path, capsys):
        conn = db.connect(tmp_path / "v4.db")
        try:
            # No override → a NEVER action is denied even when the
            # interactive confirm would say yes (only an explicit
            # --force/--yes override passes the NEVER gate).
            code, out, _ = _capture(
                capsys, cmd_execute,
                _args(action_type="git", command=["push", "origin", "main"],
                      cwd=tmp_path, db=tmp_path / "v4.db"))
            assert code == EXIT_DENIED
            assert "never" in out

            # --yes is the non-interactive alias for --force: the push
            # is *attempted* (and fails — tmp_path isn't a git repo),
            # not denied by the gate.
            code, out, _ = _capture(
                capsys, cmd_execute,
                _args(action_type="git", command=["push", "origin", "main"],
                      cwd=tmp_path, db=tmp_path / "v4.db", yes=True))
            assert code == EXIT_FAILED

            code, out, _ = _capture(
                capsys, cmd_execute,
                _args(action_type="git", command=["push", "origin", "main"],
                      cwd=tmp_path, db=tmp_path / "v4.db", force=True))
            assert code == EXIT_FAILED  # git push on tmp_path → no repo
        finally:
            conn.close()

    def test_unknown_action_type_fails_closed(self, tmp_path, capsys):
        code, out, _ = _capture(
            capsys, cmd_execute,
            _args(action_type="warp_drive", command=["engage"],
                  cwd=tmp_path, db=tmp_path / "v4.db", force=True))
        assert code == EXIT_FAILED
        assert "unknown action type" in out

    def test_empty_command_is_usage_error(self, tmp_path, capsys):
        code, out, _ = _capture(
            capsys, cmd_execute,
            _args(action_type="shell", command=[], cwd=tmp_path,
                  db=tmp_path / "v4.db"))
        assert code == EXIT_USAGE


# ==========================================================================
# friday4 actions (audit trail)
# ==========================================================================


class TestCmdActions:
    def test_empty_trail(self, tmp_path, capsys):
        code, out, _ = _capture(
            capsys, cmd_actions,
            _args(db=tmp_path / "v4.db", limit=25, action_type=None,
                  json=False))
        assert code == EXIT_OK
        assert "no actions recorded" in out

    def test_lists_recorded_actions(self, tmp_path, capsys):
        conn = db.connect(tmp_path / "v4.db")
        try:
            db.record_action(conn, "shell", command="echo one",
                             status="succeeded")
            db.record_action(conn, "shell", command="echo two",
                             status="denied")
            code, out, _ = _capture(
                capsys, cmd_actions,
                _args(db=tmp_path / "v4.db", limit=25, action_type=None,
                      json=False))
            assert code == EXIT_OK
            assert "echo one" in out
            assert "echo two" in out
            assert "succeeded" in out
            assert "denied" in out
        finally:
            conn.close()

    def test_type_filter(self, tmp_path, capsys):
        conn = db.connect(tmp_path / "v4.db")
        try:
            db.record_action(conn, "shell", command="echo sh")
            db.record_action(conn, "testing", command="pytest -q")
            code, out, _ = _capture(
                capsys, cmd_actions,
                _args(db=tmp_path / "v4.db", limit=25,
                      action_type="testing", json=False))
            assert code == EXIT_OK
            assert "pytest" in out
            assert "echo sh" not in out
        finally:
            conn.close()

    def test_json_output(self, tmp_path, capsys):
        conn = db.connect(tmp_path / "v4.db")
        try:
            db.record_action(conn, "shell", command="echo hi",
                             status="succeeded")
            code, out, _ = _capture(
                capsys, cmd_actions,
                _args(db=tmp_path / "v4.db", limit=25, action_type=None,
                      json=True))
            assert code == EXIT_OK
            rows = json.loads(out)
            assert isinstance(rows, list)
            assert rows[0]["command"] == "echo hi"
        finally:
            conn.close()
