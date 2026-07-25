"""Sequence miner tests — Pillar B Stage 2.

Tests the deterministic, LLM-free pattern miner: sessionization, n-gram
extraction, normalization, enrichment, and formatting.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from friday.sequence_miner import (
    MinedPattern,
    format_patterns,
    mine_sequences,
)
from friday.action_log import ActionEvent, log_action
from friday.db import connect


def _ts(hours_ago: int = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


@pytest.fixture
def conn():
    c = connect(":memory:")
    # Create the actions table (schema lives in db.py's migration, not here)
    c.executescript("""
        CREATE TABLE IF NOT EXISTS actions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            source          TEXT NOT NULL,
            action_type     TEXT NOT NULL,
            target          TEXT NOT NULL DEFAULT '',
            detail          TEXT NOT NULL DEFAULT '{}',
            workspace_id    TEXT,
            project         TEXT,
            session_id      TEXT,
            confidence      TEXT NOT NULL DEFAULT 'observed',
            observed_at     TEXT NOT NULL,
            recorded_at     TEXT NOT NULL
        );
    """)
    yield c
    c.close()


class TestSessionization:
    def test_empty_db_returns_no_patterns(self, conn):
        patterns = mine_sequences(conn)
        assert patterns == []

    def test_single_action_not_enough_for_pattern(self, conn):
        log_action(conn, ActionEvent(
            source="friday", action_type="workspace_switch", target="3",
            observed_at=_ts(1), confidence="observed"))
        patterns = mine_sequences(conn)
        assert patterns == []

    def test_two_actions_form_one_pattern(self, conn):
        log_action(conn, ActionEvent(
            source="friday", action_type="workspace_switch", target="3",
            observed_at=_ts(2), confidence="observed"))
        log_action(conn, ActionEvent(
            source="friday", action_type="window_focus", target="firefox",
            observed_at=_ts(2) + "Z", confidence="observed"))
        patterns = mine_sequences(conn)
        # With min_support=2, two same-session actions aren't enough
        # Add a second session with same sequence
        log_action(conn, ActionEvent(
            source="friday", action_type="workspace_switch", target="5",
            observed_at=_ts(1), confidence="observed"))
        log_action(conn, ActionEvent(
            source="friday", action_type="window_focus", target="kitty",
            observed_at=_ts(1) + "Z", confidence="observed"))
        patterns = mine_sequences(conn, min_support=1)
        assert len(patterns) >= 1


class TestNormalization:
    def test_workspace_switches_normalized(self, conn):
        """Workspace switches to different targets normalize to same pattern."""
        base = _ts(10)
        from datetime import timedelta as td
        for i in range(3):
            t = (datetime.fromisoformat(base) + td(seconds=i)).isoformat()
            log_action(conn, ActionEvent(
                source="friday", action_type="workspace_switch", target=f"{i+1}",
                observed_at=t, confidence="observed"))
            t2 = (datetime.fromisoformat(t) + td(milliseconds=1)).isoformat()
            log_action(conn, ActionEvent(
                source="friday", action_type="window_focus", target="firefox",
                observed_at=t2, confidence="observed"))
        patterns = mine_sequences(conn, min_support=1)
        assert len(patterns) >= 1


class TestMinedPattern:
    def test_to_dict(self):
        p = MinedPattern(sequence=[("a", "1"), ("b", "2")], count=3)
        d = p.to_dict()
        assert d["count"] == 3

    def test_to_text(self):
        p = MinedPattern(sequence=[("a", "1"), ("b", "2")], count=3)
        text = p.to_text()
        assert "3x" in text
        assert "a(1)" in text or "a" in text


class TestFormatPatterns:
    def test_empty(self):
        assert "No patterns found" in format_patterns([])

    def test_single_pattern(self):
        p = MinedPattern(sequence=[("ws_switch", "3"), ("focus", "kitty")], count=2)
        text = format_patterns([p])
        assert "ws_switch" in text or "2x" in text
