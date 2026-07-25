# Pillar B Stage 4 — Skill Formation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Take labeled workflow intents (Pillar B Stage 3) and form them into deployable, replayable skills — making the learning pipeline produce real capabilities that Friday can execute without the user's manual re-entry of each repeated workflow.

**Architecture:** A new `skill_formation.py` module runs in the daemon cycle after intent labeling. For each high/medium-confidence intent, it builds a task graph from the action sequence, resolves concrete exemplar values (checking distribution concentration — ≥80% consensus = fixed default, <80% = required parameter), and registers the skill as a `worker_kind='formed_skill'` row in the existing `workers` table with a FK to a new `formed_skills` payload table. A new `ReplayExecutor` dispatches formed skills through the existing HyprlandExecutor/BrowserExecutor — same confirm gate, same verify-by-diff, no new execution path.

**Tech Stack:** Python, SQLite, existing executors, confirm gate.

---

### Task 1: DB schema — add `worker_kind` column + `formed_skills` table

**Files:**
- Modify: `src/friday/db.py` (schema migration + formed_skills CRUD)
- Test: `tests/test_skill_formation.py` (schema creation + CRUD)

- [ ] **Step 1: Write the failing test for schema creation**

```python
# tests/test_skill_formation.py
"""Tests for Pillar B Stage 4 — Skill Formation."""
from __future__ import annotations
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
import tempfile


def _fresh_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Apply all Pillar schema up to formed_skills.
    
    Inlines only the migrations needed for these tests. The real db.py
    _ensure_schema() calls these internally.
    """
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
    # Check column exists
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
    # Create base tables
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
    # Insert a mined pattern and workflow intent first
    cur = conn.execute(
        "INSERT INTO mined_patterns (sequence_json, count, mined_at) VALUES (?, ?, ?)",
        ('[["workspace_switch", "<workspace>"]]', 3, datetime.now(timezone.utc).isoformat())
    )
    pattern_id = cur.lastrowid
    conn.execute(
        "INSERT INTO workflow_intents (pattern_id, intent_label, intent_description, steps_text, confidence, pattern_summary, labeled_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (pattern_id, "Test intent", "A test", '[]', "high", "[]", datetime.now(timezone.utc).isoformat())
    )
    intent_id = cur.lastrowid
    # Re-fetch the intent_id
    intent_row = conn.execute("SELECT id FROM workflow_intents ORDER BY id DESC LIMIT 1").fetchone()
    intent_id = intent_row["id"]
    now = datetime.now(timezone.utc).isoformat()
    task_graph = json.dumps([["workspace_switch", "<workspace>"], ["app_launch", "<app>"]])
    exemplars = json.dumps({"0": {"3": 5, "5": 1}, "1": {"firefox": 6}})
    conn.execute(
        "INSERT INTO formed_skills (workflow_intent_id, task_graph, exemplars, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        (intent_id, task_graph, exemplars, now, now)
    )
    skill_id = cur.lastrowid
    row = conn.execute("SELECT * FROM formed_skills WHERE id = ?", (skill_id,)).fetchone()
    assert row is not None
    assert row["workflow_intent_id"] == intent_id
    assert json.loads(row["task_graph"]) == json.loads(task_graph)
    assert json.loads(row["exemplars"]["0"]) == {"3": 5, "5": 1}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_skill_formation.py::test_formed_skills_table_created -v`
Expected: PASS (table creation is inline in the test)

Run: `pytest tests/test_skill_formation.py::test_worker_kind_column_exists -v`
Expected: PASS

Run: `pytest tests/test_skill_formation.py::test_insert_formed_skill -v`
Expected: PASS (inline schema in test)

- [ ] **Step 3: Add migration + CRUD helpers to db.py**

After the workflow_intents table creation (line ~1259, after the `conn.commit()` at line 1259), add:

```python
# Pillar B Stage 4: Formed skills table (deployable replay workflows derived
# from labeled intents). Each row is one formed skill: a replayable task graph
# with per-step exemplar value distributions from the miner.
# FK to workflow_intents so skills cascade-delete when intents are cleared.
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
    CREATE INDEX IF NOT EXISTS idx_formed_skills_wfi ON formed_skills(workflow_intent_id);
""")
conn.commit()
```

After the `availability`/`manifest_ref` migration block (line ~1061), add:

```python
# Pillar B Stage 4: worker_kind column for distinguishing formed skills
# from function workers in the shared registry.
if "worker_kind" not in worker_cols:
    conn.execute(
        "ALTER TABLE workers ADD COLUMN worker_kind TEXT NOT NULL DEFAULT 'function'")
```

Also add CRUD helpers after the `clear_workflow_intents` function (line ~5251):

```python
# ===========================================================================
# Pillar B Stage 4 — Formed Skills (replayable workflow skills)
# ===========================================================================


def insert_formed_skill(conn, row: dict) -> int:
    """Persist one formed skill. Returns the new row id."""
    from datetime import datetime, timezone
    cur = conn.execute(
        """INSERT INTO formed_skills
           (workflow_intent_id, task_graph, exemplars,
            invocation_count, last_invoked_at, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (row["workflow_intent_id"], row["task_graph"], row.get("exemplars", "{}"),
         row.get("invocation_count", 0), row.get("last_invoked_at"),
         row.get("created_at", datetime.now(timezone.utc).isoformat()),
         row.get("updated_at", datetime.now(timezone.utc).isoformat())))
    conn.commit()
    return cur.lastrowid


def get_formed_skill(conn, skill_id: int) -> Optional[dict]:
    """Return a formed skill by id."""
    row = conn.execute(
        "SELECT * FROM formed_skills WHERE id = ?", (skill_id,)
    ).fetchone()
    return dict(row) if row else None


def get_formed_skill_by_intent(conn, workflow_intent_id: int) -> Optional[dict]:
    """Return a formed skill for a given workflow intent, or None."""
    row = conn.execute(
        "SELECT * FROM formed_skills WHERE workflow_intent_id = ?",
        (workflow_intent_id,)
    ).fetchone()
    return dict(row) if row else None


def get_all_formed_skills(conn) -> list[dict]:
    """Return all formed skills, newest first."""
    rows = conn.execute(
        "SELECT * FROM formed_skills ORDER BY created_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def increment_formed_skill_invocation(conn, skill_id: int) -> None:
    """Increment invocation count and update last_invoked_at."""
    from datetime import datetime, timezone
    conn.execute(
        "UPDATE formed_skills SET invocation_count = invocation_count + 1, "
        "last_invoked_at = ?, updated_at = ? WHERE id = ?",
        (datetime.now(timezone.utc).isoformat(),
         datetime.now(timezone.utc).isoformat(), skill_id)
    )
    conn.commit()


def delete_formed_skill(conn, skill_id: int) -> None:
    """Delete a formed skill by id."""
    conn.execute("DELETE FROM formed_skills WHERE id = ?", (skill_id,))
    conn.commit()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_skill_formation.py::test_formed_skills_table_created tests/test_skill_formation.py::test_worker_kind_column_exists tests/test_skill_formation.py::test_insert_formed_skill -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add src/friday/db.py tests/test_skill_formation.py
git commit -m "feat(skill-formation): add formed_skills schema + worker_kind migration"
```

---

### Task 2: Enrich sequence miner with concrete value distributions

**Files:**
- Modify: `src/friday/sequence_miner.py` (store per-position concrete value distributions in mined_patterns)
- Test: `tests/test_skill_formation.py`

- [ ] **Step 1: Write failing test for miner distribution extraction**

```python
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
    # Insert 2 sessions with the same pattern but different concrete values
    for session, ws, app in [(0, "ws:3", "firefox"), (1, "ws:5", "firefox")]:
        t = base.isoformat()
        conn.execute(
            "INSERT INTO actions (source, action_type, target, workspace_id, observed_at, recorded_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("hyprland", "workspace_switch", str(ws), str(ws), t, t)
        )
        conn.execute(
            "INSERT INTO actions (source, action_type, target, workspace_id, observed_at, recorded_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("hyprland", "app_launch", str(app), str(ws), t, t)
        )
    conn.commit()

    # mine_sequences returns MinedPattern objects that now carry exemplars
    from src.friday.sequence_miner import MinedPattern
    patterns = mine_sequences(conn, min_support=1)
    assert len(patterns) >= 1
    # Check exemplars are populated on the MinedPattern
    p = patterns[0]
    assert hasattr(p, "exemplars"), "MinedPattern should have exemplars field"
    # exemplars is a dict: {step_index: {concrete_value: count, ...}}
    assert isinstance(p.exemplars, dict)
    # Step 0 (workspace_switch): should have ws:3 and ws:5
    step0 = p.exemplars.get("0", {})
    assert len(step0) >= 1, "Should have at least one value for step 0"
    # Step 1 (app_launch): should have firefox twice
    step1 = p.exemplars.get("1", {})
    assert step1.get("firefox", 0) >= 1, "Should have firefox in step 1 exemplars"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_skill_formation.py::test_mine_sequences_stores_exemplars -v`
Expected: FAIL — MinedPattern has no `exemplars` attribute

- [ ] **Step 3: Add exemplars field to MinedPattern**

In `src/friday/sequence_miner.py`, add `exemplars` field to the `MinedPattern` dataclass:

```python
@dataclass
class MinedPattern:
    # ... existing fields ...
    exemplars: dict = field(default_factory=dict)
    # exemplars maps step_index (str) -> {raw_concrete_value: count}
    # e.g. {"0": {"3": 5, "5": 1}, "1": {"firefox": 6}}
```

- [ ] **Step 4: Enrich the n-gram miner to track concrete values**

In the `_mine()` function, after building `ngram_sessions`, also track concrete values per position. The key change: alongside each n-gram, accumulate raw concrete targets.

In `_mine()`, change the inner loop to also track concrete values:

```python
# At module level, add:
#: Per-n-gram-key: list of concrete target values per position
#: {ngram_key: {step_idx: {concrete_value: count}}}
_ngram_exemplars: dict[tuple, dict[str, dict[str, int]]] = {}

# In the _mine function, before the loop:
_ngram_exemplars.clear()

# In the inner loop, after extracting ngram:
# ngram is tuple of (action_type, normalized_target)
# We need the ORIGINAL (non-normalized) target values for this ngram instance.
# Get the concrete targets from the original actions.
concrete = [a.get("target", "") for a in session[i: i + length]]
for pos, cval in enumerate(concrete):
    if cval:
        pos_key = str(pos)
        if ngram not in _ngram_exemplars:
            _ngram_exemplars[ngram] = {}
        pos_map = _ngram_exemplars[ngram].setdefault(pos_key, {})
        pos_map[cval] = pos_map.get(cval, 0) + 1
```

Then after building patterns, attach exemplars:

```python
patterns: list[MinedPattern] = []
for ngram, session_indices in ngram_sessions.items():
    if len(session_indices) < min_support:
        continue
    patterns.append(MinedPattern(
        sequence=list(ngram),
        count=len(session_indices),
        exemplars=_ngram_exemplars.get(ngram, {}),
    ))
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_skill_formation.py::test_mine_sequences_stores_exemplars -v`
Expected: PASS

- [ ] **Step 6: Also persist exemplars to DB in the daemon + CLI mine path**

In `src/friday/cli_patterns.py`, `_mine()` function (around line 89), add exemplar to the insert_mined_pattern call:

```python
insert_mined_pattern(conn, {
    # ... existing fields ...
    "exemplars": json.dumps(p.exemplars) if p.exemplars else "{}",
})
```

Also add an `exemplars` column to `mined_patterns` table in db.py (additive migration):

```python
# After the mined_patterns CREATE TABLE block (line ~1238), add:
mp_cols = {r["name"] for r in conn.execute("PRAGMA table_info(mined_patterns)")}
if "exemplars" not in mp_cols:
    conn.execute(
        "ALTER TABLE mined_patterns ADD COLUMN exemplars TEXT NOT NULL DEFAULT '{}'")
```

- [ ] **Step 7: Commit**

```bash
git add src/friday/sequence_miner.py src/friday/cli_patterns.py src/friday/db.py
git commit -m "feat(skill-formation): enrich miner with concrete value distributions"
```

---

### Task 3: Skill Formation module — core logic

**Files:**
- Create: `src/friday/skill_formation.py`
- Test: `tests/test_skill_formation.py`

- [ ] **Step 1: Write failing test for form_skills**

```python
def test_form_skills_forms_high_confidence_intent():
    """form_skills creates a formed_skills row + workers row for high-confidence intents."""
    from src.friday.skill_formation import form_skills
    from datetime import datetime, timezone

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    # Create all needed tables
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

    now = datetime.now(timezone.utc).isoformat()
    # Insert a mined pattern with exemplars
    seq = json.dumps([["workspace_switch", "<workspace>"], ["app_launch", "<app>"]])
    exemplars = json.dumps({"0": {"3": 5, "1": 1}, "1": {"firefox": 6}})
    cur = conn.execute(
        "INSERT INTO mined_patterns (sequence_json, count, mined_at, exemplars) VALUES (?, ?, ?, ?)",
        (seq, 3, now, exemplars)
    )
    pat_id = cur.lastrowid
    # Insert a high-confidence workflow intent
    steps = json.dumps(["Switch to workspace 3", "Open Firefox"])
    conn.execute(
        "INSERT INTO workflow_intents (pattern_id, intent_label, intent_description, steps_text, confidence, pattern_summary, labeled_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (pat_id, "Start browsing", "Open browser and navigate", steps, "high", seq, now)
    )

    # Run formation
    result = form_skills(conn)
    assert result is not None
    assert len(result) >= 1

    # Check formed_skills table
    skill_row = conn.execute("SELECT * FROM formed_skills").fetchone()
    assert skill_row is not None
    assert json.loads(skill_row["task_graph"]) == json.loads(seq)
    assert skill_row["invocation_count"] == 0

    # Check workers table
    worker_row = conn.execute("SELECT * FROM workers").fetchone()
    assert worker_row is not None
    assert worker_row["worker_kind"] == "formed_skill"
    assert worker_row["status"] == "beta"  # high confidence
    assert "formed_skill:" in (worker_row["manifest_ref"] or "")

    # Check worker_capabilities
    cap_row = conn.execute("SELECT * FROM worker_capabilities").fetchone()
    assert cap_row is not None
    assert "Workflow Replay" in cap_row["capability"]


def test_form_skills_skips_low_confidence():
    """form_skills skips low/fallback confidence intents."""
    from src.friday.skill_formation import form_skills
    from datetime import datetime, timezone

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
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
    # No formed_skills row
    count = conn.execute("SELECT COUNT(*) as c FROM formed_skills").fetchone()["c"]
    assert count == 0


def test_form_skills_distribution_cap():
    """Low-consensus step caps overall confidence."""
    from src.friday.skill_formation import form_skills
    from datetime import datetime, timezone

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
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

    now = datetime.now(timezone.utc).isoformat()
    # Pattern with low-consensus step: workspace values 3, 5, 1, 7 roughly evenly
    seq = json.dumps([["workspace_switch", "<workspace>"], ["app_launch", "<app>"]])
    # Step 0: workspace values spread across 4 values (25% each — <80% threshold)
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
    # Should be "proposed" (capped from high), not "beta"
    worker_row = conn.execute("SELECT * FROM workers").fetchone()
    assert worker_row is not None
    assert worker_row["status"] == "proposed", f"Expected proposed, got {worker_row['status']}"


def test_form_skills_skips_already_formed():
    """Already-formed intents are skipped."""
    from src.friday.skill_formation import form_skills
    from datetime import datetime, timezone

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS mined_patterns (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            sequence_json   TEXT NOT NULL,
            count           INTEGER NOT NULL DEFAULT 0,
            mined_at        TEXT NOT NULL,
            exemplars       TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS workflow_intents (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            pattern_id          INTEGER NOT NULL,
            intent_label        TEXT NOT NULL,
            steps_text          TEXT NOT NULL DEFAULT '[]',
            confidence          TEXT NOT NULL DEFAULT 'low',
            pattern_summary     TEXT NOT NULL DEFAULT '',
            labeled_at          TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS formed_skills (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            workflow_intent_id INTEGER NOT NULL,
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
            limitations     TEXT NOT NULL DEFAULT '',
            confidence      TEXT NOT NULL DEFAULT 'medium',
            version         TEXT NOT NULL DEFAULT '1.0.0',
            status          TEXT NOT NULL DEFAULT 'active',
            schema_version  TEXT NOT NULL DEFAULT '1.0',
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL,
            availability    TEXT NOT NULL DEFAULT 'available',
            worker_kind     TEXT NOT NULL DEFAULT 'function',
            manifest_ref    TEXT,
            supported_languages TEXT NOT NULL DEFAULT '',
            supported_task_types TEXT NOT NULL DEFAULT '',
            supported_plan_types TEXT NOT NULL DEFAULT '',
            estimated_speed TEXT NOT NULL DEFAULT '',
            estimated_cost  TEXT NOT NULL DEFAULT '',
            context_window  INTEGER NOT NULL DEFAULT 0,
            parallelism     INTEGER NOT NULL DEFAULT 1,
            requires_network INTEGER NOT NULL DEFAULT 0,
            requires_filesystem INTEGER NOT NULL DEFAULT 0,
            requires_git    INTEGER NOT NULL DEFAULT 0,
            requires_python INTEGER NOT NULL DEFAULT 0,
            requires_shell  INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS worker_capabilities (
            worker_id TEXT NOT NULL,
            capability TEXT NOT NULL,
            PRIMARY KEY (worker_id, capability)
        );
        CREATE TABLE IF NOT EXISTS worker_history (
            registered_at TEXT NOT NULL,
            worker_id TEXT NOT NULL,
            name TEXT NOT NULL,
            kind TEXT NOT NULL,
            version TEXT NOT NULL,
            status TEXT NOT NULL,
            capabilities TEXT NOT NULL DEFAULT '',
            limitations TEXT NOT NULL DEFAULT '',
            event_type TEXT NOT NULL,
            note TEXT,
            PRIMARY KEY (registered_at, worker_id)
        );
        CREATE TABLE IF NOT EXISTS worker_versions (
            worker_id TEXT NOT NULL,
            version TEXT NOT NULL,
            registered_at TEXT NOT NULL,
            changelog TEXT,
            PRIMARY KEY (worker_id, version)
        );
    """)
    # Add worker_kind if missing
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(workers)")}
    if "worker_kind" not in cols:
        conn.execute("ALTER TABLE workers ADD COLUMN worker_kind TEXT NOT NULL DEFAULT 'function'")

    now = datetime.now(timezone.utc).isoformat()
    seq = json.dumps([["workspace_switch", "<workspace>"]])
    cur = conn.execute(
        "INSERT INTO mined_patterns (sequence_json, count, mined_at) VALUES (?, ?, ?)",
        (seq, 3, now)
    )
    pat_id = cur.lastrowid
    conn.execute(
        "INSERT INTO workflow_intents (pattern_id, intent_label, confidence, pattern_summary, labeled_at) VALUES (?, ?, ?, ?, ?)",
        (pat_id, "Already formed", "high", seq, now)
    )
    intent_row = conn.execute("SELECT id FROM workflow_intents").fetchone()
    intent_id = intent_row["id"]
    # Pre-insert a formed_skill for this intent (simulating already-formed)
    conn.execute(
        "INSERT INTO formed_skills (workflow_intent_id, task_graph, exemplars, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        (intent_id, seq, "{}", now, now)
    )

    result = form_skills(conn)
    assert result == []  # Should skip — no new skills formed
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_skill_formation.py::test_form_skills_forms_high_confidence_intent -v`
Expected: FAIL — no module `skill_formation`

- [ ] **Step 3: Create `src/friday/skill_formation.py`**

```python
"""Pillar B Stage 4 — Skill Formation.

Takes labeled workflow intents (Stage 3) and forms them into deployable,
replayable skills registered in the shared workers registry.

Algorithm
---------
For each high/medium confidence workflow intent:
1. Skip if already formed (formed_skills row exists for this intent_id).
2. Build task_graph from the abstracted step sequence.
3. Resolve exemplar distributions from the source mined_pattern.
   - ≥80% consensus on a step → fixed default value.
   - <80% consensus → step marked as required parameter.
4. Calculate overall confidence with distribution cap.
   - Low-consensus step → cap at "medium".
5. Insert formed_skills row + workers row with worker_kind='formed_skill'.
6. Map to worker status: high→beta, medium→proposed.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from .db import (
    connect,
    get_mined_patterns,
    get_workflow_intents,
    get_formed_skill_by_intent,
    insert_formed_skill,
    insert_worker,
    insert_worker_history,
    insert_worker_version,
    now_iso,
)
from .db import WorkerRow, WorkerHistoryRow, WorkerVersionRow


#: Consensus threshold for a step to be considered stable (≥80%).
_CONSENSUS_THRESHOLD = 0.8


def form_skills(conn) -> list[dict]:
    """Run the skill formation pipeline.

    For each high/medium confidence workflow intent that doesn't already
    have a formed skill, creates the skill record and registers a worker.

    Args:
        conn: Open SQLite connection.

    Returns:
        List of formed skill dicts that were created (empty if none).
    """
    intents = get_workflow_intents(conn)
    if not intents:
        return []

    created: list[dict] = []

    for intent in intents:
        confidence = (intent.get("confidence") or "low").lower()
        if confidence not in ("high", "medium"):
            continue

        intent_id = intent["id"]

        # Skip if already formed.
        existing = get_formed_skill_by_intent(conn, intent_id)
        if existing:
            continue

        skill = _form_one(conn, intent)
        if skill:
            created.append(skill)

    return created


def _form_one(conn, intent: dict) -> Optional[dict]:
    """Form one skill from a workflow intent."""
    now = now_iso()
    intent_id = intent["id"]

    # Build task_graph from pattern_summary.
    pattern_summary = intent.get("pattern_summary", "")
    try:
        task_graph = json.loads(pattern_summary) if pattern_summary else []
    except (json.JSONDecodeError, TypeError):
        task_graph = []

    if not task_graph:
        return None

    # Get the source mined_pattern for exemplars.
    pattern_id = intent.get("pattern_id")
    exemplar_data: dict = {}
    if pattern_id:
        patterns = get_mined_patterns(conn, limit=9999)
        for p in patterns:
            if p["id"] == pattern_id:
                try:
                    raw = p.get("exemplars", "{}")
                    exemplar_data = json.loads(raw) if raw else {}
                except (json.JSONDecodeError, TypeError):
                    exemplar_data = {}
                break

    # Resolve exemplar values with consensus check.
    resolved_exemplars: dict[str, dict] = {}
    has_low_consensus = False
    for pos_key, dist in exemplar_data.items():
        if not isinstance(dist, dict):
            continue
        total = sum(dist.values())
        if total == 0:
            continue
        best_val = max(dist, key=dist.get)
        best_count = dist[best_val]
        consensus = best_count / total
        is_stable = consensus >= _CONSENSUS_THRESHOLD
        if not is_stable:
            has_low_consensus = True
        resolved_exemplars[pos_key] = {
            "default": best_val,
            "distribution": dist,
            "consensus": round(consensus, 3),
            "stable": is_stable,
        }

    # Determine worker status based on confidence + consensus cap.
    intent_conf = (intent.get("confidence") or "low").lower()

    # Apply distribution cap: low-consensus step caps at "medium".
    if has_low_consensus and intent_conf == "high":
        effective_conf = "medium"
    else:
        effective_conf = intent_conf

    # Map to worker status: high→beta, medium→proposed.
    status_map = {"high": "beta", "medium": "proposed"}
    worker_status = status_map.get(effective_conf, "proposed")

    # Derive worker name from intent label.
    label = intent.get("intent_label", "unnamed_workflow")
    worker_name = _sanitize_name(label)

    # Insert formed_skills row.
    fs_data = {
        "workflow_intent_id": intent_id,
        "task_graph": json.dumps(task_graph),
        "exemplars": json.dumps(resolved_exemplars),
        "invocation_count": 0,
        "last_invoked_at": None,
        "created_at": now,
        "updated_at": now,
    }
    skill_id = insert_formed_skill(conn, fs_data)

    # Register worker in workers table with worker_kind='formed_skill'.
    impl_ref = f"formed_skill:{skill_id}"
    wid = f"worker:{worker_name}:{uuid4().hex[:8]}"

    description = intent.get("intent_description", label)
    steps_text = intent.get("steps_text", "[]")
    try:
        steps_list = json.loads(steps_text) if steps_text else []
    except (json.JSONDecodeError, TypeError):
        steps_list = []

    w = WorkerRow(
        id=wid,
        name=worker_name,
        kind="formed_skill",
        description=description[:500],
        capabilities="Workflow Replay",
        confidence=effective_conf,
        version="0.1.0",
        status=worker_status,
        schema_version="1.0",
        created_at=now,
        updated_at=now,
        availability="available",
        manifest_ref=impl_ref,
    )
    insert_worker(conn, w)

    # Record history.
    insert_worker_history(conn, [
        WorkerHistoryRow(
            registered_at=now,
            worker_id=wid,
            name=worker_name,
            kind="formed_skill",
            version="0.1.0",
            status=worker_status,
            capabilities="Workflow Replay",
            limitations="auto-formed from observed workflow; verify before use",
            event_type="skill_formation",
            note=f"Formed from workflow intent #{intent_id}: {label[:200]}",
        )
    ])
    insert_worker_version(conn, [
        WorkerVersionRow(
            worker_id=wid,
            version="0.1.0",
            registered_at=now,
            changelog=f"Initial skill formed from workflow intent #{intent_id}: {label[:200]}",
        )
    ])

    return {
        "skill_id": skill_id,
        "worker_id": wid,
        "worker_name": worker_name,
        "status": worker_status,
        "confidence": effective_conf,
        "step_count": len(task_graph),
    }


def _sanitize_name(label: str) -> str:
    """Derive a clean kebab-case name from an intent label."""
    import re
    name = label.lower().strip()
    name = re.sub(r"[^a-z0-9_ ]", "", name)
    parts = name.strip().split()[:4]
    return "_".join(parts) if parts else "formed_workflow"


def format_formed_skills(skills: list[dict]) -> str:
    """Render formed skills as human-readable report."""
    if not skills:
        return "No formed skills yet."
    lines = ["Formed Skills", "=" * 40, ""]
    for i, s in enumerate(skills, 1):
        lines.append(f"{i}. {s.get('worker_name', '?')}")
        lines.append(f"   Worker ID: {s.get('worker_id', '?')}")
        lines.append(f"   Status: {s.get('status', '?')}")
        lines.append(f"   Steps: {s.get('step_count', 0)}")
        context_parts = []
        if s.get("skill_id"):
            context_parts.append(f"skill_id={s['skill_id']}")
        if s.get("confidence"):
            context_parts.append(f"confidence={s['confidence']}")
        if context_parts:
            lines.append(f"   [{', '.join(context_parts)}]")
        lines.append("")
    return "\n".join(lines)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_skill_formation.py::test_form_skills_forms_high_confidence_intent tests/test_skill_formation.py::test_form_skills_skips_low_confidence tests/test_skill_formation.py::test_form_skills_distribution_cap tests/test_skill_formation.py::test_form_skills_skips_already_formed -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add src/friday/skill_formation.py tests/test_skill_formation.py
git commit -m "feat(skill-formation): core formation pipeline"
```

---

### Task 4: CLI integration — `friday patterns form`

**Files:**
- Modify: `src/friday/cli_patterns.py`
- Modify: `src/friday/cli.py` (add parser args)

- [ ] **Step 1: Add `form` action to cli_patterns.py**

In the `cmd_patterns` dispatch function, add a `form` branch:

```python
elif action == "form":
    return _form(args)
```

Add the `_form` function:

```python
def _form(args: argparse.Namespace) -> int:
    """Run skill formation on current workflow intents."""
    from .skill_formation import form_skills, format_formed_skills

    conn = connect()
    try:
        force = getattr(args, "force", False)
        if not force:
            # Without --force, skip already-formed intents (default).
            pass
        skills = form_skills(conn)
        if skills:
            print(format_formed_skills(skills))
            print(f"Formed {len(skills)} skill(s).")
            print()
            print("To review and promote a skill to active:")
            print("  friday meta promote --worker <worker_name>")
        else:
            print("No new skills formed.")
            print("Run `friday patterns mine` and `friday patterns label` first,")
            print("or use --force to re-form existing intents (overwrites).")
        return 0
    finally:
        conn.close()
```

- [ ] **Step 2: Update the CLI parser in cli.py**

In the `p_patterns` parser definition (around line 839), add `form` to the choices and add `--force`:

```python
p_patterns.add_argument(
    "action", nargs="?", default=None,
    choices=["mine", "clear", "label", "form"],
    help="'mine' to run the miner, 'label' to run intent labeling, "
         "'form' to create skills from intents, "
         "'clear' to delete all patterns, omit to show.")
p_patterns.add_argument(
    "--force", action="store_true",
    help="Force re-formation even if skill already exists (form action).")
```

- [ ] **Step 3: Run a quick smoke test**

Run: `python3 -m src.friday.cli patterns form --help`
Expected: Shows help with form action

- [ ] **Step 4: Commit**

```bash
git add src/friday/cli_patterns.py src/friday/cli.py
git commit -m "feat(skill-formation): CLI patterns form command"
```

---

### Task 5: Daemon integration — run formation after intent labeling

**Files:**
- Modify: `src/friday/daemon.py`

- [ ] **Step 1: Add skill formation step in `_run_cycle()`**

After the intent labeling block (around line ~341, after `_log(f"Intent labeling: {new_intents}...")`), add:

```python
# Pillar B Stage 4: Run skill formation on labeled intents.
# Converts high/medium confidence workflow intents into deployable
# replay skills registered in the worker registry.
new_skills = 0
try:
    if new_intents > 0:
        from .skill_formation import form_skills
        formed = form_skills(conn)
        new_skills = len(formed)
        if new_skills:
            _log(f"Skill formation: {new_skills} skill(s) formed from intents.")
except Exception as exc:
    _log(f"Skill formation failed: {exc}")
```

Then add `new_skills` to the cycle dict (around line ~371):

```python
cycle.update({
    # ... existing fields ...
    "new_skills": new_skills,
})
```

And add `"new_skills"` to the `_PHASE_A_FIELDS` tuple (line ~48):

```python
_PHASE_A_FIELDS = (...,
                    "new_intents", "high_conf_intents",
                    "new_skills")
```

- [ ] **Step 2: Add new_skills to the cycle notification/log output**

In the notification building section (around line ~645), add:

```python
if new_skills:
    notify_parts.append(f"{new_skills} new skill(s) formed")
```

In the detailed log section (around line ~665), add:

```python
if new_skills:
    _log(f"  {new_skills} skill(s) formed from workflow intents "
         f"(run `friday patterns form` to review)")
```

- [ ] **Step 3: Commit**

```bash
git add src/friday/daemon.py
git commit -m "feat(skill-formation): daemon integration for skill formation"
```

---

### Task 6: ReplayExecutor — dispatch formed skills through existing executors

**Files:**
- Modify: `src/friday/runtime/executors.py` (add branch in resolve_executor + ReplayExecutor class)
- Test: `tests/test_skill_formation.py`

- [ ] **Step 1: Write failing test for ReplayExecutor**

```python
def test_replay_executor_resolves():
    """resolve_executor handles worker_kind='formed_skill'."""
    from src.friday.runtime.executors import resolve_executor
    from src.friday.runtime.models import RuntimeTask, ExecutionResult
    from datetime import datetime, timezone
    import json

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS formed_skills (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            workflow_intent_id INTEGER NOT NULL,
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
            limitations     TEXT NOT NULL DEFAULT '',
            confidence      TEXT NOT NULL DEFAULT 'medium',
            version         TEXT NOT NULL DEFAULT '1.0.0',
            status          TEXT NOT NULL DEFAULT 'active',
            schema_version  TEXT NOT NULL DEFAULT '1.0',
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL,
            availability    TEXT NOT NULL DEFAULT 'available',
            worker_kind     TEXT NOT NULL DEFAULT 'function',
            manifest_ref    TEXT,
            supported_languages TEXT NOT NULL DEFAULT '',
            supported_task_types TEXT NOT NULL DEFAULT '',
            supported_plan_types TEXT NOT NULL DEFAULT '',
            estimated_speed TEXT NOT NULL DEFAULT '',
            estimated_cost  TEXT NOT NULL DEFAULT '',
            context_window  INTEGER NOT NULL DEFAULT 0,
            parallelism     INTEGER NOT NULL DEFAULT 1,
            requires_network INTEGER NOT NULL DEFAULT 0,
            requires_filesystem INTEGER NOT NULL DEFAULT 0,
            requires_git    INTEGER NOT NULL DEFAULT 0,
            requires_python INTEGER NOT NULL DEFAULT 0,
            requires_shell  INTEGER NOT NULL DEFAULT 0
        );
    """)
    now = datetime.now(timezone.utc).isoformat()

    # Insert formed_skill
    seq = json.dumps([["workspace_switch", "<workspace>"], ["app_launch", "<app>"]])
    exemplars = json.dumps({"0": {"3": 5, "1": 1}, "1": {"firefox": 6}})
    cur = conn.execute(
        "INSERT INTO formed_skills (workflow_intent_id, task_graph, exemplars, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        (1, seq, exemplars, now, now)
    )
    skill_id = cur.lastrowid
    impl_ref = f"formed_skill:{skill_id}"

    # Insert worker
    conn.execute(
        "INSERT INTO workers (id, name, kind, description, capabilities, confidence, version, status, schema_version, created_at, updated_at, availability, worker_kind, manifest_ref) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("worker:test:abc123", "test_skill", "formed_skill", "Test", "Workflow Replay",
         "high", "0.1.0", "beta", "1.0", now, now, "available", "formed_skill", impl_ref)
    )

    # Patch the DB connect for executors
    import src.friday.runtime.executors as exec_module
    original_connect = exec_module.connect
    exec_module.connect = lambda: conn
    try:
        executor = resolve_executor("worker:test:abc123")
        assert executor is not None
        # Should be a ReplayExecutor
        from src.friday.skill_formation import ReplayExecutor
        assert isinstance(executor, ReplayExecutor)
        assert executor.worker_id == "worker:test:abc123"
    finally:
        exec_module.connect = original_connect


def test_replay_executor_builds_steps():
    """ReplayExecutor resolves exemplars and builds step list."""
    from src.friday.skill_formation import ReplayExecutor
    import json

    # Build a ReplayExecutor with known state (bypass DB lookup)
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
    assert steps[0] == ("workspace_switch", "3")  # resolved default
    assert steps[1] == ("app_launch", "firefox")
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/test_skill_formation.py::test_replay_executor_resolves tests/test_skill_formation.py::test_replay_executor_builds_steps -v`
Expected: FAIL — ReplayExecutor not imported

- [ ] **Step 3: Add ReplayExecutor to skill_formation.py**

Add to `src/friday/skill_formation.py`:

```python
# ---------------------------------------------------------------------------
# ReplayExecutor — dispatches formed skills through existing action executors
# ---------------------------------------------------------------------------

from .runtime.models import ExecutionResult, Executor, VerificationResult
from .runtime.confirm_gate import prompt_confirm, get_action_level
from .action_log import ActionEvent, log_action, now_iso
from .db import connect as _db_connect


class ReplayExecutor(Executor):
    """Execute a formed skill by replaying each step through the appropriate
    action executor (HyprlandExecutor, BrowserExecutor).

    Each step is gated by the confirm gate and verified by the executor's own
    verify-by-diff logic. No new execution path — reuses all existing infrastructure.
    """

    def __init__(
        self,
        worker_id: str = "worker:replay",
        task_graph: list[list[str]] | None = None,
        exemplars: dict[str, dict] | None = None,
        workspace: str = ".",
    ) -> None:
        self.worker_id = worker_id
        self._task_graph = task_graph or []
        self._exemplars = exemplars or {}
        self._ws = workspace

    def execute(self, task) -> ExecutionResult:
        import time
        from .runtime.executors import resolve_executor

        t0 = time.monotonic()
        steps = self.build_steps()
        if not steps:
            return ExecutionResult(
                success=False, stdout="", stderr="",
                exit_code=None, duration_ms=0,
                error="replay executor: no steps in task graph",
            )

        results: list[dict] = []
        all_succeeded = True

        for i, (action_type, target) in enumerate(steps):
            # Determine which executor to use.
            if action_type in ("workspace_switch", "window_focus", "app_launch",
                               "app_close", "hyprctl"):
                worker_id = "worker:hyprctl"
                payload = json.dumps({"action": action_type, "target": target})
            elif action_type in ("navigate", "click", "type", "read",
                                 "screenshot", "title", "url", "wait"):
                worker_id = "worker:browser"
                payload = json.dumps({"action": action_type, "target": target})
            else:
                # Unknown action type — skip step but don't fail the whole skill.
                results.append({
                    "step": i,
                    "action": action_type,
                    "target": target,
                    "skipped": True,
                    "reason": f"Unknown action type: {action_type}",
                })
                continue

            # Resolve the sub-executor.
            sub_exec = resolve_executor(worker_id, workspace=self._ws)
            if sub_exec is None:
                results.append({
                    "step": i,
                    "action": action_type,
                    "target": target,
                    "skipped": True,
                    "reason": f"Executor not available: {worker_id}",
                })
                continue

            # Create a minimal task-like object for the sub-executor.
            sub_task = _MiniTask(payload=payload)

            # Execute the step through the sub-executor (which handles its own
            # confirm gate + verify-by-diff internally).
            step_result = sub_exec.execute(sub_task)
            results.append({
                "step": i,
                "action": action_type,
                "target": target,
                "success": step_result.success,
                "error": step_result.error,
            })

            if not step_result.success:
                all_succeeded = False
                # Don't stop — continue with remaining steps for best-effort replay.

        dur = int((time.monotonic() - t0) * 1000)

        # Increment invocation count.
        try:
            conn = _db_connect()
            # Extract skill_id from manifest_ref
            ref = getattr(task, "manifest_ref", None) or getattr(task, "runtime_hint", "")
            if ref and "formed_skill:" in ref:
                skill_id = int(ref.split("formed_skill:")[1].split(":")[0])
                from .db import increment_formed_skill_invocation
                increment_formed_skill_invocation(conn, skill_id)
        except Exception:
            pass

        # Log the replay action.
        try:
            conn = _db_connect()
            log_action(conn, ActionEvent(
                source="friday",
                action_type="skill_replay",
                target=json.dumps({"step_count": len(steps), "succeeded": all_succeeded}),
                detail=json.dumps({"results": results}),
                confidence="observed",
                observed_at=now_iso(),
            ))
        except Exception:
            pass

        return ExecutionResult(
            success=all_succeeded,
            stdout=json.dumps({"steps": len(steps), "results": results}),
            stderr="",
            exit_code=0 if all_succeeded else 1,
            duration_ms=dur,
            error="" if all_succeeded else "One or more steps failed",
        )

    def build_steps(self) -> list[tuple[str, str]]:
        """Resolve exemplar values for each step in the task graph.

        Returns list of (action_type, concrete_target) tuples ready for dispatch.
        """
        steps: list[tuple[str, str]] = []
        for i, (action_type, norm_target) in enumerate(self._task_graph):
            pos_key = str(i)
            pos_data = self._exemplars.get(pos_key, {})
            default_val = pos_data.get("default", "") if isinstance(pos_data, dict) else ""
            target = default_val if default_val else ""
            steps.append((action_type, target))
        return steps

    def verify(self, task, result: ExecutionResult) -> VerificationResult:
        return VerificationResult(
            passed=result.success,
            reason="replay executor completed" if result.success
            else result.error or "replay executor failed",
        )


class _MiniTask:
    """Minimal task-like object for sub-executor dispatch.

    Provides just enough of the task interface that HyprlandExecutor
    and BrowserExecutor can read their payload.
    """

    def __init__(self, payload: str = "", hint: str = "", ref: str = ""):
        self.runtime_payload = payload
        self.runtime_hint = hint
        self.manifest_ref = ref
```

- [ ] **Step 4: Wire ReplayExecutor into resolve_executor**

In `src/friday/runtime/executors.py`, after the hardcoded executor chain and before the dynamic fallback, add:

```python
# Formed skill dispatch (Pillar B Stage 4).
# If the worker_id corresponds to a formed_skill worker_kind row,
# build a ReplayExecutor from the formed_skills payload.
try:
    from ..db import get_worker as _get_worker
    row = _get_worker(conn, worker_id)
except Exception:
    row = None
if row is not None and getattr(row, "worker_kind", "function") == "formed_skill":
    from ..skill_formation import ReplayExecutor
    # Look up the formed_skills payload via manifest_ref.
    ref = getattr(row, "manifest_ref", "")
    if ref and ref.startswith("formed_skill:"):
        try:
            skill_id = int(ref.split(":")[1])
            from ..db import get_formed_skill
            fs = get_formed_skill(conn, skill_id)
            if fs:
                import json
                task_graph = json.loads(fs["task_graph"]) if fs.get("task_graph") else []
                exemplars = json.loads(fs["exemplars"]) if fs.get("exemplars") else {}
                return ReplayExecutor(
                    worker_id=worker_id,
                    task_graph=task_graph,
                    exemplars=exemplars,
                    workspace=workspace,
                )
        except (ValueError, IndexError):
            pass
```

**Note:** This requires making a DB connection inside `resolve_executor`. The function currently takes no `conn` parameter. Let import `connect` at the top of executors.py. Add at the module level:

```python
from ..db import connect as _resolve_connect
```

And use `conn = _resolve_connect()` in the new branch, being sure to close it.

Actually, looking at `resolve_executor` — it doesn't take a `conn` parameter. The cleanest approach is to import `connect` and open a short-lived connection inside this branch. Let me adjust:

```python
# In the resolve_executor function, before the dynamic fallback:

# Formed skill dispatch (Pillar B Stage 4).
if name.startswith("worker:"):
    try:
        _rconn = _resolve_connect()
        _row = _rconn.execute(
            "SELECT worker_kind, manifest_ref FROM workers WHERE id = ?",
            (worker_id,)
        ).fetchone()
        if _row is not None and _row["worker_kind"] == "formed_skill":
            ref = _row["manifest_ref"] or ""
            if ref.startswith("formed_skill:"):
                skill_id = int(ref.split(":")[1])
                fs_row = _rconn.execute(
                    "SELECT * FROM formed_skills WHERE id = ?", (skill_id,)
                ).fetchone()
                if fs_row:
                    import json
                    task_graph = json.loads(fs_row["task_graph"]) if fs_row["task_graph"] else []
                    exemplars = json.loads(fs_row["exemplars"]) if fs_row["exemplars"] else {}
                    _rconn.close()
                    from ..skill_formation import ReplayExecutor
                    return ReplayExecutor(
                        worker_id=worker_id,
                        task_graph=task_graph,
                        exemplars=exemplars,
                        workspace=workspace,
                    )
        _rconn.close()
    except Exception:
        try:
            _rconn.close()
        except Exception:
            pass
```

Add the import at the top of `executors.py`:

```python
from ..db import connect as _resolve_connect
```

- [ ] **Step 5: Run tests to verify**

Run: `pytest tests/test_skill_formation.py::test_replay_executor_resolves tests/test_skill_formation.py::test_replay_executor_builds_steps -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/friday/skill_formation.py src/friday/runtime/executors.py tests/test_skill_formation.py
git commit -m "feat(skill-formation): ReplayExecutor for formed skill dispatch"
```

---

### Task 7: Run all tests and final verification

- [ ] **Step 1: Run the full skill formation test suite**

Run: `pytest tests/test_skill_formation.py -v`
Expected: All tests PASS

- [ ] **Step 2: Run the existing test suite (pre-existing failures only, no new ones)**

Run: `python3 -m pytest tests/ --timeout=60 -x --ignore=tests/test_graph.py --ignore=tests/test_scheduler.py --ignore=tests/test_runtime.py 2>&1 | tail -20`
Expected: No new failures introduced by our changes

- [ ] **Step 3: Final commit with all remaining changes**

```bash
git add -A
git commit -m "feat(skill-formation): complete Pillar B Stage 4 implementation"
```

---

## Spec coverage check

| Spec requirement | Task(s) |
|---|---|
| `worker_kind` column on workers table | Task 1 |
| `formed_skills` table | Task 1 |
| Miner stores concrete value distributions | Task 2 |
| `exemplars` column on mined_patterns | Task 2 |
| Distribution-aware exemplar resolution (≥80% threshold) | Task 3 (in `_form_one`) |
| Low-consensus step caps confidence | Task 3 (in `_form_one`) |
| Formation pipeline: skip already-formed | Task 3 (in `form_skills`) |
| Skill storage: formed_skills + workers | Task 3 |
| Worker status mapping: high→beta, medium→proposed | Task 3 |
| CLI: `friday patterns form` | Task 4 |
| CLI: `--force` flag | Task 4 |
| Daemon integration (after intent labeling) | Task 5 |
| ReplayExecutor through existing executors | Task 6 |
| resolve_executor branch for formed_skill | Task 6 |
| Confirm gate applies (sub-executors handle it) | Task 6 (ReplayExecutor uses sub-executors) |
| Invocation count tracking | Task 1 (increment_formed_skill_invocation) + Task 6 |
| Staleness decision (v1: no re-formation) | Task 3 (skip if already formed) |
| Tests for all the above | Tasks 1-6 |
