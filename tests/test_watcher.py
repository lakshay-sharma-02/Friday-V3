"""Tests for the Persistent Watcher engine — CRUD and condition checking."""

from __future__ import annotations

import pytest

from friday.watcher import (
    WatcherEngine, _check_shell_exit_code,
    _check_file_modified, _check_process_running,
    format_watcher, format_watchers,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def eng():
    from friday.db import connect
    conn = connect(":memory:")
    engine = WatcherEngine(conn)
    yield engine
    conn.close()


# ---------------------------------------------------------------------------
# CRUD operations
# ---------------------------------------------------------------------------


class TestCRUD:
    def test_create_and_get(self, eng):
        w = eng.create("tests pass", "shell_exit_code", {"command": "echo ok"})
        assert w.id > 0
        assert w.name == "tests pass"
        assert w.condition_type == "shell_exit_code"

    def test_get_returns_none_for_missing(self, eng):
        assert eng.get("nonexistent") is None

    def test_get_returns_watcher(self, eng):
        eng.create("test-watcher", "shell_exit_code", {"command": "echo ok"})
        w = eng.get("test-watcher")
        assert w is not None
        assert w.name == "test-watcher"

    def test_list_all_empty(self, eng):
        assert eng.list_all() == []

    def test_list_all(self, eng):
        eng.create("a", "shell_exit_code", {"command": "echo a"})
        eng.create("b", "http_status", {"url": "http://localhost"})
        watchers = eng.list_all()
        assert len(watchers) == 2
        assert watchers[0].name == "a"
        assert watchers[1].name == "b"

    def test_create_duplicate_raises(self, eng):
        eng.create("dup", "shell_exit_code", {"command": "echo"})
        with pytest.raises(ValueError, match="already exists"):
            eng.create("dup", "shell_exit_code", {"command": "echo"})

    def test_create_invalid_type_raises(self, eng):
        with pytest.raises(ValueError, match="Invalid condition type"):
            eng.create("bad", "invalid_type", {})

    def test_delete(self, eng):
        eng.create("temp", "shell_exit_code", {"command": "echo"})
        assert eng.get("temp") is not None
        assert eng.delete("temp") is True
        assert eng.get("temp") is None

    def test_delete_nonexistent(self, eng):
        assert eng.delete("nonexistent") is False


# ---------------------------------------------------------------------------
# Condition checkers
# ---------------------------------------------------------------------------


class TestConditionCheckers:
    def test_shell_exit_code_success(self):
        met, error = _check_shell_exit_code({"command": "echo ok"})
        assert met is True
        assert error is None

    def test_shell_exit_code_failure(self):
        met, error = _check_shell_exit_code({"command": "false"})
        assert met is False
        assert error is not None

    def test_shell_exit_code_missing_command(self):
        met, error = _check_shell_exit_code({})
        assert met is False
        assert "No command" in error

    def test_shell_exit_code_timeout(self):
        met, error = _check_shell_exit_code({
            "command": "sleep 10",
            "timeout": 0.1,
        })
        assert met is False
        assert "timed out" in error.lower()

    def test_file_modified_exists(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("hello")
        met, error = _check_file_modified({"path": str(f)}, prev_result=None)
        assert met is True
        assert error is None

    def test_file_modified_not_found(self):
        met, error = _check_file_modified({"path": "/nonexistent"}, prev_result=None)
        assert met is False
        assert "not found" in error.lower()

    def test_file_modified_no_change(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("hello")
        # First check (prev_result=None) → fires
        met, _ = _check_file_modified({"path": str(f)}, prev_result=None)
        assert met is True
        # Check again with a recent last_checked_at → doesn't fire
        from datetime import datetime, timezone
        recent = datetime.now(timezone.utc).isoformat()
        met, _ = _check_file_modified(
            {"path": str(f), "last_checked_at": recent},
            prev_result=True,
        )
        assert met is False

    def test_file_modified_change_detected(self, tmp_path):
        """File modified after last_checked_at triggers again."""
        f = tmp_path / "test.txt"
        f.write_text("hello")
        from datetime import datetime, timedelta, timezone
        old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        met, _ = _check_file_modified(
            {"path": str(f), "last_checked_at": old},
            prev_result=True,
        )
        assert met is True

    def test_process_running_pgrep_available(self):
        # pgrep for a common system process
        met, error = _check_process_running({"process": "systemd"})
        # This may fail if pgrep isn't available or systemd isn't running in test env
        # Just verify it returns without crashing
        assert isinstance(met, bool)
        assert error is None or isinstance(error, str)

    def test_process_running_no_name(self):
        met, error = _check_process_running({})
        assert met is False
        assert "No process" in error


# ---------------------------------------------------------------------------
# Check engine
# ---------------------------------------------------------------------------


class TestCheck:
    def test_check_one_returns_result(self, eng):
        eng.create("test-check", "shell_exit_code", {"command": "echo ok"})
        watcher = eng.get("test-check")
        result = eng.check_one(watcher)
        assert result["met"] is True
        assert result["triggered"] is True  # first check always triggers
        assert result["error"] is None

    def test_check_one_failure(self, eng):
        eng.create("fail-check", "shell_exit_code", {"command": "false"})
        watcher = eng.get("fail-check")
        result = eng.check_one(watcher)
        assert result["met"] is False
        assert result["triggered"] is False

    def test_check_one_updates_db(self, eng):
        eng.create("db-check", "shell_exit_code", {"command": "echo ok"})
        watcher = eng.get("db-check")
        eng.check_one(watcher)
        updated = eng.get("db-check")
        assert updated.last_checked_at is not None
        assert updated.last_result is True

    def test_check_all_runs_due_watchers(self, eng):
        eng.create("w1", "shell_exit_code", {"command": "echo ok"})
        eng.create("w2", "shell_exit_code", {"command": "echo hi"})
        results = eng.check_all()
        assert len(results) == 2
        assert results[0]["met"] is True
        assert results[1]["met"] is True

    def test_is_due_new_watcher(self, eng):
        from friday.watcher import Watcher
        w = Watcher(name="test", condition_type="shell_exit_code",
                     last_checked_at=None)
        assert eng._is_due(w) is True

    def test_is_due_after_interval(self, eng):
        from datetime import datetime, timedelta, timezone
        from friday.watcher import Watcher
        old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        w = Watcher(name="old", condition_type="shell_exit_code",
                     last_checked_at=old, check_interval_seconds=300)
        assert eng._is_due(w) is True

    def test_is_due_not_yet(self, eng):
        from datetime import datetime, timezone
        from friday.watcher import Watcher
        recent = datetime.now(timezone.utc).isoformat()
        w = Watcher(name="recent", condition_type="shell_exit_code",
                     last_checked_at=recent, check_interval_seconds=3600)
        assert eng._is_due(w) is False


# ---------------------------------------------------------------------------
# Auto-watcher creation and pruning
# ---------------------------------------------------------------------------


class TestAutoWatchers:
    def test_create_auto_watcher(self, eng):
        w = eng.create_auto_watcher("browser test", "active_app", {"app": "chromium"})
        assert w is not None
        assert w.name.startswith("[auto] ")
        assert w.condition_type == "active_app"
        assert w.repeat is True

    def test_create_auto_watcher_duplicate_replaces(self, eng):
        w1 = eng.create_auto_watcher("test", "active_app", {"app": "chromium"})
        w2 = eng.create_auto_watcher("test", "active_app", {"app": "chromium"})
        # Should replace, so only one should exist
        watchers = [w for w in eng.list_all() if w.name == w1.name]
        assert len(watchers) == 1

    def test_prune_auto_watchers_keeps_fresh(self, eng):
        eng.create_auto_watcher("fresh", "active_app", {"app": "code"}, ttl_minutes=60)
        pruned = eng.prune_auto_watchers()
        assert pruned == 0
        # Should still exist
        assert eng.get("[auto] fresh") is not None

    def test_create_auto_watcher_with_session(self, eng):
        """Auto-watchers get the right check interval based on TTL."""
        w = eng.create_auto_watcher(
            "test", "active_app", {"app": "code"}, ttl_minutes=10)
        assert w is not None
        # check_interval should be min(ttl_minutes * 60, 300)
        assert w.check_interval_seconds == 300  # min(600, 300) = 300

    def test_auto_watcher_short_ttl(self, eng):
        w = eng.create_auto_watcher(
            "short", "active_app", {"app": "code"}, ttl_minutes=1)
        assert w is not None
        assert w.check_interval_seconds == 60  # min(60, 300) = 60


# ---------------------------------------------------------------------------
# Screen-aware condition types (unit tests via mock)
# ---------------------------------------------------------------------------


class TestScreenConditionCheckers:
    def test_active_app_matches_process(self, monkeypatch):
        """_check_active_app returns True when app matches active process."""
        from friday.screen import ScreenContext

        def mock_collect(*args, **kwargs):
            return ScreenContext(active_window_process="Code", active_window_title="main.py")
        monkeypatch.setattr("friday.screen.collect_screen_context", mock_collect)

        from friday.watcher import _check_active_app
        met, _ = _check_active_app({"app": "code"}, None)
        assert met is True

    def test_active_app_matches_class(self, monkeypatch):
        """_check_active_app matches on window class too."""
        from friday.screen import ScreenContext

        def mock_collect(*args, **kwargs):
            return ScreenContext(
                active_window_class="Chromium-browser",
                active_window_process="Chromium",
                active_window_title="GitHub — Chromium",
            )
        monkeypatch.setattr("friday.screen.collect_screen_context", mock_collect)

        from friday.watcher import _check_active_app
        met, _ = _check_active_app({"app": "chromium"}, None)
        assert met is True

    def test_active_app_no_match(self, monkeypatch):
        from friday.screen import ScreenContext

        def mock_collect(*args, **kwargs):
            return ScreenContext(active_window_process="Alacritty", active_window_title="~")
        monkeypatch.setattr("friday.screen.collect_screen_context", mock_collect)

        from friday.watcher import _check_active_app
        met, error = _check_active_app({"app": "code"}, None)
        assert met is False
        assert "not active" in error.lower()

    def test_active_app_window_title_regex(self, monkeypatch):
        from friday.screen import ScreenContext

        def mock_collect(*args, **kwargs):
            return ScreenContext(active_window_title="GitHub — Brave", active_window_process="Brave")
        monkeypatch.setattr("friday.screen.collect_screen_context", mock_collect)

        from friday.watcher import _check_active_app
        met, _ = _check_active_app({"window_title": "GitHub"}, None)
        assert met is True

    def test_active_app_bad_regex(self, monkeypatch):
        from friday.screen import ScreenContext

        def mock_collect(*args, **kwargs):
            return ScreenContext(active_window_title="Hello")
        monkeypatch.setattr("friday.screen.collect_screen_context", mock_collect)

        from friday.watcher import _check_active_app
        met, error = _check_active_app({"window_title": "["}, None)
        assert met is False
        assert "Invalid regex" in error

    def test_active_app_no_params(self):
        from friday.watcher import _check_active_app
        met, error = _check_active_app({}, None)
        assert met is False
        assert "No app" in error

    def test_clipboard_content_contains_match(self, monkeypatch):
        def mock_read_clipboard(*args, **kwargs):
            return ("https://github.com/friday-project", "wl-paste")
        monkeypatch.setattr("friday.screen._read_clipboard", mock_read_clipboard)

        from friday.watcher import _check_clipboard_content
        met, _ = _check_clipboard_content({"contains": "github.com"}, None)
        assert met is True

    def test_clipboard_content_no_match(self, monkeypatch):
        def mock_read_clipboard(*args, **kwargs):
            return ("git commit -m 'fix'", "wl-paste")
        monkeypatch.setattr("friday.screen._read_clipboard", mock_read_clipboard)

        from friday.watcher import _check_clipboard_content
        met, error = _check_clipboard_content({"contains": "github"}, None)
        assert met is False
        assert "did not contain" in error

    def test_clipboard_content_empty(self, monkeypatch):
        def mock_read_clipboard(*args, **kwargs):
            return ("", "wl-paste")
        monkeypatch.setattr("friday.screen._read_clipboard", mock_read_clipboard)

        from friday.watcher import _check_clipboard_content
        met, error = _check_clipboard_content({"contains": "test"}, None)
        assert met is False
        assert "empty" in error.lower()

    def test_clipboard_content_regex_match(self, monkeypatch):
        def mock_read_clipboard(*args, **kwargs):
            return ("ERROR: ModuleNotFoundError: No module named 'foo'", "wl-paste")
        monkeypatch.setattr("friday.screen._read_clipboard", mock_read_clipboard)

        from friday.watcher import _check_clipboard_content
        met, _ = _check_clipboard_content({"regex": r"Error|ERROR"}, None)
        assert met is True

    def test_clipboard_content_min_length(self, monkeypatch):
        def mock_read_clipboard(*args, **kwargs):
            return ("hi", "wl-paste")
        monkeypatch.setattr("friday.screen._read_clipboard", mock_read_clipboard)

        from friday.watcher import _check_clipboard_content
        met, error = _check_clipboard_content({"contains": "hi", "min_length": 10}, None)
        assert met is False

    def test_clipboard_no_params(self):
        from friday.watcher import _check_clipboard_content
        met, error = _check_clipboard_content({}, None)
        assert met is False
        assert "No " in error

    def test_window_title_contains(self, monkeypatch):
        from friday.screen import ScreenContext

        def mock_collect(*args, **kwargs):
            return ScreenContext(active_window_title="main.py — VS Code")
        monkeypatch.setattr("friday.screen.collect_screen_context", mock_collect)

        from friday.watcher import _check_window_title
        met, _ = _check_window_title({"contains": "VS Code"}, None)
        assert met is True

    def test_window_title_regex(self, monkeypatch):
        from friday.screen import ScreenContext

        def mock_collect(*args, **kwargs):
            return ScreenContext(active_window_title="GitHub.com — Brave")
        monkeypatch.setattr("friday.screen.collect_screen_context", mock_collect)

        from friday.watcher import _check_window_title
        met, _ = _check_window_title({"title": r"GitHub|github"}, None)
        assert met is True

    def test_window_title_no_window(self, monkeypatch):
        from friday.screen import ScreenContext

        def mock_collect(*args, **kwargs):
            return ScreenContext(active_window_title="")
        monkeypatch.setattr("friday.screen.collect_screen_context", mock_collect)

        from friday.watcher import _check_window_title
        met, error = _check_window_title({"contains": "test"}, None)
        assert met is False
        assert "Could not detect" in error


# ---------------------------------------------------------------------------
# Auto-watcher global mode
# ---------------------------------------------------------------------------


class TestGlobalMode:
    def test_global_mode_defaults_to_off(self):
        from friday.db import connect
        from friday.watcher import is_global_mode

        conn = connect(":memory:")
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS operator_preferences (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    set_at TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'explicit'
                );
            """)
            conn.commit()
            assert is_global_mode(conn) is False
        finally:
            conn.close()

    def test_set_global_mode_on(self):
        from friday.db import connect
        from friday.watcher import is_global_mode, set_global_mode

        conn = connect(":memory:")
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS operator_preferences (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    set_at TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'explicit'
                );
            """)
            conn.commit()

            set_global_mode(conn, True)
            assert is_global_mode(conn) is True

            set_global_mode(conn, False)
            assert is_global_mode(conn) is False
        finally:
            conn.close()

    def test_global_bypasses_tuning(self):
        from friday.db import connect
        from friday.watcher import (
            should_create_auto_watcher, set_global_mode,
            set_tuning_rule, is_global_mode,
        )

        conn = connect(":memory:")
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS operator_preferences (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    set_at TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'explicit'
                );
            """)
            conn.commit()

            # Set a tuning rule to ignore brave.
            set_tuning_rule(conn, "brave", "ignore")
            # Without global mode, brave should be ignored.
            assert should_create_auto_watcher(conn, "brave") is False

            # Enable global mode.
            set_global_mode(conn, True)
            # Now even with the ignore rule, global mode wins.
            assert should_create_auto_watcher(conn, "brave") is True

            # Disable global mode.
            set_global_mode(conn, False)
            # Back to normal — brave should be ignored again.
            assert should_create_auto_watcher(conn, "brave") is False
        finally:
            conn.close()

    def test_global_returns_true_for_unknown_app(self):
        from friday.db import connect
        from friday.watcher import should_create_auto_watcher, set_global_mode

        conn = connect(":memory:")
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS operator_preferences (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    set_at TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'explicit'
                );
            """)
            conn.commit()

            # Unknown app normally returns False.
            assert should_create_auto_watcher(conn, "SomeRandomApp") is False

            # With global mode, unknown app returns True.
            set_global_mode(conn, True)
            assert should_create_auto_watcher(conn, "SomeRandomApp") is True
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Auto-watcher tuning config
# ---------------------------------------------------------------------------


class TestTuningConfig:
    def test_tune_key_format(self):
        from friday.watcher import _tune_key
        key = _tune_key("Brave Browser")
        assert "auto_watcher_tune_" in key
        assert "brave" in key
        assert " " not in key

    def test_set_and_get_rules(self):
        from friday.db import connect
        from friday.watcher import get_tuning_rules, set_tuning_rule, remove_tuning_rule

        conn = connect(":memory:")
        try:
            # Ensure operator_preferences table exists.
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS operator_preferences (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    set_at TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'explicit'
                );
            """)
            conn.commit()

            # No rules initially.
            assert get_tuning_rules(conn) == []

            # Add a rule.
            set_tuning_rule(conn, "brave", "ignore")
            rules = get_tuning_rules(conn)
            assert len(rules) == 1
            assert rules[0]["app"] == "brave"
            assert rules[0]["action"] == "ignore"

            # Remove it.
            removed = remove_tuning_rule(conn, "brave")
            assert removed is True
            assert get_tuning_rules(conn) == []

        finally:
            conn.close()

    def test_set_invalid_action(self):
        from friday.watcher import set_tuning_rule
        import sqlite3
        conn = sqlite3.connect(":memory:")
        try:
            with pytest.raises(ValueError, match="Must be 'watch' or 'ignore'"):
                set_tuning_rule(conn, "test", "invalid")
        finally:
            conn.close()

    def test_reset_defaults(self):
        from friday.db import connect
        from friday.watcher import (
            get_tuning_rules, set_tuning_rule, reset_tuning_defaults
        )

        conn = connect(":memory:")
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS operator_preferences (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    set_at TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'explicit'
                );
            """)
            conn.commit()

            set_tuning_rule(conn, "brave", "ignore")
            set_tuning_rule(conn, "code", "watch")
            assert len(get_tuning_rules(conn)) == 2

            count = reset_tuning_defaults(conn)
            assert count == 2
            assert get_tuning_rules(conn) == []

        finally:
            conn.close()

    def test_should_create_auto_watcher_by_category(self):
        from friday.db import connect
        from friday.watcher import should_create_auto_watcher

        conn = connect(":memory:")
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS operator_preferences (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    set_at TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'explicit'
                );
            """)
            conn.commit()

            # Known browser → should watch.
            assert should_create_auto_watcher(conn, "Brave-browser") is True
            # Known IDE → should watch.
            assert should_create_auto_watcher(conn, "Code") is True
            # Unknown app → should NOT watch.
            assert should_create_auto_watcher(conn, "RandomApp") is False
            # Empty → should NOT watch.
            assert should_create_auto_watcher(conn, "") is False
        finally:
            conn.close()

    def test_should_create_auto_watcher_tuning_override(self):
        from friday.db import connect
        from friday.watcher import (
            should_create_auto_watcher, set_tuning_rule,
            get_tuning_rules, reset_tuning_defaults
        )

        conn = connect(":memory:")
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS operator_preferences (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    set_at TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'explicit'
                );
            """)
            conn.commit()

            # Known browser normally watches.
            assert should_create_auto_watcher(conn, "brave") is True

            # After adding ignore rule, it should be ignored.
            set_tuning_rule(conn, "brave", "ignore")
            assert should_create_auto_watcher(conn, "brave") is False

            # Unknown app normally doesn't watch.
            assert should_create_auto_watcher(conn, "slack") is False

            # After adding watch rule, it should watch.
            set_tuning_rule(conn, "slack", "watch")
            assert should_create_auto_watcher(conn, "slack") is True
        finally:
            conn.close()

    def test_should_create_auto_watcher_substring_match(self):
        from friday.db import connect
        from friday.watcher import should_create_auto_watcher, set_tuning_rule

        conn = connect(":memory:")
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS operator_preferences (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    set_at TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'explicit'
                );
            """)
            conn.commit()

            # Set ignore for "slack" — should match "Slack" and "slack-desktop".
            set_tuning_rule(conn, "slack", "ignore")
            assert should_create_auto_watcher(conn, "slack-desktop") is False
            assert should_create_auto_watcher(conn, "Slack") is False
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Auto-watcher stats
# ---------------------------------------------------------------------------


class TestAutoWatcherStats:
    def test_stats_empty_db(self):
        from friday.watcher import get_auto_watcher_stats
        from friday.db import connect

        conn = connect(":memory:")
        try:
            stats = get_auto_watcher_stats(conn)
            assert stats["active_auto_watchers"] == 0
            assert stats["triggered_count"] == 0
            assert stats["total_traditional"] == 0
            assert stats["by_app"] == []
            assert stats["ignored_apps"] == []
            assert stats["global_mode"]["enabled"] is False
        finally:
            conn.close()

    def test_stats_with_auto_watchers(self, eng):
        from friday.watcher import get_auto_watcher_stats

        # Create some auto-watchers.
        eng.create_auto_watcher("browser: Brave active", "active_app", {"app": "Brave"})
        eng.create_auto_watcher("browser: Chrome active", "active_app", {"app": "Chrome"})
        eng.create_auto_watcher("url: https://github.com", "window_title", {"contains": "github"})

        # Create a traditional watcher.
        eng.create("tests pass", "shell_exit_code", {"command": "pytest"})

        stats = get_auto_watcher_stats(eng._conn)
        assert stats["active_auto_watchers"] == 3
        assert stats["total_traditional"] == 1
        assert stats["triggered_count"] == 0  # none have fired
        assert len(stats["by_app"]) == 3

    def test_stats_counts_triggers(self, eng):
        from friday.watcher import get_auto_watcher_stats

        eng.create_auto_watcher("browser: Brave active", "active_app", {"app": "Brave"})
        eng.create_auto_watcher("browser: Chrome active", "active_app", {"app": "Chrome"})

        # Manually set one watcher's last_result to simulate trigger.
        conn = eng._conn
        conn.execute(
            "UPDATE persistent_watchers SET last_result=1 WHERE name=?",
            ("[auto] browser: Brave active",),
        )
        conn.commit()

        stats = get_auto_watcher_stats(conn)
        assert stats["triggered_count"] == 1
        # Brave should show 1 trigger.
        brave_stats = [a for a in stats["by_app"] if a["app"] == "Brave"]
        assert len(brave_stats) == 1
        assert brave_stats[0]["triggered"] == 1
        assert brave_stats[0]["total"] == 1

    def test_stats_ignored_apps(self):
        from friday.watcher import get_auto_watcher_stats, set_tuning_rule
        from friday.db import connect

        conn = connect(":memory:")
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS operator_preferences (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    set_at TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'explicit'
                );
            """)
            conn.commit()

            set_tuning_rule(conn, "slack", "ignore")
            set_tuning_rule(conn, "spotify", "ignore")

            stats = get_auto_watcher_stats(conn)
            assert "slack" in stats["ignored_apps"]
            assert "spotify" in stats["ignored_apps"]
        finally:
            conn.close()

    def test_stats_global_mode_timing(self):
        from friday.watcher import (
            get_auto_watcher_stats, set_global_mode,
        )
        from friday.db import connect

        conn = connect(":memory:")
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS operator_preferences (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    set_at TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'explicit'
                );
            """)
            conn.commit()

            # Global mode off.
            stats = get_auto_watcher_stats(conn)
            assert stats["global_mode"]["enabled"] is False

            # Enable global mode.
            set_global_mode(conn, True)
            stats = get_auto_watcher_stats(conn)
            assert stats["global_mode"]["enabled"] is True
            assert stats["global_mode"]["enabled_since"] is not None

            # Duration should be a positive float.
            assert stats["global_mode"]["duration_hours"] >= 0
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


class TestFormatting:
    def test_format_watcher(self, eng):
        eng.create("test", "shell_exit_code", {"command": "echo ok"})
        w = eng.get("test")
        text = format_watcher(w)
        assert "test" in text
        assert "shell_exit_code" in text

    def test_format_watcher_verbose(self, eng):
        eng.create("verbose", "shell_exit_code", {"command": "echo ok"})
        w = eng.get("verbose")
        text = format_watcher(w, verbose=True)
        assert "command" in text
        assert "echo ok" in text

    def test_format_watchers_empty(self):
        text = format_watchers([])
        assert "No persistent watchers" in text

    def test_format_watchers(self, eng):
        eng.create("a", "shell_exit_code", {"command": "echo a"})
        text = format_watchers(eng.list_all())
        assert "a" in text
        assert "shell_exit_code" in text
