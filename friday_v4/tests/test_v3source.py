"""Tests for V3DataSource — V4's read-only bridge to V3's database."""

from __future__ import annotations

import json
import sqlite3

import pytest

from friday_v4.proactive.v3source import V3DataSource


def _make_db(path, observations: bool = True, actions: bool = True,
             ambient: bool = True) -> sqlite3.Connection:
    """Create a V3-schema sqlite DB with a little data."""
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE observations (
            id TEXT PRIMARY KEY,
            observed_at TEXT NOT NULL,
            source TEXT NOT NULL,
            subject TEXT NOT NULL,
            aspect TEXT NOT NULL,
            value TEXT NOT NULL,
            confidence TEXT NOT NULL,
            scope TEXT DEFAULT '',
            detail TEXT
        );
        CREATE TABLE actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            action_type TEXT NOT NULL,
            target TEXT DEFAULT '',
            detail TEXT DEFAULT '{}',
            workspace_id TEXT,
            project TEXT,
            session_id TEXT,
            confidence TEXT DEFAULT 'observed',
            observed_at TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        );
        CREATE TABLE ambient_feed (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            event_type TEXT NOT NULL,
            title TEXT NOT NULL,
            detail TEXT DEFAULT '',
            source TEXT DEFAULT 'daemon',
            priority INTEGER DEFAULT 0,
            category TEXT DEFAULT 'system',
            dismissed INTEGER DEFAULT 0,
            actionable INTEGER DEFAULT 0,
            action_label TEXT DEFAULT '',
            action_command TEXT DEFAULT '',
            mission_id TEXT DEFAULT '',
            graph_id TEXT DEFAULT '',
            project TEXT DEFAULT '',
            payload TEXT DEFAULT '',
            confidence REAL DEFAULT 1.0,
            salience REAL DEFAULT 0.0
        );
    """)
    if observations:
        conn.execute(
            "INSERT INTO observations (id, observed_at, source, subject,"
            " aspect, value, confidence) VALUES"
            " ('obs1', datetime('now', '-1 hour'), 'git_observer', 'repoA',"
            " 'commits', '3', 'observed')")
        conn.execute(
            "INSERT INTO observations (id, observed_at, source, subject,"
            " aspect, value, confidence) VALUES"
            " ('obs2', datetime('now', '-30 minutes'), 'git_observer',"
            " 'repoA', 'dirty', 'true', 'observed')")
    if actions:
        conn.execute(
            "INSERT INTO actions (source, action_type, target, project,"
            " observed_at, recorded_at) VALUES ('daemon', 'build_check',"
            " 'repoA', 'repoA', datetime('now', '-10 minutes'),"
            " datetime('now'))")
    if ambient:
        conn.execute(
            "INSERT INTO ambient_feed (timestamp, event_type, title,"
            " priority) VALUES (datetime('now', '-5 minutes'),"
            " 'build_failed', 'Build failed', 2)")
    conn.commit()
    return conn


@pytest.fixture
def v3db(tmp_path):
    """A temp V3-schema DB; returns (path, status_path)."""
    db_path = tmp_path / "friday.db"
    status_path = tmp_path / "daemon.status"
    _make_db(db_path)
    status_path.write_text(json.dumps({"state": "running",
                                       "last_cycle_at": "2026-01-01T00:00:00"}))
    return db_path, status_path


class TestV3DataSource:
    def test_available_with_v3_schema(self, v3db):
        db_path, _ = v3db
        src = V3DataSource(db_path=db_path)
        assert src.is_available() is True

    def test_unavailable_when_db_missing(self, tmp_path):
        src = V3DataSource(db_path=tmp_path / "nope.db")
        assert src.is_available() is False

    def test_unavailable_when_schema_missing(self, tmp_path):
        # A DB that exists but lacks V3's tables.
        conn = sqlite3.connect(tmp_path / "friday.db")
        conn.execute("CREATE TABLE other (id INTEGER)")
        conn.commit()
        conn.close()
        src = V3DataSource(db_path=tmp_path / "friday.db")
        assert src.is_available() is False

    def test_recent_observations(self, v3db):
        db_path, _ = v3db
        src = V3DataSource(db_path=db_path)
        obs = src.recent_observations(hours=24)
        assert len(obs) == 2
        assert obs[0]["source"] == "git_observer"
        assert obs[0]["subject"] == "repoA"

    def test_recent_actions(self, v3db):
        db_path, _ = v3db
        src = V3DataSource(db_path=db_path)
        acts = src.recent_actions(hours=24)
        assert len(acts) == 1
        assert acts[0]["action_type"] == "build_check"

    def test_recent_ambient_events(self, v3db):
        db_path, _ = v3db
        src = V3DataSource(db_path=db_path)
        events = src.recent_ambient_events(hours=24)
        assert len(events) == 1
        assert events[0]["priority"] == 2

    def test_observation_counts(self, v3db):
        db_path, _ = v3db
        src = V3DataSource(db_path=db_path)
        counts = src.observation_counts(hours=24)
        assert counts["by_source"]["git_observer"] == 2
        assert counts["by_type"]["build_check"] == 1

    def test_daemon_state(self, v3db):
        db_path, status_path = v3db
        src = V3DataSource(db_path=db_path, status_path=status_path)
        assert src.daemon_state().get("state") == "running"

    def test_daemon_state_missing(self, tmp_path):
        src = V3DataSource(db_path=tmp_path / "friday.db",
                           status_path=tmp_path / "nope.status")
        assert src.daemon_state() == {}

    def test_workspace_digest_includes_data(self, v3db):
        db_path, status_path = v3db
        src = V3DataSource(db_path=db_path, status_path=status_path)
        digest = src.workspace_digest(hours=24)
        assert "observations" in digest
        assert "repoA" in digest
        assert "daemon running" in digest

    def test_workspace_digest_empty_when_unavailable(self, tmp_path):
        src = V3DataSource(db_path=tmp_path / "nope.db")
        assert src.workspace_digest() == ""

    def test_all_queries_safe_when_unavailable(self, tmp_path):
        src = V3DataSource(db_path=tmp_path / "nope.db")
        assert src.recent_observations() == []
        assert src.recent_actions() == []
        assert src.recent_ambient_events() == []
        assert src.observation_counts() == {}

    def test_readonly_never_writes(self, v3db):
        db_path, _ = v3db
        src = V3DataSource(db_path=db_path)
        before = db_path.stat().st_mtime_ns
        src.recent_observations()
        src.observation_counts()
        # The DB file must be untouched (read-only URI).
        assert db_path.stat().st_mtime_ns == before
