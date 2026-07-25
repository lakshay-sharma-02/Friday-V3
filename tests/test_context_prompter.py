"""Context prompter tests — Pillar C.

Tests that build_context_prompt correctly assembles learned context
from workflow intents, sessions, and repositories. Uses an in-memory
DB seeded with minimal data.
"""

from __future__ import annotations

import json
import pytest

from friday.context_prompter import build_context_prompt
from friday.db import connect


@pytest.fixture
def conn():
    c = connect(":memory:")
    yield c
    c.close()


class TestBuildContextPrompt:
    def test_empty_db_returns_empty_string(self, conn):
        result = build_context_prompt(conn)
        assert result == ""

    def _seed_pattern(self, conn, pid=1):
        conn.execute(
            "INSERT INTO mined_patterns "
            "(id, sequence_json, count, distinct_sessions, first_seen, last_seen, "
            "common_workspace, common_project, confidence, mined_at) VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (pid, '[]', 3, 3, "2025-01-01", "2025-01-02", "", "",
             "derived", "2025-01-02"),
        )

    def test_with_workflow_intent_includes_learned_section(self, conn):
        self._seed_pattern(conn)
        conn.execute(
            "INSERT INTO workflow_intents "
            "(pattern_id, intent_label, intent_description, steps_text, "
            "confidence, pattern_summary, labeled_at) VALUES "
            "(?, ?, ?, ?, ?, ?, ?)",
            (1, "Start dev server", "Open kitty and run npm",
             '[]', "high", '[]', "2025-01-01T00:00:00"),
        )
        conn.commit()
        result = build_context_prompt(conn)
        assert "LEARNED CONTEXT" in result
        assert "Start dev server" in result

    def test_with_recent_session(self, conn):
        conn.execute(
            "INSERT INTO sessions (id, start_time, end_time, repositories, "
            "primary_repo, observations, activity, confidence, duration_min, "
            "built_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("s1", "2025-01-01T10:00:00", "2025-01-01T12:00:00",
             '[]', "friday", '[]', "coding", "high", 120,
             "2025-01-01T12:00:00"),
        )
        conn.commit()
        result = build_context_prompt(conn)
        assert "LEARNED CONTEXT" in result
        assert "friday" in result

    def test_with_project_listings(self, conn):
        conn.execute(
            "INSERT OR IGNORE INTO repositories "
            "(name, path, ingestion_time, is_dirty) VALUES (?, ?, ?, ?)",
            ("friday-v3", "/tmp/friday", "2025-01-01T00:00:00", 0),
        )
        conn.commit()
        conn.execute(
            "UPDATE repositories SET commit_count=20, maturity='stable', "
            "last_commit_date='2025-01-01' WHERE name='friday-v3'")
        conn.commit()
        result = build_context_prompt(conn)
        assert "LEARNED CONTEXT" in result
        assert "friday-v3" in result

    def test_low_confidence_intents_excluded(self, conn):
        self._seed_pattern(conn)
        conn.execute(
            "INSERT INTO workflow_intents "
            "(pattern_id, intent_label, intent_description, steps_text, "
            "confidence, pattern_summary, labeled_at) VALUES "
            "(?, ?, ?, ?, ?, ?, ?)",
            (1, "Low conf intent", "test", '[]', "low", '[]',
             "2025-01-01T00:00:00"),
        )
        conn.commit()
        result = build_context_prompt(conn)
        assert "Low conf" not in result
