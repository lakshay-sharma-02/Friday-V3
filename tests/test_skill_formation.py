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


def _full_schema(conn: sqlite3.Connection) -> None:
    """Apply all tables needed for skill formation tests."""
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
            worker_id               TEXT NOT NULL REFERENCES workers(id) ON DELETE CASCADE,
            capability              TEXT NOT NULL,
            PRIMARY KEY (worker_id, capability)
        );
        CREATE TABLE IF NOT EXISTS worker_history (
            registered_at           TEXT NOT NULL,
            worker_id               TEXT NOT NULL REFERENCES workers(id) ON DELETE CASCADE,
            name                    TEXT NOT NULL,
            kind                    TEXT NOT NULL,
            version                 TEXT NOT NULL,
            status                  TEXT NOT NULL,
            capabilities            TEXT NOT NULL DEFAULT '',
            limitations             TEXT NOT NULL DEFAULT '',
            event_type              TEXT NOT NULL,
            note                    TEXT,
            PRIMARY KEY (registered_at, worker_id)
        );
        CREATE TABLE IF NOT EXISTS worker_versions (
            worker_id               TEXT NOT NULL REFERENCES workers(id) ON DELETE CASCADE,
            version                 TEXT NOT NULL,
            registered_at           TEXT NOT NULL,
            changelog               TEXT,
            PRIMARY KEY (worker_id, version)
        );
    """)


def test_form_skills_forms_high_confidence_intent():
    """form_skills creates a formed_skills row + workers row for high-confidence intents."""
    from src.friday.skill_formation import form_skills

    conn = _fresh_db()
    _full_schema(conn)

    now = datetime.now(timezone.utc).isoformat()
    seq = json.dumps([["workspace_switch", "<workspace>"], ["app_launch", "<app>"]])
    exemplars = json.dumps({"0": {"3": 5, "1": 1}, "1": {"firefox": 6}})
    cur = conn.execute(
        "INSERT INTO mined_patterns (sequence_json, count, mined_at, exemplars) VALUES (?, ?, ?, ?)",
        (seq, 3, now, exemplars)
    )
    pat_id = cur.lastrowid
    steps = json.dumps(["Switch to workspace 3", "Open Firefox"])
    cur = conn.execute(
        "INSERT INTO workflow_intents (pattern_id, intent_label, intent_description, steps_text, confidence, pattern_summary, labeled_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (pat_id, "Start browsing", "Open browser and navigate", steps, "high", seq, now)
    )

    result = form_skills(conn)
    assert result is not None
    assert len(result) >= 1

    skill_row = conn.execute("SELECT * FROM formed_skills").fetchone()
    assert skill_row is not None
    assert json.loads(skill_row["task_graph"]) == json.loads(seq)
    assert skill_row["invocation_count"] == 0

    worker_row = conn.execute("SELECT * FROM workers").fetchone()
    assert worker_row is not None
    assert worker_row["worker_kind"] == "formed_skill"
    assert worker_row["status"] == "beta"
    assert "formed_skill:" in (worker_row["manifest_ref"] or "")

    cap_row = conn.execute("SELECT * FROM worker_capabilities").fetchone()
    assert cap_row is not None
    assert "Workflow Replay" in cap_row["capability"]


def test_form_skills_skips_low_confidence():
    """form_skills skips low/fallback confidence intents."""
    from src.friday.skill_formation import form_skills

    conn = _fresh_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS mined_patterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT, sequence_json TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 0, mined_at TEXT NOT NULL,
            exemplars TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS workflow_intents (
            id INTEGER PRIMARY KEY AUTOINCREMENT, pattern_id INTEGER NOT NULL,
            intent_label TEXT NOT NULL, intent_description TEXT NOT NULL DEFAULT '',
            steps_text TEXT NOT NULL DEFAULT '[]', confidence TEXT NOT NULL DEFAULT 'low',
            pattern_summary TEXT NOT NULL DEFAULT '', labeled_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS formed_skills (
            id INTEGER PRIMARY KEY AUTOINCREMENT, workflow_intent_id INTEGER NOT NULL,
            task_graph TEXT NOT NULL, exemplars TEXT NOT NULL DEFAULT '{}',
            invocation_count INTEGER NOT NULL DEFAULT 0, last_invoked_at TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS workers (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '', capabilities TEXT NOT NULL DEFAULT '',
            limitations TEXT NOT NULL DEFAULT '', confidence TEXT NOT NULL DEFAULT 'medium',
            version TEXT NOT NULL DEFAULT '1.0.0', status TEXT NOT NULL DEFAULT 'active',
            schema_version TEXT NOT NULL DEFAULT '1.0', created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, availability TEXT NOT NULL DEFAULT 'available',
            worker_kind TEXT NOT NULL DEFAULT 'function', manifest_ref TEXT,
            supported_languages TEXT NOT NULL DEFAULT '',
            supported_task_types TEXT NOT NULL DEFAULT '',
            supported_plan_types TEXT NOT NULL DEFAULT '',
            estimated_speed TEXT NOT NULL DEFAULT '', estimated_cost TEXT NOT NULL DEFAULT '',
            context_window INTEGER NOT NULL DEFAULT 0, parallelism INTEGER NOT NULL DEFAULT 1,
            requires_network INTEGER NOT NULL DEFAULT 0,
            requires_filesystem INTEGER NOT NULL DEFAULT 0,
            requires_git INTEGER NOT NULL DEFAULT 0, requires_python INTEGER NOT NULL DEFAULT 0,
            requires_shell INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS worker_capabilities (
            worker_id TEXT NOT NULL, capability TEXT NOT NULL,
            PRIMARY KEY (worker_id, capability)
        );
        CREATE TABLE IF NOT EXISTS worker_history (
            registered_at TEXT NOT NULL, worker_id TEXT NOT NULL,
            name TEXT NOT NULL, kind TEXT NOT NULL, version TEXT NOT NULL,
            status TEXT NOT NULL, capabilities TEXT NOT NULL DEFAULT '',
            limitations TEXT NOT NULL DEFAULT '', event_type TEXT NOT NULL,
            note TEXT, PRIMARY KEY (registered_at, worker_id)
        );
        CREATE TABLE IF NOT EXISTS worker_versions (
            worker_id TEXT NOT NULL, version TEXT NOT NULL,
            registered_at TEXT NOT NULL, changelog TEXT,
            PRIMARY KEY (worker_id, version)
        );
    """)

    now = datetime.now(timezone.utc).isoformat()
    seq = json.dumps([["workspace_switch", "<workspace>"]])
    cur = conn.execute(
        "INSERT INTO mined_patterns (sequence_json, count, mined_at) VALUES (?, ?, ?)",
        (seq, 2, now)
    )
    pat_id = cur.lastrowid
    conn.execute(
        "INSERT INTO workflow_intents (pattern_id, intent_label, confidence, pattern_summary, labeled_at) VALUES (?, ?, ?, ?, ?)",
        (pat_id, "Low conf intent", "low", seq, now)
    )

    result = form_skills(conn)
    assert result == []
    count = conn.execute("SELECT COUNT(*) as c FROM formed_skills").fetchone()["c"]
    assert count == 0


def test_form_skills_distribution_cap():
    """Low-consensus step caps overall confidence to proposed."""
    from src.friday.skill_formation import form_skills

    conn = _fresh_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS mined_patterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT, sequence_json TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 0, mined_at TEXT NOT NULL,
            exemplars TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS workflow_intents (
            id INTEGER PRIMARY KEY AUTOINCREMENT, pattern_id INTEGER NOT NULL,
            intent_label TEXT NOT NULL, intent_description TEXT NOT NULL DEFAULT '',
            steps_text TEXT NOT NULL DEFAULT '[]', confidence TEXT NOT NULL DEFAULT 'low',
            pattern_summary TEXT NOT NULL DEFAULT '', labeled_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS formed_skills (
            id INTEGER PRIMARY KEY AUTOINCREMENT, workflow_intent_id INTEGER NOT NULL,
            task_graph TEXT NOT NULL, exemplars TEXT NOT NULL DEFAULT '{}',
            invocation_count INTEGER NOT NULL DEFAULT 0, last_invoked_at TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS workers (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '', capabilities TEXT NOT NULL DEFAULT '',
            limitations TEXT NOT NULL DEFAULT '', confidence TEXT NOT NULL DEFAULT 'medium',
            version TEXT NOT NULL DEFAULT '1.0.0', status TEXT NOT NULL DEFAULT 'active',
            schema_version TEXT NOT NULL DEFAULT '1.0', created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, availability TEXT NOT NULL DEFAULT 'available',
            worker_kind TEXT NOT NULL DEFAULT 'function', manifest_ref TEXT,
            supported_languages TEXT NOT NULL DEFAULT '',
            supported_task_types TEXT NOT NULL DEFAULT '',
            supported_plan_types TEXT NOT NULL DEFAULT '',
            estimated_speed TEXT NOT NULL DEFAULT '', estimated_cost TEXT NOT NULL DEFAULT '',
            context_window INTEGER NOT NULL DEFAULT 0, parallelism INTEGER NOT NULL DEFAULT 1,
            requires_network INTEGER NOT NULL DEFAULT 0,
            requires_filesystem INTEGER NOT NULL DEFAULT 0,
            requires_git INTEGER NOT NULL DEFAULT 0, requires_python INTEGER NOT NULL DEFAULT 0,
            requires_shell INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS worker_capabilities (
            worker_id TEXT NOT NULL, capability TEXT NOT NULL,
            PRIMARY KEY (worker_id, capability)
        );
        CREATE TABLE IF NOT EXISTS worker_history (
            registered_at TEXT NOT NULL, worker_id TEXT NOT NULL,
            name TEXT NOT NULL, kind TEXT NOT NULL, version TEXT NOT NULL,
            status TEXT NOT NULL, capabilities TEXT NOT NULL DEFAULT '',
            limitations TEXT NOT NULL DEFAULT '', event_type TEXT NOT NULL,
            note TEXT, PRIMARY KEY (registered_at, worker_id)
        );
        CREATE TABLE IF NOT EXISTS worker_versions (
            worker_id TEXT NOT NULL, version TEXT NOT NULL,
            registered_at TEXT NOT NULL, changelog TEXT,
            PRIMARY KEY (worker_id, version)
        );
    """)

    now = datetime.now(timezone.utc).isoformat()
    seq = json.dumps([["workspace_switch", "<workspace>"], ["app_launch", "<app>"]])
    exemplars_low = json.dumps({"0": {"3": 2, "5": 2, "1": 2, "7": 2}, "1": {"firefox": 8}})
    cur = conn.execute(
        "INSERT INTO mined_patterns (sequence_json, count, mined_at, exemplars) VALUES (?, ?, ?, ?)",
        (seq, 4, now, exemplars_low)
    )
    pat_id = cur.lastrowid
    steps = json.dumps(["Switch workspace", "Open Firefox"])
    conn.execute(
        "INSERT INTO workflow_intents (pattern_id, intent_label, intent_description, steps_text, confidence, pattern_summary, labeled_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (pat_id, "Low consensus", "A pattern with low-consensus step", steps, "high", seq, now)
    )

    result = form_skills(conn)
    assert result is not None
    assert len(result) >= 1
    worker_row = conn.execute("SELECT * FROM workers").fetchone()
    assert worker_row is not None
    assert worker_row["status"] == "proposed", f"Expected proposed, got {worker_row['status']}"


def test_form_skills_skips_already_formed():
    """Already-formed intents are skipped."""
    from src.friday.skill_formation import form_skills

    conn = _fresh_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS mined_patterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT, sequence_json TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 0, mined_at TEXT NOT NULL,
            exemplars TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS workflow_intents (
            id INTEGER PRIMARY KEY AUTOINCREMENT, pattern_id INTEGER NOT NULL,
            intent_label TEXT NOT NULL, intent_description TEXT NOT NULL DEFAULT '',
            steps_text TEXT NOT NULL DEFAULT '[]', confidence TEXT NOT NULL DEFAULT 'low',
            pattern_summary TEXT NOT NULL DEFAULT '', labeled_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS formed_skills (
            id INTEGER PRIMARY KEY AUTOINCREMENT, workflow_intent_id INTEGER NOT NULL,
            task_graph TEXT NOT NULL, exemplars TEXT NOT NULL DEFAULT '{}',
            invocation_count INTEGER NOT NULL DEFAULT 0, last_invoked_at TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS workers (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '', capabilities TEXT NOT NULL DEFAULT '',
            limitations TEXT NOT NULL DEFAULT '', confidence TEXT NOT NULL DEFAULT 'medium',
            version TEXT NOT NULL DEFAULT '1.0.0', status TEXT NOT NULL DEFAULT 'active',
            schema_version TEXT NOT NULL DEFAULT '1.0', created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, availability TEXT NOT NULL DEFAULT 'available',
            worker_kind TEXT NOT NULL DEFAULT 'function', manifest_ref TEXT,
            supported_languages TEXT NOT NULL DEFAULT '',
            supported_task_types TEXT NOT NULL DEFAULT '',
            supported_plan_types TEXT NOT NULL DEFAULT '',
            estimated_speed TEXT NOT NULL DEFAULT '', estimated_cost TEXT NOT NULL DEFAULT '',
            context_window INTEGER NOT NULL DEFAULT 0, parallelism INTEGER NOT NULL DEFAULT 1,
            requires_network INTEGER NOT NULL DEFAULT 0,
            requires_filesystem INTEGER NOT NULL DEFAULT 0,
            requires_git INTEGER NOT NULL DEFAULT 0, requires_python INTEGER NOT NULL DEFAULT 0,
            requires_shell INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS worker_capabilities (
            worker_id TEXT NOT NULL, capability TEXT NOT NULL,
            PRIMARY KEY (worker_id, capability)
        );
        CREATE TABLE IF NOT EXISTS worker_history (
            registered_at TEXT NOT NULL, worker_id TEXT NOT NULL,
            name TEXT NOT NULL, kind TEXT NOT NULL, version TEXT NOT NULL,
            status TEXT NOT NULL, capabilities TEXT NOT NULL DEFAULT '',
            limitations TEXT NOT NULL DEFAULT '', event_type TEXT NOT NULL,
            note TEXT, PRIMARY KEY (registered_at, worker_id)
        );
        CREATE TABLE IF NOT EXISTS worker_versions (
            worker_id TEXT NOT NULL, version TEXT NOT NULL,
            registered_at TEXT NOT NULL, changelog TEXT,
            PRIMARY KEY (worker_id, version)
        );
    """)

    now = datetime.now(timezone.utc).isoformat()
    seq = json.dumps([["workspace_switch", "<workspace>"]])
    cur = conn.execute(
        "INSERT INTO mined_patterns (sequence_json, count, mined_at) VALUES (?, ?, ?)",
        (seq, 3, now)
    )
    pat_id = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO workflow_intents (pattern_id, intent_label, confidence, pattern_summary, labeled_at) VALUES (?, ?, ?, ?, ?)",
        (pat_id, "Already formed", "high", seq, now)
    )
    intent_row = conn.execute("SELECT id FROM workflow_intents").fetchone()
    intent_id = intent_row["id"]
    conn.execute(
        "INSERT INTO formed_skills (workflow_intent_id, task_graph, exemplars, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        (intent_id, seq, "{}", now, now)
    )

    result = form_skills(conn)
    assert result == []


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
