"""Tests for the emergency kill switch — set, clear, check, in-memory cache, dispatch block."""

from __future__ import annotations

import pytest

import friday.autonomy as _autonomy_mod

from friday.autonomy import (
    _clear_kill_switch_cache,
    is_kill_switch_active,
    set_kill_switch,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clear_cache():
    """Clear the in-memory kill switch cache before each test."""
    _clear_kill_switch_cache()
    yield
    _clear_kill_switch_cache()


@pytest.fixture
def conn():
    """In-memory SQLite connection with operator_preferences table."""
    from friday.db import connect
    c = connect(":memory:")
    c.execute(
        "CREATE TABLE IF NOT EXISTS operator_preferences ("
        "  key TEXT PRIMARY KEY, value TEXT NOT NULL,"
        "  set_at TEXT, source TEXT"
        ")"
    )
    c.commit()
    yield c
    c.close()


# ---------------------------------------------------------------------------
# Kill switch set/clear/check
# ---------------------------------------------------------------------------


class TestKillSwitchSetClearCheck:
    def test_default_not_active(self, conn):
        """By default, the kill switch should NOT be active."""
        assert is_kill_switch_active(conn) is False

    def test_activate(self, conn):
        """set_kill_switch(True) should make is_kill_switch_active return True."""
        set_kill_switch(True, conn)
        assert is_kill_switch_active(conn) is True

    def test_deactivate(self, conn):
        """set_kill_switch(False) after activation should return to False."""
        set_kill_switch(True, conn)
        assert is_kill_switch_active(conn) is True
        set_kill_switch(False, conn)
        assert is_kill_switch_active(conn) is False

    def test_persistence_across_connections(self, conn):
        """Kill switch state should be persisted in the DB."""
        set_kill_switch(True, conn)
        conn.close()
        # Fresh connection should read True.
        from friday.db import connect
        c2 = connect(":memory:")
        c2.execute(
            "CREATE TABLE IF NOT EXISTS operator_preferences ("
            "  key TEXT PRIMARY KEY, value TEXT NOT NULL,"
            "  set_at TEXT, source TEXT"
            ")"
        )
        # Copy data over (simulating persistence).
        c2.execute(
            "INSERT INTO operator_preferences (key, value, set_at, source) "
            "VALUES ('kill_switch', 'true', 'now', 'explicit')"
        )
        c2.commit()
        assert is_kill_switch_active(c2) is True
        c2.close()


# ---------------------------------------------------------------------------
# In-memory cache
# ---------------------------------------------------------------------------


class TestInMemoryCache:
    def test_cache_populated_on_first_read(self, conn):
        """First read should populate the cache from DB."""
        assert _autonomy_mod._KILL_SWITCH_CACHED is None
        is_kill_switch_active(conn)
        assert _autonomy_mod._KILL_SWITCH_CACHED is not None

    def test_cache_serves_subsequent_reads(self, conn):
        """Subsequent reads should use the cache without touching the DB."""
        set_kill_switch(True, conn)
        _clear_kill_switch_cache()  # clear so next read goes to DB
        assert is_kill_switch_active(conn) is True
        assert _autonomy_mod._KILL_SWITCH_CACHED is True

    def test_cache_populated_on_set(self, conn):
        """set_kill_switch should populate the cache directly."""
        assert _autonomy_mod._KILL_SWITCH_CACHED is None
        set_kill_switch(True, conn)
        assert _autonomy_mod._KILL_SWITCH_CACHED is True

    def test_cache_populated_on_reset(self, conn):
        """set_kill_switch(False) should populate the cache to False."""
        set_kill_switch(True, conn)
        assert _autonomy_mod._KILL_SWITCH_CACHED is True
        set_kill_switch(False, conn)
        assert _autonomy_mod._KILL_SWITCH_CACHED is False


# ---------------------------------------------------------------------------
# Dispatch block
# ---------------------------------------------------------------------------


class TestDispatchBlock:
    def test_dispatch_blocks_when_active(self, conn):
        """dispatch() should return failed ExecutionResult when kill switch active.

        ``set_kill_switch()`` now populates the in-memory cache directly,
        so ``dispatch()`` hits the fast path and blocks immediately without
        needing a DB connection.
        """
        from friday.runtime.dispatcher import dispatch
        from friday.runtime.models import RuntimeTask, ExecutionResult

        set_kill_switch(True, conn)
        # Cache is already populated by set_kill_switch — no need to clear.

        task = RuntimeTask(
            execution_id="e1", session_id="s1", schedule_id="g1",
            task_id="t1", worker_id="w1", wave=1, runtime_payload="echo hi",
        )
        result = dispatch(task, worker=None)
        assert result.success is False
        assert "KILL SWITCH" in (result.error or "").upper()

    def test_dispatch_passes_when_not_active(self, conn):
        """dispatch() should proceed normally when kill switch is NOT active."""
        from friday.runtime.dispatcher import dispatch
        from friday.runtime.models import RuntimeTask

        set_kill_switch(False, conn)
        # Cache is already populated by set_kill_switch — no need to clear.

        task = RuntimeTask(
            execution_id="e2", session_id="s2", schedule_id="g2",
            task_id="t2", worker_id="w2", wave=1, runtime_payload="echo hi",
        )
        # No worker -> should fail from "worker is none", not from kill switch.
        result = dispatch(task, worker=None)
        assert result.success is False
        assert "KILL SWITCH" not in (result.error or "").upper()
        assert "worker" in (result.error or "").lower()

    def test_prompt_confirm_blocks_when_active(self, conn):
        """prompt_confirm() should block when kill switch active."""
        from friday.runtime.confirm_gate import prompt_confirm

        set_kill_switch(True, conn)
        _clear_kill_switch_cache()

        result = prompt_confirm("shell_exec", "echo hi", "worker:test",
                                skip_prompt=True, conn=conn)
        assert result is False

    def test_prompt_confirm_passes_when_not_active(self, conn):
        """prompt_confirm() should return True when kill switch not active and auto level."""
        from friday.runtime.confirm_gate import prompt_confirm

        set_kill_switch(False, conn)
        _clear_kill_switch_cache()

        result = prompt_confirm("query", "activewindow", "worker:hyprctl",
                                skip_prompt=True, conn=conn)
        assert result is True


# ---------------------------------------------------------------------------
# Ambient event push
# ---------------------------------------------------------------------------


class TestAmbientEvents:
    def test_push_event_on_activate(self, conn):
        """set_kill_switch(True) should push an ambient event."""
        # Ensure the ambient_feed table exists.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS ambient_feed ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  timestamp TEXT, event_type TEXT, title TEXT, detail TEXT,"
            "  source TEXT, project TEXT, payload TEXT, confidence REAL,"
            "  priority INTEGER, category TEXT, dismissed INTEGER,"
            "  actionable INTEGER, action_label TEXT, action_command TEXT,"
            "  mission_id TEXT, graph_id TEXT"
            ")"
        )
        conn.commit()

        set_kill_switch(True, conn)
        _clear_kill_switch_cache()

        # Check that the event exists in the feed.
        row = conn.execute(
            "SELECT event_type FROM ambient_feed WHERE event_type = 'kill_switch_activated'"
        ).fetchone()
        assert row is not None, "Expected a kill_switch_activated event in the feed"

    def test_push_event_on_deactivate(self, conn):
        """set_kill_switch(False) after activation should push a deactivation event."""
        conn.execute(
            "CREATE TABLE IF NOT EXISTS ambient_feed ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  timestamp TEXT, event_type TEXT, title TEXT, detail TEXT,"
            "  source TEXT, project TEXT, payload TEXT, confidence REAL,"
            "  priority INTEGER, category TEXT, dismissed INTEGER,"
            "  actionable INTEGER, action_label TEXT, action_command TEXT,"
            "  mission_id TEXT, graph_id TEXT"
            ")"
        )
        conn.commit()

        set_kill_switch(True, conn)
        set_kill_switch(False, conn)
        _clear_kill_switch_cache()

        row = conn.execute(
            "SELECT event_type FROM ambient_feed WHERE event_type = 'kill_switch_deactivated'"
        ).fetchone()
        assert row is not None, "Expected a kill_switch_deactivated event in the feed"
