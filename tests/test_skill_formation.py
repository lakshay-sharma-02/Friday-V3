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


def test_form_skills_force_reforms_already_formed():
    """form_skills with force=True re-forms intents that already have skills."""
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
    # Pre-insert a formed_skill for this intent.
    conn.execute(
        "INSERT INTO formed_skills (workflow_intent_id, task_graph, exemplars, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        (intent_id, seq, "{}", now, now)
    )

    # Without force, should skip.
    result_without = form_skills(conn)
    assert result_without == []

    # With force=True, should re-form.
    result_force = form_skills(conn, force=True)
    assert len(result_force) >= 1
    assert result_force[0]["step_count"] == 1

    # Old formed_skill should be replaced (only 1 row).
    count = conn.execute("SELECT COUNT(*) as c FROM formed_skills").fetchone()["c"]
    assert count == 1, f"Expected 1 formed_skill, got {count}"


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


def test_replay_executor_builds_steps():
    """ReplayExecutor resolves exemplars and builds step list."""
    from src.friday.skill_formation import ReplayExecutor

    task_graph = [["workspace_switch", "<workspace>"], ["app_launch", "<app>"]]
    exemplars = {
        "0": {"default": "3", "distribution": {"3": 5, "1": 1}, "consensus": 0.833, "stable": True},
        "1": {"default": "firefox", "distribution": {"firefox": 6}, "consensus": 1.0, "stable": True},
    }
    executor = ReplayExecutor(
        worker_id="worker:test:abc123",
        task_graph=task_graph,
        exemplars=exemplars,
    )
    steps = executor.build_steps()
    assert len(steps) == 2
    assert steps[0] == ("workspace_switch", "3")
    assert steps[1] == ("app_launch", "firefox")


def test_replay_executor_abort_on_failure():
    """ReplayExecutor aborts on first failure when strategy is abort."""
    from src.friday.skill_formation import ReplayExecutor
    from src.friday.runtime.models import RuntimeTask, ExecutionResult
    from unittest.mock import patch

    executor = ReplayExecutor(
        worker_id="worker:test:abc",
        task_graph=[["workspace_switch", "<workspace>"], ["app_launch", "<app>"]],
        exemplars={
            "0": {"default": "3", "distribution": {"3": 5}, "consensus": 1.0, "stable": True},
            "1": {"default": "firefox", "distribution": {"firefox": 6}, "consensus": 1.0, "stable": True},
        },
        on_failure="abort",
    )
    task = RuntimeTask(
        execution_id="test", session_id="s", schedule_id="s",
        task_id="t", worker_id="w", wave=1,
        runtime_payload="",
    )

    class FailingMock:
        worker_id = "worker:hyprctl"
        def execute(self, task):
            return ExecutionResult(
                success=False, stdout="", stderr="", exit_code=1,
                duration_ms=1, error="mock failure")

    with patch.object(executor, "_resolve_strategies", return_value={}):
        with patch("src.friday.runtime.executors.resolve_executor",
                   return_value=FailingMock()):
            result = executor.execute(task)

    assert result.success is False
    import json
    output = json.loads(result.stdout)
    results = output["results"]
    assert len(results) == 1, (
        f"Expected only 1 result (aborted), got {len(results)}"
    )
    assert results[0]["step"] == 0
    assert results[0]["success"] is False
    assert results[0].get("aborted_remaining") == 1, (
        "Expected aborted_remaining=1 (1 step skipped)"
    )


def test_replay_executor_skip_on_failure():
    """ReplayExecutor skips failed steps and continues."""
    from src.friday.skill_formation import ReplayExecutor
    from src.friday.runtime.models import RuntimeTask, ExecutionResult
    from unittest.mock import patch

    executor = ReplayExecutor(
        worker_id="worker:test:abc",
        task_graph=[["workspace_switch", "<workspace>"], ["app_launch", "<app>"]],
        exemplars={
            "0": {"default": "3", "distribution": {"3": 5}, "consensus": 1.0, "stable": True},
            "1": {"default": "firefox", "distribution": {"firefox": 6}, "consensus": 1.0, "stable": True},
        },
        on_failure="skip",
    )
    task = RuntimeTask(
        execution_id="test", session_id="s", schedule_id="s",
        task_id="t", worker_id="w", wave=1,
        runtime_payload="",
    )
    class AlternatingMock:
        worker_id = "worker:hyprctl"
        call_count = 0
        def execute(self, task):
            self.call_count += 1
            if self.call_count <= 1:
                return ExecutionResult(
                    success=False, stdout="", stderr="", exit_code=1,
                    duration_ms=1, error="mock failure")
            return ExecutionResult(
                success=True, stdout="ok", stderr="", exit_code=0,
                duration_ms=1)

    with patch.object(executor, "_resolve_strategies", return_value={}):
        with patch("src.friday.runtime.executors.resolve_executor",
                   return_value=AlternatingMock()):
            result = executor.execute(task)

    assert result.success is True, (
        f"Expected overall success with skip strategy, got: {result.error}"
    )
    import json
    output = json.loads(result.stdout)
    results = output["results"]
    assert len(results) == 2, (
        f"Expected 2 results (skip continues), got {len(results)}"
    )
    assert results[0]["success"] is False
    assert results[0]["skipped"] is True
    assert results[1]["success"] is True


def test_replay_executor_retry_alt_on_failure():
    """ReplayExecutor retries with alternate exemplar on failure."""
    from src.friday.skill_formation import ReplayExecutor
    from src.friday.runtime.models import RuntimeTask, ExecutionResult
    from unittest.mock import patch

    executor = ReplayExecutor(
        worker_id="worker:test:abc",
        task_graph=[["workspace_switch", "<workspace>"]],
        exemplars={
            "0": {
                "default": "3",
                "distribution": {"3": 5, "5": 2},
                "consensus": 0.714,
                "stable": False,
            },
        },
        on_failure="retry_alt",
    )
    task = RuntimeTask(
        execution_id="test", session_id="s", schedule_id="s",
        task_id="t", worker_id="w", wave=1,
        runtime_payload="",
    )
    first_run = [True]
    class RetryMock:
        worker_id = "worker:hyprctl"
        def execute(self, task):
            payload = getattr(task, "runtime_payload", "")
            if first_run[0]:
                first_run[0] = False
                return ExecutionResult(
                    success=False, stdout="", stderr="", exit_code=1,
                    duration_ms=1, error="mock failure")
            assert '"target": "5"' in payload, (
                f"Expected alt target in payload, got: {payload}"
            )
            return ExecutionResult(
                success=True, stdout="ok", stderr="", exit_code=0,
                duration_ms=1)

    with patch.object(executor, "_resolve_strategies", return_value={}):
        with patch("src.friday.runtime.executors.resolve_executor",
                   return_value=RetryMock()):
            result = executor.execute(task)

    assert result.success is True, (
        f"Expected success after retry_alt, got: {result.error}"
    )
    import json
    output = json.loads(result.stdout)
    results = output["results"]
    assert len(results) == 1
    assert results[0]["success"] is True
    assert results[0]["exemplar_source"] == "retry_alt"


def test_replay_executor_auto_downgrade_from_log():
    """ReplayExecutor auto-downgrades abort to skip when log shows 3+ failures."""
    from src.friday.skill_formation import ReplayExecutor, _db_connect
    from src.friday.runtime.models import RuntimeTask
    from unittest.mock import patch

    # Seed the action log in an in-memory DB.
    from src.friday.db import connect as _real_connect
    test_conn = _real_connect(":memory:")
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    # Ensure actions table exists.
    test_conn.execute("""
        CREATE TABLE IF NOT EXISTS actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL, action_type TEXT NOT NULL,
            target TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '{}',
            workspace_id TEXT, project TEXT, session_id TEXT,
            confidence TEXT NOT NULL DEFAULT 'observed',
            observed_at TEXT NOT NULL, recorded_at TEXT NOT NULL
        )
    """)
    for i in range(3):
        detail = json.dumps({
            "results": [{"step": 0, "success": False, "error": "fail"}],
            "strategy": "abort",
        })
        # _resolve_strategies checks target column for skill_id in LIKE query
        # AND in the post-filter step. Ensure both target and detail are set.
        target = json.dumps({"skill_id": 1, "step_count": 1, "succeeded": False})
        test_conn.execute(
            "INSERT INTO actions (source, action_type, target, detail, "
            "observed_at, recorded_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("friday", "skill_replay", target, detail, now, now)
        )
    test_conn.commit()

    executor = ReplayExecutor(
        worker_id="worker:test:abc",
        task_graph=[["workspace_switch", "<workspace>"], ["app_launch", "<app>"]],
        exemplars={
            "0": {"default": "3", "distribution": {"3": 5}, "consensus": 1.0, "stable": True},
            "1": {"default": "firefox", "distribution": {"firefox": 6}, "consensus": 1.0, "stable": True},
        },
        on_failure="abort",
    )

    # Patch _db_connect to return our seeded test DB.
    original_db = _db_connect
    try:
        import src.friday.skill_formation as sf
        sf._db_connect = lambda: test_conn
        strategies = executor._resolve_strategies(1)
        assert "0" in strategies, f"Expected step 0 downgraded, got {strategies}"
        assert strategies["0"] == "skip"
    finally:
        sf._db_connect = original_db

    test_conn.close()


def test_replay_executor_retry_alt_no_exemplars():
    """retry_alt with single-value distribution falls back to abort when no
    alternative exemplar is available."""
    from src.friday.skill_formation import ReplayExecutor
    from src.friday.runtime.models import RuntimeTask, ExecutionResult
    from unittest.mock import patch

    executor = ReplayExecutor(
        worker_id="worker:test:abc",
        task_graph=[["workspace_switch", "<workspace>"]],
        exemplars={
            "0": {
                "default": "3",
                "distribution": {"3": 5},  # Only one value in distribution
                "consensus": 1.0,
                "stable": True,
            },
        },
        on_failure="retry_alt",
    )
    task = RuntimeTask(
        execution_id="test", session_id="s", schedule_id="s",
        task_id="t", worker_id="w", wave=1,
        runtime_payload="",
    )

    class FailMock:
        def __init__(self):
            self.call_count = 0
        worker_id = "worker:hyprctl"
        def execute(self, task):
            self.call_count += 1
            return ExecutionResult(
                success=False, stdout="", stderr="", exit_code=1,
                duration_ms=1, error="mock failure")

    mock_instance = FailMock()
    with patch.object(executor, "_resolve_strategies", return_value={}):
        with patch("src.friday.runtime.executors.resolve_executor",
                   return_value=mock_instance):
            result = executor.execute(task)

    # No alternative exemplar available — should abort, not retry.
    assert result.success is False
    assert mock_instance.call_count == 1, (
        f"Expected 1 call (no retry), got {mock_instance.call_count}"
    )
    import json
    output = json.loads(result.stdout)
    results = output["results"]
    assert len(results) == 1
    assert results[0]["success"] is False
    assert "aborted_remaining" in results[0], (
        "Expected abort behavior when no alt exemplars"
    )


def test_replay_executor_retry_alt_all_exhausted():
    """retry_alt aborts when both the primary and the alternative exemplar fail."""
    from src.friday.skill_formation import ReplayExecutor
    from src.friday.runtime.models import RuntimeTask, ExecutionResult
    from unittest.mock import patch

    executor = ReplayExecutor(
        worker_id="worker:test:abc",
        task_graph=[["workspace_switch", "<workspace>"]],
        exemplars={
            "0": {
                "default": "3",
                "distribution": {"3": 5, "5": 2},
                "consensus": 0.714,
                "stable": False,
            },
        },
        on_failure="retry_alt",
    )
    task = RuntimeTask(
        execution_id="test", session_id="s", schedule_id="s",
        task_id="t", worker_id="w", wave=1,
        runtime_payload="",
    )

    call_count = [0]
    class FailAllMock:
        worker_id = "worker:hyprctl"
        def execute(self, task):
            call_count[0] += 1
            return ExecutionResult(
                success=False, stdout="", stderr="", exit_code=1,
                duration_ms=1, error="mock failure")

    with patch.object(executor, "_resolve_strategies", return_value={}):
        with patch("src.friday.runtime.executors.resolve_executor",
                   return_value=FailAllMock()):
            result = executor.execute(task)

    # Both primary and alt were tried — both failed.
    assert result.success is False
    assert call_count[0] == 2, (
        f"Expected 2 calls (primary + alt), got {call_count[0]}"
    )
    import json
    output = json.loads(result.stdout)
    results = output["results"]
    assert len(results) == 1
    assert results[0]["success"] is False
    assert "aborted_remaining" in results[0]


def test_replay_executor_auto_downgrade_at_two_failures():
    """Auto-downgrade does NOT trigger at exactly 2 failures (threshold is 3)."""
    from src.friday.skill_formation import ReplayExecutor, _db_connect
    from unittest.mock import patch

    import sqlite3
    test_conn = sqlite3.connect(":memory:")
    test_conn.row_factory = sqlite3.Row
    test_conn.execute("""
        CREATE TABLE IF NOT EXISTS actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL, action_type TEXT NOT NULL,
            target TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '{}',
            workspace_id TEXT, project TEXT, session_id TEXT,
            confidence TEXT NOT NULL DEFAULT 'observed',
            observed_at TEXT NOT NULL, recorded_at TEXT NOT NULL
        )
    """)
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    for i in range(2):  # Only 2 failures — below the 3+ threshold
        detail = json.dumps({
            "results": [{"step": 0, "success": False, "error": "fail"}],
            "strategy": "abort",
        })
        target = json.dumps({"skill_id": 1, "step_count": 1, "succeeded": False})
        test_conn.execute(
            "INSERT INTO actions (source, action_type, target, detail, "
            "observed_at, recorded_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("friday", "skill_replay", target, detail, now, now)
        )
    test_conn.commit()

    executor = ReplayExecutor(
        worker_id="worker:test:abc",
        task_graph=[["workspace_switch", "<workspace>"], ["app_launch", "<app>"]],
        exemplars={
            "0": {"default": "3", "distribution": {"3": 5}, "consensus": 1.0, "stable": True},
            "1": {"default": "firefox", "distribution": {"firefox": 6}, "consensus": 1.0, "stable": True},
        },
        on_failure="abort",
    )

    original_db = _db_connect
    try:
        import src.friday.skill_formation as sf
        sf._db_connect = lambda: test_conn
        strategies = executor._resolve_strategies(1)
        assert strategies == {}, f"Expected empty strategies at 2 failures, got {strategies}"
    finally:
        sf._db_connect = original_db

    test_conn.close()


def test_replay_executor_mixed_success_failure_with_skip():
    """Skip strategy with mixed success/failure: step 0 succeeds, step 1 fails
    (skipped), step 2 succeeds. Overall report shows partial results."""
    from src.friday.skill_formation import ReplayExecutor
    from src.friday.runtime.models import RuntimeTask, ExecutionResult
    from unittest.mock import patch

    executor = ReplayExecutor(
        worker_id="worker:test:abc",
        task_graph=[
            ["workspace_switch", "<workspace>"],
            ["app_launch", "<app>"],
            ["navigate", "<url>"],
        ],
        exemplars={
            "0": {"default": "3", "distribution": {"3": 5}, "consensus": 1.0, "stable": True},
            "1": {"default": "firefox", "distribution": {"firefox": 6}, "consensus": 1.0, "stable": True},
            "2": {"default": "https://example.com", "distribution": {"https://example.com": 4}, "consensus": 1.0, "stable": True},
        },
        on_failure="skip",
    )
    task = RuntimeTask(
        execution_id="test", session_id="s", schedule_id="s",
        task_id="t", worker_id="w", wave=1,
        runtime_payload="",
    )

    call = [0]
    class MixedMock:
        worker_id = "worker:hyprctl"
        def execute(self, task):
            call[0] += 1
            if call[0] == 1:
                return ExecutionResult(
                    success=True, stdout="ok", stderr="", exit_code=0,
                    duration_ms=1)
            if call[0] == 2:
                return ExecutionResult(
                    success=False, stdout="", stderr="", exit_code=1,
                    duration_ms=1, error="mock failure")
            return ExecutionResult(
                success=True, stdout="ok", stderr="", exit_code=0,
                duration_ms=1)

    with patch.object(executor, "_resolve_strategies", return_value={}):
        with patch("src.friday.runtime.executors.resolve_executor",
                   return_value=MixedMock()):
            result = executor.execute(task)

    # Overall success is True (skip continued past step 1 failure).
    assert result.success is True, (
        f"Expected overall success with skip, got: {result.error}"
    )
    import json
    output = json.loads(result.stdout)
    results = output["results"]
    assert len(results) == 3, (
        f"Expected 3 results (all steps run), got {len(results)}"
    )
    assert results[0]["success"] is True, "Step 0 should succeed"
    assert results[0]["step"] == 0
    assert results[1]["success"] is False, "Step 1 should fail"
    assert results[1]["step"] == 1
    assert results[1].get("skipped") is True, "Step 1 should be marked skipped"
    assert results[2]["success"] is True, "Step 2 should succeed"
    assert results[2]["step"] == 2


def test_replay_executor_skips_unknown_actions():
    """ReplayExecutor skips unknown action types gracefully."""
    from src.friday.skill_formation import ReplayExecutor
    from src.friday.runtime.models import RuntimeTask

    task_graph = [["unknown_action", "some_target"]]
    executor = ReplayExecutor(
        worker_id="worker:test:abc",
        task_graph=task_graph,
        exemplars={"0": {"default": "x", "distribution": {"x": 1}, "consensus": 1.0, "stable": True}},
    )
    task = RuntimeTask(
        execution_id="test", session_id="s", schedule_id="s",
        task_id="t", worker_id="w", wave=1,
        runtime_payload="",
    )
    result = executor.execute(task)
    # Should not fail — just skip unknown steps
    assert result.success, "Should not fail on unknown action"
    import json
    results = json.loads(result.stdout)["results"]
    assert len(results) == 1
    assert results[0]["skipped"] is True
    assert "Unknown action type" in results[0]["reason"]


class TestAutoDispatch:
    """Tests for auto_dispatch_skills matching and dispatch logic."""

    def _seed_skill_and_pattern(
        self, conn: sqlite3.Connection,
        task_graph: list,
        pattern_seq: list | None = None,
        skill_status: str = "beta",
    ) -> tuple[int, str]:
        """Helper: seed a formed_skill + worker row, and optionally a
        mined_pattern row. Returns (skill_id, worker_id)."""
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()

        # mined pattern (optional).
        seq = pattern_seq or task_graph
        conn.execute(
            "INSERT INTO mined_patterns (sequence_json, count, mined_at) VALUES (?, ?, ?)",
            (json.dumps(seq), 3, now)
        )
        pat_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        # workflow intent.
        conn.execute(
            "INSERT INTO workflow_intents (pattern_id, intent_label, confidence, "
            "pattern_summary, labeled_at) VALUES (?, ?, ?, ?, ?)",
            (pat_id, "test skill", "high", json.dumps(task_graph), now)
        )
        intent_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        # formed_skill.
        conn.execute(
            "INSERT INTO formed_skills (workflow_intent_id, task_graph, exemplars, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (intent_id, json.dumps(task_graph), json.dumps({
                "0": {"default": "3", "distribution": {"3": 5}, "consensus": 1.0, "stable": True},
                "1": {"default": "firefox", "distribution": {"firefox": 6}, "consensus": 1.0, "stable": True},
            }), now, now)
        )
        skill_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        # worker.
        worker_id = f"worker:test_skill:{skill_id}"
        conn.execute(
            """INSERT INTO workers (id, name, kind, status, manifest_ref,
               worker_kind, created_at, updated_at)
               VALUES (?, ?, 'formed_skill', ?, ?, 'formed_skill', ?, ?)""",
            (worker_id, f"test_skill_{skill_id}", skill_status,
             f"formed_skill:{skill_id}", now, now)
        )
        conn.commit()
        return skill_id, worker_id

    def test_matches_task_graph_to_pattern(self):
        """auto_dispatch_skills finds a formed skill whose task_graph
        matches a mined pattern."""
        from src.friday.skill_formation import auto_dispatch_skills
        from unittest.mock import patch

        conn = _fresh_db()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS mined_patterns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sequence_json TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 0,
                mined_at TEXT NOT NULL, exemplars TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS workflow_intents (
                id INTEGER PRIMARY KEY AUTOINCREMENT, pattern_id INTEGER NOT NULL,
                intent_label TEXT NOT NULL, confidence TEXT NOT NULL DEFAULT 'low',
                pattern_summary TEXT NOT NULL DEFAULT '', labeled_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS formed_skills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workflow_intent_id INTEGER NOT NULL,
                task_graph TEXT NOT NULL, exemplars TEXT NOT NULL DEFAULT '{}',
                invocation_count INTEGER NOT NULL DEFAULT 0, last_invoked_at TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS workers (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active', manifest_ref TEXT,
                worker_kind TEXT NOT NULL DEFAULT 'function',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL, action_type TEXT NOT NULL,
                target TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '{}',
                workspace_id TEXT, project TEXT, session_id TEXT,
                confidence TEXT NOT NULL DEFAULT 'observed',
                observed_at TEXT NOT NULL, recorded_at TEXT NOT NULL
            );
        """)

        task_graph = [["workspace_switch", "<workspace>"], ["app_launch", "<app>"]]
        self._seed_skill_and_pattern(conn, task_graph)

        # Patch resolve_executor so auto_dispatch doesn't actually try
        # to invoke Hyprland.
        from unittest.mock import patch
        from src.friday.runtime.models import ExecutionResult
        class MockReplayExecutor:
            def execute(self, task):
                return ExecutionResult(
                    success=True, stdout="ok", stderr="", exit_code=0,
                    duration_ms=1)

        # auto_dispatch_skills uses from .runtime.executors import
        # resolve_executor internally, so patch the source module.
        with patch("src.friday.runtime.executors.resolve_executor",
                   return_value=MockReplayExecutor()):
            results = auto_dispatch_skills(conn)

        assert len(results) >= 1, (
            f"Expected at least 1 auto-dispatch, got {results}"
        )
        assert results[0]["succeeded"] is True
        assert "test_skill" in results[0].get("worker_name", "")

    def test_no_match_returns_empty(self):
        """auto_dispatch_skills returns [] when no pattern matches."""
        from src.friday.skill_formation import auto_dispatch_skills

        conn = _fresh_db()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS mined_patterns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sequence_json TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 0,
                mined_at TEXT NOT NULL, exemplars TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS workflow_intents (
                id INTEGER PRIMARY KEY AUTOINCREMENT, pattern_id INTEGER NOT NULL,
                intent_label TEXT NOT NULL, confidence TEXT NOT NULL DEFAULT 'low',
                pattern_summary TEXT NOT NULL DEFAULT '', labeled_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS formed_skills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workflow_intent_id INTEGER NOT NULL,
                task_graph TEXT NOT NULL, exemplars TEXT NOT NULL DEFAULT '{}',
                invocation_count INTEGER NOT NULL DEFAULT 0, last_invoked_at TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS workers (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active', manifest_ref TEXT,
                worker_kind TEXT NOT NULL DEFAULT 'function',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
        """)

        # Seed skill with workspace_switch + app_launch.
        self._seed_skill_and_pattern(
            conn,
            task_graph=[["workspace_switch", "<workspace>"], ["app_launch", "<app>"]],
            # pattern_seq has a different action type so it won't match
            pattern_seq=[["navigate", "<url>"], ["type", "<text>"]],
        )

        from unittest.mock import patch
        from src.friday.runtime.models import ExecutionResult
        class MockReplayExecutor:
            def execute(self, task):
                return ExecutionResult(
                    success=True, stdout="ok", stderr="", exit_code=0,
                    duration_ms=1)
        with patch("src.friday.runtime.executors.resolve_executor",
                   return_value=MockReplayExecutor()):
            results = auto_dispatch_skills(conn)

        assert results == [], (
            f"Expected empty results for non-matching pattern, got {results}"
        )

    def test_skips_non_beta_skills(self):
        """auto_dispatch_skills skips skills with status other than beta/proposed."""
        from src.friday.skill_formation import auto_dispatch_skills

        conn = _fresh_db()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS mined_patterns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sequence_json TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 0,
                mined_at TEXT NOT NULL, exemplars TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS workflow_intents (
                id INTEGER PRIMARY KEY AUTOINCREMENT, pattern_id INTEGER NOT NULL,
                intent_label TEXT NOT NULL, confidence TEXT NOT NULL DEFAULT 'low',
                pattern_summary TEXT NOT NULL DEFAULT '', labeled_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS formed_skills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workflow_intent_id INTEGER NOT NULL,
                task_graph TEXT NOT NULL, exemplars TEXT NOT NULL DEFAULT '{}',
                invocation_count INTEGER NOT NULL DEFAULT 0, last_invoked_at TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS workers (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active', manifest_ref TEXT,
                worker_kind TEXT NOT NULL DEFAULT 'function',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
        """)

        # Seed a skill with status='deprecated' (not beta/proposed).
        task_graph = [["workspace_switch", "<workspace>"], ["app_launch", "<app>"]]
        self._seed_skill_and_pattern(conn, task_graph, skill_status="deprecated")

        from unittest.mock import patch
        from src.friday.runtime.models import ExecutionResult
        class MockReplayExecutor:
            def execute(self, task):
                return ExecutionResult(
                    success=True, stdout="ok", stderr="", exit_code=0,
                    duration_ms=1)
        with patch("src.friday.runtime.executors.resolve_executor",
                   return_value=MockReplayExecutor()):
            results = auto_dispatch_skills(conn)

        assert results == [], (
            f"Expected empty for deprecated skill, got {results}"
        )

    def test_rate_limit_respects_last_invoked(self):
        """auto_dispatch_skills skips skills invoked within the interval."""
        from src.friday.skill_formation import auto_dispatch_skills, _AUTO_DISPATCH_INTERVAL
        from datetime import datetime, timedelta, timezone

        conn = _fresh_db()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS mined_patterns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sequence_json TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 0,
                mined_at TEXT NOT NULL, exemplars TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS workflow_intents (
                id INTEGER PRIMARY KEY AUTOINCREMENT, pattern_id INTEGER NOT NULL,
                intent_label TEXT NOT NULL, confidence TEXT NOT NULL DEFAULT 'low',
                pattern_summary TEXT NOT NULL DEFAULT '', labeled_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS formed_skills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workflow_intent_id INTEGER NOT NULL,
                task_graph TEXT NOT NULL, exemplars TEXT NOT NULL DEFAULT '{}',
                invocation_count INTEGER NOT NULL DEFAULT 0, last_invoked_at TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS workers (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active', manifest_ref TEXT,
                worker_kind TEXT NOT NULL DEFAULT 'function',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
        """)

        task_graph = [["workspace_switch", "<workspace>"], ["app_launch", "<app>"]]
        skill_id, worker_id = self._seed_skill_and_pattern(conn, task_graph)

        # Set last_invoked_at to recent (within the rate-limit interval).
        recent = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
        conn.execute(
            "UPDATE formed_skills SET last_invoked_at = ? WHERE id = ?",
            (recent, skill_id)
        )
        conn.commit()

        from unittest.mock import patch
        from src.friday.runtime.models import ExecutionResult
        class MockReplayExecutor:
            def execute(self, task):
                return ExecutionResult(
                    success=True, stdout="ok", stderr="", exit_code=0,
                    duration_ms=1)
        with patch("src.friday.runtime.executors.resolve_executor",
                   return_value=MockReplayExecutor()):
            results = auto_dispatch_skills(conn)

        assert results == [], (
            f"Expected empty (rate-limited), got {results}"
        )

    def test_dispatches_to_action_log(self):
        """auto_dispatch_skills writes a skill_auto_dispatch entry to actions."""
        from src.friday.skill_formation import auto_dispatch_skills

        conn = _fresh_db()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS mined_patterns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sequence_json TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 0,
                mined_at TEXT NOT NULL, exemplars TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS workflow_intents (
                id INTEGER PRIMARY KEY AUTOINCREMENT, pattern_id INTEGER NOT NULL,
                intent_label TEXT NOT NULL, confidence TEXT NOT NULL DEFAULT 'low',
                pattern_summary TEXT NOT NULL DEFAULT '', labeled_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS formed_skills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workflow_intent_id INTEGER NOT NULL,
                task_graph TEXT NOT NULL, exemplars TEXT NOT NULL DEFAULT '{}',
                invocation_count INTEGER NOT NULL DEFAULT 0, last_invoked_at TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS workers (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active', manifest_ref TEXT,
                worker_kind TEXT NOT NULL DEFAULT 'function',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL, action_type TEXT NOT NULL,
                target TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '{}',
                workspace_id TEXT, project TEXT, session_id TEXT,
                confidence TEXT NOT NULL DEFAULT 'observed',
                observed_at TEXT NOT NULL, recorded_at TEXT NOT NULL
            );
        """)

        task_graph = [["workspace_switch", "<workspace>"], ["app_launch", "<app>"]]
        self._seed_skill_and_pattern(conn, task_graph)

        from unittest.mock import patch
        from src.friday.runtime.models import ExecutionResult
        class MockReplayExecutor:
            def execute(self, task):
                return ExecutionResult(
                    success=True, stdout="ok", stderr="", exit_code=0,
                    duration_ms=1)
        with patch("src.friday.runtime.executors.resolve_executor",
                   return_value=MockReplayExecutor()):
            results = auto_dispatch_skills(conn)

        assert len(results) >= 1

        # Verify the action log entry was written.
        log_rows = conn.execute(
            "SELECT * FROM actions WHERE action_type = 'skill_auto_dispatch'"
        ).fetchall()
        assert len(log_rows) >= 1, (
            f"Expected skill_auto_dispatch action log entry"
        )
        target = json.loads(log_rows[0]["target"])
        assert target.get("succeeded") is True
        assert "test_skill" in target.get("worker_name", "")
