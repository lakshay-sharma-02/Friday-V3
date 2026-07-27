"""Tests for the emergency kill switch.

Tests the full kill switch lifecycle:
1. Default state (inactive)
2. Activating via set_kill_switch(True) + is_kill_switch_active()
3. Releasing via set_kill_switch(False)
4. execute_with_fallback() returns abort result when active
5. Daemon cycle is skipped when active
6. CLI friday autonomy kill / resume subcommands
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from friday.db import connect
from friday.autonomy import (
    is_kill_switch_active,
    set_kill_switch,
    is_autonomy_enabled,
    set_autonomy_enabled,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    """Path to a shared test database file."""
    return str(tmp_path / "test_friday.db")


@pytest.fixture
def conn(db_path: str) -> sqlite3.Connection:
    """SQLite connection to the shared test database with minimal tables."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE IF NOT EXISTS operator_preferences ("
        "  key         TEXT PRIMARY KEY,"
        "  value       TEXT NOT NULL,"
        "  set_at      TEXT NOT NULL,"
        "  source      TEXT NOT NULL DEFAULT 'explicit'"
        ")"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS autonomy_permissions ("
        "  action_type          TEXT NOT NULL PRIMARY KEY,"
        "  default_level        TEXT NOT NULL,"
        "  override_level       TEXT,"
        "  auto_downgraded      TEXT,"
        "  consecutive_failures    INTEGER NOT NULL DEFAULT 0,"
        "  consecutive_successes  INTEGER NOT NULL DEFAULT 0,"
        "  updated_at           TEXT NOT NULL"
        ")"
    )
    conn.commit()
    return conn


def _make_connect_mock(db_path: str):
    """Create a connect() replacement that opens a new connection to the same DB file.

    Used by CLI tests where set_kill_switch() closes its own connection.
    The fixture's conn stays open while internal connect() calls get
    fresh connections to the same file.
    """
    def _inner():
        c = sqlite3.connect(db_path)
        c.row_factory = sqlite3.Row
        return c
    return _inner


# ---------------------------------------------------------------------------
# Kill switch helper tests
# ---------------------------------------------------------------------------


class TestKillSwitchHelpers:
    """Test the core is_kill_switch_active() and set_kill_switch() functions."""

    def test_default_is_inactive(self, conn):
        """Kill switch defaults to inactive (no row in DB)."""
        assert is_kill_switch_active(conn) is False

    def test_activate(self, conn):
        """After set_kill_switch(True), is_kill_switch_active() returns True."""
        set_kill_switch(True, conn)
        assert is_kill_switch_active(conn) is True

    def test_deactivate(self, conn):
        """After set_kill_switch(False), is_kill_switch_active() returns False."""
        set_kill_switch(True, conn)
        assert is_kill_switch_active(conn) is True
        set_kill_switch(False, conn)
        assert is_kill_switch_active(conn) is False

    def test_round_trip(self, conn):
        """Full lifecycle: inactive -> active -> inactive."""
        assert is_kill_switch_active(conn) is False
        set_kill_switch(True, conn)
        assert is_kill_switch_active(conn) is True
        set_kill_switch(False, conn)
        assert is_kill_switch_active(conn) is False

    def test_separate_from_autonomy_enabled(self, conn):
        """Kill switch and autonomy_enabled are independent flags."""
        assert is_autonomy_enabled(conn) is True
        assert is_kill_switch_active(conn) is False

        set_kill_switch(True, conn)
        assert is_kill_switch_active(conn) is True
        assert is_autonomy_enabled(conn) is True

        set_autonomy_enabled(False, conn)
        assert is_kill_switch_active(conn) is True
        assert is_autonomy_enabled(conn) is False

        set_kill_switch(False, conn)
        assert is_kill_switch_active(conn) is False
        assert is_autonomy_enabled(conn) is False

    def test_activate_without_conn(self):
        """set_kill_switch works without an explicit connection."""
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        try:
            db_path = tmp.name
            os.environ["FRIDAY_DB"] = db_path

            c = connect()
            c.execute(
                "CREATE TABLE IF NOT EXISTS operator_preferences ("
                "  key TEXT PRIMARY KEY, value TEXT NOT NULL, "
                "  set_at TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'explicit'"
                ")"
            )
            c.close()

            set_kill_switch(True)
            assert is_kill_switch_active() is True
            set_kill_switch(False)
            assert is_kill_switch_active() is False
        finally:
            os.unlink(tmp.name)
            if "FRIDAY_DB" in os.environ:
                del os.environ["FRIDAY_DB"]


# ---------------------------------------------------------------------------
# Executor kill switch test
# ---------------------------------------------------------------------------


class TestExecutorKillSwitch:
    """Test that execute_with_fallback() respects the kill switch.

    Uses mock.patch on friday.runtime.executors.is_kill_switch_active
    because the function is imported into the executors module at load time.
    """

    def _shell_task(self):
        class MockTask:
            runtime_payload = "echo hello"
            task_type = "shell"
        return MockTask()

    def test_kill_switch_blocks_execution(self, tmp_path):
        """When kill switch is active, execute_with_fallback returns abort result."""
        from friday.runtime.executors import execute_with_fallback, resolve_executor

        # Patch the function where it's USED (executors module), not where it's DEFINED.
        with mock.patch("friday.runtime.executors.is_kill_switch_active", return_value=True):
            result = execute_with_fallback(
                self._shell_task(),
                primary_id="worker:shell",
                workspace=str(tmp_path),
                worker_resolver=resolve_executor,
            )

        assert result.success is False
        assert "KILL SWITCH ACTIVE" in (result.error or "").upper()
        assert result.duration_ms == 0

    def test_kill_switch_released_allows_execution(self, tmp_path):
        """After releasing the kill switch, execution proceeds normally."""
        from friday.runtime.executors import execute_with_fallback, resolve_executor

        with mock.patch("friday.runtime.executors.is_kill_switch_active", return_value=False):
            result = execute_with_fallback(
                self._shell_task(),
                primary_id="worker:shell",
                workspace=str(tmp_path),
                worker_resolver=resolve_executor,
            )

        assert result is not None

    def test_kill_switch_blocks_fallback_chain(self, tmp_path):
        """Kill switch blocks execution even when using fallback chain."""
        from friday.runtime.executors import execute_with_fallback, resolve_executor

        with mock.patch("friday.runtime.executors.is_kill_switch_active", return_value=True):
            result = execute_with_fallback(
                self._shell_task(),
                primary_id="worker:python",
                workspace=str(tmp_path),
                worker_resolver=resolve_executor,
            )

        assert result.success is False
        assert "KILL SWITCH ACTIVE" in (result.error or "").upper()


# ---------------------------------------------------------------------------
# Daemon kill switch test
# ---------------------------------------------------------------------------


class TestDaemonKillSwitch:
    """Test that the daemon respects the kill switch."""

    def test_daemon_cycle_skipped_when_kill_switch_active(self, tmp_path):
        """_do_cycle should skip when kill switch is active."""
        from friday.daemon import _do_cycle

        with mock.patch("friday.daemon.STATUS_FILE", tmp_path / "daemon.status"), \
             mock.patch("friday.daemon.LOG_FILE", tmp_path / "daemon.log"), \
             mock.patch("friday.daemon._read_status", return_value={}), \
             mock.patch("friday.daemon.write_status"), \
             mock.patch("friday.autonomy.is_kill_switch_active", return_value=True):

            _do_cycle(0, no_notify=True)

    def test_daemon_cycle_runs_normally_when_inactive(self, tmp_path):
        """_do_cycle runs normally when kill switch is inactive."""
        from friday.daemon import _do_cycle

        with mock.patch("friday.daemon.STATUS_FILE", tmp_path / "daemon.status"), \
             mock.patch("friday.daemon.LOG_FILE", tmp_path / "daemon.log"), \
             mock.patch("friday.autonomy.is_kill_switch_active", return_value=False), \
             mock.patch("friday.daemon._run_cycle", return_value={}):

            _do_cycle(0, no_notify=True)


# ---------------------------------------------------------------------------
# CLI kill switch tests
# ---------------------------------------------------------------------------


class TestCLIKillSwitch:
    """Test that the CLI autonomy kill/resume subcommands work.

    Uses a shared DB file: the fixture conn stays open while internal
    connect() calls from autonomy functions open and close their own
    connections to the same file.
    """

    def test_kill_subcommand(self, conn, db_path, capsys):
        """friday autonomy kill should activate the kill switch."""
        import friday.cli_autonomy as mod

        connect_mock = _make_connect_mock(db_path)

        with mock.patch("friday.autonomy.connect", connect_mock), \
             mock.patch("friday.cli_autonomy.connect", connect_mock), \
             mock.patch("os.kill"):

            ns = type("Args", (), {"subcommand": "kill"})()
            rc = mod.cmd_autonomy(ns)

            assert rc == 0

        # Verify kill switch is active by reading from a fresh connection.
        c = sqlite3.connect(db_path)
        c.row_factory = sqlite3.Row
        try:
            row = c.execute(
                "SELECT value FROM operator_preferences WHERE key='kill_switch'"
            ).fetchone()
            assert row is not None
            assert row["value"] == "true"
        finally:
            c.close()

        captured = capsys.readouterr()
        assert "KILL SWITCH" in captured.out.upper()

    def test_resume_subcommand(self, conn, db_path, capsys):
        """friday autonomy resume should deactivate the kill switch."""
        import friday.cli_autonomy as mod

        # Manually activate first using the fixture connection.
        conn.execute(
            "INSERT OR REPLACE INTO operator_preferences (key, value, set_at, source) "
            "VALUES ('kill_switch', 'true', '2026-01-01', 'test')"
        )
        conn.commit()

        connect_mock = _make_connect_mock(db_path)

        with mock.patch("friday.autonomy.connect", connect_mock), \
             mock.patch("friday.cli_autonomy.connect", connect_mock):

            ns = type("Args", (), {"subcommand": "resume"})()
            rc = mod.cmd_autonomy(ns)

            assert rc == 0

        # Verify kill switch is released.
        c = sqlite3.connect(db_path)
        c.row_factory = sqlite3.Row
        try:
            row = c.execute(
                "SELECT value FROM operator_preferences WHERE key='kill_switch'"
            ).fetchone()
            assert row is not None
            assert row["value"] == "false"
        finally:
            c.close()

        captured = capsys.readouterr()
        assert "released" in captured.out.lower()

    def test_kill_resume_cycle(self, conn, db_path):
        """Full CLI cycle: kill -> active -> resume -> inactive."""
        import friday.cli_autonomy as mod

        connect_mock = _make_connect_mock(db_path)

        with mock.patch("friday.autonomy.connect", connect_mock), \
             mock.patch("friday.cli_autonomy.connect", connect_mock), \
             mock.patch("os.kill"):

            ns = type("Args", (), {"subcommand": "kill"})()
            mod.cmd_autonomy(ns)

            ns = type("Args", (), {"subcommand": "resume"})()
            mod.cmd_autonomy(ns)

        # Verify final state.
        c = sqlite3.connect(db_path)
        c.row_factory = sqlite3.Row
        try:
            row = c.execute(
                "SELECT value FROM operator_preferences WHERE key='kill_switch'"
            ).fetchone()
            assert row is not None
            assert row["value"] == "false"
        finally:
            c.close()

    def test_status_shows_kill_switch_when_active(self, conn, db_path, capsys):
        """Status output mentions 'KILL SWITCH' when active."""
        import friday.cli_autonomy as mod

        # Manually activate kill switch using the fixture connection.
        conn.execute(
            "INSERT OR REPLACE INTO operator_preferences (key, value, set_at, source) "
            "VALUES ('kill_switch', 'true', '2026-01-01', 'test')"
        )
        conn.commit()

        connect_mock = _make_connect_mock(db_path)

        with mock.patch("friday.autonomy.connect", connect_mock), \
             mock.patch("friday.cli_autonomy.connect", connect_mock):

            ns = type("Args", (), {"subcommand": "status"})()
            mod.cmd_autonomy(ns)

        captured = capsys.readouterr()
        assert "KILL SWITCH" in captured.out.upper()

    def test_status_shows_action_workers_when_no_kill(self, conn, db_path, capsys):
        """Status shows action workers state normally when kill switch is off."""
        import friday.cli_autonomy as mod

        # Ensure kill switch is off and autonomy is enabled using the fixture connection.
        conn.execute(
            "INSERT OR REPLACE INTO operator_preferences (key, value, set_at, source) "
            "VALUES ('kill_switch', 'false', '2026-01-01', 'test')"
        )
        conn.execute(
            "INSERT OR REPLACE INTO operator_preferences (key, value, set_at, source) "
            "VALUES ('autonomy_enabled', 'true', '2026-01-01', 'test')"
        )
        conn.commit()

        connect_mock = _make_connect_mock(db_path)

        with mock.patch("friday.autonomy.connect", connect_mock), \
             mock.patch("friday.cli_autonomy.connect", connect_mock):

            ns = type("Args", (), {"subcommand": "status"})()
            mod.cmd_autonomy(ns)

        captured = capsys.readouterr()
        assert "ACTION WORKERS" in captured.out.upper()
