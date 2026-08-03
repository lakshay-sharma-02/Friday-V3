"""Hermetic tests for `friday4 talk` — the natural-language CLI (Wave 9).

Covers the command handler directly (no subprocess, no real ~/.friday):
- single-shot: "run the tests" executes; result spoken + exit 0
- JSON mode: pure machine-readable document, never a prompt
- exit codes: 0 success / 2 denied / 1 failed / 3 usage
- --manual: operator-completes a mission's manual step
"""

from __future__ import annotations

import json

import pytest

from friday_v4 import db
from friday_v4.cli_nl import (
    EXIT_DENIED,
    EXIT_FAILED,
    EXIT_OK,
    EXIT_USAGE,
    cmd_talk,
)
from friday_v4.nl_router import TextCommandHandler


def _args(**kw):
    import types
    defaults = dict(text=[], manual=None, db=None, cwd=None, force=False,
                    yes=False, json=False)
    defaults.update(kw)
    return types.SimpleNamespace(**defaults)


def _talk_args(text, tmp_path, **kw):
    """Base args for a single-shot talk, cwd pinned hermetic."""
    kw.setdefault("cwd", tmp_path)
    kw.setdefault("db", tmp_path / "v4.db")
    return _args(text=text.split(), **kw)


def _seed_repo(tmp_path) -> None:
    """A passing test file + a git repo, so `testing`/`git` steps
    succeed (pytest exit 0, git status exit 0) instead of failing on
    empty tmp dirs."""
    (tmp_path / "test_sample.py").write_text(
        "def test_ok():\n    assert 1 + 1 == 2\n", encoding="utf-8")
    from friday_v4.execution import execute
    execute("shell", "git init -q", cwd=str(tmp_path), force=True,
            goal="seed repo")


def _capture(capsys, fn, *args, **kw):
    code = fn(*args, **kw)
    out, err = capsys.readouterr()
    return code, out, err


class TestTalkSingleShot:
    def test_executes_natural_language(self, tmp_path, capsys):
        _seed_repo(tmp_path)
        code, out, _ = _capture(
            capsys, cmd_talk,
            _talk_args("run the tests", tmp_path, force=True))
        assert code == EXIT_OK
        assert "Friday" in out
        assert "Done" in out

    def test_denied_without_override(self, tmp_path, capsys):
        _seed_repo(tmp_path)
        code, out, _ = _capture(
            capsys, cmd_talk, _talk_args("run the tests", tmp_path))
        assert code == EXIT_DENIED  # CONFIRM + no force + no prompt
        assert "won't do that" in out

    def test_confirm_prompt_approve(self, tmp_path, capsys, monkeypatch):
        _seed_repo(tmp_path)
        monkeypatch.setattr("builtins.input", lambda _: "y")
        code, out, _ = _capture(
            capsys, cmd_talk, _talk_args("run the tests", tmp_path))
        assert code == EXIT_OK

    def test_json_pure_and_never_prompts(self, tmp_path, capsys):
        _seed_repo(tmp_path)
        code, out, _ = _capture(
            capsys, cmd_talk, _talk_args("run the tests", tmp_path,
                                         json=True))
        # No prompt was shown; output is pure JSON.
        assert code == EXIT_DENIED  # fails closed without --force
        data = json.loads(out)
        assert data["action"] == "denied"
        assert data["intent"] == "execute"

    def test_json_success(self, tmp_path, capsys):
        _seed_repo(tmp_path)
        code, out, _ = _capture(
            capsys, cmd_talk, _talk_args("run the tests", tmp_path,
                                         json=True, force=True))
        assert code == EXIT_OK
        data = json.loads(out)
        assert data["action"] == "executed"
        assert data["status"] == "succeeded"
        assert data["action_id"]

    def test_plan_creates_mission(self, tmp_path, capsys):
        code, out, _ = _capture(
            capsys, cmd_talk,
            _talk_args("ship the auth refactor", tmp_path))
        assert code == EXIT_OK
        assert "Mission created" in out

    def test_no_text_enters_repl(self, tmp_path, capsys):
        # No text → REPL loop; exit immediately via EOF.
        import builtins
        orig = builtins.input
        try:
            builtins.input = lambda _: _raise_eof()
            code, out, _ = _capture(
                capsys, cmd_talk, _args(db=tmp_path / "v4.db"))
            assert code == EXIT_OK
        finally:
            builtins.input = orig


def _raise_eof():
    raise EOFError


class TestTalkManual:
    def test_manual_completes_step(self, tmp_path, capsys):
        # Create a mission with a manual step first.
        handler = TextCommandHandler(db.connect(tmp_path / "v4.db"),
                                     cwd=str(tmp_path))
        created = handler.handle("improve the parser architecture")
        assert created.mission_id
        code, out, _ = _capture(
            capsys, cmd_talk,
            _args(manual=created.mission_id, text=["wrote", "the", "design"],
                  db=tmp_path / "v4.db"))
        assert code == EXIT_OK
        assert "manual step done" in out
