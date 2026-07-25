"""Tests for the Friday Daemon (ambient observation loop)."""

from __future__ import annotations

import json
import os
import signal
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# PID file management
# ---------------------------------------------------------------------------


class TestPidManagement:
    def test_write_and_read_pid(self, tmp_path: Path) -> None:
        from friday.daemon import _write_pid, _read_pid, _remove_pid, FRIDAY_DIR

        # Override FRIDAY_DIR for testing.
        original = FRIDAY_DIR
        try:
            import friday.daemon as daemon
            daemon.FRIDAY_DIR = tmp_path
            daemon.PID_FILE = tmp_path / "daemon.pid"

            _write_pid(12345)
            assert daemon.PID_FILE.read_text().strip() == "12345"
            assert _read_pid() == 12345

            _remove_pid()
            assert not daemon.PID_FILE.exists()
            assert _read_pid() is None
        finally:
            daemon.FRIDAY_DIR = original
            daemon.PID_FILE = original / "daemon.pid"

    def test_is_pid_running(self) -> None:
        from friday.daemon import _is_pid_running

        # Current process is always running.
        assert _is_pid_running(os.getpid())

        # A very large PID cannot exist (within int32 range).
        assert not _is_pid_running(2147483647)


# ---------------------------------------------------------------------------
# Status file
# ---------------------------------------------------------------------------


class TestStatusFile:
    def test_write_and_read_status(self, tmp_path: Path) -> None:
        from friday.daemon import write_status, _read_status, STATUS_FILE, FRIDAY_DIR

        original = FRIDAY_DIR
        try:
            import friday.daemon as daemon
            daemon.FRIDAY_DIR = tmp_path
            daemon.STATUS_FILE = tmp_path / "daemon.status"

            # Default status when no file exists.
            default = _read_status()
            assert default["state"] == "stopped"
            assert default["cycle_count"] == 0

            # Write and read.
            write_status(state="running", pid=999, cycle_count=5)
            status = _read_status()
            assert status["state"] == "running"
            assert status["pid"] == 999
            assert status["cycle_count"] == 5

            # Partial update preserves other fields.
            write_status(last_cycle_outcome="succeeded")
            status = _read_status()
            assert status["state"] == "running"  # preserved
            assert status["cycle_count"] == 5     # preserved
            assert status["last_cycle_outcome"] == "succeeded"
        finally:
            daemon.FRIDAY_DIR = original
            daemon.STATUS_FILE = original / "daemon.status"

    def test_corrupted_status_file(self, tmp_path: Path) -> None:
        from friday.daemon import _read_status, STATUS_FILE, FRIDAY_DIR

        original = FRIDAY_DIR
        try:
            import friday.daemon as daemon
            daemon.FRIDAY_DIR = tmp_path
            daemon.STATUS_FILE = tmp_path / "daemon.status"

            # Corrupted JSON.
            daemon.STATUS_FILE.write_text("not json")
            status = _read_status()
            assert status["state"] == "stopped"
            assert status["cycle_count"] == 0
        finally:
            daemon.FRIDAY_DIR = original
            daemon.STATUS_FILE = original / "daemon.status"


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


class TestLogging:
    def test_log_creates_file(self, tmp_path: Path) -> None:
        from friday.daemon import _log, LOG_FILE, FRIDAY_DIR

        original = FRIDAY_DIR
        try:
            import friday.daemon as daemon
            daemon.FRIDAY_DIR = tmp_path
            daemon.LOG_FILE = tmp_path / "daemon.log"

            _log("Test message")
            assert daemon.LOG_FILE.exists()
            content = daemon.LOG_FILE.read_text()
            assert "Test message" in content
            assert "[" in content  # timestamp
        finally:
            daemon.FRIDAY_DIR = original
            daemon.LOG_FILE = original / "daemon.log"

    def test_log_appends(self, tmp_path: Path) -> None:
        from friday.daemon import _log, LOG_FILE, FRIDAY_DIR

        original = FRIDAY_DIR
        try:
            import friday.daemon as daemon
            daemon.FRIDAY_DIR = tmp_path
            daemon.LOG_FILE = tmp_path / "daemon.log"

            _log("First")
            _log("Second")
            lines = daemon.LOG_FILE.read_text().splitlines()
            assert len(lines) == 2
            assert "First" in lines[0]
            assert "Second" in lines[1]
        finally:
            daemon.FRIDAY_DIR = original
            daemon.LOG_FILE = original / "daemon.log"


# ---------------------------------------------------------------------------
# Cycle runner
# ---------------------------------------------------------------------------


class TestCycleRunner:
    @patch("friday.observe.refresh")
    def test_successful_cycle(self, mock_refresh, tmp_path, monkeypatch) -> None:
        """A successful cycle updates watch_history and returns outcome."""
        from friday.daemon import _run_cycle, FRIDAY_DIR, LOCK_FILE

        # Use a temp .friday dir and a unique lock file name to avoid collision.
        original_friday = FRIDAY_DIR
        original_lock = LOCK_FILE
        try:
            import friday.daemon as daemon
            daemon.FRIDAY_DIR = tmp_path
            test_lock = tmp_path / "test-watch.lock"
            daemon.LOCK_FILE = test_lock

            # Mock the refresh return value.
            mock_report = MagicMock()
            mock_report.repos_scanned = 5
            mock_report.repos_changed = 2
            mock_report.knowledge_updated = 3
            mock_report.understanding_updated = 1
            mock_report.initiatives_changed = 0
            mock_report.insights_changed = 0
            mock_refresh.return_value = mock_report

            # Use an in-memory DB for the watch_history insert.
            monkeypatch.setenv("FRIDAY_DB", ":memory:")

            result = _run_cycle()
            assert result["cycle_outcome"] == "succeeded"
            assert result["repos_scanned"] == 5
            assert result["repos_changed"] == 2
        finally:
            daemon.FRIDAY_DIR = original_friday
            daemon.LOCK_FILE = original_lock

    @patch("friday.observe.refresh")
    def test_failed_cycle_records_error(self, mock_refresh, tmp_path, monkeypatch) -> None:
        """A failed cycle captures the error in the result."""
        from friday.daemon import _run_cycle, FRIDAY_DIR, LOCK_FILE

        original_friday = FRIDAY_DIR
        original_lock = LOCK_FILE
        try:
            import friday.daemon as daemon
            daemon.FRIDAY_DIR = tmp_path
            test_lock = tmp_path / "test-watch.lock"
            daemon.LOCK_FILE = test_lock

            mock_refresh.side_effect = RuntimeError("Pipeline crashed!")
            monkeypatch.setenv("FRIDAY_DB", ":memory:")

            result = _run_cycle()
            assert result["cycle_outcome"] == "failed"
            assert "Pipeline crashed!" in result.get("error_detail", "")
        finally:
            daemon.FRIDAY_DIR = original_friday
            daemon.LOCK_FILE = original_lock

    @patch("friday.daemon._run_cycle")
    def test_do_cycle_updates_status(self, mock_run_cycle, tmp_path) -> None:
        """_do_cycle writes status after running."""
        from friday.daemon import _do_cycle, FRIDAY_DIR, STATUS_FILE

        original_friday = FRIDAY_DIR
        original_status = STATUS_FILE
        try:
            import friday.daemon as daemon
            daemon.FRIDAY_DIR = tmp_path
            daemon.STATUS_FILE = tmp_path / "daemon.status"

            mock_run_cycle.return_value = {
                "cycle_outcome": "succeeded",
                "repos_scanned": 3,
                "repos_changed": 1,
            }

            _do_cycle(0, no_notify=True)

            status = daemon._read_status()
            assert status["cycle_count"] == 1
            assert status["last_cycle_outcome"] == "succeeded"
        finally:
            daemon.FRIDAY_DIR = original_friday
            daemon.STATUS_FILE = original_status


# ---------------------------------------------------------------------------
# Daemon lifecycle
# ---------------------------------------------------------------------------


class TestDaemonLifecycle:
    def test_is_running_when_no_pid_file(self, tmp_path) -> None:
        from friday.daemon import is_running, FRIDAY_DIR, PID_FILE

        original_friday = FRIDAY_DIR
        try:
            import friday.daemon as daemon
            daemon.FRIDAY_DIR = tmp_path
            daemon.PID_FILE = tmp_path / "daemon.pid"

            assert not is_running()
        finally:
            daemon.FRIDAY_DIR = original_friday
            daemon.PID_FILE = original_friday / "daemon.pid"

    def test_is_running_with_stale_pid(self, tmp_path) -> None:
        """A PID file with a non-existent process ID should return False."""
        from friday.daemon import is_running, _write_pid, FRIDAY_DIR, PID_FILE

        original_friday = FRIDAY_DIR
        try:
            import friday.daemon as daemon
            daemon.FRIDAY_DIR = tmp_path
            daemon.PID_FILE = tmp_path / "daemon.pid"

            _write_pid(999999999)  # unlikely to be a real PID
            assert not is_running()
        finally:
            daemon.FRIDAY_DIR = original_friday
            daemon.PID_FILE = original_friday / "daemon.pid"

    def test_stop_returns_1_when_not_running(self, tmp_path) -> None:
        from friday.daemon import stop, FRIDAY_DIR, PID_FILE

        original_friday = FRIDAY_DIR
        try:
            import friday.daemon as daemon
            daemon.FRIDAY_DIR = tmp_path
            daemon.PID_FILE = tmp_path / "daemon.pid"

            assert stop() == 1  # not running
        finally:
            daemon.FRIDAY_DIR = original_friday
            daemon.PID_FILE = original_friday / "daemon.pid"


# ---------------------------------------------------------------------------
# Notification
# ---------------------------------------------------------------------------


class TestNotifications:
    def test_notify_does_not_crash(self) -> None:
        """Notification should silently handle missing notify-send."""
        from friday.daemon import _notify

        # Should not raise.
        _notify("Test Title", "Test Message")


# ---------------------------------------------------------------------------
# Signal handling
# ---------------------------------------------------------------------------


class TestSignalHandling:
    def test_sigterm_sets_shutdown_flag(self) -> None:
        import friday.daemon as daemon_mod
        from friday.daemon import _handle_sigterm

        daemon_mod._daemon_shutdown = False
        _handle_sigterm(None, None)
        assert daemon_mod._daemon_shutdown is True

    def test_sighup_sets_cycle_flag(self) -> None:
        import friday.daemon as daemon_mod
        from friday.daemon import _handle_sighup

        daemon_mod._daemon_cycle_now = False
        _handle_sighup(None, None)
        assert daemon_mod._daemon_cycle_now is True


# ---------------------------------------------------------------------------
# CLI daemon module
# ---------------------------------------------------------------------------


class TestCliDaemon:
    def test_cli_daemon_status_shows_stopped(self, tmp_path, capsys) -> None:
        """friday daemon status should show 'stopped' when no PID file."""
        from friday.cli_daemon import cmd_daemon
        from friday.daemon import FRIDAY_DIR, STATUS_FILE, PID_FILE

        import friday.daemon as daemon_mod
        import friday.cli_daemon as cli_mod

        original_friday = FRIDAY_DIR
        original_status = STATUS_FILE
        original_pid = PID_FILE
        try:
            daemon_mod.FRIDAY_DIR = tmp_path
            daemon_mod.STATUS_FILE = tmp_path / "daemon.status"
            daemon_mod.PID_FILE = tmp_path / "daemon.pid"
            cli_mod.FRIDAY_DIR = tmp_path
            cli_mod.PID_FILE = tmp_path / "daemon.pid"
            cli_mod.STATUS_FILE = tmp_path / "daemon.status"

            import argparse
            args = argparse.Namespace(action="status", interval=900, no_notify=False, lines=50)
            rc = cmd_daemon(args)
            assert rc == 0

            captured = capsys.readouterr()
            assert "stopped" in captured.out.lower() or "stopped" in captured.out
        finally:
            daemon_mod.FRIDAY_DIR = original_friday
            daemon_mod.STATUS_FILE = original_status
            daemon_mod.PID_FILE = original_pid
            cli_mod.FRIDAY_DIR = original_friday
            cli_mod.PID_FILE = original_pid
            cli_mod.STATUS_FILE = original_status

    def test_cli_daemon_logs_empty(self, tmp_path, capsys) -> None:
        """friday daemon logs should handle empty log file."""
        from friday.cli_daemon import cmd_daemon
        from friday.daemon import LOG_FILE, FRIDAY_DIR

        import friday.daemon as daemon_mod
        import friday.cli_daemon as cli_mod

        original_friday = FRIDAY_DIR
        original_log = LOG_FILE
        try:
            daemon_mod.FRIDAY_DIR = tmp_path
            daemon_mod.LOG_FILE = tmp_path / "daemon.log"
            cli_mod.LOG_FILE = tmp_path / "daemon.log"
            daemon_mod.LOG_FILE.touch()  # empty file

            import argparse
            args = argparse.Namespace(action="logs", lines=50, interval=900, no_notify=False)
            rc = cmd_daemon(args)
            assert rc == 0
        finally:
            daemon_mod.FRIDAY_DIR = original_friday
            daemon_mod.LOG_FILE = original_log
            cli_mod.LOG_FILE = original_log
