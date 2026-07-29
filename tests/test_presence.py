"""Tests for the Presence & Attention system — PresenceDetector, state machine,
attention levels, focus mode, and deferred interrupt queue."""

from __future__ import annotations

import datetime as dt
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Generator
import pytest

# We import the module-under-test directly. DB-dependent functions use
# an in-memory SQLite database.


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def db():
    """In-memory SQLite connection with schema applied."""
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    # Create the tables we need
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS operator_preferences (
            key         TEXT PRIMARY KEY,
            value       TEXT NOT NULL,
            source      TEXT NOT NULL DEFAULT 'explicit',
            updated_at  TEXT
        );
        CREATE TABLE IF NOT EXISTS deferred_interrupts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            priority INTEGER NOT NULL DEFAULT 0,
            message TEXT NOT NULL DEFAULT '',
            state_at_creation TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            delivered_at TEXT
        );
        CREATE TABLE IF NOT EXISTS working_memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            context_key TEXT NOT NULL,
            value TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'working',
            source TEXT NOT NULL DEFAULT 'system',
            context TEXT,
            priority INTEGER NOT NULL DEFAULT 0,
            ttl_seconds INTEGER NOT NULL DEFAULT 3600,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ambient_feed (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            event_type TEXT NOT NULL,
            title TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT 'daemon',
            project TEXT NOT NULL DEFAULT '',
            payload TEXT NOT NULL DEFAULT '',
            confidence REAL NOT NULL DEFAULT 1.0,
            priority INTEGER NOT NULL DEFAULT 0,
            category TEXT NOT NULL DEFAULT 'system',
            dismissed INTEGER NOT NULL DEFAULT 0,
            actionable INTEGER NOT NULL DEFAULT 0,
            action_label TEXT NOT NULL DEFAULT '',
            action_command TEXT NOT NULL DEFAULT '',
            mission_id TEXT NOT NULL DEFAULT '',
            graph_id TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS repositories (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            path TEXT NOT NULL,
            default_branch TEXT,
            is_dirty INTEGER NOT NULL DEFAULT 0,
            first_commit_date TEXT,
            last_commit_date TEXT,
            remote_url TEXT,
            commit_count INTEGER,
            readme_summary TEXT,
            license TEXT,
            primary_author TEXT,
            ingestion_time TEXT NOT NULL
        );
    """)
    conn.commit()
    yield conn
    conn.close()


# ===========================================================================
# Tests: PresenceDetector
# ===========================================================================


class TestPresenceDetector:
    """Tests for the core presence detection state machine."""

    def test_away_when_no_signals(self):
        """When no signals are available, the state should be AWAY."""
        from friday.presence import PresenceDetector, PresenceState, PresenceSignals

        detector = PresenceDetector()
        signals = PresenceSignals()  # all fields default to None/0
        state = detector.determine_state(signals)
        assert state == PresenceState.AWAY

    def test_away_when_idle_too_long(self):
        """When idle for >= AWAY_TIMEOUT, state should be AWAY."""
        from friday.presence import PresenceDetector, PresenceState, PresenceSignals

        detector = PresenceDetector()
        signals = PresenceSignals(
            idle_seconds=detector.AWAY_TIMEOUT + 60,  # 15+ min
            last_active_at=datetime.now(timezone.utc).isoformat(),
        )
        state = detector.determine_state(signals)
        assert state == PresenceState.AWAY

    def test_desk_active_when_recent_input(self):
        """When idle time is low, state should be DESK_ACTIVE."""
        from friday.presence import PresenceDetector, PresenceState, PresenceSignals

        detector = PresenceDetector()
        signals = PresenceSignals(
            idle_seconds=10,  # 10 seconds idle
            last_active_at=datetime.now(timezone.utc).isoformat(),
        )
        state = detector.determine_state(signals)
        assert state == PresenceState.DESK_ACTIVE

    def test_desk_idle_when_idle_time_exceeds_threshold(self):
        """When idle for >= DESK_IDLE_TIMEOUT but < AWAY_TIMEOUT, should be DESK_IDLE."""
        from friday.presence import PresenceDetector, PresenceState, PresenceSignals

        detector = PresenceDetector()
        signals = PresenceSignals(
            idle_seconds=detector.DESK_IDLE_TIMEOUT + 60,  # 6 min idle
            last_active_at=datetime.now(timezone.utc).isoformat(),
        )
        state = detector.determine_state(signals)
        assert state == PresenceState.DESK_IDLE

    def test_in_meeting_when_calendar_event_active(self):
        """When a calendar event is active, state should be IN_MEETING."""
        from friday.presence import PresenceDetector, PresenceState, PresenceSignals

        detector = PresenceDetector()
        signals = PresenceSignals(
            current_event_title="Sprint Planning",
            current_event_minutes_remaining=25,
            idle_seconds=120,  # 2 min idle (actively listening)
            last_active_at=datetime.now(timezone.utc).isoformat(),
        )
        state = detector.determine_state(signals)
        assert state == PresenceState.IN_MEETING

    def test_deep_focus_when_recent_git_and_low_idle(self):
        """When recently coding with low idle, state should be DEEP_FOCUS."""
        from friday.presence import PresenceDetector, PresenceState, PresenceSignals

        detector = PresenceDetector()
        signals = PresenceSignals(
            idle_seconds=30,  # 30s idle
            git_activity_minutes_ago=5,  # 5 min since last commit
            last_active_at=datetime.now(timezone.utc).isoformat(),
        )
        state = detector.determine_state(signals)
        assert state == PresenceState.DEEP_FOCUS

    def test_sleeping_when_late_night_no_activity(self):
        """During late night hours with no activity, should be SLEEPING."""
        from friday.presence import PresenceDetector, PresenceState, PresenceSignals

        detector = PresenceDetector()
        signals = PresenceSignals(
            local_hour=2,  # 2 AM
            idle_seconds=3600,  # 1 hour idle
            typical_sleep_hour=23,
            typical_wake_hour=7,
        )
        state = detector.determine_state(signals)
        assert state == PresenceState.SLEEPING

    def test_smoothing_prevents_flicker(self):
        """Smoothing should not change state if MIN_STATE_DURATION hasn't elapsed."""
        from friday.presence import PresenceDetector, PresenceState, PresenceSignals

        detector = PresenceDetector()

        # Force initial state to DESK_ACTIVE by smoothing into it first
        detector._current_state = PresenceState.DESK_ACTIVE
        detector._state_changed_at = None  # reset so first call accepts the state

        # Now the detector thinks current state is DESK_ACTIVE.
        # Immediately try to switch to DESK_IDLE — should be held back by smoothing
        state = detector.smooth_state(PresenceState.DESK_IDLE)
        assert state == PresenceState.DESK_ACTIVE  # still DESK_ACTIVE (too soon to change)

    def test_detect_returns_valid_state(self, db):
        """detect() should return a valid PresenceState without crashing."""
        from friday.presence import PresenceDetector, PresenceState

        detector = PresenceDetector()
        state = detector.detect(db)
        assert isinstance(state, PresenceState)

    def test_collect_signals_graceful_degradation(self, db):
        """collect_signals() should not crash even without proper signal sources."""
        from friday.presence import PresenceDetector, PresenceSignals

        detector = PresenceDetector()
        signals = detector.collect_signals(db)
        assert isinstance(signals, PresenceSignals)


# ===========================================================================
# Tests: Attention levels
# ===========================================================================


class TestAttentionLevels:
    """Tests for attention level mapping and interrupt gating."""

    def test_urgent_always_passes(self):
        """Priority 3 events should always pass regardless of state."""
        from friday.presence import (PresenceState, should_interrupt)

        for state in PresenceState:
            assert should_interrupt(state, 3) is True, (
                f"Priority 3 should interrupt in {state}")

    def test_deep_focus_blocks_normal(self):
        """In DEEP_FOCUS, priority 1 should NOT pass."""
        from friday.presence import (PresenceState, should_interrupt)

        assert should_interrupt(PresenceState.DEEP_FOCUS, 1) is False
        assert should_interrupt(PresenceState.DEEP_FOCUS, 2) is False  # not urgent
        assert should_interrupt(PresenceState.DEEP_FOCUS, 3) is True  # urgent

    def test_desk_active_passes_important(self):
        """In DESK_ACTIVE, priority 2 should pass."""
        from friday.presence import (PresenceState, should_interrupt)

        assert should_interrupt(PresenceState.DESK_ACTIVE, 2) is True
        assert should_interrupt(PresenceState.DESK_ACTIVE, 1) is True
        assert should_interrupt(PresenceState.DESK_ACTIVE, 3) is True


# ===========================================================================
# Tests: Deferred interrupt queue (DB-dependent)
# ===========================================================================


class TestDeferredQueue:
    """Tests for the deferred interrupt queue."""

    def test_enqueue_and_deliver(self, db):
        """Enqueue an interrupt and verify it gets delivered when appropriate."""
        from friday.presence import (PresenceState, enqueue_deferred_interrupt,
                                     deliver_pending_interrupts)

        # Enqueue a priority-2 interrupt
        eid = enqueue_deferred_interrupt(
            db, "test_event", "Something important", 2,
            PresenceState.DESK_IDLE,
        )
        assert eid > 0

        # Deliver for DESK_ACTIVE (permissive state — should deliver)
        delivered = deliver_pending_interrupts(db, PresenceState.DESK_ACTIVE)
        assert len(delivered) == 1
        assert delivered[0]["event_type"] == "test_event"
        assert delivered[0]["message"] == "Something important"
        assert delivered[0]["priority"] == 2

    def test_defer_when_state_not_permissive(self, db):
        """Interrupts should not be delivered when state is restrictive."""
        from friday.presence import (PresenceState, enqueue_deferred_interrupt,
                                     deliver_pending_interrupts)

        enqueue_deferred_interrupt(
            db, "test_event", "Something normal", 1,
            PresenceState.DEEP_FOCUS,
        )

        # Trying to deliver for DEEP_FOCUS should skip priority-1 events
        delivered = deliver_pending_interrupts(db, PresenceState.DEEP_FOCUS)
        assert len(delivered) == 0  # not delivered — still in restrictive state

    def test_multiple_interrupts_priority_order(self, db):
        """Multiple deferred interrupts should be delivered in priority order."""
        from friday.presence import (PresenceState, enqueue_deferred_interrupt,
                                     deliver_pending_interrupts)

        enqueue_deferred_interrupt(db, "low", "Low priority", 1, PresenceState.AWAY)
        enqueue_deferred_interrupt(db, "high", "High priority", 3, PresenceState.AWAY)
        enqueue_deferred_interrupt(db, "medium", "Medium priority", 2, PresenceState.AWAY)

        delivered = deliver_pending_interrupts(db, PresenceState.DESK_ACTIVE, max_per_cycle=3)

        # Should deliver high priority first
        assert len(delivered) >= 1
        assert delivered[0]["event_type"] == "high"
        assert delivered[0]["priority"] == 3


# ===========================================================================
# Tests: Focus mode
# ===========================================================================


class TestFocusMode:
    """Tests for manual focus mode."""

    def test_set_focus_mode(self, db):
        """set_focus_mode should persist focus mode with expiration."""
        from friday.presence import set_focus_mode, is_focus_mode

        msg = set_focus_mode(db, 30)
        assert "Focus mode enabled" in msg
        assert "30 minutes" in msg

        # Verify focus mode is active
        assert is_focus_mode(db) is True

    def test_disable_focus_mode(self, db):
        """disable_focus_mode should clear focus mode."""
        from friday.presence import set_focus_mode, disable_focus_mode, is_focus_mode

        set_focus_mode(db, 30)
        assert is_focus_mode(db) is True

        msg = disable_focus_mode(db)
        assert "Focus mode disabled" in msg
        assert is_focus_mode(db) is False

    def test_focus_mode_expires(self, db):
        """Focus mode should auto-expire after the configured duration."""
        from friday.presence import set_focus_mode, is_focus_mode

        msg = set_focus_mode(db, 0)  # 0 minutes = already expired
        assert "Focus mode enabled" in msg

        # is_focus_mode should return False since it expired immediately
        assert is_focus_mode(db) is False


# ===========================================================================
# Tests: Formatting utilities
# ===========================================================================


class TestFormatting:
    """Tests for human-readable formatting."""

    def test_format_state_all_values(self):
        """All presence states should have human-readable labels."""
        from friday.presence import format_state, PresenceState

        for state in PresenceState:
            label = format_state(state)
            assert isinstance(label, str)
            assert len(label) > 0
            assert label != state.value  # should be more readable than raw value

    def test_attention_for_state_all_values(self):
        """All presence states should map to an attention level."""
        from friday.presence import attention_for_state, PresenceState, AttentionLevel

        for state in PresenceState:
            level = attention_for_state(state)
            assert isinstance(level, AttentionLevel)
            # Ensure level is within valid range
            assert 0 <= level <= 4
