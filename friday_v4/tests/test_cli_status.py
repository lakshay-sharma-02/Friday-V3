"""Hermetic tests for `friday4 status` and `friday4 db status`.

All tests monkeypatch Path.home to tmp_path so no real ~/.friday files
are touched, and stub the subsystem probes so the unified status renders
deterministically regardless of the machine.
"""

from __future__ import annotations

import argparse
import json

import pytest

from friday_v4 import cli_status
from friday_v4.cli_status import (
    cmd_db_status,
    cmd_status,
    _probe_daemon,
    _probe_db,
    _probe_security,
)


# ==========================================================================
# Subsystem probes (unit-level, no real ~/.friday)
# ==========================================================================


class TestProbes:
    def test_probe_daemon_not_running(self, tmp_path, monkeypatch):
        import friday_v4.daemon as daemon_mod
        monkeypatch.setattr(daemon_mod, "_PID_FILE", tmp_path / "daemon.pid")
        monkeypatch.setattr(daemon_mod, "_STATUS_FILE", tmp_path / "status.json")
        ok, detail = _probe_daemon()
        assert ok is False
        assert "not running" in detail

    def test_probe_daemon_running_from_status(self, tmp_path, monkeypatch):
        import friday_v4.daemon as daemon_mod
        monkeypatch.setattr(daemon_mod, "_PID_FILE", tmp_path / "daemon.pid")
        monkeypatch.setattr(daemon_mod, "_STATUS_FILE", tmp_path / "status.json")
        (tmp_path / "daemon.pid").write_text("999999")
        (tmp_path / "status.json").write_text(json.dumps({
            "state": "running", "pid": 999999, "uptime_seconds": 120,
            "components": {"notifier": True, "security": False},
        }))
        # pid 999999 is not alive → is_running() False → stale path.
        ok, detail = _probe_daemon()
        assert ok is False
        assert "not running" in detail

    def test_probe_security_no_state(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cli_status.Path, "home",
                            classmethod(lambda cls: tmp_path))
        ok, detail = _probe_security()
        assert ok is True
        assert "ready" in detail

    def test_probe_security_reads_last_scan(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cli_status.Path, "home",
                            classmethod(lambda cls: tmp_path))
        state_dir = tmp_path / ".friday"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "v4_security_last.json").write_text(json.dumps({
            "scans": 3, "report": {"grade": "B"},
        }))
        ok, detail = _probe_security()
        assert ok is True
        assert "3 scan" in detail
        assert "grade: B" in detail

    def test_probe_db_missing(self, tmp_path, monkeypatch):
        from friday_v4 import db as db_mod
        monkeypatch.setattr(db_mod, "_DEFAULT_DB", tmp_path / "v4.db")
        ok, detail = _probe_db()
        assert ok is None  # informational, not a failure
        assert "not created" in detail

    def test_probe_db_after_connect(self, tmp_path, monkeypatch):
        from friday_v4 import db as db_mod
        db_path = tmp_path / "v4.db"
        monkeypatch.setattr(db_mod, "_DEFAULT_DB", db_path)
        db_mod.connect(db_path).close()
        ok, detail = _probe_db()
        assert ok is True
        assert "schema v" in detail
        assert "tables" in detail


# ==========================================================================
# cmd_status (stubbed probes → deterministic output)
# ==========================================================================


def _stub_probes(monkeypatch):
    """Stub every probe so status output is deterministic."""
    monkeypatch.setattr(cli_status, "_probe_daemon",
                        lambda: (True, "running (pid 1, up 1.0 min, 4/6 up)"))
    monkeypatch.setattr(cli_status, "_probe_voice",
                        lambda: (True, "voice stack installed (configured provider: edge-tts)"))
    monkeypatch.setattr(cli_status, "_probe_desktop",
                        lambda: (True, "abstraction ready (hyprland)"))
    monkeypatch.setattr(cli_status, "_probe_security",
                        lambda: (True, "3 scan(s) run, last grade: B"))
    monkeypatch.setattr(cli_status, "_probe_proactive",
                        lambda: (True, "anticipation engine ready"))
    monkeypatch.setattr(cli_status, "_probe_intelligence",
                        lambda: (True, "drift + anomaly detectors ready"))
    monkeypatch.setattr(cli_status, "_probe_web",
                        lambda: (True, "dashboard running on http://127.0.0.1:8899"))
    monkeypatch.setattr(cli_status, "_probe_collab",
                        lambda: (True, "ready (1 known peer(s))"))
    monkeypatch.setattr(cli_status, "_probe_db",
                        lambda: (True, "schema v3, 9 tables, 12 rows"))
    monkeypatch.setattr(cli_status, "_probe_v3",
                        lambda: (True, "V3 bridge connected"))
    monkeypatch.setattr(cli_status, "_probe_mobile",
                        lambda: (True, "companion API running on http://127.0.0.1:8900"))


class TestCmdStatus:
    def test_status_all_ok(self, capsys, monkeypatch):
        _stub_probes(monkeypatch)
        rc = cmd_status(argparse.Namespace())
        out = capsys.readouterr().out
        assert rc == 0
        for name in ("daemon", "voice", "desktop", "security", "proactive",
                     "intelligence", "web", "collab", "db", "v3",
                     "mobile"):
            assert name in out
        assert "All subsystems ready" in out

    def test_status_returns_nonzero_when_down(self, capsys, monkeypatch):
        _stub_probes(monkeypatch)
        monkeypatch.setattr(cli_status, "_probe_web",
                            lambda: (False, "not running (friday4 web)"))
        rc = cmd_status(argparse.Namespace())
        out = capsys.readouterr().out
        assert rc == 1
        assert "Some subsystems unavailable" in out

    def test_status_never_raises_on_probe_error(self, capsys, monkeypatch):
        _stub_probes(monkeypatch)

        def _boom():
            raise RuntimeError("probe exploded")
        monkeypatch.setattr(cli_status, "_probe_collab", _boom)
        rc = cmd_status(argparse.Namespace())  # must not raise
        out = capsys.readouterr().out
        assert rc == 1
        assert "probe error" in out


# ==========================================================================
# cmd_db_status
# ==========================================================================


class TestCmdDbStatus:
    def test_db_status_missing(self, tmp_path, capsys):
        rc = cmd_db_status(argparse.Namespace(db=tmp_path / "nope.db"))
        out = capsys.readouterr().out
        assert rc == 0
        assert "not created" in out

    def test_db_status_with_data(self, tmp_path, capsys):
        from friday_v4 import db as db_mod
        path = tmp_path / "v4.db"
        conn = db_mod.connect(path)
        db_mod.create_mission(conn, "a")
        conn.close()
        rc = cmd_db_status(argparse.Namespace(db=path))
        out = capsys.readouterr().out
        assert rc == 0
        assert "schema" in out
        assert "missions" in out


# ==========================================================================
# Parser wiring
# ==========================================================================


class TestParsers:
    def test_doctor_registers_status_and_doctor(self):
        """`friday4 status` is registered once, by cli_doctor, pointing at
        the unified command (no duplicate 'status' parser)."""
        import argparse
        from friday_v4.cli_doctor import build_doctor_parser
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        build_doctor_parser(sub)
        for cmd in ("doctor", "status"):
            args = parser.parse_args([cmd])
            assert callable(args.func), f"{cmd} has no func"

    def test_build_db_parser_sets_func(self):
        import argparse
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        cli_status.build_db_parser(sub)
        args = parser.parse_args(["db", "status"])
        assert callable(args.func)

    def test_status_probes_cover_every_layer(self):
        names = [n for n, _ in cli_status.STATUS_PROBES]
        for expected in ("daemon", "voice", "desktop", "security",
                         "proactive", "intelligence", "web", "collab",
                         "db", "v3", "mobile"):
            assert expected in names
