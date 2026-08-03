"""V4 state database — the Wave 9.0 foundation.

One stdlib ``sqlite3`` module, one schema, ``FRIDAY_V4_DB`` override.
Tables (per ``WAVE_9_AGENCY_CORE.md`` §4.0):

    missions / mission_steps   — persistent goals & progress
    actions                    — audited executions (what, when, result, undo)
    memories                   — long-term facts (provenance, confidence, decay)
    relationships              — depth, tone, preferences per person/peer
    skills                     — learned workflows + confidence + verification
    sessions / exchanges       — conversation history & context

Design laws (V4):
- Pure stdlib (``sqlite3``); no ORM, no external deps.
- Schema versioned via ``PRAGMA user_version`` + an ordered migration list.
- ``connect(read_only=True)`` opens ``mode=ro`` — usable by bridges that
  must never write (mirrors the V3 bridge philosophy inside V4).
- Every public helper is guarded: a bad table/row/query yields ``None``/``[]``
  or a safe default — never a crash.
- Tests are hermetic: always pass an explicit ``path`` (tmp_path); never
  touch the real ``~/.friday``.

Usage:
    conn = connect(path)            # applies pending migrations
    mid = create_mission(conn, "ship auth refactor")
    step_id = add_mission_step(conn, mid, "migrate session handling", payload={...})
    record_action(conn, "git", goal="stage changes", status="pending", ...)
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger("friday_v4.db")

#: Default V4 state DB. Overridable per-process via FRIDAY_V4_DB.
_DEFAULT_DB = Path.home() / ".friday" / "v4.db"


def default_db_path() -> Path:
    """The V4 state DB path (``FRIDAY_V4_DB`` env override wins)."""
    env = os.environ.get("FRIDAY_V4_DB")
    if env:
        return Path(env)
    return _DEFAULT_DB


# ──────────────────────────────────────────────────────────────────────────
# Schema + migrations
# ──────────────────────────────────────────────────────────────────────────

#: Ordered migrations: (version, sql-or-callable). Version N applies
#: after N-1; ``PRAGMA user_version`` tracks the applied version. A
#: callable migration receives the connection and is responsible for its
#: own idempotency (used where ``ALTER TABLE`` cannot use ``IF NOT
#: EXISTS``). Append new versions, never edit shipped ones.
def _migrate_v8_add_permission_mission_columns(conn: sqlite3.Connection) -> None:
    """Add mission provenance to permission requests (idempotent).

    The autonomy loop's durable asks now carry which mission + step they
    came from, so a "yes" can auto-complete the mission step and
    immediately evaluate the next one (mission auto-advance). ``ALTER
    TABLE ADD COLUMN`` has no ``IF NOT EXISTS`` in SQLite, so check
    ``PRAGMA table_info`` and add each column only when missing (same
    pattern as the Wave 10 skill columns).
    """
    cols = {r["name"] for r in conn.execute(
        "PRAGMA table_info(permission_requests)").fetchall()}
    if "mission_id" not in cols:
        conn.execute(
            "ALTER TABLE permission_requests ADD COLUMN mission_id TEXT")
    if "step_id" not in cols:
        conn.execute(
            "ALTER TABLE permission_requests ADD COLUMN step_id TEXT")


def _migrate_v3_add_skill_columns(conn: sqlite3.Connection) -> None:
    """Add the Wave 10 shadow-first skill columns (idempotent).

    ``ALTER TABLE ADD COLUMN`` has no ``IF NOT EXISTS`` in SQLite, so a
    plain-SQL migration would crash when re-applied after a version
    rewind (the recovery path exercised by ``migrate()`` callers). Check
    ``PRAGMA table_info`` and add each column only when missing.
    """
    cols = {r["name"] for r in conn.execute(
        "PRAGMA table_info(skills)").fetchall()}
    if "shadow_matches" not in cols:
        conn.execute(
            "ALTER TABLE skills ADD COLUMN shadow_matches "
            "INTEGER NOT NULL DEFAULT 0")
    if "version" not in cols:
        conn.execute(
            "ALTER TABLE skills ADD COLUMN version "
            "INTEGER NOT NULL DEFAULT 1")


_MIGRATIONS: list[tuple[int, object]] = [
    # (version, sql-str) or (version, callable(conn)); callables are used
    # where ALTER TABLE can't be idempotent (no IF NOT EXISTS in SQLite).
    (
        1,
        """
        CREATE TABLE IF NOT EXISTS missions (
            id          TEXT PRIMARY KEY,
            title       TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            status      TEXT NOT NULL DEFAULT 'planned',
                -- planned | active | paused | completed | cancelled | failed
            priority    TEXT NOT NULL DEFAULT 'medium',
                -- low | medium | high
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS mission_steps (
            id          TEXT PRIMARY KEY,
            mission_id  TEXT NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
            title       TEXT NOT NULL,
            status      TEXT NOT NULL DEFAULT 'pending',
                -- pending | running | completed | failed | skipped
            position    INTEGER NOT NULL DEFAULT 0,
            payload     TEXT NOT NULL DEFAULT '{}',   -- JSON
            result      TEXT NOT NULL DEFAULT '',
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_mission_steps_mission
            ON mission_steps(mission_id);

        CREATE TABLE IF NOT EXISTS actions (
            id               TEXT PRIMARY KEY,
            action_type      TEXT NOT NULL,
                -- shell | git | file | python | testing | desktop | ...
            status           TEXT NOT NULL DEFAULT 'pending',
                -- pending | approved | denied | running | succeeded | failed
            permission_level TEXT NOT NULL DEFAULT 'confirm',
                -- auto | confirm | never
            goal             TEXT NOT NULL DEFAULT '',
            command          TEXT NOT NULL DEFAULT '',
            cwd              TEXT NOT NULL DEFAULT '',
            result_code      INTEGER,
            output           TEXT NOT NULL DEFAULT '',
            undo_payload     TEXT NOT NULL DEFAULT '{}',   -- JSON
            created_at       TEXT NOT NULL,
            updated_at       TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_actions_created ON actions(created_at);

        CREATE TABLE IF NOT EXISTS memories (
            id               TEXT PRIMARY KEY,
            mem_key          TEXT NOT NULL,
            value            TEXT NOT NULL,
            source           TEXT NOT NULL DEFAULT '',
            confidence       REAL NOT NULL DEFAULT 0.5,
            decay_policy     TEXT NOT NULL DEFAULT 'none',
                -- none | time | usage
            created_at       TEXT NOT NULL,
            updated_at       TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_key ON memories(mem_key);

        CREATE TABLE IF NOT EXISTS relationships (
            id                TEXT PRIMARY KEY,
            peer              TEXT NOT NULL,
            depth             REAL NOT NULL DEFAULT 0.0,
            tone              TEXT NOT NULL DEFAULT 'neutral',
            preferences       TEXT NOT NULL DEFAULT '{}',   -- JSON
            interaction_count INTEGER NOT NULL DEFAULT 0,
            created_at        TEXT NOT NULL,
            updated_at        TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_relationships_peer
            ON relationships(peer);

        CREATE TABLE IF NOT EXISTS skills (
            id                 TEXT PRIMARY KEY,
            name               TEXT NOT NULL,
            steps              TEXT NOT NULL DEFAULT '[]',   -- JSON
            confidence         REAL NOT NULL DEFAULT 0.0,
            verification_state TEXT NOT NULL DEFAULT 'shadow',
                -- shadow | verified | promoted | demoted
            failure_count      INTEGER NOT NULL DEFAULT 0,
            last_verified      TEXT,
            created_at         TEXT NOT NULL,
            updated_at         TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_skills_name ON skills(name);

        CREATE TABLE IF NOT EXISTS sessions (
            id         TEXT PRIMARY KEY,
            surface    TEXT NOT NULL DEFAULT 'cli',
                -- voice | cli | web | desktop | collab | shared
                --   ('shared' = the Wave 15 one-presence thread, one
                --    per UTC day, joined by every conversational surface)
            started_at TEXT NOT NULL,
            ended_at   TEXT
        );

        CREATE TABLE IF NOT EXISTS exchanges (
            id         TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            role       TEXT NOT NULL,
                -- user | friday
            content    TEXT NOT NULL,
            intent     TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_exchanges_session
            ON exchanges(session_id);
        """,
    ),
    (
        2,
        """
        CREATE TABLE IF NOT EXISTS working_memory (
            id          TEXT PRIMARY KEY,
            context_key TEXT NOT NULL,
            value       TEXT NOT NULL,
            category    TEXT NOT NULL DEFAULT 'working',
            source      TEXT NOT NULL DEFAULT 'system',
            priority    INTEGER NOT NULL DEFAULT 0,
            ttl_seconds INTEGER NOT NULL DEFAULT 3600,
            created_at  TEXT NOT NULL,
            expires_at  TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_working_memory_key
            ON working_memory(context_key);
        CREATE INDEX IF NOT EXISTS idx_working_memory_expires
            ON working_memory(expires_at);
        """,
    ),
    (
        3,
        # Callable: ALTER TABLE has no IF NOT EXISTS in SQLite; the
        # callable checks PRAGMA table_info so re-application after a
        # version rewind (recovery path) never hits duplicate columns.
        _migrate_v3_add_skill_columns,
    ),
    (
        4,
        """
        CREATE TABLE IF NOT EXISTS ambient_events (
            id         TEXT PRIMARY KEY,
            topic      TEXT NOT NULL,
            payload    TEXT NOT NULL,
            priority   INTEGER NOT NULL DEFAULT 0,
                -- 0 routine | 1 important | 2 critical
            source     TEXT NOT NULL DEFAULT 'system',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_ambient_topic
            ON ambient_events(topic);
        CREATE INDEX IF NOT EXISTS idx_ambient_created
            ON ambient_events(created_at);
        """,
    ),
    (
        5,
        """
        CREATE TABLE IF NOT EXISTS watches (
            id         TEXT PRIMARY KEY,
            name       TEXT NOT NULL DEFAULT '',
                -- operator-provided skill name hint, e.g. "deploy routine"
            status     TEXT NOT NULL DEFAULT 'active',
                -- active | stopped | formed
            context    TEXT NOT NULL DEFAULT '',
                -- repo/cwd at capture time (generalization signal)
            note       TEXT NOT NULL DEFAULT '',
            skill_id   TEXT,
                -- the skill formed from this demonstration (formed only)
            started_at TEXT NOT NULL,
            ended_at   TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_watches_status
            ON watches(status);
        """,
    ),
    (
        6,
        """
        CREATE TABLE IF NOT EXISTS desktop_events (
            id         TEXT PRIMARY KEY,
            event_type TEXT NOT NULL DEFAULT 'app_switch',
                -- app_switch | app_open | app_focus | window_change
            app        TEXT NOT NULL DEFAULT '',
                -- the app class, e.g. brave / code / firefox
            title      TEXT NOT NULL DEFAULT '',
                -- window title when known ("your YouTube channel")
            repo       TEXT NOT NULL DEFAULT '',
                -- repo context when probing succeeded (generalization)
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_desktop_events_created
            ON desktop_events(created_at);
        """,
    ),
    (
        7,
        """
        CREATE TABLE IF NOT EXISTS permission_requests (
            id          TEXT PRIMARY KEY,
            description TEXT NOT NULL DEFAULT '',
                -- operator-facing ask, e.g. "run git status?"
            action_type TEXT NOT NULL DEFAULT '',
            command     TEXT NOT NULL DEFAULT '',
            cwd         TEXT NOT NULL DEFAULT '',
            goal        TEXT NOT NULL DEFAULT '',
            source      TEXT NOT NULL DEFAULT 'autonomy',
                -- autonomy | dispatch | mission | operator | learn | promote
            status      TEXT NOT NULL DEFAULT 'pending',
                -- pending | approved | denied | expired
            created_at  TEXT NOT NULL,
            responded_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_permission_requests_status
            ON permission_requests(status);

        CREATE TABLE IF NOT EXISTS operator_overrides (
            id          TEXT PRIMARY KEY,
            action_type TEXT NOT NULL,
            command     TEXT NOT NULL,
                -- '' = whole action type blocked
            reason      TEXT NOT NULL DEFAULT '',
                -- the operator's words, e.g. "no" / "do it a different way"
            source      TEXT NOT NULL DEFAULT 'talk',
            created_at  TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_operator_overrides_key
            ON operator_overrides(action_type, command);
        """,
    ),
    (8, _migrate_v8_add_permission_mission_columns),
]


# ──────────────────────────────────────────────────────────────────────────
# Connection & migrations
# ──────────────────────────────────────────────────────────────────────────


def now_iso() -> str:
    """UTC ISO-8601 timestamp (microsecond precision) for DB rows.

    Microseconds keep within-process row ordering deterministic when
    records are inserted in quick succession (the watch bridge merges
    audited actions + desktop events chronologically; second-precision
    timestamps would tie and break real order). Lexicographic ordering
    still works because ISO-8601 UTC zero-pads every field.
    """
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _new_id() -> str:
    return uuid.uuid4().hex


def schema_version(conn: sqlite3.Connection) -> int:
    """Applied schema version from ``PRAGMA user_version`` (0 = fresh)."""
    try:
        row = conn.execute("PRAGMA user_version").fetchone()
        return int(row[0] if row else 0)
    except sqlite3.Error:
        return 0


def migrate(conn: sqlite3.Connection) -> int:
    """Apply pending migrations; returns the new schema version.

    Safe to run repeatedly (idempotent — every DDL uses ``IF NOT EXISTS``
    and ``user_version`` is bumped only after the script succeeds, so a
    partially-applied script re-runs cleanly). On ``sqlite3.Error`` the
    error is re-raised and callers may treat the DB as unready.
    """
    current = schema_version(conn)
    for version, migration in _MIGRATIONS:
        if version <= current:
            continue
        try:
            if callable(migration):
                migration(conn)
            else:
                conn.executescript(migration)
            conn.execute(f"PRAGMA user_version = {version}")
        except sqlite3.Error:
            conn.rollback()
            raise
    return schema_version(conn)


def connect(path: Optional[Path] = None,
            read_only: bool = False) -> sqlite3.Connection:
    """Open the V4 state DB, applying pending migrations.

    Args:
        path: DB file. Defaults to ``default_db_path()``. Tests pass a
            ``tmp_path`` location (hermetic).
        read_only: Open with ``mode=ro`` (never writes). The file must
            already exist in read-only mode.

    Returns:
        A ``sqlite3.Connection`` with ``Row`` factory. Raises
        ``sqlite3.Error`` only if the DB cannot be opened at all
        (e.g. missing file in read-only mode); callers degrade.
    """
    db_path = path or default_db_path()
    if read_only:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    migrate(conn)
    return conn


# ──────────────────────────────────────────────────────────────────────────
# Row helpers
# ──────────────────────────────────────────────────────────────────────────


def _row_to_dict(row: Optional[sqlite3.Row]) -> Optional[dict]:
    return dict(row) if row is not None else None


def _rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict]:
    return [dict(r) for r in rows]


def _fetchone(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> Optional[dict]:
    try:
        return _row_to_dict(conn.execute(sql, params).fetchone())
    except sqlite3.Error as exc:
        logger.debug(f"db fetchone failed: {exc}")
        return None


def _fetchall(conn: sqlite3.Connection, sql: str,
              params: tuple = ()) -> list[dict]:
    try:
        return _rows_to_dicts(conn.execute(sql, params).fetchall())
    except sqlite3.Error as exc:
        logger.debug(f"db fetchall failed: {exc}")
        return []


def _execute(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> Optional[int]:
    """Run a write; returns rows affected (0 = no-op). None on error.

    ``rowcount`` (not ``lastrowid``) is returned so UPDATE/DELETE helpers
    can distinguish "no matching row" (0) from success — ``lastrowid`` is
    stale and never None for non-INSERT statements.
    """
    try:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.rowcount
    except sqlite3.Error as exc:
        logger.debug(f"db write failed: {exc}")
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        return None


# ──────────────────────────────────────────────────────────────────────────
# Missions
# ──────────────────────────────────────────────────────────────────────────


def create_mission(conn: sqlite3.Connection, title: str,
                   description: str = "", priority: str = "medium",
                   status: str = "planned",
                   mission_id: Optional[str] = None) -> Optional[str]:
    """Create a mission; returns its id (None on failure)."""
    mid = mission_id or _new_id()
    ts = now_iso()
    ok = _execute(
        conn,
        "INSERT INTO missions (id, title, description, status, priority, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (mid, title, description, status, priority, ts, ts),
    )
    return mid if ok else None


def get_mission(conn: sqlite3.Connection, mission_id: str) -> Optional[dict]:
    return _fetchone(conn, "SELECT * FROM missions WHERE id = ?", (mission_id,))


def list_missions(conn: sqlite3.Connection, status: Optional[str] = None,
                  limit: int = 100) -> list[dict]:
    if status:
        return _fetchall(
            conn,
            "SELECT * FROM missions WHERE status = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (status, limit),
        )
    return _fetchall(conn, "SELECT * FROM missions "
                           "ORDER BY created_at DESC LIMIT ?", (limit,))


def update_mission(conn: sqlite3.Connection, mission_id: str,
                   status: Optional[str] = None,
                   title: Optional[str] = None,
                   description: Optional[str] = None,
                   priority: Optional[str] = None) -> bool:
    """Update mission fields (only the provided ones); returns success."""
    sets: list[str] = []
    params: list[Any] = []
    for field, value in (("status", status), ("title", title),
                         ("description", description), ("priority", priority)):
        if value is not None:
            sets.append(f"{field} = ?")
            params.append(value)
    if not sets:
        return False
    sets.append("updated_at = ?")
    params.append(now_iso())
    params.append(mission_id)
    return bool(_execute(conn, f"UPDATE missions SET {', '.join(sets)} "
                               f"WHERE id = ?", tuple(params)))


def add_mission_step(conn: sqlite3.Connection, mission_id: str, title: str,
                     position: Optional[int] = None,
                     payload: Optional[dict] = None,
                     step_id: Optional[str] = None) -> Optional[str]:
    """Add a step to a mission; returns the step id (None on failure)."""
    sid = step_id or _new_id()
    ts = now_iso()
    if position is None:
        rows = _fetchall(conn, "SELECT COALESCE(MAX(position), -1) AS m "
                               "FROM mission_steps WHERE mission_id = ?",
                         (mission_id,))
        position = int(rows[0]["m"] + 1) if rows else 0
    ok = _execute(
        conn,
        "INSERT INTO mission_steps (id, mission_id, title, status, position, "
        "payload, result, created_at, updated_at) VALUES (?, ?, ?, 'pending', "
        "?, ?, '', ?, ?)",
        (sid, mission_id, title, position,
         json.dumps(payload or {}), ts, ts),
    )
    return sid if ok else None


def list_mission_steps(conn: sqlite3.Connection, mission_id: str) -> list[dict]:
    return _fetchall(conn, "SELECT * FROM mission_steps WHERE mission_id = ? "
                           "ORDER BY position", (mission_id,))


def update_mission_step(conn: sqlite3.Connection, step_id: str,
                        status: Optional[str] = None,
                        result: Optional[str] = None,
                        payload: Optional[dict] = None) -> bool:
    sets: list[str] = []
    params: list[Any] = []
    if status is not None:
        sets.append("status = ?")
        params.append(status)
    if result is not None:
        sets.append("result = ?")
        params.append(result)
    if payload is not None:
        sets.append("payload = ?")
        params.append(json.dumps(payload))
    if not sets:
        return False
    sets.append("updated_at = ?")
    params.append(now_iso())
    params.append(step_id)
    return bool(_execute(conn, f"UPDATE mission_steps SET {', '.join(sets)} "
                               f"WHERE id = ?", tuple(params)))


# ──────────────────────────────────────────────────────────────────────────
# Actions (audit log)
# ──────────────────────────────────────────────────────────────────────────


def recent_ambient_events(conn: sqlite3.Connection,
                          topic: Optional[str] = None,
                          limit: int = 50) -> list[dict]:
    """Recent durable ambient events (newest first), optionally per topic."""
    if topic:
        return _fetchall(
            conn,
            "SELECT id, topic, payload, priority, source, created_at "
            "FROM ambient_events WHERE topic = ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (topic, limit))
    return _fetchall(
        conn,
        "SELECT id, topic, payload, priority, source, created_at "
        "FROM ambient_events ORDER BY created_at DESC, rowid DESC LIMIT ?",
        (limit,))


def ambient_events_since(conn: sqlite3.Connection,
                         since_rowid: int = 0,
                         limit: int = 100) -> list[dict]:
    """Ambient events after ``since_rowid``, oldest first (SSE stream).

    ``rowid`` is the durable queue's insert order (the ``id`` column is a
    uuid, not ordered). A subscriber that tracks the last rowid it saw
    can poll this for exactly the events it missed — the Wave 11 replay
    contract for web tabs / mobile clients.
    """
    return _fetchall(
        conn,
        "SELECT id, rowid, topic, payload, priority, source, created_at "
        "FROM ambient_events WHERE rowid > ? "
        "ORDER BY rowid ASC LIMIT ?",
        (int(since_rowid), limit))



def record_action(conn: sqlite3.Connection, action_type: str,
                  goal: str = "", status: str = "pending",
                  permission_level: str = "confirm", command: str = "",
                  cwd: str = "", undo_payload: Optional[dict] = None,
                  action_id: Optional[str] = None) -> Optional[str]:
    """Record an execution attempt into the audit log; returns its id."""
    aid = action_id or _new_id()
    ts = now_iso()
    ok = _execute(
        conn,
        "INSERT INTO actions (id, action_type, status, permission_level, "
        "goal, command, cwd, undo_payload, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (aid, action_type, status, permission_level, goal, command, cwd,
         json.dumps(undo_payload or {}), ts, ts),
    )
    return aid if ok else None


def finish_action(conn: sqlite3.Connection, action_id: str,
                  status: str, result_code: Optional[int] = None,
                  output: str = "",
                  undo_payload: Optional[dict] = None) -> bool:
    sets = ["status = ?", "updated_at = ?"]
    params: list[Any] = [status, now_iso()]
    if result_code is not None:
        sets.append("result_code = ?")
        params.append(result_code)
    sets.append("output = ?")
    params.append(output)
    if undo_payload is not None:
        sets.append("undo_payload = ?")
        params.append(json.dumps(undo_payload))
    params.append(action_id)
    return bool(_execute(conn, f"UPDATE actions SET {', '.join(sets)} "
                               f"WHERE id = ?", tuple(params)))


def delete_mission(conn: sqlite3.Connection, mission_id: str) -> bool:
    """Delete a mission (cascades to its steps via FK)."""
    return bool(_execute(conn, "DELETE FROM missions WHERE id = ?",
                         (mission_id,)))


def delete_mission_steps(conn: sqlite3.Connection,
                         mission_id: str) -> bool:
    """Delete all steps of a mission (used by mission adaptation)."""
    return bool(_execute(conn, "DELETE FROM mission_steps WHERE mission_id = ?",
                         (mission_id,)))


def recent_actions(conn: sqlite3.Connection, limit: int = 50,
                   action_type: Optional[str] = None) -> list[dict]:
    if action_type:
        return _fetchall(conn, "SELECT * FROM actions WHERE action_type = ? "
                               "ORDER BY created_at DESC LIMIT ?",
                         (action_type, limit))
    return _fetchall(conn, "SELECT * FROM actions "
                           "ORDER BY created_at DESC LIMIT ?", (limit,))


# ──────────────────────────────────────────────────────────────────────────
# Memories (facts with provenance)
# ──────────────────────────────────────────────────────────────────────────


def store_memory(conn: sqlite3.Connection, mem_key: str, value: str,
                 source: str = "", confidence: float = 0.5,
                 decay_policy: str = "none") -> Optional[str]:
    """Upsert a memory fact by key; returns the memory id."""
    ts = now_iso()
    mid = _new_id()
    ok = _execute(
        conn,
        "INSERT INTO memories (id, mem_key, value, source, confidence, "
        "decay_policy, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(mem_key) DO UPDATE SET value = excluded.value, "
        "source = excluded.source, confidence = excluded.confidence, "
        "decay_policy = excluded.decay_policy, "
        "updated_at = excluded.updated_at",
        (mid, mem_key, value, source, confidence, decay_policy, ts, ts),
    )
    if not ok:
        return None
    row = _fetchone(conn, "SELECT id FROM memories WHERE mem_key = ?",
                    (mem_key,))
    return row["id"] if row else mid


def recall_memory(conn: sqlite3.Connection, mem_key: str) -> Optional[dict]:
    """Fetch a memory fact by key.

    Touches ``updated_at`` as a usage-decay signal (``decay_policy='usage'``
    rows are sorted by recency of access in ``list_memories``). On a
    read-only connection the touch is skipped silently — the row is still
    returned.
    """
    row = _fetchone(conn, "SELECT * FROM memories WHERE mem_key = ?", (mem_key,))
    if row:
        _execute(conn, "UPDATE memories SET updated_at = ? WHERE id = ?",
                 (now_iso(), row["id"]))
    return row


def list_memories(conn: sqlite3.Connection, limit: int = 100,
                  mem_key_prefix: Optional[str] = None) -> list[dict]:
    if mem_key_prefix:
        return _fetchall(conn, "SELECT * FROM memories WHERE mem_key LIKE ? "
                               "ORDER BY updated_at DESC LIMIT ?",
                         (f"{mem_key_prefix}%", limit))
    return _fetchall(conn, "SELECT * FROM memories "
                           "ORDER BY updated_at DESC LIMIT ?", (limit,))


def forget_memory(conn: sqlite3.Connection, mem_key: str) -> bool:
    return bool(_execute(conn, "DELETE FROM memories WHERE mem_key = ?",
                         (mem_key,)))


def set_memory_confidence(conn: sqlite3.Connection, mem_key: str,
                          confidence: float) -> bool:
    """Update only a memory fact's confidence (no usage touch).

    Used by the memory layer's decay sweep — fading a fact must not look
    like a "use" (``updated_at`` is deliberately left untouched so
    usage-decay keeps measuring the true idle time).
    """
    return bool(_execute(conn, "UPDATE memories SET confidence = ? "
                               "WHERE mem_key = ?", (confidence, mem_key)))


# ──────────────────────────────────────────────────────────────────────────
# Working memory (ephemeral context with TTL)
# ──────────────────────────────────────────────────────────────────────────


def _add_seconds(iso_ts: str, seconds: int) -> str:
    """ISO timestamp + ``seconds`` (for TTL expiry computation)."""
    try:
        dt = datetime.fromisoformat(iso_ts)
        return (dt + timedelta(seconds=seconds)).isoformat(timespec="seconds")
    except (TypeError, ValueError):
        return iso_ts


def set_working_context(conn: sqlite3.Connection, context_key: str, value: str,
                        category: str = "working", source: str = "system",
                        priority: int = 0, ttl_seconds: int = 3600,
                        now: Optional[str] = None) -> Optional[str]:
    """Upsert a working-memory entry by key; returns its id (None on error).

    ``expires_at`` = ``now + ttl_seconds``. Pass ``now`` for deterministic
    tests; defaults to ``now_iso()``.
    """
    ts = now or now_iso()
    expires = _add_seconds(ts, ttl_seconds)
    wid = _new_id()
    ok = _execute(
        conn,
        "INSERT INTO working_memory (id, context_key, value, category, source, "
        "priority, ttl_seconds, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(context_key) DO UPDATE SET value = excluded.value, "
        "category = excluded.category, source = excluded.source, "
        "priority = excluded.priority, ttl_seconds = excluded.ttl_seconds, "
        "expires_at = excluded.expires_at",
        (wid, context_key, value, category, source, priority, ttl_seconds, ts, expires),
    )
    if not ok:
        return None
    row = _fetchone(conn, "SELECT id FROM working_memory WHERE context_key = ?",
                    (context_key,))
    return row["id"] if row else wid


def get_working_context(conn: sqlite3.Connection,
                        context_key: str) -> Optional[dict]:
    return _fetchone(conn, "SELECT * FROM working_memory WHERE context_key = ?",
                     (context_key,))


def list_working_contexts(conn: sqlite3.Connection,
                          limit: int = 100) -> list[dict]:
    return _fetchall(conn, "SELECT * FROM working_memory "
                           "ORDER BY priority DESC, created_at DESC LIMIT ?",
                      (limit,))


def count_working(conn: sqlite3.Connection) -> int:
    rows = _fetchall(conn, "SELECT COUNT(*) AS c FROM working_memory")
    return int(rows[0]["c"]) if rows else 0


def delete_working_context(conn: sqlite3.Connection,
                           context_key: str) -> bool:
    return bool(_execute(conn, "DELETE FROM working_memory WHERE context_key = ?",
                         (context_key,)))


def clear_expired_working(conn: sqlite3.Connection,
                          now: Optional[str] = None) -> int:
    """Delete expired working-memory entries; returns count removed."""
    ts = now or now_iso()
    return int(_execute(conn, "DELETE FROM working_memory WHERE expires_at < ?",
                        (ts,)) or 0)


def evict_working_contexts(conn: sqlite3.Connection,
                           max_entries: int = 50) -> int:
    """Evict the lowest-priority entries beyond ``max_entries``; count."""
    total = count_working(conn)
    if total <= max_entries:
        return 0
    overage = total - max_entries
    ids = [r["id"] for r in _fetchall(
        conn, "SELECT id FROM working_memory "
              "ORDER BY priority ASC, created_at DESC LIMIT ?", (overage,))]
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    return int(_execute(conn, f"DELETE FROM working_memory "
                              f"WHERE id IN ({placeholders})", tuple(ids)) or 0)


def clear_working(conn: sqlite3.Connection) -> int:
    """Delete ALL working-memory entries; returns count removed."""
    return int(_execute(conn, "DELETE FROM working_memory") or 0)


# ──────────────────────────────────────────────────────────────────────────
# Relationships
# ──────────────────────────────────────────────────────────────────────────


def upsert_relationship(conn: sqlite3.Connection, peer: str,
                        depth: Optional[float] = None,
                        tone: Optional[str] = None,
                        preferences: Optional[dict] = None,
                        interaction_delta: int = 1) -> Optional[str]:
    """Create or update a relationship row for ``peer``; returns its id."""
    ts = now_iso()
    existing = _fetchone(conn, "SELECT id FROM relationships WHERE peer = ?",
                         (peer,))
    if existing:
        sets = ["updated_at = ?",
                "interaction_count = interaction_count + ?"]
        params: list[Any] = [ts, interaction_delta]
        if depth is not None:
            sets.append("depth = ?")
            params.append(depth)
        if tone is not None:
            sets.append("tone = ?")
            params.append(tone)
        if preferences is not None:
            sets.append("preferences = ?")
            params.append(json.dumps(preferences))
        params.append(peer)
        _execute(conn, f"UPDATE relationships SET {', '.join(sets)} "
                       f"WHERE peer = ?", tuple(params))
        return existing["id"]
    rid = _new_id()
    ok = _execute(
        conn,
        "INSERT INTO relationships (id, peer, depth, tone, preferences, "
        "interaction_count, created_at, updated_at) VALUES (?, ?, ?, ?, ?, "
        "?, ?, ?)",
        (rid, peer, depth or 0.0, tone or "neutral",
         json.dumps(preferences or {}), interaction_delta, ts, ts),
    )
    return rid if ok else None


def get_relationship(conn: sqlite3.Connection, peer: str) -> Optional[dict]:
    return _fetchone(conn, "SELECT * FROM relationships WHERE peer = ?", (peer,))


def list_relationships(conn: sqlite3.Connection, limit: int = 100) -> list[dict]:
    return _fetchall(conn, "SELECT * FROM relationships "
                           "ORDER BY depth DESC LIMIT ?", (limit,))


def _relationship_preferences(conn: sqlite3.Connection,
                              peer: str) -> dict:
    """The parsed ``preferences`` JSON for a peer ({} when absent)."""
    row = get_relationship(conn, peer)
    if not row:
        return {}
    try:
        prefs = json.loads(row.get("preferences") or "{}")
        return prefs if isinstance(prefs, dict) else {}
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return {}


def set_tone_direction(conn: sqlite3.Connection, peer: str,
                       tone: Optional[str] = None,
                       verbosity: Optional[int] = None,
                       request: str = "") -> bool:
    """Persist an explicit tone-direction for a peer (Wave 17).

    "Be more casual, Tony" stores the operator's requested tone (and/or
    verbosity) with the exact request words + timestamp, so Friday can
    apply it across every surface AND explain it later ("I'm briefer
    because you asked me to be — on <date>"). Stored in the
    relationship's ``preferences`` JSON under ``tone_direction`` — the
    depth-derived ``tone`` column stays untouched so the relationship
    layer can keep computing what the *default* would be.

    Returns True when a direction is stored (an empty one is a no-op
    that returns False). Never raises.
    """
    if tone is None and verbosity is None:
        return False
    prefs = _relationship_preferences(conn, peer)
    direction = {
        "tone": tone,
        "verbosity": int(verbosity) if verbosity is not None else None,
        "request": (request or "").strip(),
        "set_at": now_iso(),
    }
    prefs["tone_direction"] = direction
    rid = upsert_relationship(conn, peer, preferences=prefs,
                              interaction_delta=0)
    return bool(rid)


def get_tone_direction(conn: sqlite3.Connection,
                       peer: str) -> Optional[dict]:
    """The stored tone-direction dict, or None (never raises)."""
    prefs = _relationship_preferences(conn, peer)
    direction = prefs.get("tone_direction")
    if not isinstance(direction, dict):
        return None
    return {
        "tone": direction.get("tone"),
        "verbosity": direction.get("verbosity"),
        "request": direction.get("request", ""),
        "set_at": direction.get("set_at", ""),
    }


def clear_tone_direction(conn: sqlite3.Connection, peer: str) -> bool:
    """Remove a stored tone-direction ("be yourself again")."""
    prefs = _relationship_preferences(conn, peer)
    if "tone_direction" not in prefs:
        return False
    prefs.pop("tone_direction", None)
    rid = upsert_relationship(conn, peer, preferences=prefs,
                              interaction_delta=0)
    return bool(rid)


# ──────────────────────────────────────────────────────────────────────────
# Skills
# ──────────────────────────────────────────────────────────────────────────


def create_skill(conn: sqlite3.Connection, name: str,
                 steps: Optional[list] = None,
                 confidence: float = 0.0,
                 verification_state: str = "shadow",
                 shadow_matches: int = 0,
                 version: int = 1) -> Optional[str]:
    sid = _new_id()
    ts = now_iso()
    ok = _execute(
        conn,
        "INSERT INTO skills (id, name, steps, confidence, verification_state, "
        "shadow_matches, version, failure_count, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
        (sid, name, json.dumps(steps or []), confidence,
         verification_state, shadow_matches, version, ts, ts),
    )
    return sid if ok else None


def get_skill(conn: sqlite3.Connection, name: str) -> Optional[dict]:
    return _fetchone(conn, "SELECT * FROM skills WHERE name = ?", (name,))


def list_skills(conn: sqlite3.Connection,
                verification_state: Optional[str] = None,
                limit: int = 100) -> list[dict]:
    if verification_state:
        return _fetchall(conn, "SELECT * FROM skills WHERE verification_state = ? "
                               "ORDER BY confidence DESC LIMIT ?",
                         (verification_state, limit))
    return _fetchall(conn, "SELECT * FROM skills "
                           "ORDER BY confidence DESC LIMIT ?", (limit,))


def update_skill(conn: sqlite3.Connection, skill_id: str,
                 steps: Optional[list] = None,
                 confidence: Optional[float] = None,
                 verification_state: Optional[str] = None,
                 failure_count: Optional[int] = None,
                 shadow_matches: Optional[int] = None,
                 version: Optional[int] = None,
                 last_verified: Optional[str] = None) -> bool:
    sets: list[str] = []
    params: list[Any] = []
    if steps is not None:
        sets.append("steps = ?")
        params.append(json.dumps(steps))
    if confidence is not None:
        sets.append("confidence = ?")
        params.append(confidence)
    if verification_state is not None:
        sets.append("verification_state = ?")
        params.append(verification_state)
    if failure_count is not None:
        sets.append("failure_count = ?")
        params.append(failure_count)
    if shadow_matches is not None:
        sets.append("shadow_matches = ?")
        params.append(shadow_matches)
    if version is not None:
        sets.append("version = ?")
        params.append(version)
    if last_verified is not None:
        sets.append("last_verified = ?")
        params.append(last_verified)
    if not sets:
        return False
    sets.append("updated_at = ?")
    params.append(now_iso())
    params.append(skill_id)
    return bool(_execute(conn, f"UPDATE skills SET {', '.join(sets)} "
                                f"WHERE id = ?", tuple(params)))


def record_skill_shadow_match(conn: sqlite3.Connection,
                              skill_id: str) -> bool:
    """Increment a skill's shadow-match counter; returns success.

    Shadow matches are the evidence a skill's steps actually match the
    operator's real workflow — promotion requires N matches + operator
    approval (see ``skills/shadow.py``).
    """
    return bool(_execute(
        conn,
        "UPDATE skills SET shadow_matches = shadow_matches + 1, "
        "updated_at = ? WHERE id = ?", (now_iso(), skill_id)))


# ──────────────────────────────────────────────────────────────────────────
# Watches (Wave 14 — explicit demonstration capture)
# ──────────────────────────────────────────────────────────────────────────


def start_watch(conn: sqlite3.Connection, name: str = "",
                context: str = "", note: str = "",
                watch_id: Optional[str] = None) -> Optional[str]:
    """Open an explicit demonstration capture; returns its id.

    "Watch me" = tag a window on the audit trail. Everything the
    operator executes between ``start_watch`` and ``end_watch`` is the
    demonstration; ``skills/watcher.py`` parameterizes it into a skill.
    Only one watch is active at a time (starting a new one closes the
    previous with ``status='stopped'`` and no skill).
    """
    # Close any dangling active watch so only one capture is live.
    for row in _fetchall(conn, "SELECT id FROM watches "
                               "WHERE status = 'active'"):
        _execute(conn, "UPDATE watches SET status = 'stopped', "
                       "ended_at = ? WHERE id = ?", (now_iso(), row["id"]))
    wid = watch_id or _new_id()
    ok = _execute(
        conn,
        "INSERT INTO watches (id, name, status, context, note, "
        "started_at) VALUES (?, ?, 'active', ?, ?, ?)",
        (wid, name, context, note, now_iso()),
    )
    return wid if ok else None


def end_watch(conn: sqlite3.Connection, watch_id: str,
              skill_id: Optional[str] = None) -> bool:
    """Close a watch; optionally link the skill formed from it."""
    sets = ["status = ?", "ended_at = ?"]
    params: list[Any] = ["formed" if skill_id else "stopped", now_iso()]
    if skill_id:
        sets.append("skill_id = ?")
        params.append(skill_id)
    params.append(watch_id)
    return bool(_execute(conn, f"UPDATE watches SET {', '.join(sets)} "
                               f"WHERE id = ? AND status = 'active'",
                         tuple(params)))


def get_watch(conn: sqlite3.Connection, watch_id: str) -> Optional[dict]:
    return _fetchone(conn, "SELECT * FROM watches WHERE id = ?", (watch_id,))


def active_watch(conn: sqlite3.Connection) -> Optional[dict]:
    """The currently active watch, or None."""
    return _fetchone(conn, "SELECT * FROM watches WHERE status = 'active' "
                           "ORDER BY started_at DESC LIMIT 1")


def list_watches(conn: sqlite3.Connection, status: Optional[str] = None,
                 limit: int = 50) -> list[dict]:
    if status:
        return _fetchall(conn, "SELECT * FROM watches WHERE status = ? "
                               "ORDER BY started_at DESC LIMIT ?",
                         (status, limit))
    return _fetchall(conn, "SELECT * FROM watches "
                           "ORDER BY started_at DESC LIMIT ?", (limit,))


def actions_between(conn: sqlite3.Connection,
                    start_iso: str, end_iso: str) -> list[dict]:
    """Audited actions recorded inside a time window (oldest first).

    The raw material of a demonstration: everything the operator
    executed while a watch was open, in real order.
    """
    return _fetchall(
        conn,
        "SELECT * FROM actions WHERE created_at >= ? AND created_at <= ? "
        "ORDER BY created_at ASC, rowid ASC",
        (start_iso, end_iso),
    )


def record_desktop_event(conn: sqlite3.Connection, event_type: str = "app_switch",
                         app: str = "", title: str = "", repo: str = "",
                         event_id: Optional[str] = None) -> Optional[str]:
    """Record an observed desktop event (app open/focus/switch).

    The observer's raw material for ``WatchRecorder`` (the "watch me"
    bridge — app opens are captured into skills) and for future ambient
    context. Guarded: never raises; returns the event id or None.
    """
    eid = event_id or _new_id()
    ts = now_iso()
    ok = _execute(
        conn,
        "INSERT INTO desktop_events (id, event_type, app, title, repo, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (eid, event_type, app, title, repo, ts),
    )
    return eid if ok else None


def desktop_events_between(conn: sqlite3.Connection,
                           start_iso: str, end_iso: str) -> list[dict]:
    """Desktop events observed inside a time window (oldest first).

    The capture side of the watch bridge: app opens/focuses that
    happened while a watch was open become demonstration material.
    """
    return _fetchall(
        conn,
        "SELECT * FROM desktop_events WHERE created_at >= ? AND created_at <= ? "
        "ORDER BY created_at ASC, rowid ASC",
        (start_iso, end_iso),
    )


def recent_desktop_events(conn: sqlite3.Connection,
                          limit: int = 20) -> list[dict]:
    """Most recent desktop events (newest first) — dispatch context."""
    return _fetchall(
        conn,
        "SELECT * FROM desktop_events "
        "ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,),
    )


def last_activity_at(conn: sqlite3.Connection) -> Optional[str]:
    """The most recent operator-activity timestamp (idle detection).

    The autonomy loop's busy gate: before asking permission for
    CONFIRM-level work, Friday checks whether the operator has been
    active recently (desktop events, audited actions, or spoken/typed
    exchanges). ``None`` means no activity recorded yet (treat as idle
    — there is nothing to interrupt). Guarded: never raises, degrades
    to None on any failure.
    """
    latest: Optional[str] = None
    for sql in (
        "SELECT MAX(created_at) FROM desktop_events",
        "SELECT MAX(created_at) FROM actions",
        "SELECT MAX(created_at) FROM exchanges",
    ):
        try:
            row = conn.execute(sql).fetchone()
            ts = row[0] if row else None
        except sqlite3.Error:
            ts = None
        if ts and (latest is None or ts > latest):
            latest = ts
    return latest


# ──────────────────────────────────────────────────────────────────────────
# Permission requests + operator overrides (autonomy loop)
# ──────────────────────────────────────────────────────────────────────────


def create_permission_request(conn: sqlite3.Connection,
                              description: str, action_type: str,
                              command: str = "", cwd: str = "",
                              goal: str = "", source: str = "autonomy",
                              request_id: Optional[str] = None,
                              mission_id: Optional[str] = None,
                              step_id: Optional[str] = None) -> Optional[str]:
    """Create a durable permission ask; returns its id (None on failure).

    The autonomy loop's CONFIRM path: Friday wants to do something
    state-changing, so it records the ask durably (survives restarts,
    web/talk/voice can resolve it) instead of a transient banner. The
    operator's "yes, run it" resolves it through the real gate.

    ``mission_id``/``step_id`` carry provenance so a "yes" can
    auto-complete the mission step and immediately evaluate the next
    one (mission auto-advance).
    """
    rid = request_id or _new_id()
    ok = _execute(
        conn,
        "INSERT INTO permission_requests (id, description, action_type, "
        "command, cwd, goal, source, status, created_at, mission_id, step_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
        (rid, description, action_type, command, cwd, goal, source, now_iso(),
         mission_id, step_id),
    )
    return rid if ok else None


def get_permission_request(conn: sqlite3.Connection,
                           request_id: str) -> Optional[dict]:
    return _fetchone(conn, "SELECT * FROM permission_requests WHERE id = ?",
                     (request_id,))


def pending_permission_requests(conn: sqlite3.Connection,
                                limit: int = 20) -> list[dict]:
    """Open permission asks, oldest first (FIFO resolution)."""
    return _fetchall(
        conn,
        "SELECT * FROM permission_requests WHERE status = 'pending' "
        "ORDER BY created_at ASC, rowid ASC LIMIT ?", (limit,))


def resolve_permission_request(conn: sqlite3.Connection, request_id: str,
                               status: str) -> bool:
    """Approve / deny / expire a pending request; returns success."""
    return bool(_execute(
        conn,
        "UPDATE permission_requests SET status = ?, responded_at = ? "
        "WHERE id = ? AND status = 'pending'",
        (status, now_iso(), request_id)))


def expire_permission_requests(conn: sqlite3.Connection,
                               older_than_iso: str) -> int:
    """Expire pending requests older than a timestamp (stale asks)."""
    return int(_execute(
        conn,
        "UPDATE permission_requests SET status = 'expired', "
        "responded_at = ? WHERE status = 'pending' AND created_at < ?",
        (now_iso(), older_than_iso)) or 0)


def record_override(conn: sqlite3.Connection, action_type: str,
                    command: str, reason: str = "",
                    source: str = "talk") -> Optional[str]:
    """Record an operator override — a declined/redirected action.

    "No" / "do it a different way" / "don't run that" are remembered so
    the autonomy loop never proposes the same action again (idempotent
    per action_type+command — re-overrides update the reason).
    """
    existing = _fetchone(conn,
                         "SELECT id FROM operator_overrides "
                         "WHERE action_type = ? AND command = ?",
                         (action_type, command))
    if existing:
        _execute(conn, "UPDATE operator_overrides SET reason = ?, "
                       "source = ? WHERE id = ?",
                 (reason, source, existing["id"]))
        return existing["id"]
    oid = _new_id()
    ok = _execute(
        conn,
        "INSERT INTO operator_overrides (id, action_type, command, reason, "
        "source, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (oid, action_type, command, reason, source, now_iso()),
    )
    return oid if ok else None


def is_overridden(conn: sqlite3.Connection, action_type: str,
                  command: str = "") -> bool:
    """Whether an action is blocked by an operator override.

    Matches the exact command, or ANY command of that action type when
    the override command is '' ("never run git" / "never deploy").
    """
    rows = _fetchall(conn,
                     "SELECT command FROM operator_overrides "
                     "WHERE action_type = ?", (action_type,))
    for row in rows:
        if not row["command"] or row["command"] == command:
            return True
    return False


def list_overrides(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    return _fetchall(conn, "SELECT * FROM operator_overrides "
                           "ORDER BY created_at DESC LIMIT ?", (limit,))


def clear_overrides(conn: sqlite3.Connection,
                    action_type: Optional[str] = None) -> int:
    """Remove overrides (all, or per action type) — the operator's
    explicit 'you can do that again' path."""
    if action_type:
        return int(_execute(conn,
                            "DELETE FROM operator_overrides "
                            "WHERE action_type = ?", (action_type,)) or 0)
    return int(_execute(conn, "DELETE FROM operator_overrides") or 0)


# ──────────────────────────────────────────────────────────────────────────
# Sessions & exchanges
# ──────────────────────────────────────────────────────────────────────────


def start_session(conn: sqlite3.Connection,
                  surface: str = "cli") -> Optional[str]:
    sid = _new_id()
    ok = _execute(conn, "INSERT INTO sessions (id, surface, started_at) "
                        "VALUES (?, ?, ?)", (sid, surface, now_iso()))
    return sid if ok else None


def end_session(conn: sqlite3.Connection, session_id: str) -> bool:
    return bool(_execute(conn, "UPDATE sessions SET ended_at = ? WHERE id = ?",
                         (now_iso(), session_id)))


def get_session(conn: sqlite3.Connection, session_id: str) -> Optional[dict]:
    return _fetchone(conn, "SELECT * FROM sessions WHERE id = ?", (session_id,))


def log_exchange(conn: sqlite3.Connection, session_id: str, role: str,
                 content: str, intent: str = "") -> Optional[str]:
    eid = _new_id()
    ok = _execute(
        conn,
        "INSERT INTO exchanges (id, session_id, role, content, intent, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (eid, session_id, role, content, intent, now_iso()),
    )
    return eid if ok else None


def session_exchanges(conn: sqlite3.Connection, session_id: str,
                      limit: int = 200) -> list[dict]:
    return _fetchall(conn, "SELECT * FROM exchanges WHERE session_id = ? "
                           "ORDER BY created_at LIMIT ?", (session_id, limit))


def recent_exchanges(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    """Most recent exchanges across all sessions (newest first).

    Powers the reasoning layer's conversation provider — "what did we
    talk about?" is answered from real conversation history, newest
    turns first.
    """
    return _fetchall(conn, "SELECT * FROM exchanges "
                           "ORDER BY created_at DESC, rowid DESC LIMIT ?",
                      (limit,))


def get_or_create_shared_session(conn: sqlite3.Connection,
                                 now: Optional[str] = None) -> Optional[str]:
    """The ONE conversation thread that follows the operator (Wave 15).

    All conversational surfaces (talk, ask, voice, web chat) append to
    the SAME session — one per UTC day (``surface='shared'``), created
    on first use that day. "A conversation started in the terminal
    continues on the web dashboard and in voice" is literally one row:
    the shared session is the single presence. Pass ``now`` for
    deterministic tests; defaults to ``now_iso()``. Never raises — a
    DB failure degrades to None (callers skip logging).
    """
    ts = now or now_iso()
    # ``substr(started_at, 1, 10)`` compares the ISO date portion
    # lexicographically — no dependence on SQLite's date() parser for
    # microsecond + timezone timestamps.
    row = _fetchone(
        conn,
        "SELECT id FROM sessions WHERE surface = 'shared' "
        "AND substr(started_at, 1, 10) = substr(?, 1, 10) "
        "ORDER BY started_at DESC LIMIT 1",
        (ts,))
    if row:
        return row["id"]
    sid = _new_id()
    _execute(conn, "INSERT INTO sessions (id, surface, started_at) "
                   "VALUES (?, 'shared', ?)", (sid, ts))
    # Re-read so a concurrent process that created today's shared
    # session between our SELECT and INSERT wins the thread (best
    # effort — the one presence stays one row in the common case).
    row = _fetchone(
        conn,
        "SELECT id FROM sessions WHERE surface = 'shared' "
        "AND substr(started_at, 1, 10) = substr(?, 1, 10) "
        "ORDER BY started_at DESC LIMIT 1",
        (ts,))
    return row["id"] if row else sid


def find_shared_session(conn: sqlite3.Connection,
                        now: Optional[str] = None) -> Optional[str]:
    """Today's shared session id WITHOUT creating it (read-only probes).

    The dashboard/companion use this when they must never write — a
    read-only ``mode=ro`` connection cannot INSERT, so
    ``get_or_create_shared_session`` would fail silently and return a
    phantom id. This lookup returns the real thread when one exists,
    else None (the honest "no conversation yet"). Same lexical day
    comparison. Never raises — None on any failure.
    """
    ts = now or now_iso()
    row = _fetchone(
        conn,
        "SELECT id FROM sessions WHERE surface = 'shared' "
        "AND substr(started_at, 1, 10) = substr(?, 1, 10) "
        "ORDER BY started_at DESC LIMIT 1",
        (ts,))
    return row["id"] if row else None


def recent_exchanges_since(conn: sqlite3.Connection,
                           since_iso: str,
                           until_iso: Optional[str] = None,
                           limit: int = 100) -> list[dict]:
    """Exchanges recorded at-or-after ``since_iso`` (newest first).

    Wave 15 time-window recall: "what did we talk about this morning?"
    filters the conversation log to the window instead of the last N
    turns. ``until_iso`` is exclusive when given. Guarded — empty on
    any failure (never raises).
    """
    if until_iso:
        return _fetchall(
            conn,
            "SELECT * FROM exchanges WHERE created_at >= ? "
            "AND created_at < ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (since_iso, until_iso, limit))
    return _fetchall(
        conn,
        "SELECT * FROM exchanges WHERE created_at >= ? "
        "ORDER BY created_at DESC, rowid DESC LIMIT ?",
        (since_iso, limit))


def recent_exchange_history(conn: sqlite3.Connection,
                            limit: int = 8) -> list[dict]:
    """Recent exchanges as LLM follow-up context (oldest first, never raises).

    Wave 13: ``friday4 ask`` and the talk/voice router thread this into
    the reasoning engine's LLM synthesis prompt so follow-ups ("and the
    tests?") resolve with conversation context. Returns a slice of
    ``recent_exchanges`` (newest first) reversed to oldest-first;
    guarded — an unusable connection yields an empty list.
    """
    try:
        rows = recent_exchanges(conn, limit=max(limit, 1)) or []
        history = [{"role": r.get("role", ""),
                     "content": r.get("content", "")} for r in rows]
        return list(reversed(history))[-limit:]
    except Exception:
        return []


def count_exchanges(conn: sqlite3.Connection, role: Optional[str] = None) -> int:
    """Total exchanges, optionally scoped to a role (0 on any failure).

    Feeds the relationship layer's interaction-volume signal — real
    conversation history, never a guess.
    """
    if role:
        rows = _fetchall(conn, "SELECT COUNT(*) AS c FROM exchanges "
                               "WHERE role = ?", (role,))
    else:
        rows = _fetchall(conn, "SELECT COUNT(*) AS c FROM exchanges")
    return int(rows[0]["c"]) if rows else 0


def list_sessions(conn: sqlite3.Connection, limit: int = 1000) -> list[dict]:
    """All sessions, newest first (bounded)."""
    return _fetchall(conn, "SELECT * FROM sessions "
                           "ORDER BY started_at DESC LIMIT ?", (limit,))


# ──────────────────────────────────────────────────────────────────────────
# Inspection (friday4 db status)
# ──────────────────────────────────────────────────────────────────────────


def db_status(path: Optional[Path] = None) -> dict:
    """Summary of the V4 DB (or {} on any failure — never raises).

    Powers ``friday4 db status`` / the unified ``friday4 status`` db row.
    """
    db_path = path or default_db_path()
    info: dict[str, Any] = {
        "path": str(db_path),
        "exists": db_path.exists(),
        "schema_version": 0,
        "tables": {},
        "total_rows": 0,
    }
    if not db_path.exists():
        return info
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            info["schema_version"] = schema_version(conn)
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
            rows = 0
            counts: dict[str, int] = {}
            for (name,) in tables:
                count = conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
                counts[name] = int(count)
                rows += int(count)
            info["tables"] = counts
            info["total_rows"] = rows
        finally:
            conn.close()
    except sqlite3.Error as exc:
        logger.debug(f"db_status failed: {exc}")
        info["exists"] = True  # exists but unreadable
    return info
