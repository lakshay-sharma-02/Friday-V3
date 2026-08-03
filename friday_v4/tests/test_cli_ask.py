"""Hermetic tests for `friday4 ask` — the reasoning CLI (Wave 9).

Covers the command handler directly (no subprocess, no real ~/.friday):
- a seeded DB produces an evidence-cited answer (exit 0)
- empty DB → honest "I don't know yet" (exit 1, no fabrication)
- JSON mode: pure machine-readable document
- usage error when no question is given
"""

from __future__ import annotations

import json

import pytest

from friday_v4 import db
from friday_v4.cli_ask import (
    EXIT_FAILED,
    EXIT_OK,
    EXIT_USAGE,
    cmd_ask,
)


def _args(**kw):
    import types
    defaults = dict(question=None, db=None, json=False)
    defaults.update(kw)
    return types.SimpleNamespace(**defaults)


def _capture(capsys, fn, *args, **kw):
    code = fn(*args, **kw)
    out, err = capsys.readouterr()
    return code, out, err


def _seed(tmp_path):
    conn = db.connect(tmp_path / "v4.db")
    mid = db.create_mission(conn, "ship the auth refactor", status="active")
    sid = db.add_mission_step(conn, mid, "migrate session handling")
    db.update_mission_step(conn, sid, status="completed")
    db.record_action(conn, "testing", goal="run the tests",
                     status="succeeded")
    db.store_memory(conn, "operator.pref", "prefers pytest",
                    source="voice", confidence=0.8)
    conn.close()


class TestCmdAsk:
    def test_answers_with_evidence(self, tmp_path, capsys):
        _seed(tmp_path)
        code, out, _ = _capture(
            capsys, cmd_ask,
            _args(question=["what's", "the", "status", "of", "my",
                            "projects?"],
                  db=tmp_path / "v4.db"))
        assert code == EXIT_OK
        assert "Friday" in out
        assert "mission" in out.lower()

    def test_unknown_answers_honestly(self, tmp_path, capsys):
        code, out, _ = _capture(
            capsys, cmd_ask,
            _args(question=["what's", "the", "status", "of", "my",
                            "projects?"],
                  db=tmp_path / "v4.db"))
        assert code == EXIT_FAILED
        assert "don't know" in out

    def test_json_pure(self, tmp_path, capsys):
        _seed(tmp_path)
        code, out, _ = _capture(
            capsys, cmd_ask,
            _args(question=["what's", "the", "status", "of", "my",
                            "projects?"],
                  db=tmp_path / "v4.db", json=True))
        assert code == EXIT_OK
        data = json.loads(out)
        assert data["known"] is True
        assert data["evidence"]  # citations present

    def test_missing_question_is_usage(self, tmp_path, capsys):
        code, out, _ = _capture(
            capsys, cmd_ask, _args(question=None, db=tmp_path / "v4.db"))
        assert code == EXIT_USAGE
