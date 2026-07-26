"""End-to-end smoke test for the full skills pipeline.

Exercises the complete flow:
  actions → mine_sequences → label_intent → form_skills → list/verify

Uses in-memory SQLite for isolation. Does not call the LLM (the intent
labeler falls back to deterministic labels when LLM is unavailable).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from io import StringIO
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Schema: all tables needed for the full pipeline
# ---------------------------------------------------------------------------

_SCHEMA = """
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

CREATE TABLE IF NOT EXISTS mined_patterns (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    sequence_json   TEXT NOT NULL,
    count           INTEGER NOT NULL DEFAULT 0,
    distinct_sessions INTEGER NOT NULL DEFAULT 0,
    first_seen      TEXT NOT NULL DEFAULT '',
    last_seen       TEXT NOT NULL DEFAULT '',
    common_workspace TEXT NOT NULL DEFAULT '',
    common_project  TEXT NOT NULL DEFAULT '',
    confidence      TEXT NOT NULL DEFAULT 'derived',
    mined_at        TEXT NOT NULL,
    exemplars       TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS workflow_intents (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern_id          INTEGER NOT NULL REFERENCES mined_patterns(id) ON DELETE CASCADE,
    intent_label        TEXT NOT NULL,
    intent_description  TEXT NOT NULL DEFAULT '',
    steps_text          TEXT NOT NULL DEFAULT '[]',
    confidence          TEXT NOT NULL DEFAULT 'low',
    pattern_summary     TEXT NOT NULL DEFAULT '',
    labeled_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS formed_skills (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_intent_id INTEGER NOT NULL REFERENCES workflow_intents(id) ON DELETE CASCADE,
    task_graph      TEXT NOT NULL,
    exemplars       TEXT NOT NULL DEFAULT '{}',
    invocation_count INTEGER NOT NULL DEFAULT 0,
    last_invoked_at TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workers (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    kind            TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    capabilities    TEXT NOT NULL DEFAULT '',
    supported_languages     TEXT NOT NULL DEFAULT '',
    supported_task_types    TEXT NOT NULL DEFAULT '',
    supported_plan_types    TEXT NOT NULL DEFAULT '',
    limitations             TEXT NOT NULL DEFAULT '',
    estimated_speed         TEXT NOT NULL DEFAULT '',
    estimated_cost          TEXT NOT NULL DEFAULT '',
    context_window          INTEGER NOT NULL DEFAULT 0,
    parallelism             INTEGER NOT NULL DEFAULT 1,
    requires_network        INTEGER NOT NULL DEFAULT 0,
    requires_filesystem     INTEGER NOT NULL DEFAULT 0,
    requires_git            INTEGER NOT NULL DEFAULT 0,
    requires_python         INTEGER NOT NULL DEFAULT 0,
    requires_shell          INTEGER NOT NULL DEFAULT 0,
    confidence              TEXT NOT NULL DEFAULT 'medium',
    version                 TEXT NOT NULL DEFAULT '1.0.0',
    status                  TEXT NOT NULL DEFAULT 'active',
    schema_version          TEXT NOT NULL DEFAULT '1.0',
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL,
    availability            TEXT NOT NULL DEFAULT 'available',
    manifest_ref            TEXT,
    worker_kind             TEXT NOT NULL DEFAULT 'function'
);

CREATE TABLE IF NOT EXISTS worker_capabilities (
    worker_id   TEXT NOT NULL REFERENCES workers(id) ON DELETE CASCADE,
    capability  TEXT NOT NULL,
    PRIMARY KEY (worker_id, capability)
);

CREATE TABLE IF NOT EXISTS worker_history (
    registered_at  TEXT NOT NULL,
    worker_id      TEXT NOT NULL REFERENCES workers(id) ON DELETE CASCADE,
    name           TEXT NOT NULL,
    kind           TEXT NOT NULL,
    version        TEXT NOT NULL,
    status         TEXT NOT NULL,
    capabilities   TEXT NOT NULL DEFAULT '',
    limitations    TEXT NOT NULL DEFAULT '',
    event_type     TEXT NOT NULL,
    note           TEXT,
    PRIMARY KEY (registered_at, worker_id)
);

CREATE TABLE IF NOT EXISTS worker_versions (
    worker_id   TEXT NOT NULL REFERENCES workers(id) ON DELETE CASCADE,
    version     TEXT NOT NULL,
    registered_at TEXT NOT NULL,
    changelog   TEXT,
    PRIMARY KEY (worker_id, version)
);
"""


@pytest.fixture
def conn():
    """In-memory SQLite connection with all tables."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(_SCHEMA)
    yield c
    c.close()


def _ts(base: datetime, offset_minutes: int = 0) -> str:
    """Return ISO timestamp at *base + offset_minutes*."""
    return (base + timedelta(minutes=offset_minutes)).isoformat()


# ---------------------------------------------------------------------------
# Helpers: seed actions that form detectable patterns
# ---------------------------------------------------------------------------

def seed_two_sessions(conn) -> None:
    """Insert 2 sessions, each with workspace_switch → app_launch.

    Session 1 at T+0: workspace_switch "3", then app_launch "firefox"
    Session 2 at T+1 day: workspace_switch "5", then app_launch "firefox"

    The 24h gap between sessions is >> 30 min gap, so they form separate
    sessions. The miner can detect the repeated pattern.
    """
    base = datetime(2026, 7, 26, 10, 0, 0, tzinfo=timezone.utc)

    # Session 1
    for i, (action_type, target, ws) in enumerate([
        ("workspace_switch", "3", "3"),
        ("app_launch", "firefox", "3"),
    ]):
        conn.execute(
            "INSERT INTO actions (source, action_type, target, workspace_id, "
            "observed_at, recorded_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("hyprland", action_type, target, ws,
             _ts(base, i), _ts(base, i)),
        )

    # Session 2 (next day — >30 min gap from session 1)
    base2 = base + timedelta(days=1, hours=4)
    for i, (action_type, target, ws) in enumerate([
        ("workspace_switch", "5", "5"),
        ("app_launch", "firefox", "5"),
    ]):
        conn.execute(
            "INSERT INTO actions (source, action_type, target, workspace_id, "
            "observed_at, recorded_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("hyprland", action_type, target, ws,
             _ts(base2, i), _ts(base2, i)),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# The actual smoke test
# ---------------------------------------------------------------------------

class TestSkillsPipelineSmoke:
    """End-to-end: actions → mine → label → form → list."""

    def test_full_pipeline(self, conn):
        """Run the full skills pipeline and verify each stage produces output."""
        # ---------------------------------------------------------------
        # Stage 0: Seed action events
        # ---------------------------------------------------------------
        seed_two_sessions(conn)

        action_count = conn.execute("SELECT COUNT(*) as c FROM actions").fetchone()["c"]
        assert action_count == 4, f"Expected 4 actions, got {action_count}"

        # ---------------------------------------------------------------
        # Stage 1: Mine sequences
        # ---------------------------------------------------------------
        from friday.sequence_miner import mine_sequences

        patterns = mine_sequences(conn, min_support=1)
        assert len(patterns) >= 1, (
            f"Expected at least 1 mined pattern, got {len(patterns)}"
        )

        # The pattern should be (workspace_switch,<workspace>) → (app_launch,<app>)
        best = patterns[0]
        expected_seq = [("workspace_switch", "<workspace>"), ("app_launch", "<app>")]
        assert best.sequence == expected_seq, (
            f"Expected {expected_seq}, got {best.sequence}"
        )
        assert best.count >= 2, (
            f"Expected pattern in ≥2 sessions, got {best.count}"
        )
        # Exemplars should have concrete value distributions
        assert len(best.exemplars) >= 1, "Expected exemplars on mined pattern"
        step0 = best.exemplars.get("0", {})
        assert "3" in step0 and "5" in step0, (
            f"Expected workspace values 3 and 5 in exemplars, got {step0}"
        )

        # ---------------------------------------------------------------
        # Stage 2: Persist mined patterns + label intents
        # ---------------------------------------------------------------
        from friday.db import (
            insert_mined_pattern, insert_workflow_intent, now_iso)

        for p in patterns:
            pid = insert_mined_pattern(conn, {
                "sequence_json": json.dumps([[t, tg] for t, tg in p.sequence]),
                "count": p.count,
                "distinct_sessions": p.count,
                "first_seen": p.first_seen or now_iso(),
                "last_seen": p.last_seen or now_iso(),
                "common_workspace": p.common_workspace or "",
                "common_project": p.common_project or "",
                "confidence": "derived",
                "exemplars": json.dumps(p.exemplars) if p.exemplars else "{}",
                "mined_at": now_iso(),
            })

            # Label this pattern.
            from friday.intent_labeler import label_intent
            intent = label_intent(
                pattern_sequence=[tuple(item) for item in p.sequence],
                pattern_count=p.count,
                workspace=p.common_workspace,
                project=p.common_project,
            )
            assert intent.intent_label, "Expected non-empty intent label"
            assert len(intent.steps) > 0, "Expected at least 1 step"

            # Force high confidence so form_skills always picks it up
            # regardless of LLM availability.
            effective_conf = "high" if intent.confidence in (
                "fallback", "low", "medium"
            ) else intent.confidence

            insert_workflow_intent(conn, {
                "pattern_id": pid,
                "intent_label": intent.intent_label,
                "intent_description": intent.intent_description or "",
                "steps_text": json.dumps(intent.steps),
                "confidence": effective_conf,
                "pattern_summary": json.dumps(
                    [[t, tg] for t, tg in intent.pattern_seq]
                ),
                "labeled_at": intent.labeled_at or now_iso(),
            })

        # Verify intents were persisted
        from friday.db import get_workflow_intents
        intents = get_workflow_intents(conn)
        assert len(intents) >= 1, f"Expected ≥1 intent, got {len(intents)}"

        # ---------------------------------------------------------------
        # Stage 3: Form skills from intents
        # ---------------------------------------------------------------
        from friday.skill_formation import form_skills, format_formed_skills

        skills = form_skills(conn)
        # At least one skill should be formed (high confidence intents)
        assert len(skills) >= 1, f"Expected ≥1 formed skill, got {len(skills)}"

        skill = skills[0]
        assert skill["status"] in ("beta", "proposed"), (
            f"Unexpected status: {skill['status']}"
        )
        assert skill["step_count"] == 2, (
            f"Expected 2 steps, got {skill['step_count']}"
        )
        assert skill["worker_name"], "Expected non-empty worker name"

        # Verify the formed_skills table has the row
        from friday.db import get_all_formed_skills
        formed = get_all_formed_skills(conn)
        assert len(formed) == 1, f"Expected 1 formed skill, got {len(formed)}"
        fs = formed[0]
        assert json.loads(fs["task_graph"]) == [
            ["workspace_switch", "<workspace>"],
            ["app_launch", "<app>"],
        ]

        # Verify the workers table has the row
        from friday.db import get_worker_by_name
        worker = get_worker_by_name(conn, skill["worker_name"])
        assert worker is not None, (
            f"Worker '{skill['worker_name']}' not found in registry"
        )
        assert worker.worker_kind == "formed_skill"
        assert worker.kind == "formed_skill"
        assert worker.status == skill["status"]
        assert worker.manifest_ref == f"formed_skill:{skill['skill_id']}"

        # Verify worker_capabilities
        cap_row = conn.execute(
            "SELECT capability FROM worker_capabilities WHERE worker_id = ?",
            (worker.id,)
        ).fetchone()
        assert cap_row is not None, "Expected worker_capability row"
        assert "Workflow Replay" in cap_row["capability"]

        # ---------------------------------------------------------------
        # Stage 4: Verify formed skills via DB queries (cmd_skills is tested
        # separately in test_cli_skills.py and would close the DB connection).
        # ---------------------------------------------------------------
        from friday.db import get_all_formed_skills
        all_formed = get_all_formed_skills(conn)
        assert len(all_formed) >= 1, "Expected ≥1 formed skill in DB"
        assert all_formed[0]["task_graph"], "Expected non-empty task_graph"
        assert all_formed[0]["invocation_count"] == 0, "New skill should have 0 invocations"

        # ---------------------------------------------------------------
        # Stage 5: Force re-form (--force flag)
        # ---------------------------------------------------------------
        skills_reformed = form_skills(conn, force=True)
        assert len(skills_reformed) >= 1, (
            f"Expected ≥1 re-formed skill, got {len(skills_reformed)}"
        )
        # Only 1 formed_skills row should exist (old one replaced)
        count = conn.execute(
            "SELECT COUNT(*) as c FROM formed_skills"
        ).fetchone()["c"]
        assert count == 1, f"Expected 1 formed_skill after re-form, got {count}"

        # ---------------------------------------------------------------
        # Stage 6: Verify idempotency (re-running form_skills does nothing)
        # ---------------------------------------------------------------
        skills_again = form_skills(conn)
        assert skills_again == [], (
            "Re-running form_skills should return empty (all intents already formed)"
        )

    def test_pipeline_with_low_confidence_skips(self, conn):
        """Low confidence intents are skipped by form_skills."""
        now = datetime.now(timezone.utc).isoformat()

        # Seed a mined pattern and a low-confidence intent directly
        cur = conn.execute(
            "INSERT INTO mined_patterns (sequence_json, count, mined_at) "
            "VALUES (?, ?, ?)",
            (json.dumps([["test_action", "<test>"]]), 1, now),
        )
        pat_id = cur.lastrowid

        conn.execute(
            "INSERT INTO workflow_intents (pattern_id, intent_label, confidence, "
            "pattern_summary, labeled_at) VALUES (?, ?, ?, ?, ?)",
            (pat_id, "Low confidence test", "low",
             json.dumps([["test_action", "<test>"]]), now),
        )

        from friday.skill_formation import form_skills
        skills = form_skills(conn)
        assert skills == [], (
            "Should not form skills from low-confidence intents"
        )

    def test_cli_skills_list_empty(self, conn):
        """Skills list with only the pipeline tables shows empty state."""
        import argparse
        from friday import cli_skills as mod

        original = mod.connect
        try:
            mod.connect = lambda: conn
            with patch("sys.stdout", new_callable=StringIO) as cap:
                rc = mod.cmd_skills(argparse.Namespace(action=None))
            output = cap.getvalue()
            assert rc == 0
            assert "No formed skills yet" in output
        finally:
            mod.connect = original

    def test_replay_executor_builds_steps_with_exemplars(self):
        """ReplayExecutor correctly resolves exemplars from a formed skill."""
        from friday.skill_formation import ReplayExecutor

        task_graph = [["workspace_switch", "<workspace>"], ["app_launch", "<app>"]]
        exemplars = {
            "0": {"default": "3", "distribution": {"3": 5, "1": 1},
                  "consensus": 0.833, "stable": True},
            "1": {"default": "firefox", "distribution": {"firefox": 6},
                  "consensus": 1.0, "stable": True},
        }
        exe = ReplayExecutor(
            worker_id="worker:test:e2e",
            task_graph=task_graph,
            exemplars=exemplars,
        )
        steps = exe.build_steps()
        assert steps == [
            ("workspace_switch", "3"),
            ("app_launch", "firefox"),
        ]
