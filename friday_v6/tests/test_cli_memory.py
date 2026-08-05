"""Hermetic tests for `friday6 memory` — the Wave 10 memory CLI.

Covers the command handlers directly (no subprocess, no real ~/.friday):
- store → recall round-trip with provenance
- recall of a missing fact is honest (exit 1, no fabrication)
- JSON mode: pure machine-readable document
- forget deletes; list filters by subject; status shows counts + working
  memory
- bare keys resolve to the operator subject
"""

from __future__ import annotations

import json
import types

from friday_v6 import db
from friday_v6.cli_memory import (
    EXIT_FAILED,
    EXIT_OK,
    cmd_memory_forget,
    cmd_memory_list,
    cmd_memory_recall,
    cmd_memory_status,
    cmd_memory_store,
)


def _args(**kw):
    defaults = dict(key=None, value=None, source="", confidence=0.7,
                    policy="usage", subject=None, limit=50, db=None,
                    json=False)
    defaults.update(kw)
    return types.SimpleNamespace(**defaults)


def _capture(capsys, fn, *args, **kw):
    code = fn(*args, **kw)
    out, err = capsys.readouterr()
    return code, out, err


class TestMemoryCLI:
    def test_store_recall_roundtrip(self, tmp_path, capsys):
        dbp = tmp_path / "v4.db"
        code, out, _ = _capture(capsys, cmd_memory_store,
                                _args(key="operator.name", value="Lakshay",
                                      source="voice:2026-08-01", db=dbp))
        assert code == EXIT_OK
        assert "Noted" in out

        code, out, _ = _capture(capsys, cmd_memory_recall,
                                _args(key="operator.name", db=dbp))
        assert code == EXIT_OK
        assert "Lakshay" in out

        # Provenance persisted.
        conn = db.connect(dbp)
        assert db.recall_memory(conn, "operator.name")["source"] == \
            "voice:2026-08-01"
        conn.close()

    def test_bare_key_uses_operator_subject(self, tmp_path, capsys):
        dbp = tmp_path / "v4.db"
        code, _, _ = _capture(capsys, cmd_memory_store,
                              _args(key="name", value="Lakshay", db=dbp))
        assert code == EXIT_OK
        conn = db.connect(dbp)
        assert db.recall_memory(conn, "operator.name")["value"] == "Lakshay"
        conn.close()

    def test_recall_missing_is_honest(self, tmp_path, capsys):
        code, out, _ = _capture(capsys, cmd_memory_recall,
                                _args(key="operator.nope",
                                      db=tmp_path / "v4.db"))
        assert code == EXIT_FAILED
        assert "don't remember" in out.lower()

    def test_json_pure(self, tmp_path, capsys):
        dbp = tmp_path / "v4.db"
        _capture(capsys, cmd_memory_store, _args(key="operator.name",
                                                 value="Lakshay", db=dbp))
        code, out, _ = _capture(capsys, cmd_memory_recall,
                                _args(key="operator.name", db=dbp, json=True))
        assert code == EXIT_OK
        data = json.loads(out)
        assert data["subject"] == "operator"
        assert data["predicate"] == "name"
        assert data["value"] == "Lakshay"

    def test_forget(self, tmp_path, capsys):
        dbp = tmp_path / "v4.db"
        _capture(capsys, cmd_memory_store, _args(key="operator.name",
                                                 value="Lakshay", db=dbp))
        code, out, _ = _capture(capsys, cmd_memory_forget,
                                _args(key="operator.name", db=dbp))
        assert code == EXIT_OK
        conn = db.connect(dbp)
        assert db.recall_memory(conn, "operator.name") is None
        conn.close()

    def test_list_filters_by_subject(self, tmp_path, capsys):
        dbp = tmp_path / "v4.db"
        _capture(capsys, cmd_memory_store, _args(key="operator.name",
                                                 value="Lakshay", db=dbp))
        _capture(capsys, cmd_memory_store, _args(key="project.active",
                                                 value="yes", db=dbp))
        code, out, _ = _capture(capsys, cmd_memory_list,
                                _args(subject="project", db=dbp))
        assert code == EXIT_OK
        assert "active" in out and "name" not in out

    def test_status_shows_count_and_working(self, tmp_path, capsys):
        dbp = tmp_path / "v4.db"
        from friday_v6.memory import WorkingMemory
        conn = db.connect(dbp)
        WorkingMemory(conn).set("current_task", "Refactoring auth",
                                priority=3)
        conn.close()
        _capture(capsys, cmd_memory_store, _args(key="operator.name",
                                                 value="Lakshay", db=dbp))
        code, out, _ = _capture(capsys, cmd_memory_status,
                                _args(db=dbp, json=True))
        assert code == EXIT_OK
        data = json.loads(out)
        assert data["fact_count"] >= 1
        assert "Refactoring auth" in data["working_memory"]
