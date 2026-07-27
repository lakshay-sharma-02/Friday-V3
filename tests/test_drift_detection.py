"""Tests for skill drift detection.

Tests detect_skill_drift() and format_drift_reports():
1. No skills in DB -> empty report
2. Skills with <3 invocations -> skipped (insufficient history)
3. Healthy skill (>80% success over 5 invocations)
4. Degrading skill (50-80% success, step failing >2x)
5. Unhealthy skill (<50% success, step failing >5x)
6. Mixed health status across multiple skills
7. Skill with exemplar stability + success
8. Empty/invalid replay log entries (graceful handling)
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest import mock

import pytest

from friday.skill_formation import detect_skill_drift, format_drift_reports


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    """In-memory SQLite DB with required tables."""
    db_path = tmp_path / "test_friday.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    # Create formed_skills table.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS formed_skills (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            workflow_intent_id INTEGER NOT NULL DEFAULT 0,
            task_graph      TEXT NOT NULL DEFAULT '[]',
            exemplars       TEXT NOT NULL DEFAULT '{}',
            invocation_count INTEGER NOT NULL DEFAULT 0,
            last_invoked_at TEXT,
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL
        )
    """)

    # Create workers table (minimal for JOIN).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS workers (
            id              TEXT PRIMARY KEY,
            name            TEXT NOT NULL,
            kind            TEXT NOT NULL DEFAULT 'formed_skill',
            status          TEXT NOT NULL DEFAULT 'proposed',
            manifest_ref    TEXT,
            version         TEXT DEFAULT '0.1.0',
            capabilities    TEXT DEFAULT '',
            confidence      TEXT DEFAULT 'medium',
            description     TEXT DEFAULT '',
            schema_version  TEXT DEFAULT '1.0',
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL,
            availability    TEXT DEFAULT 'available',
            worker_kind     TEXT DEFAULT 'formed_skill'
        )
    """)

    # Create actions table (for skill_replay log entries).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS actions (
            eid             INTEGER PRIMARY KEY AUTOINCREMENT,
            source          TEXT NOT NULL DEFAULT 'friday',
            action_type     TEXT NOT NULL,
            target          TEXT DEFAULT '',
            detail          TEXT DEFAULT '{}',
            workspace_id    TEXT,
            project         TEXT,
            session_id      TEXT,
            confidence      TEXT DEFAULT 'observed',
            observed_at     TEXT NOT NULL,
            recorded_at     TEXT NOT NULL
        )
    """)

    conn.commit()
    return conn


def _insert_skill(conn, skill_id: int, name: str, inv_count: int,
                  task_graph: list | None = None,
                  exemplars: dict | None = None,
                  worker_id: str | None = None):
    """Insert a formed skill + associated worker."""
    now = "2026-01-01T00:00:00"
    tg = json.dumps(task_graph or [["workspace_switch", "3"], ["app_launch", "firefox"]])
    ex = json.dumps(exemplars or {})
    wid = worker_id or f"worker:{name}:abcdef01"

    conn.execute(
        "INSERT OR IGNORE INTO formed_skills "
        "(id, workflow_intent_id, task_graph, exemplars, invocation_count, "
        " last_invoked_at, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (skill_id, skill_id, tg, ex, inv_count, now, now, now)
    )
    conn.execute(
        "INSERT OR IGNORE INTO workers "
        "(id, name, kind, status, manifest_ref, created_at, updated_at) "
        "VALUES (?, ?, 'formed_skill', 'beta', ?, ?, ?)",
        (wid, name, f"formed_skill:{skill_id}", now, now)
    )
    conn.commit()


def _insert_replay(conn, skill_id: int, succeeded: bool,
                   step_results: list[dict], source: str = "friday"):
    """Insert a skill_replay action log entry."""
    now = "2026-01-01T00:00:00"
    target = json.dumps({
        "skill_id": skill_id,
        "succeeded": succeeded,
        "step_count": len(step_results),
    })
    detail = json.dumps({"results": step_results, "strategy": "abort"})

    conn.execute(
        "INSERT INTO actions "
        "(source, action_type, target, detail, observed_at, recorded_at) "
        "VALUES (?, 'skill_replay', ?, ?, ?, ?)",
        (source, target, detail, now, now)
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestDetectDrift:
    """Tests for detect_skill_drift()."""

    def test_no_skills(self, conn):
        """Empty DB -> empty report."""
        reports = detect_skill_drift(conn)
        assert reports == []

    def test_skill_without_enough_invocations(self, conn):
        """Skill with <3 invocations -> skipped."""
        _insert_skill(conn, 1, "test_skill", inv_count=1)

        # Insert only 1 replay entry.
        _insert_replay(conn, 1, True, [
            {"step": 0, "action": "workspace_switch", "success": True},
            {"step": 1, "action": "app_launch", "success": True},
        ])

        reports = detect_skill_drift(conn)
        assert reports == []

    def test_healthy_skill(self, conn):
        """Skill with >80% success over 5 invocations -> healthy."""
        _insert_skill(conn, 1, "healthy_skill", inv_count=5)

        # 5 invocations, 4 success + 1 failure = 80% success rate.
        for _ in range(4):
            _insert_replay(conn, 1, True, [
                {"step": 0, "action": "workspace_switch", "success": True},
                {"step": 1, "action": "app_launch", "success": True},
            ])
        # One partial failure (step 0 works, step 1 fails).
        _insert_replay(conn, 1, False, [
            {"step": 0, "action": "workspace_switch", "success": True},
            {"step": 1, "action": "app_launch", "success": False},
        ])

        reports = detect_skill_drift(conn)
        assert len(reports) == 1
        assert reports[0].overall_health == "healthy"
        assert reports[0].overall_success_rate >= 0.8

    def test_degrading_skill(self, conn):
        """Skill with ~60% success rate over 5 invocations -> degrading."""
        _insert_skill(conn, 1, "degrading_skill", inv_count=5)

        # 3 successes, 2 failures -> 60% success rate (50-80% band = degrading).
        for _ in range(3):
            _insert_replay(conn, 1, True, [
                {"step": 0, "action": "workspace_switch", "success": True},
                {"step": 1, "action": "app_launch", "success": True},
            ])
        for _ in range(2):
            _insert_replay(conn, 1, False, [
                {"step": 0, "action": "workspace_switch", "success": False},
                {"step": 1, "action": "app_launch", "success": False},
            ])

        reports = detect_skill_drift(conn)
        assert len(reports) == 1
        assert reports[0].overall_health == "degrading"

    def test_unhealthy_skill(self, conn):
        """Skill with <50% success -> unhealthy."""
        _insert_skill(conn, 1, "unhealthy_skill", inv_count=10)

        # 6 failures out of 10 = 40% success rate.
        for _ in range(6):
            _insert_replay(conn, 1, False, [
                {"step": 0, "action": "workspace_switch", "success": False},
                {"step": 1, "action": "app_launch", "success": False},
            ])
        # 4 successes.
        for _ in range(4):
            _insert_replay(conn, 1, True, [
                {"step": 0, "action": "workspace_switch", "success": True},
                {"step": 1, "action": "app_launch", "success": True},
            ])

        reports = detect_skill_drift(conn)
        assert len(reports) == 1
        assert reports[0].overall_health == "unhealthy"
        assert reports[0].overall_success_rate < 0.5

    def test_mixed_skills(self, conn):
        """Multiple skills with different health levels."""
        # Healthy skill (5 invocations, 90% success).
        _insert_skill(conn, 1, "healthy_skill", inv_count=5)
        for _ in range(5):
            _insert_replay(conn, 1, True, [
                {"step": 0, "action": "workspace_switch", "success": True},
            ])

        # Unhealthy skill (5 invocations, 20% success).
        _insert_skill(conn, 2, "unhealthy_skill", inv_count=5,
                      worker_id="worker:unhealthy:bbbbbbbb")
        for _ in range(4):
            _insert_replay(conn, 2, False, [
                {"step": 0, "action": "workspace_switch", "success": False},
            ])
        _insert_replay(conn, 2, True, [
            {"step": 0, "action": "workspace_switch", "success": True},
        ])

        reports = detect_skill_drift(conn)
        # Only skill_id=1 has >=3 invocations matching (skill_id=2 was just
        # inserted but we need to check the filter).
        # Actually, skill 2 has 5 invocations too, so both should appear.
        assert len(reports) == 2
        health_map = {r.worker_name: r.overall_health for r in reports}
        assert health_map.get("healthy_skill") == "healthy"
        assert health_map.get("unhealthy_skill") == "unhealthy"

    def test_skill_with_exemplar_info(self, conn):
        """Skill with formation-time exemplar stability data."""
        exemplars = {
            "0": {
                "default": "3",
                "distribution": {"3": 8, "5": 2},
                "consensus": 0.8,
                "stable": True,
            },
            "1": {
                "default": "firefox",
                "distribution": {"firefox": 6, "kitty": 4},
                "consensus": 0.6,
                "stable": False,
            },
        }
        _insert_skill(conn, 1, "exemplar_skill", inv_count=5, exemplars=exemplars)

        for _ in range(4):
            _insert_replay(conn, 1, True, [
                {"step": 0, "action": "workspace_switch", "success": True},
                {"step": 1, "action": "app_launch", "success": True},
            ])
        _insert_replay(conn, 1, True, [
            {"step": 0, "action": "workspace_switch", "success": True},
            {"step": 1, "action": "app_launch", "success": True},
        ])

        reports = detect_skill_drift(conn)
        assert len(reports) == 1
        assert len(reports[0].step_breakdown) == 2
        # Step 0 should have exemplar_stable=True, step 1 should be False.
        step0 = [s for s in reports[0].step_breakdown if s["step_idx"] == 0][0]
        step1 = [s for s in reports[0].step_breakdown if s["step_idx"] == 1][0]
        assert step0["exemplar_stable"] is True
        assert step0["exemplar_consensus"] == 0.8
        assert step1["exemplar_stable"] is False
        assert step1["exemplar_consensus"] == 0.6

    def test_empty_replay_log_entries(self, conn):
        """Malformed/incomplete replay entries don't crash."""
        _insert_skill(conn, 1, "resilient_skill", inv_count=5)

        # Normal entries.
        for _ in range(3):
            _insert_replay(conn, 1, True, [
                {"step": 0, "action": "workspace_switch", "success": True},
            ])

        # Entry with empty detail (edge case).
        now = "2026-01-01T00:00:00"
        conn.execute(
            "INSERT INTO actions "
            "(source, action_type, target, detail, observed_at, recorded_at) "
            "VALUES ('friday', 'skill_replay', ?, '', ?, ?)",
            (json.dumps({"skill_id": 1, "succeeded": True}), now, now)
        )
        conn.commit()

        # Entry with non-dict detail.
        conn.execute(
            "INSERT INTO actions "
            "(source, action_type, target, detail, observed_at, recorded_at) "
            "VALUES ('friday', 'skill_replay', ?, 'null', ?, ?)",
            (json.dumps({"skill_id": 1, "succeeded": False}), now, now)
        )
        conn.commit()

        reports = detect_skill_drift(conn)
        assert len(reports) == 1  # Should still produce a report despite bad entries

    def test_format_drift_reports(self, conn):
        """format_drift_reports produces structured output with summary."""
        _insert_skill(conn, 1, "test_skill", inv_count=5)
        for _ in range(3):
            _insert_replay(conn, 1, True, [
                {"step": 0, "action": "workspace_switch", "success": True},
            ])
        for _ in range(2):
            _insert_replay(conn, 1, False, [
                {"step": 0, "action": "workspace_switch", "success": False},
            ])

        reports = detect_skill_drift(conn)
        output = format_drift_reports(reports)
        assert isinstance(output, str)
        assert len(output) > 50
        assert "Skill Drift Analysis" in output
        assert "Summary:" in output
        assert "Degrading" in output or "Unhealthy" in output

    def test_format_empty_reports(self):
        """format_drift_reports with empty list prints message."""
        output = format_drift_reports([])
        assert "sufficient" in output.lower() or "No skills" in output
