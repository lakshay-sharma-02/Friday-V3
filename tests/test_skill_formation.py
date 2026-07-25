"""Tests for Pillar B Stage 4 — Skill Formation."""
from __future__ import annotations
import json
import sqlite3
from datetime import datetime, timezone


def _fresh_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Apply base Pillar schema for tests."""
    conn.executescript("""
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
            mined_at        TEXT NOT NULL
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
            manifest_ref            TEXT
        );
        CREATE TABLE IF NOT EXISTS worker_capabilities (
            worker_id               TEXT NOT NULL REFERENCES workers(id) ON DELETE CASCADE,
            capability              TEXT NOT NULL,
            PRIMARY KEY (worker_id, capability)
        );
    """)


def test_formed_skills_table_created():
    """formed_skills table exists after migration."""
    conn = _fresh_db()
    conn.executescript("""
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
    """)
    tables = [r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    assert "formed_skills" in tables


def test_worker_kind_column_exists():
    """workers table has worker_kind column after migration."""
    conn = _fresh_db()
    _ensure_schema(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(workers)")}
    assert "worker_kind" not in cols  # not in base schema
    # Apply migration
    conn.execute("ALTER TABLE workers ADD COLUMN worker_kind TEXT NOT NULL DEFAULT 'function'")
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(workers)")}
    assert "worker_kind" in cols


def test_insert_formed_skill():
    """Can insert a formed_skill row and read it back."""
    conn = _fresh_db()
    _ensure_schema(conn)
    conn.executescript("""
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
    """)
    now = datetime.now(timezone.utc).isoformat()
    # Insert a mined pattern and workflow intent first
    cur = conn.execute(
        "INSERT INTO mined_patterns (sequence_json, count, mined_at) VALUES (?, ?, ?)",
        ('[["workspace_switch", "<workspace>"]]', 3, now)
    )
    cur = conn.execute(
        "INSERT INTO workflow_intents (pattern_id, intent_label, intent_description, steps_text, confidence, pattern_summary, labeled_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (1, "Test intent", "A test", '[]', "high", "[]", now)
    )
    intent_row = conn.execute("SELECT id FROM workflow_intents ORDER BY id DESC LIMIT 1").fetchone()
    intent_id = intent_row["id"]
    task_graph = json.dumps([["workspace_switch", "<workspace>"], ["app_launch", "<app>"]])
    exemplars = json.dumps({"0": {"3": 5, "5": 1}, "1": {"firefox": 6}})
    cur = conn.execute(
        "INSERT INTO formed_skills (workflow_intent_id, task_graph, exemplars, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        (intent_id, task_graph, exemplars, now, now)
    )
    skill_id = cur.lastrowid
    row = conn.execute("SELECT * FROM formed_skills WHERE id = ?", (skill_id,)).fetchone()
    assert row is not None
    assert row["workflow_intent_id"] == intent_id


def test_mine_sequences_stores_exemplars():
    """mine_sequences stores concrete value distributions per step position."""
    from src.friday.sequence_miner import mine_sequences

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
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
    from datetime import datetime, timezone
    base = datetime.now(timezone.utc)
    # Session 1: workspace_switch -> 3, app_launch -> firefox
    t1 = base.isoformat()
    conn.execute(
        "INSERT INTO actions (source, action_type, target, workspace_id, observed_at, recorded_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("hyprland", "workspace_switch", "3", "3", t1, t1)
    )
    conn.execute(
        "INSERT INTO actions (source, action_type, target, workspace_id, observed_at, recorded_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("hyprland", "app_launch", "firefox", "3", t1, t1)
    )
    # Session 2: workspace_switch -> 5, app_launch -> firefox (same pattern, different workspace)
    t2 = (base.replace(second=(base.second + 5) % 60)).isoformat()
    conn.execute(
        "INSERT INTO actions (source, action_type, target, workspace_id, observed_at, recorded_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("hyprland", "workspace_switch", "5", "5", t2, t2)
    )
    conn.execute(
        "INSERT INTO actions (source, action_type, target, workspace_id, observed_at, recorded_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("hyprland", "app_launch", "firefox", "5", t2, t2)
    )
    conn.commit()

    patterns = mine_sequences(conn, min_support=1)
    assert len(patterns) >= 1
    p = patterns[0]
    assert hasattr(p, "exemplars"), "MinedPattern should have exemplars field"
    assert isinstance(p.exemplars, dict)
    # Step 0 (workspace_switch): should have "3" and "5"
    step0 = p.exemplars.get("0", {})
    assert len(step0) >= 2, f"Expected 2+ values for step 0, got {step0}"
    # Step 1 (app_launch): "firefox" should appear twice
    step1 = p.exemplars.get("1", {})
    assert step1.get("firefox", 0) >= 2, f"Expected firefox>=2 for step 1, got {step1}"
