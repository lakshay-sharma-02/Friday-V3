"""Hermetic tests for `friday4 mission` — the missions-layer CLI (Wave 9 + 18).

The Wiring Law surface: every layer gets its ``friday4 <layer> …``
command. Planning honors ``FRIDAY_V4_CLAUDE_PLANNER`` through the same
``make_planner`` construction point as talk/voice/web, so this CLI never
diverges from the NL path. All tests are hermetic (tmp DB via ``--db``).
"""

from __future__ import annotations

import json

from friday_v4 import db
from friday_v4 import cli_missions as cli


def _args(**kw):
    from types import SimpleNamespace
    base = {"db": None, "cwd": None, "json": False,
            "title": None, "priority": "medium",
            "goal": None, "reason": None,
            "force": False, "manual_result": None,
            "status": None, "id": None}
    base.update(kw)
    return SimpleNamespace(**base)


class TestMissionCli:
    def test_create_and_status_roundtrip(self, tmp_path, capsys):
        dbp = tmp_path / "v4.db"
        assert cli.cmd_mission_create(_args(goal="ship the auth refactor",
                                            db=str(dbp))) == 0
        out = capsys.readouterr().out
        assert "ship the auth refactor" in out
        assert "step(s)" in out or "progress" in out

        # The mission persisted to the tmp DB.
        conn = db.connect(dbp)
        try:
            missions = db.list_missions(conn)
            assert missions and missions[0]["title"] == "ship the auth refactor"
            mid = missions[0]["id"]
        finally:
            conn.close()

        assert cli.cmd_mission_status(_args(id=mid, db=str(dbp))) == 0
        assert "ship the auth refactor" in capsys.readouterr().out

    def test_create_json(self, tmp_path, capsys):
        dbp = tmp_path / "v4.db"
        assert cli.cmd_mission_create(_args(goal="migrate the DB",
                                            db=str(dbp), json=True)) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["title"] == "migrate the DB"
        assert data["status"] == "planned"
        assert isinstance(data["steps"], list)

    def test_list_empty_and_with_status_filter(self, tmp_path, capsys):
        dbp = tmp_path / "v4.db"
        assert cli.cmd_mission_list(_args(db=str(dbp))) == 0
        assert "No missions" in capsys.readouterr().out

        cli.cmd_mission_create(_args(goal="ship the auth refactor",
                                     db=str(dbp)))
        capsys.readouterr()  # clear
        assert cli.cmd_mission_list(_args(db=str(dbp))) == 0
        assert "ship the auth refactor" in capsys.readouterr().out
        # Status filter: nothing active yet → empty list message.
        assert cli.cmd_mission_list(_args(db=str(dbp), status="active")) == 0
        assert "No missions" in capsys.readouterr().out

    def test_lifecycle_transitions(self, tmp_path):
        dbp = tmp_path / "v4.db"
        cli.cmd_mission_create(_args(goal="ship the auth refactor",
                                     db=str(dbp)))
        conn = db.connect(dbp)
        try:
            mid = db.list_missions(conn)[0]["id"]
        finally:
            conn.close()
        assert cli.cmd_mission_start(_args(id=mid, db=str(dbp))) == 0
        conn = db.connect(dbp)
        try:
            assert db.get_mission(conn, mid)["status"] == "active"
        finally:
            conn.close()
        assert cli.cmd_mission_pause(_args(id=mid, db=str(dbp))) == 0
        assert cli.cmd_mission_cancel(_args(id=mid, db=str(dbp))) == 0
        conn = db.connect(dbp)
        try:
            assert db.get_mission(conn, mid)["status"] == "cancelled"
        finally:
            conn.close()
        assert cli.cmd_mission_delete(_args(id=mid, db=str(dbp))) == 0
        conn = db.connect(dbp)
        try:
            assert db.get_mission(conn, mid) is None
        finally:
            conn.close()

    def test_replan_reports_change(self, tmp_path, capsys):
        dbp = tmp_path / "v4.db"
        cli.cmd_mission_create(_args(goal="improve the parser architecture",
                                     db=str(dbp)))
        conn = db.connect(dbp)
        try:
            mid = db.list_missions(conn)[0]["id"]
        finally:
            conn.close()
        assert cli.cmd_mission_replan(_args(id=mid, db=str(dbp),
                                            reason="reality changed")) == 0
        out = capsys.readouterr().out
        assert "plan changed because" in out
        assert "reality changed" in out

    def test_replan_missing_mission(self, tmp_path, capsys):
        assert cli.cmd_mission_replan(
            _args(id="nope", db=str(tmp_path / "v4.db"))) == 1
        assert "No mission" in capsys.readouterr().out

    def test_advance_manual_step(self, tmp_path, capsys):
        dbp = tmp_path / "v4.db"
        # An unrecognized goal → a single manual step.
        cli.cmd_mission_create(_args(goal="decide on the team ritual",
                                     db=str(dbp)))
        conn = db.connect(dbp)
        try:
            mid = db.list_missions(conn)[0]["id"]
        finally:
            conn.close()
        assert cli.cmd_mission_start(_args(id=mid, db=str(dbp))) == 0
        assert cli.cmd_mission_advance(
            _args(id=mid, db=str(dbp), manual_result="done by hand")) == 0
        out = capsys.readouterr().out
        assert "manual_completed" in out
        conn = db.connect(dbp)
        try:
            assert db.get_mission(conn, mid)["status"] == "completed"
        finally:
            conn.close()

    def test_cli_deterministic_without_optin(self, tmp_path, monkeypatch):
        """Without FRIDAY_V4_CLAUDE_PLANNER, claude is never consulted."""
        from friday_v4.missions import claude_planner as cp_mod
        called: list[str] = []
        monkeypatch.setattr(cp_mod, "find_tool",
                            lambda name: called.append(name) or "/fake/claude")
        dbp = tmp_path / "v4.db"
        cli.cmd_mission_create(_args(goal="improve the parser architecture",
                                     db=str(dbp)))
        assert called == []
