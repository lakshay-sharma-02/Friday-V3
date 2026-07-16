"""SQLite storage for Friday's knowledge base.

Schema is deliberately flat: relationships and cross-project observations are
re-derived at summary time from stored rows, so we never persist derived pairs.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional


def db_path() -> Path:
    override = os.environ.get("FRIDAY_DB")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".friday" / "friday.db"


SCHEMA = """
CREATE TABLE IF NOT EXISTS repositories (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    path            TEXT NOT NULL UNIQUE,
    default_branch  TEXT,
    is_dirty        INTEGER NOT NULL DEFAULT 0,
    first_commit_date TEXT,
    last_commit_date TEXT,
    remote_url      TEXT,
    commit_count    INTEGER,
    readme_summary  TEXT,
    license         TEXT,
    primary_author  TEXT,
    ingestion_time  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS languages (
    repo_id     INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    language    TEXT NOT NULL,
    file_count  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (repo_id, language)
);

CREATE TABLE IF NOT EXISTS technologies (
    repo_id   INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    tech      TEXT NOT NULL,
    evidence  TEXT NOT NULL,
    PRIMARY KEY (repo_id, tech)
);

CREATE TABLE IF NOT EXISTS relationships (
    id       INTEGER PRIMARY KEY,
    repo_a   INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    repo_b   INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    kind     TEXT NOT NULL,
    evidence TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    strength TEXT NOT NULL DEFAULT 'Medium'
);

CREATE TABLE IF NOT EXISTS architecture (
    repo_id         INTEGER PRIMARY KEY REFERENCES repositories(id) ON DELETE CASCADE,
    architecture    TEXT NOT NULL,
    evidence        TEXT NOT NULL,
    data_flow       TEXT,
    known_patterns  TEXT,
    complexity      TEXT,
    confidence      TEXT
);

CREATE TABLE IF NOT EXISTS components (
    repo_id   INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    name      TEXT NOT NULL,
    evidence  TEXT NOT NULL,
    strength  TEXT NOT NULL DEFAULT 'Medium',
    PRIMARY KEY (repo_id, name)
);

CREATE TABLE IF NOT EXISTS entry_points (
    repo_id   INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    kind      TEXT NOT NULL,
    detail    TEXT NOT NULL,
    evidence  TEXT NOT NULL,
    PRIMARY KEY (repo_id, kind, detail)
);

CREATE TABLE IF NOT EXISTS snapshots (
    id               INTEGER PRIMARY KEY,
    observed_at      TEXT NOT NULL,
    repo_path        TEXT NOT NULL,
    repo_name        TEXT,
    default_branch   TEXT,
    commit_count     INTEGER,
    last_commit_date TEXT,
    is_dirty         INTEGER NOT NULL DEFAULT 0,
    readme_hash      TEXT,
    architecture_hash TEXT,
    identity_hash    TEXT
);

CREATE TABLE IF NOT EXISTS observations (
    id          TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    source      TEXT NOT NULL,
    subject     TEXT NOT NULL,
    aspect      TEXT NOT NULL,
    value       TEXT NOT NULL,
    confidence  TEXT NOT NULL,
    scope       TEXT NOT NULL DEFAULT '',
    detail      TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    id              TEXT PRIMARY KEY,
    start_time      TEXT NOT NULL,
    end_time        TEXT NOT NULL,
    repositories    TEXT NOT NULL,
    primary_repo    TEXT,
    observations    TEXT NOT NULL,
    activity        TEXT NOT NULL,
    confidence      TEXT NOT NULL,
    duration_min    REAL NOT NULL,
    branch          TEXT,
    summary         TEXT,
    built_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge (
    id                  TEXT PRIMARY KEY,
    type                TEXT NOT NULL,
    subject             TEXT NOT NULL,
    statement           TEXT NOT NULL,
    confidence          TEXT NOT NULL,
    evidence_ids        TEXT NOT NULL,
    status              TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    last_verified       TEXT,
    verification_count  INTEGER NOT NULL DEFAULT 0,
    is_static           INTEGER NOT NULL DEFAULT 0
);

-- M8.2: Knowledge Evolution. Append-only. History is never mutated.
-- One full snapshot of every knowledge entry as it stood after a build.
CREATE TABLE IF NOT EXISTS knowledge_history (
    build_at            TEXT NOT NULL,
    knowledge_id        TEXT NOT NULL,
    type                TEXT NOT NULL,
    subject             TEXT NOT NULL,
    statement           TEXT NOT NULL,
    confidence          TEXT NOT NULL,
    evidence_ids        TEXT NOT NULL,
    status              TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    verification_count  INTEGER NOT NULL DEFAULT 0,
    is_static            INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (build_at, knowledge_id)
);

-- M8.2: deterministic evolution events derived from history diffs.
-- Every record references: knowledge id, previous version, new version,
-- evidence ids, timestamp, reason. Append-only.
CREATE TABLE IF NOT EXISTS evolution_events (
    id                  TEXT PRIMARY KEY,
    build_at            TEXT NOT NULL,
    event_type          TEXT NOT NULL,
    knowledge_id        TEXT NOT NULL,
    previous_confidence TEXT,
    new_confidence      TEXT,
    previous_status     TEXT,
    new_status          TEXT,
    previous_statement  TEXT,
    new_statement       TEXT,
    reason              TEXT NOT NULL,
    evidence_ids        TEXT NOT NULL DEFAULT '',
    related_ids         TEXT NOT NULL DEFAULT '',
    timestamp           TEXT NOT NULL
);

-- M8.3: Understanding Engine. Write-only layer on top of Knowledge. NEVER
-- reads observations/context directly. Every understanding cites knowledge ids.
-- Append-only history + evolution, mirroring knowledge_history/evolution_events.
CREATE TABLE IF NOT EXISTS understanding (
    id                  TEXT PRIMARY KEY,
    type                TEXT NOT NULL,
    subject             TEXT NOT NULL,
    statement           TEXT NOT NULL,
    confidence          TEXT NOT NULL,
    status              TEXT NOT NULL,
    knowledge_ids       TEXT NOT NULL DEFAULT '',
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    build_at            TEXT NOT NULL,
    retired_at          TEXT
);

-- One append-only snapshot of every understanding per build. Never mutated.
CREATE TABLE IF NOT EXISTS understanding_history (
    build_at            TEXT NOT NULL,
    understanding_id    TEXT NOT NULL,
    type                TEXT NOT NULL,
    subject             TEXT NOT NULL,
    statement           TEXT NOT NULL,
    confidence          TEXT NOT NULL,
    status              TEXT NOT NULL,
    knowledge_ids       TEXT NOT NULL DEFAULT '',
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    reinforced_count    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (build_at, understanding_id)
);

-- Deterministic evolution events derived from understanding history diffs.
CREATE TABLE IF NOT EXISTS understanding_evolution (
    id                  TEXT PRIMARY KEY,
    build_at            TEXT NOT NULL,
    event_type          TEXT NOT NULL,
    understanding_id    TEXT NOT NULL,
    previous_confidence TEXT,
    new_confidence      TEXT,
    previous_status     TEXT,
    new_status          TEXT,
    previous_statement  TEXT,
    new_statement       TEXT,
    reason              TEXT NOT NULL,
    knowledge_ids       TEXT NOT NULL DEFAULT '',
    timestamp           TEXT NOT NULL
);

-- M8.4: Initiative Engine. Write-only layer on top of Understanding. NEVER
-- reads observations/context/repositories directly. Every initiative cites
-- understanding ids (and knowledge ids). Append-only history + evolution +
-- relationships (merge/split), mirroring the understanding tables.
CREATE TABLE IF NOT EXISTS initiatives (
    id                          TEXT PRIMARY KEY,
    title                       TEXT NOT NULL,
    initiative_type             TEXT NOT NULL,
    status                      TEXT NOT NULL,
    confidence                  TEXT NOT NULL,
    statement                   TEXT NOT NULL DEFAULT '',
    started_at                  TEXT,
    updated_at                  TEXT NOT NULL,
    completed_at                TEXT,
    participating_repositories   TEXT NOT NULL DEFAULT '',
    understanding_ids           TEXT NOT NULL DEFAULT '',
    knowledge_ids               TEXT NOT NULL DEFAULT '',
    build_at                    TEXT NOT NULL,
    created_at                  TEXT NOT NULL DEFAULT ''
);

-- One append-only snapshot of every initiative per build. Never mutated.
CREATE TABLE IF NOT EXISTS initiative_history (
    build_at               TEXT NOT NULL,
    initiative_id          TEXT NOT NULL,
    title                  TEXT NOT NULL,
    initiative_type        TEXT NOT NULL,
    status                 TEXT NOT NULL,
    confidence             TEXT NOT NULL,
    started_at             TEXT,
    completed_at           TEXT,
    participating_repositories TEXT NOT NULL DEFAULT '',
    understanding_ids      TEXT NOT NULL DEFAULT '',
    knowledge_ids          TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (build_at, initiative_id)
);

-- Deterministic lifecycle / merge / split events derived from history diffs.
CREATE TABLE IF NOT EXISTS initiative_evolution (
    id                  TEXT PRIMARY KEY,
    build_at            TEXT NOT NULL,
    event_type          TEXT NOT NULL,
    initiative_id       TEXT NOT NULL,
    parent_ids          TEXT NOT NULL DEFAULT '',
    child_ids           TEXT NOT NULL DEFAULT '',
    previous_status     TEXT,
    new_status          TEXT,
    previous_confidence TEXT,
    new_confidence      TEXT,
    previous_title      TEXT,
    new_title           TEXT,
    reason              TEXT NOT NULL,
    understanding_ids   TEXT NOT NULL DEFAULT '',
    knowledge_ids       TEXT NOT NULL DEFAULT '',
    timestamp           TEXT NOT NULL
);

-- Explicit merge/split edges. Parent/child references preserved forever.
CREATE TABLE IF NOT EXISTS initiative_relationships (
    id                  TEXT PRIMARY KEY,
    relationship_type    TEXT NOT NULL,   -- 'merge' or 'split'
    parent_ids          TEXT NOT NULL DEFAULT '',
    child_ids           TEXT NOT NULL DEFAULT '',
    build_at            TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    note                TEXT
);

-- M8.5: Insight Engine. Write-only layer on top of Understanding/Initiatives/
-- Knowledge. NEVER reads observations/context/repositories directly. Every
-- insight cites understanding ids (and/or initiative ids and/or knowledge ids).
-- Append-only history + evolution, mirroring the understanding/insight tables.
-- Insights are EPHEMERAL: a build retires insights whose triggering conditions
-- no longer hold, so the layer stays a live "what deserves attention" feed.
CREATE TABLE IF NOT EXISTS insights (
    id                      TEXT PRIMARY KEY,
    title                   TEXT NOT NULL,
    insight_type            TEXT NOT NULL,
    statement               TEXT NOT NULL,
    status                  TEXT NOT NULL,
    confidence              TEXT NOT NULL,
    started_at              TEXT,
    updated_at              TEXT NOT NULL,
    retired_at              TEXT,
    understanding_ids       TEXT NOT NULL DEFAULT '',
    initiative_ids          TEXT NOT NULL DEFAULT '',
    knowledge_ids           TEXT NOT NULL DEFAULT '',
    build_at                TEXT NOT NULL,
    created_at              TEXT NOT NULL DEFAULT ''
);

-- One append-only snapshot of every insight per build. Never mutated.
CREATE TABLE IF NOT EXISTS insight_history (
    build_at                TEXT NOT NULL,
    insight_id              TEXT NOT NULL,
    title                   TEXT NOT NULL,
    insight_type            TEXT NOT NULL,
    statement               TEXT NOT NULL,
    status                  TEXT NOT NULL,
    confidence              TEXT NOT NULL,
    understanding_ids       TEXT NOT NULL DEFAULT '',
    initiative_ids          TEXT NOT NULL DEFAULT '',
    knowledge_ids           TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (build_at, insight_id)
);

-- Deterministic lifecycle (Candidate->Observed->Verified->Stable->Retired) and
-- retirement events derived from build diffs. Append-only.
CREATE TABLE IF NOT EXISTS insight_evolution (
    id                      TEXT PRIMARY KEY,
    build_at                TEXT NOT NULL,
    event_type              TEXT NOT NULL,
    insight_id              TEXT NOT NULL,
    previous_status         TEXT,
    new_status              TEXT,
    previous_confidence     TEXT,
    new_confidence          TEXT,
    previous_statement      TEXT,
    new_statement           TEXT,
    reason                  TEXT NOT NULL,
    understanding_ids       TEXT NOT NULL DEFAULT '',
    initiative_ids          TEXT NOT NULL DEFAULT '',
    knowledge_ids           TEXT NOT NULL DEFAULT '',
    timestamp               TEXT NOT NULL
);
"""


@dataclass
class Repository:
    id: Optional[int]
    name: str
    path: str
    default_branch: Optional[str]
    is_dirty: bool
    first_commit_date: Optional[str]
    last_commit_date: Optional[str]
    remote_url: Optional[str]
    commit_count: Optional[int]
    readme_summary: Optional[str]
    license: Optional[str]
    primary_author: Optional[str]
    ingestion_time: str
    maturity: Optional[str] = None
    readme_quality: Optional[str] = None
    readme_completeness: Optional[str] = None


@dataclass
class LangRow:
    language: str
    file_count: int


@dataclass
class TechRow:
    tech: str
    evidence: str


@dataclass
class RelationshipRow:
    repo_a: int
    repo_b: int
    kind: str
    evidence: str
    priority: int = 0
    strength: str = "Medium"


@dataclass
class ArchitectureRow:
    repo_id: int
    architecture: str
    evidence: str
    data_flow: Optional[str]
    known_patterns: Optional[str]
    complexity: Optional[str]
    confidence: Optional[str] = None


@dataclass
class ComponentRow:
    repo_id: int
    name: str
    evidence: str
    strength: str = "Medium"


@dataclass
class EntryPointRow:
    repo_id: int
    kind: str
    detail: str
    evidence: str


def connect(path: Optional[Path] = None) -> sqlite3.Connection:
    if path is None:
        path = db_path()
    # Handle in-memory database
    if isinstance(path, str) and path == ":memory:":
        conn = sqlite3.connect(":memory:")
    else:
        if isinstance(path, str):
            path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Apply additive schema changes idempotently (M2/M4 columns)."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(repositories)")}
    for col, ctype in (
        ("maturity", "TEXT"),
        ("readme_quality", "TEXT"),
        ("readme_completeness", "TEXT"),
    ):
        if col not in cols:
            conn.execute(f"ALTER TABLE repositories ADD COLUMN {col} {ctype}")
    # M4: evidence-strength model.
    for table, col in (
        ("relationships", "strength"),
        ("components", "strength"),
        ("architecture", "confidence"),
    ):
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if col not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT NOT NULL DEFAULT 'Medium'")
    # M8.1.5: static vs temporal knowledge marker.
    know_cols = {r["name"] for r in conn.execute("PRAGMA table_info(knowledge)")}
    if "is_static" not in know_cols:
        conn.execute("ALTER TABLE knowledge ADD COLUMN is_static INTEGER NOT NULL DEFAULT 0")
    conn.commit()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def upsert_repository(
    conn: sqlite3.Connection,
    *,
    name: str,
    path: str,
    default_branch: Optional[str],
    is_dirty: bool,
    first_commit_date: Optional[str],
    last_commit_date: Optional[str],
    remote_url: Optional[str],
    commit_count: Optional[int],
    readme_summary: Optional[str],
    license: Optional[str],
    primary_author: Optional[str],
) -> int:
    """Insert or update a repository by path; returns its row id."""
    cur = conn.execute(
        """
        INSERT INTO repositories
            (name, path, default_branch, is_dirty, first_commit_date, last_commit_date, remote_url,
             commit_count, readme_summary, license, primary_author, ingestion_time)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(path) DO UPDATE SET
            name=excluded.name,
            default_branch=excluded.default_branch,
            is_dirty=excluded.is_dirty,
            first_commit_date=excluded.first_commit_date,
            last_commit_date=excluded.last_commit_date,
            remote_url=excluded.remote_url,
            commit_count=excluded.commit_count,
            readme_summary=excluded.readme_summary,
            license=excluded.license,
            primary_author=excluded.primary_author,
            ingestion_time=excluded.ingestion_time
        """,
        (
            name,
            path,
            default_branch,
            int(is_dirty),
            first_commit_date,
            last_commit_date,
            remote_url,
            commit_count,
            readme_summary,
            license,
            primary_author,
            now_iso(),
        ),
    )
    conn.commit()
    row = conn.execute("SELECT id FROM repositories WHERE path = ?", (path,)).fetchone()
    return row["id"]


def replace_children(
    conn: sqlite3.Connection,
    repo_id: int,
    languages: list[LangRow],
    technologies: list[TechRow],
) -> None:
    conn.execute("DELETE FROM languages WHERE repo_id = ?", (repo_id,))
    conn.execute("DELETE FROM technologies WHERE repo_id = ?", (repo_id,))
    conn.executemany(
        "INSERT OR REPLACE INTO languages (repo_id, language, file_count) VALUES (?, ?, ?)",
        [(repo_id, l.language, l.file_count) for l in languages],
    )
    conn.executemany(
        "INSERT OR REPLACE INTO technologies (repo_id, tech, evidence) VALUES (?, ?, ?)",
        [(repo_id, t.tech, t.evidence) for t in technologies],
    )
    conn.commit()


def get_repositories(conn: sqlite3.Connection) -> list[Repository]:
    rows = conn.execute("SELECT * FROM repositories ORDER BY name").fetchall()
    return [
        Repository(
            id=r["id"],
            name=r["name"],
            path=r["path"],
            default_branch=r["default_branch"],
            is_dirty=bool(r["is_dirty"]),
            first_commit_date=r["first_commit_date"],
            last_commit_date=r["last_commit_date"],
            remote_url=r["remote_url"],
            commit_count=r["commit_count"],
            readme_summary=r["readme_summary"],
            license=r["license"],
            primary_author=r["primary_author"],
            ingestion_time=r["ingestion_time"],
            maturity=r["maturity"],
            readme_quality=r["readme_quality"],
            readme_completeness=r["readme_completeness"],
        )
        for r in rows
    ]


def get_languages(conn: sqlite3.Connection, repo_id: int) -> list[LangRow]:
    rows = conn.execute(
        "SELECT language, file_count FROM languages WHERE repo_id = ?", (repo_id,)
    ).fetchall()
    return [LangRow(language=r["language"], file_count=r["file_count"]) for r in rows]


def get_technologies(conn: sqlite3.Connection, repo_id: int) -> list[TechRow]:
    rows = conn.execute(
        "SELECT tech, evidence FROM technologies WHERE repo_id = ?", (repo_id,)
    ).fetchall()
    return [TechRow(tech=r["tech"], evidence=r["evidence"]) for r in rows]


def set_repo_quality(
    conn: sqlite3.Connection,
    repo_id: int,
    maturity: Optional[str],
    readme_quality: Optional[str],
    readme_completeness: Optional[str],
) -> None:
    conn.execute(
        """
        UPDATE repositories
        SET maturity = ?, readme_quality = ?, readme_completeness = ?
        WHERE id = ?
        """,
        (maturity, readme_quality, readme_completeness, repo_id),
    )
    conn.commit()


def replace_relationships(
    conn: sqlite3.Connection, repo_id: int, rels: list[RelationshipRow]
) -> None:
    """Replace all stored relationships touching `repo_id`."""
    conn.execute(
        "DELETE FROM relationships WHERE repo_a = ? OR repo_b = ?", (repo_id, repo_id)
    )
    conn.executemany(
        """INSERT INTO relationships (repo_a, repo_b, kind, evidence, priority, strength)
           VALUES (?, ?, ?, ?, ?, ?)""",
        [(r.repo_a, r.repo_b, r.kind, r.evidence, r.priority, r.strength) for r in rels],
    )
    conn.commit()


def replace_all_relationships(conn: sqlite3.Connection, rels: list[RelationshipRow]) -> None:
    """Wipe and rewrite the entire relationships table (used at ingest)."""
    conn.execute("DELETE FROM relationships")
    conn.executemany(
        """INSERT INTO relationships (repo_a, repo_b, kind, evidence, priority, strength)
           VALUES (?, ?, ?, ?, ?, ?)""",
        [(r.repo_a, r.repo_b, r.kind, r.evidence, r.priority, r.strength) for r in rels],
    )
    conn.commit()


def get_relationships(conn: sqlite3.Connection, repo_id: int) -> list[RelationshipRow]:
    rows = conn.execute(
        """SELECT repo_a, repo_b, kind, evidence, priority, strength
           FROM relationships WHERE repo_a = ? OR repo_b = ? ORDER BY priority DESC, kind""",
        (repo_id, repo_id),
    ).fetchall()
    return [
        RelationshipRow(
            repo_a=r["repo_a"],
            repo_b=r["repo_b"],
            kind=r["kind"],
            evidence=r["evidence"],
            priority=r["priority"],
            strength=r["strength"],
        )
        for r in rows
    ]


def get_all_relationships(conn: sqlite3.Connection) -> list[RelationshipRow]:
    rows = conn.execute(
        "SELECT repo_a, repo_b, kind, evidence, priority, strength FROM relationships ORDER BY priority DESC, kind"
    ).fetchall()
    return [
        RelationshipRow(
            repo_a=r["repo_a"],
            repo_b=r["repo_b"],
            kind=r["kind"],
            evidence=r["evidence"],
            priority=r["priority"],
            strength=r["strength"],
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Architecture (Milestone 3)
# ---------------------------------------------------------------------------


def upsert_architecture(
    conn: sqlite3.Connection,
    *,
    repo_id: int,
    architecture: str,
    evidence: str,
    data_flow: Optional[str] = None,
    known_patterns: Optional[str] = None,
    complexity: Optional[str] = None,
    confidence: Optional[str] = None,
) -> None:
    conn.execute(
        """
        INSERT INTO architecture
            (repo_id, architecture, evidence, data_flow, known_patterns, complexity, confidence)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(repo_id) DO UPDATE SET
            architecture=excluded.architecture,
            evidence=excluded.evidence,
            data_flow=excluded.data_flow,
            known_patterns=excluded.known_patterns,
            complexity=excluded.complexity,
            confidence=excluded.confidence
        """,
        (repo_id, architecture, evidence, data_flow, known_patterns, complexity, confidence),
    )
    conn.commit()


def get_architecture(conn: sqlite3.Connection, repo_id: int) -> Optional[ArchitectureRow]:
    row = conn.execute(
        "SELECT * FROM architecture WHERE repo_id = ?", (repo_id,)
    ).fetchone()
    if row is None:
        return None
    return ArchitectureRow(
        repo_id=row["repo_id"],
        architecture=row["architecture"],
        evidence=row["evidence"],
        data_flow=row["data_flow"],
        known_patterns=row["known_patterns"],
        complexity=row["complexity"],
        confidence=row["confidence"],
    )


def replace_components(
    conn: sqlite3.Connection, repo_id: int, components: list[ComponentRow]
) -> None:
    conn.execute("DELETE FROM components WHERE repo_id = ?", (repo_id,))
    conn.executemany(
        "INSERT OR REPLACE INTO components (repo_id, name, evidence, strength) VALUES (?, ?, ?, ?)",
        [(repo_id, c.name, c.evidence, c.strength) for c in components],
    )
    conn.commit()


def get_components(conn: sqlite3.Connection, repo_id: int) -> list[ComponentRow]:
    rows = conn.execute(
        "SELECT repo_id, name, evidence, strength FROM components WHERE repo_id = ? ORDER BY name",
        (repo_id,),
    ).fetchall()
    return [
        ComponentRow(
            repo_id=r["repo_id"], name=r["name"], evidence=r["evidence"], strength=r["strength"]
        )
        for r in rows
    ]


def replace_entry_points(
    conn: sqlite3.Connection, repo_id: int, entries: list[EntryPointRow]
) -> None:
    conn.execute("DELETE FROM entry_points WHERE repo_id = ?", (repo_id,))
    conn.executemany(
        "INSERT OR REPLACE INTO entry_points (repo_id, kind, detail, evidence) VALUES (?, ?, ?, ?)",
        [(repo_id, e.kind, e.detail, e.evidence) for e in entries],
    )
    conn.commit()


def get_entry_points(conn: sqlite3.Connection, repo_id: int) -> list[EntryPointRow]:
    rows = conn.execute(
        "SELECT repo_id, kind, detail, evidence FROM entry_points WHERE repo_id = ? "
        "ORDER BY kind, detail",
        (repo_id,),
    ).fetchall()
    return [
        EntryPointRow(
            repo_id=r["repo_id"], kind=r["kind"], detail=r["detail"], evidence=r["evidence"]
        )
        for r in rows
    ]


def all_entry_points(conn: sqlite3.Connection) -> list[EntryPointRow]:
    """Every entry point across all repositories (for cross-repo similarity)."""
    rows = conn.execute(
        "SELECT repo_id, kind, detail, evidence FROM entry_points ORDER BY repo_id, kind"
    ).fetchall()
    return [
        EntryPointRow(
            repo_id=r["repo_id"], kind=r["kind"], detail=r["detail"], evidence=r["evidence"]
        )
        for r in rows
    ]


def all_components(conn: sqlite3.Connection) -> list[ComponentRow]:
    """Every component across all repositories (for cross-repo similarity)."""
    rows = conn.execute(
        "SELECT repo_id, name, evidence, strength FROM components ORDER BY repo_id, name"
    ).fetchall()
    return [
        ComponentRow(
            repo_id=r["repo_id"], name=r["name"], evidence=r["evidence"],
            strength=r["strength"],
        )
        for r in rows
    ]


def entry_points_by_kind(conn: sqlite3.Connection, kind: str) -> list[EntryPointRow]:
    rows = conn.execute(
        "SELECT repo_id, kind, detail, evidence FROM entry_points WHERE kind = ? "
        "ORDER BY repo_id",
        (kind,),
    ).fetchall()
    return [
        EntryPointRow(
            repo_id=r["repo_id"], kind=r["kind"], detail=r["detail"], evidence=r["evidence"]
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Observation snapshots (Milestone 5) — append-only, facts only.
# ---------------------------------------------------------------------------


@dataclass
class SnapshotRow:
    observed_at: str
    repo_path: str
    repo_name: Optional[str]
    default_branch: Optional[str]
    commit_count: Optional[int]
    last_commit_date: Optional[str]
    is_dirty: bool
    readme_hash: Optional[str]
    architecture_hash: Optional[str]
    identity_hash: Optional[str]


def insert_snapshot(conn: sqlite3.Connection, snap: SnapshotRow) -> None:
    """Append one observation row. Snapshots are never updated or deleted."""
    conn.execute(
        """
        INSERT INTO snapshots
            (observed_at, repo_path, repo_name, default_branch, commit_count,
             last_commit_date, is_dirty, readme_hash, architecture_hash, identity_hash)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            snap.observed_at,
            snap.repo_path,
            snap.repo_name,
            snap.default_branch,
            snap.commit_count,
            snap.last_commit_date,
            int(snap.is_dirty),
            snap.readme_hash,
            snap.architecture_hash,
            snap.identity_hash,
        ),
    )
    conn.commit()


def latest_observation(conn: sqlite3.Connection) -> list[SnapshotRow]:
    """All snapshot rows from the single most recent prior observation run.

    Call BEFORE writing the current run so a run never diffs against itself.
    Returns [] when no observations exist yet.
    """
    row = conn.execute("SELECT MAX(observed_at) AS t FROM snapshots").fetchone()
    if row is None or row["t"] is None:
        return []
    latest = row["t"]
    rows = conn.execute(
        "SELECT * FROM snapshots WHERE observed_at = ? ORDER BY repo_path", (latest,)
    ).fetchall()
    return [
        SnapshotRow(
            observed_at=r["observed_at"],
            repo_path=r["repo_path"],
            repo_name=r["repo_name"],
            default_branch=r["default_branch"],
            commit_count=r["commit_count"],
            last_commit_date=r["last_commit_date"],
            is_dirty=bool(r["is_dirty"]),
            readme_hash=r["readme_hash"],
            architecture_hash=r["architecture_hash"],
            identity_hash=r["identity_hash"],
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Observation Engine storage (Milestone 7) — append-only generic observations.
# ---------------------------------------------------------------------------


@dataclass
class ObservationRow:
    """One persisted observation fact.

    `id` is the deterministic key `observed_at:source:subject:aspect` so the
    same fact written twice in one run is idempotent. `scope` qualifies the
    subject (e.g. a repository path) without overloading `subject`.
    """

    id: str
    observed_at: str
    source: str
    subject: str
    aspect: str
    value: str
    confidence: str
    scope: str = ""
    detail: Optional[str] = None

    def make_id(self) -> str:
        return f"{self.observed_at}:{self.source}:{self.subject}:{self.aspect}"


def insert_observations(conn: sqlite3.Connection, rows: list[ObservationRow]) -> None:
    """Append observations, idempotent on (observed_at, source, subject, aspect)."""
    for row in rows:
        row.id = row.make_id()
        conn.execute(
            """
            INSERT OR REPLACE INTO observations
                (id, observed_at, source, subject, aspect, value, confidence, scope, detail)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row.id, row.observed_at, row.source, row.subject, row.aspect,
                row.value, row.confidence, row.scope, row.detail,
            ),
        )
    conn.commit()


def latest_observations(conn: sqlite3.Connection) -> list[ObservationRow]:
    """All observation rows from the single most recent prior observation run."""
    row = conn.execute("SELECT MAX(observed_at) AS t FROM observations").fetchone()
    if row is None or row["t"] is None:
        return []
    latest = row["t"]
    rows = conn.execute(
        "SELECT * FROM observations WHERE observed_at = ? "
        "ORDER BY source, subject, aspect",
        (latest,),
    ).fetchall()
    return [
        ObservationRow(
            id=r["id"],
            observed_at=r["observed_at"],
            source=r["source"],
            subject=r["subject"],
            aspect=r["aspect"],
            value=r["value"],
            confidence=r["confidence"],
            scope=r["scope"],
            detail=r["detail"],
        )
        for r in rows
    ]


def observation_state_as_of(
    conn: sqlite3.Connection, source: str, observed_at: str
) -> list[ObservationRow]:
    """Every observation for `source` that was current as of `observed_at`.

    Deterministic: the value of an (source, subject, aspect) triple at a given
    time is the one with the largest observed_at <= the requested time. Used to
    build a per-run prior state the engine diffs against without re-reading the
    writer.
    """
    rows = conn.execute(
        """
        SELECT o1.*
        FROM observations o1
        JOIN (
            SELECT source, subject, aspect, MAX(observed_at) AS t
            FROM observations
            WHERE source = ? AND observed_at <= ?
            GROUP BY source, subject, aspect
        ) o2 ON o2.source = o1.source AND o2.subject = o1.subject
            AND o2.aspect = o1.aspect AND o2.t = o1.observed_at
        ORDER BY o1.subject, o1.aspect
        """,
        (source, observed_at),
    ).fetchall()
    return [
        ObservationRow(
            id=r["id"],
            observed_at=r["observed_at"],
            source=r["source"],
            subject=r["subject"],
            aspect=r["aspect"],
            value=r["value"],
            confidence=r["confidence"],
            scope=r["scope"],
            detail=r["detail"],
        )
        for r in rows
    ]


def observations_all(conn: sqlite3.Connection) -> list[ObservationRow]:
    """Every observation row, newest first. For CLI inspection."""
    rows = conn.execute(
        "SELECT * FROM observations ORDER BY observed_at DESC, source, subject, aspect"
    ).fetchall()
    return [
        ObservationRow(
            id=r["id"],
            observed_at=r["observed_at"],
            source=r["source"],
            subject=r["subject"],
            aspect=r["aspect"],
            value=r["value"],
            confidence=r["confidence"],
            scope=r["scope"],
            detail=r["detail"],
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Engineering Context storage (Milestone 7.2) — append-only sessions.
# ---------------------------------------------------------------------------


@dataclass
class SessionRow:
    """One derived engineering session.

    References observation ids (comma-joined) rather than duplicating raw
    observation facts. `id` is deterministic (built_at:primary_repo:start_time)
    so rebuilding the same window is idempotent and append-only by window.
    """

    id: str
    start_time: str
    end_time: str
    repositories: str
    primary_repo: Optional[str]
    observations: str
    activity: str
    confidence: str
    duration_min: float
    branch: Optional[str]
    summary: Optional[str]
    built_at: str


def insert_sessions(conn: sqlite3.Connection, rows: list[SessionRow]) -> None:
    """Append sessions. Idempotent on `id` (same window rebuild replaces)."""
    for row in rows:
        conn.execute(
            """
            INSERT OR REPLACE INTO sessions
                (id, start_time, end_time, repositories, primary_repo,
                 observations, activity, confidence, duration_min, branch,
                 summary, built_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row.id, row.start_time, row.end_time, row.repositories,
                row.primary_repo, row.observations, row.activity,
                row.confidence, row.duration_min, row.branch, row.summary,
                row.built_at,
            ),
        )
    conn.commit()


def get_session(conn: sqlite3.Connection, session_id: str) -> Optional[SessionRow]:
    row = conn.execute(
        "SELECT * FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()
    if row is None:
        return None
    return _row_to_session(row)


def sessions_all(conn: sqlite3.Connection) -> list[SessionRow]:
    """Every session, newest first."""
    rows = conn.execute(
        "SELECT * FROM sessions ORDER BY start_time DESC, id"
    ).fetchall()
    return [_row_to_session(r) for r in rows]


def sessions_on_day(conn: sqlite3.Connection, day: str) -> list[SessionRow]:
    """Sessions whose start_time UTC date equals `day` (YYYY-MM-DD)."""
    rows = conn.execute(
        "SELECT * FROM sessions WHERE date(start_time) = ? "
        "ORDER BY start_time, id",
        (day,),
    ).fetchall()
    return [_row_to_session(r) for r in rows]


def _row_to_session(r) -> SessionRow:
    return SessionRow(
        id=r["id"],
        start_time=r["start_time"],
        end_time=r["end_time"],
        repositories=r["repositories"],
        primary_repo=r["primary_repo"],
        observations=r["observations"],
        activity=r["activity"],
        confidence=r["confidence"],
        duration_min=r["duration_min"],
        branch=r["branch"],
        summary=r["summary"],
        built_at=r["built_at"],
    )


def latest_observation_time(conn: sqlite3.Connection) -> Optional[str]:
    """UTC timestamp of the most recent stored observation (read-only)."""
    row = conn.execute("SELECT MAX(observed_at) AS t FROM observations").fetchone()
    return row["t"] if row else None


def latest_session_built_at(conn: sqlite3.Connection) -> Optional[str]:
    """UTC timestamp of the most recent context build (read-only)."""
    row = conn.execute("SELECT MAX(built_at) AS t FROM sessions").fetchone()
    return row["t"] if row else None


# ---------------------------------------------------------------------------
# Knowledge Engine storage (Milestone 8.1) — append-only knowledge.
# ---------------------------------------------------------------------------


@dataclass
class KnowledgeRow:
    """One accumulated knowledge entry."""

    id: str
    type: str
    subject: str
    statement: str
    confidence: str
    evidence_ids: str
    status: str
    created_at: str
    updated_at: str
    last_verified: Optional[str]
    verification_count: int
    is_static: int = 0


def update_knowledge_status(conn: sqlite3.Connection, knowledge_id: str, status: str) -> None:
    """Apply an evidence-driven lifecycle transition (Dormant/Retired/Reactivated).

    The ONLY live-row mutation the Knowledge Evolution layer performs. The prior
    version is preserved forever in knowledge_history; this only advances the
    latest row's status. Never used for confidence/evidence/statement.
    """
    conn.execute(
        "UPDATE knowledge SET status = ? WHERE id = ?", (status, knowledge_id)
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Knowledge Evolution storage (Milestone 8.2) — append-only history + events.
# Nothing here is ever mutated. The Brain reads `knowledge` (unchanged);
# evolution layers derive change records on top.
# ---------------------------------------------------------------------------


@dataclass
class KnowledgeHistoryRow:
    """One snapshot of a knowledge entry as of a single build."""

    build_at: str
    knowledge_id: str
    type: str
    subject: str
    statement: str
    confidence: str
    evidence_ids: str
    status: str
    created_at: str
    updated_at: str
    verification_count: int
    is_static: int = 0


@dataclass
class EvolutionEventRow:
    """One deterministic evolution event derived from a history diff."""

    id: str
    build_at: str
    event_type: str
    knowledge_id: str
    previous_confidence: Optional[str]
    new_confidence: Optional[str]
    previous_status: Optional[str]
    new_status: Optional[str]
    previous_statement: Optional[str]
    new_statement: Optional[str]
    reason: str
    evidence_ids: str
    related_ids: str
    timestamp: str


def insert_knowledge_history(conn: sqlite3.Connection, rows: List[KnowledgeHistoryRow]) -> None:
    """Append a full snapshot of knowledge state for one build. Idempotent on
    (build_at, knowledge_id); re-running the same build replaces that build's
    snapshot but never touches prior builds."""
    for r in rows:
        conn.execute(
            """
            INSERT OR REPLACE INTO knowledge_history
                (build_at, knowledge_id, type, subject, statement, confidence,
                 evidence_ids, status, created_at, updated_at,
                 verification_count, is_static)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                r.build_at, r.knowledge_id, r.type, r.subject, r.statement,
                r.confidence, r.evidence_ids, r.status, r.created_at,
                r.updated_at, r.verification_count, int(r.is_static),
            ),
        )
    conn.commit()


def latest_knowledge_snapshot(conn: sqlite3.Connection) -> List[KnowledgeHistoryRow]:
    """The most recent prior build snapshot (read-only). [] on cold start."""
    row = conn.execute("SELECT MAX(build_at) AS t FROM knowledge_history").fetchone()
    if row is None or row["t"] is None:
        return []
    rows = conn.execute(
        "SELECT * FROM knowledge_history WHERE build_at = ? ORDER BY knowledge_id",
        (row["t"],),
    ).fetchall()
    return [_row_to_history(r) for r in rows]


def knowledge_history_for(conn: sqlite3.Connection, knowledge_id: str) -> List[KnowledgeHistoryRow]:
    """Every snapshot of one knowledge entry across all builds, oldest first."""
    rows = conn.execute(
        "SELECT * FROM knowledge_history WHERE knowledge_id = ? ORDER BY build_at",
        (knowledge_id,),
    ).fetchall()
    return [_row_to_history(r) for r in rows]


def insert_evolution_events(conn: sqlite3.Connection, rows: List[EvolutionEventRow]) -> None:
    """Append evolution events. Idempotent on id; never updates old rows."""
    for r in rows:
        conn.execute(
            """
            INSERT OR IGNORE INTO evolution_events
                (id, build_at, event_type, knowledge_id, previous_confidence,
                 new_confidence, previous_status, new_status, previous_statement,
                 new_statement, reason, evidence_ids, related_ids, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                r.id, r.build_at, r.event_type, r.knowledge_id,
                r.previous_confidence, r.new_confidence, r.previous_status,
                r.new_status, r.previous_statement, r.new_statement, r.reason,
                r.evidence_ids, r.related_ids, r.timestamp,
            ),
        )
    conn.commit()


def evolution_events_all(conn: sqlite3.Connection) -> List[EvolutionEventRow]:
    """Every evolution event, newest first."""
    rows = conn.execute(
        "SELECT * FROM evolution_events ORDER BY timestamp DESC, id DESC"
    ).fetchall()
    return [_row_to_event(r) for r in rows]


def evolution_events_for(conn: sqlite3.Connection, knowledge_id: str) -> List[EvolutionEventRow]:
    """Evolution events touching one knowledge entry, oldest first."""
    rows = conn.execute(
        "SELECT * FROM evolution_events WHERE knowledge_id = ? ORDER BY timestamp, id",
        (knowledge_id,),
    ).fetchall()
    return [_row_to_event(r) for r in rows]


def _row_to_history(r) -> KnowledgeHistoryRow:
    return KnowledgeHistoryRow(
        build_at=r["build_at"],
        knowledge_id=r["knowledge_id"],
        type=r["type"],
        subject=r["subject"],
        statement=r["statement"],
        confidence=r["confidence"],
        evidence_ids=r["evidence_ids"],
        status=r["status"],
        created_at=r["created_at"],
        updated_at=r["updated_at"],
        verification_count=r["verification_count"] or 0,
        is_static=bool(r["is_static"]),
    )


def _row_to_event(r) -> EvolutionEventRow:
    return EvolutionEventRow(
        id=r["id"],
        build_at=r["build_at"],
        event_type=r["event_type"],
        knowledge_id=r["knowledge_id"],
        previous_confidence=r["previous_confidence"],
        new_confidence=r["new_confidence"],
        previous_status=r["previous_status"],
        new_status=r["new_status"],
        previous_statement=r["previous_statement"],
        new_statement=r["new_statement"],
        reason=r["reason"],
        evidence_ids=r["evidence_ids"] or "",
        related_ids=r["related_ids"] or "",
        timestamp=r["timestamp"],
    )


# ---------------------------------------------------------------------------
# Understanding Engine storage (Milestone 8.3) — write-only layer over Knowledge.
# Append-only. The Brain reads `understanding` (new); knowledge tables unchanged.
# ---------------------------------------------------------------------------


@dataclass
class UnderstandingRow:
    """One derived engineering understanding."""

    id: str
    type: str
    subject: str
    statement: str
    confidence: str
    status: str
    knowledge_ids: str
    created_at: str
    updated_at: str
    build_at: str
    retired_at: Optional[str] = None


@dataclass
class UnderstandingHistoryRow:
    """One snapshot of an understanding as of a single build."""

    build_at: str
    understanding_id: str
    type: str
    subject: str
    statement: str
    confidence: str
    status: str
    knowledge_ids: str
    created_at: str
    updated_at: str
    reinforced_count: int = 0


@dataclass
class UnderstandingEvolutionRow:
    """One deterministic understanding evolution event."""

    id: str
    build_at: str
    event_type: str
    understanding_id: str
    previous_confidence: Optional[str]
    new_confidence: Optional[str]
    previous_status: Optional[str]
    new_status: Optional[str]
    previous_statement: Optional[str]
    new_statement: Optional[str]
    reason: str
    knowledge_ids: str
    timestamp: str


def insert_understanding(conn: sqlite3.Connection, rows: List[UnderstandingRow]) -> None:
    """Insert or replace understanding entries. Idempotent on id."""
    for r in rows:
        conn.execute(
            """
            INSERT OR REPLACE INTO understanding
                (id, type, subject, statement, confidence, status,
                 knowledge_ids, created_at, updated_at, build_at, retired_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                r.id, r.type, r.subject, r.statement, r.confidence, r.status,
                r.knowledge_ids, r.created_at, r.updated_at, r.build_at,
                r.retired_at,
            ),
        )
    conn.commit()


def get_all_understanding(conn: sqlite3.Connection) -> List[UnderstandingRow]:
    """Every understanding entry, newest first."""
    rows = conn.execute(
        "SELECT * FROM understanding ORDER BY updated_at DESC"
    ).fetchall()
    return [_row_to_understanding(r) for r in rows]


def get_understanding_by_id(conn: sqlite3.Connection, uid: str) -> Optional[UnderstandingRow]:
    row = conn.execute(
        "SELECT * FROM understanding WHERE id = ?", (uid,)
    ).fetchone()
    return _row_to_understanding(row) if row else None


def get_understanding_by_type(conn: sqlite3.Connection, utype: str) -> List[UnderstandingRow]:
    rows = conn.execute(
        "SELECT * FROM understanding WHERE type = ? ORDER BY updated_at DESC",
        (utype,),
    ).fetchall()
    return [_row_to_understanding(r) for r in rows]


def count_understanding(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS c FROM understanding").fetchone()
    return row["c"] if row else 0


def update_understanding_status(
    conn: sqlite3.Connection, uid: str, status: str, retired_at: Optional[str] = None
) -> None:
    """Apply a lifecycle transition (the only live-row mutation). History keeps
    the prior version forever."""
    if retired_at is not None:
        conn.execute(
            "UPDATE understanding SET status = ?, retired_at = ? WHERE id = ?",
            (status, retired_at, uid),
        )
    else:
        conn.execute(
            "UPDATE understanding SET status = ? WHERE id = ?", (status, uid)
        )
    conn.commit()


def insert_understanding_history(conn: sqlite3.Connection, rows: List[UnderstandingHistoryRow]) -> None:
    """Append a full snapshot of understanding state for one build. Idempotent on
    (build_at, understanding_id)."""
    for r in rows:
        conn.execute(
            """
            INSERT OR REPLACE INTO understanding_history
                (build_at, understanding_id, type, subject, statement, confidence,
                 status, knowledge_ids, created_at, updated_at, reinforced_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                r.build_at, r.understanding_id, r.type, r.subject, r.statement,
                r.confidence, r.status, r.knowledge_ids, r.created_at,
                r.updated_at, r.reinforced_count,
            ),
        )
    conn.commit()


def latest_understanding_snapshot(conn: sqlite3.Connection) -> List[UnderstandingHistoryRow]:
    """The most recent prior build snapshot, [] on cold start."""
    row = conn.execute("SELECT MAX(build_at) AS t FROM understanding_history").fetchone()
    if row is None or row["t"] is None:
        return []
    rows = conn.execute(
        "SELECT * FROM understanding_history WHERE build_at = ? ORDER BY understanding_id",
        (row["t"],),
    ).fetchall()
    return [_row_to_understanding_history(r) for r in rows]


def understanding_history_for(conn: sqlite3.Connection, uid: str) -> List[UnderstandingHistoryRow]:
    """Every snapshot of one understanding, oldest first."""
    rows = conn.execute(
        "SELECT * FROM understanding_history WHERE understanding_id = ? ORDER BY build_at",
        (uid,),
    ).fetchall()
    return [_row_to_understanding_history(r) for r in rows]


def insert_understanding_evolution(conn: sqlite3.Connection, rows: List[UnderstandingEvolutionRow]) -> None:
    """Append understanding evolution events. Idempotent on id."""
    for r in rows:
        conn.execute(
            """
            INSERT OR IGNORE INTO understanding_evolution
                (id, build_at, event_type, understanding_id, previous_confidence,
                 new_confidence, previous_status, new_status, previous_statement,
                 new_statement, reason, knowledge_ids, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                r.id, r.build_at, r.event_type, r.understanding_id,
                r.previous_confidence, r.new_confidence, r.previous_status,
                r.new_status, r.previous_statement, r.new_statement, r.reason,
                r.knowledge_ids, r.timestamp,
            ),
        )
    conn.commit()


def understanding_evolution_all(conn: sqlite3.Connection) -> List[UnderstandingEvolutionRow]:
    """Every understanding evolution event, newest first."""
    rows = conn.execute(
        "SELECT * FROM understanding_evolution ORDER BY timestamp DESC, id DESC"
    ).fetchall()
    return [_row_to_understanding_evolution(r) for r in rows]


def understanding_evolution_for(conn: sqlite3.Connection, uid: str) -> List[UnderstandingEvolutionRow]:
    """Evolution events touching one understanding, oldest first."""
    rows = conn.execute(
        "SELECT * FROM understanding_evolution WHERE understanding_id = ? ORDER BY timestamp, id",
        (uid,),
    ).fetchall()
    return [_row_to_understanding_evolution(r) for r in rows]


def _row_to_understanding(r) -> UnderstandingRow:
    return UnderstandingRow(
        id=r["id"],
        type=r["type"],
        subject=r["subject"],
        statement=r["statement"],
        confidence=r["confidence"],
        status=r["status"],
        knowledge_ids=r["knowledge_ids"] or "",
        created_at=r["created_at"],
        updated_at=r["updated_at"],
        build_at=r["build_at"],
        retired_at=r["retired_at"],
    )


def _row_to_understanding_history(r) -> UnderstandingHistoryRow:
    return UnderstandingHistoryRow(
        build_at=r["build_at"],
        understanding_id=r["understanding_id"],
        type=r["type"],
        subject=r["subject"],
        statement=r["statement"],
        confidence=r["confidence"],
        status=r["status"],
        knowledge_ids=r["knowledge_ids"] or "",
        created_at=r["created_at"],
        updated_at=r["updated_at"],
        reinforced_count=r["reinforced_count"] or 0,
    )


def _row_to_understanding_evolution(r) -> UnderstandingEvolutionRow:
    return UnderstandingEvolutionRow(
        id=r["id"],
        build_at=r["build_at"],
        event_type=r["event_type"],
        understanding_id=r["understanding_id"],
        previous_confidence=r["previous_confidence"],
        new_confidence=r["new_confidence"],
        previous_status=r["previous_status"],
        new_status=r["new_status"],
        previous_statement=r["previous_statement"],
        new_statement=r["new_statement"],
        reason=r["reason"],
        knowledge_ids=r["knowledge_ids"] or "",
        timestamp=r["timestamp"],
    )


# ===========================================================================
# Initiative Engine storage (Milestone 8.4) — write-only layer over
# Understanding. Append-only. The Brain reads `initiatives` (new); every
# lower layer (understanding/knowledge/observation/context) is unchanged.
# ===========================================================================


@dataclass
class InitiativeRow:
    """One derived long-running engineering initiative."""

    id: str
    title: str
    initiative_type: str
    status: str
    confidence: str
    updated_at: str
    build_at: str
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    created_at: str = ""
    statement: str = ""
    participating_repositories: str = ""
    understanding_ids: str = ""
    knowledge_ids: str = ""


@dataclass
class InitiativeHistoryRow:
    """One snapshot of an initiative as of a single build."""

    build_at: str
    initiative_id: str
    title: str
    initiative_type: str
    status: str
    confidence: str
    participating_repositories: str = ""
    understanding_ids: str = ""
    knowledge_ids: str = ""
    started_at: Optional[str] = None
    completed_at: Optional[str] = None


@dataclass
class InitiativeEvolutionRow:
    """One deterministic initiative lifecycle / merge / split event."""

    id: str
    build_at: str
    event_type: str
    initiative_id: str
    previous_status: Optional[str]
    new_status: Optional[str]
    previous_confidence: Optional[str]
    new_confidence: Optional[str]
    previous_title: Optional[str]
    new_title: Optional[str]
    reason: str
    parent_ids: str = ""
    child_ids: str = ""
    understanding_ids: str = ""
    knowledge_ids: str = ""
    timestamp: str = ""


@dataclass
class InitiativeRelationshipRow:
    """One explicit merge or split edge (parents <-> children)."""

    id: str
    relationship_type: str
    parent_ids: str
    child_ids: str
    build_at: str
    created_at: str
    note: Optional[str] = None


def insert_initiative(conn: sqlite3.Connection, rows: List[InitiativeRow]) -> None:
    """Insert or replace initiative entries. Idempotent on id."""
    for r in rows:
        conn.execute(
            """
            INSERT OR REPLACE INTO initiatives
                (id, title, initiative_type, status, confidence, statement,
                 started_at, updated_at, completed_at, participating_repositories,
                 understanding_ids, knowledge_ids, build_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                r.id, r.title, r.initiative_type, r.status, r.confidence,
                r.statement,
                r.started_at, r.updated_at, r.completed_at,
                r.participating_repositories, r.understanding_ids,
                r.knowledge_ids, r.build_at, r.created_at,
            ),
        )
    conn.commit()


def get_all_initiatives(conn: sqlite3.Connection) -> List[InitiativeRow]:
    """Every initiative entry, newest-first by updated_at."""
    rows = conn.execute(
        "SELECT * FROM initiatives ORDER BY updated_at DESC"
    ).fetchall()
    return [_row_to_initiative(r) for r in rows]


def get_initiative_by_id(
    conn: sqlite3.Connection, iid: str
) -> Optional[InitiativeRow]:
    row = conn.execute(
        "SELECT * FROM initiatives WHERE id = ?", (iid,)
    ).fetchone()
    return _row_to_initiative(row) if row else None


def get_initiative_by_type(
    conn: sqlite3.Connection, itype: str
) -> List[InitiativeRow]:
    rows = conn.execute(
        "SELECT * FROM initiatives WHERE initiative_type = ? ORDER BY updated_at DESC",
        (itype,),
    ).fetchall()
    return [_row_to_initiative(r) for r in rows]


def count_initiatives(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS c FROM initiatives").fetchone()
    return row["c"] if row else 0


def update_initiative_status(
    conn: sqlite3.Connection,
    iid: str,
    status: str,
    completed_at: Optional[str] = None,
) -> None:
    """Apply a lifecycle transition (the only live-row mutation). History keeps
    the prior version forever."""
    if completed_at is not None:
        conn.execute(
            "UPDATE initiatives SET status = ?, completed_at = ? WHERE id = ?",
            (status, completed_at, iid),
        )
    else:
        conn.execute(
            "UPDATE initiatives SET status = ? WHERE id = ?", (status, iid)
        )
    conn.commit()


def insert_initiative_history(
    conn: sqlite3.Connection, rows: List[InitiativeHistoryRow]
) -> None:
    """Append a full snapshot of initiative state for one build. Idempotent on
    (build_at, initiative_id)."""
    for r in rows:
        conn.execute(
            """
            INSERT OR REPLACE INTO initiative_history
                (build_at, initiative_id, title, initiative_type, status,
                 confidence, started_at, completed_at,
                 participating_repositories, understanding_ids, knowledge_ids)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                r.build_at, r.initiative_id, r.title, r.initiative_type,
                r.status, r.confidence, r.started_at, r.completed_at,
                r.participating_repositories, r.understanding_ids,
                r.knowledge_ids,
            ),
        )
    conn.commit()


def latest_initiative_snapshot(
    conn: sqlite3.Connection,
) -> List[InitiativeHistoryRow]:
    """The most recent prior build snapshot, [] on cold start."""
    row = conn.execute(
        "SELECT MAX(build_at) AS t FROM initiative_history"
    ).fetchone()
    if row is None or row["t"] is None:
        return []
    rows = conn.execute(
        "SELECT * FROM initiative_history WHERE build_at = ? ORDER BY initiative_id",
        (row["t"],),
    ).fetchall()
    return [_row_to_initiative_history(r) for r in rows]


def initiative_history_for(
    conn: sqlite3.Connection, iid: str
) -> List[InitiativeHistoryRow]:
    """Every snapshot of one initiative, oldest first."""
    rows = conn.execute(
        "SELECT * FROM initiative_history WHERE initiative_id = ? ORDER BY build_at",
        (iid,),
    ).fetchall()
    return [_row_to_initiative_history(r) for r in rows]


def insert_initiative_evolution(
    conn: sqlite3.Connection, rows: List[InitiativeEvolutionRow]
) -> None:
    """Append initiative evolution events. Idempotent on id."""
    for r in rows:
        conn.execute(
            """
            INSERT OR IGNORE INTO initiative_evolution
                (id, build_at, event_type, initiative_id, parent_ids, child_ids,
                 previous_status, new_status, previous_confidence,
                 new_confidence, previous_title, new_title, reason,
                 understanding_ids, knowledge_ids, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                r.id, r.build_at, r.event_type, r.initiative_id, r.parent_ids,
                r.child_ids, r.previous_status, r.new_status,
                r.previous_confidence, r.new_confidence, r.previous_title,
                r.new_title, r.reason, r.understanding_ids, r.knowledge_ids,
                r.timestamp,
            ),
        )
    conn.commit()


def initiative_evolution_all(
    conn: sqlite3.Connection,
) -> List[InitiativeEvolutionRow]:
    """Every initiative evolution event, newest first."""
    rows = conn.execute(
        "SELECT * FROM initiative_evolution ORDER BY timestamp DESC, id DESC"
    ).fetchall()
    return [_row_to_initiative_evolution(r) for r in rows]


def initiative_evolution_for(
    conn: sqlite3.Connection, iid: str
) -> List[InitiativeEvolutionRow]:
    """Evolution events touching one initiative, oldest first."""
    rows = conn.execute(
        "SELECT * FROM initiative_evolution WHERE initiative_id = ? "
        "ORDER BY timestamp, id",
        (iid,),
    ).fetchall()
    return [_row_to_initiative_evolution(r) for r in rows]


def insert_initiative_relationships(
    conn: sqlite3.Connection, rows: List[InitiativeRelationshipRow]
) -> None:
    """Append explicit merge/split edges. Idempotent on id."""
    for r in rows:
        conn.execute(
            """
            INSERT OR IGNORE INTO initiative_relationships
                (id, relationship_type, parent_ids, child_ids, build_at,
                 created_at, note)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                r.id, r.relationship_type, r.parent_ids, r.child_ids,
                r.build_at, r.created_at, r.note,
            ),
        )
    conn.commit()


def initiative_relationships_all(
    conn: sqlite3.Connection,
) -> List[InitiativeRelationshipRow]:
    rows = conn.execute(
        "SELECT * FROM initiative_relationships ORDER BY created_at"
    ).fetchall()
    return [_row_to_initiative_relationship(r) for r in rows]


def _row_to_initiative(r) -> InitiativeRow:
    return InitiativeRow(
        id=r["id"],
        title=r["title"],
        initiative_type=r["initiative_type"],
        status=r["status"],
        confidence=r["confidence"],
        statement=r["statement"] or "",
        started_at=r["started_at"],
        updated_at=r["updated_at"],
        completed_at=r["completed_at"],
        participating_repositories=r["participating_repositories"] or "",
        understanding_ids=r["understanding_ids"] or "",
        knowledge_ids=r["knowledge_ids"] or "",
        build_at=r["build_at"],
        created_at=r["created_at"] or "",
    )


def _row_to_initiative_history(r) -> InitiativeHistoryRow:
    return InitiativeHistoryRow(
        build_at=r["build_at"],
        initiative_id=r["initiative_id"],
        title=r["title"],
        initiative_type=r["initiative_type"],
        status=r["status"],
        confidence=r["confidence"],
        participating_repositories=r["participating_repositories"] or "",
        understanding_ids=r["understanding_ids"] or "",
        knowledge_ids=r["knowledge_ids"] or "",
        started_at=r["started_at"],
        completed_at=r["completed_at"],
    )


def _row_to_initiative_evolution(r) -> InitiativeEvolutionRow:
    return InitiativeEvolutionRow(
        id=r["id"],
        build_at=r["build_at"],
        event_type=r["event_type"],
        initiative_id=r["initiative_id"],
        previous_status=r["previous_status"],
        new_status=r["new_status"],
        previous_confidence=r["previous_confidence"],
        new_confidence=r["new_confidence"],
        previous_title=r["previous_title"],
        new_title=r["new_title"],
        reason=r["reason"],
        parent_ids=r["parent_ids"] or "",
        child_ids=r["child_ids"] or "",
        understanding_ids=r["understanding_ids"] or "",
        knowledge_ids=r["knowledge_ids"] or "",
        timestamp=r["timestamp"],
    )


def _row_to_initiative_relationship(r) -> InitiativeRelationshipRow:
    return InitiativeRelationshipRow(
        id=r["id"],
        relationship_type=r["relationship_type"],
        parent_ids=r["parent_ids"] or "",
        child_ids=r["child_ids"] or "",
        build_at=r["build_at"],
        created_at=r["created_at"],
        note=r["note"],
    )


# ===========================================================================
# Insight Engine storage (Milestone 8.5) — write-only layer over
# Understanding/Initiatives/Knowledge. Append-only. The Brain reads `insights`
# (new); every lower layer (understanding/initiatives/knowledge/observation/
# context) is unchanged.
# ===========================================================================


@dataclass
class InsightRow:
    """One derived engineering insight worth human attention."""

    id: str
    title: str
    insight_type: str
    statement: str
    status: str
    confidence: str
    updated_at: str
    build_at: str
    started_at: Optional[str] = None
    retired_at: Optional[str] = None
    created_at: str = ""
    understanding_ids: str = ""
    initiative_ids: str = ""
    knowledge_ids: str = ""


@dataclass
class InsightHistoryRow:
    """One snapshot of an insight as of a single build."""

    build_at: str
    insight_id: str
    title: str
    insight_type: str
    statement: str
    status: str
    confidence: str
    understanding_ids: str = ""
    initiative_ids: str = ""
    knowledge_ids: str = ""


@dataclass
class InsightEvolutionRow:
    """One deterministic insight lifecycle / retirement event."""

    id: str
    build_at: str
    event_type: str
    insight_id: str
    previous_status: Optional[str]
    new_status: Optional[str]
    previous_confidence: Optional[str]
    new_confidence: Optional[str]
    previous_statement: Optional[str]
    new_statement: Optional[str]
    reason: str
    understanding_ids: str = ""
    initiative_ids: str = ""
    knowledge_ids: str = ""
    timestamp: str = ""


def insert_insight(conn: sqlite3.Connection, rows: List[InsightRow]) -> None:
    """Insert or replace insight entries. Idempotent on id (stable per rule)."""
    for r in rows:
        conn.execute(
            """
            INSERT OR REPLACE INTO insights
                (id, title, insight_type, statement, status, confidence,
                 started_at, updated_at, retired_at, understanding_ids,
                 initiative_ids, knowledge_ids, build_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                r.id, r.title, r.insight_type, r.statement, r.status,
                r.confidence, r.started_at, r.updated_at, r.retired_at,
                r.understanding_ids, r.initiative_ids, r.knowledge_ids,
                r.build_at, r.created_at,
            ),
        )
    conn.commit()


def get_all_insights(conn: sqlite3.Connection) -> List[InsightRow]:
    """Every insight entry, newest-first by updated_at."""
    rows = conn.execute(
        "SELECT * FROM insights ORDER BY updated_at DESC"
    ).fetchall()
    return [_row_to_insight(r) for r in rows]


def get_insight_by_id(
    conn: sqlite3.Connection, iid: str
) -> Optional[InsightRow]:
    row = conn.execute(
        "SELECT * FROM insights WHERE id = ?", (iid,)
    ).fetchone()
    return _row_to_insight(row) if row else None


def get_insights_by_type(
    conn: sqlite3.Connection, itype: str
) -> List[InsightRow]:
    rows = conn.execute(
        "SELECT * FROM insights WHERE insight_type = ? ORDER BY updated_at DESC",
        (itype,),
    ).fetchall()
    return [_row_to_insight(r) for r in rows]


def count_insights(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS c FROM insights").fetchone()
    return row["c"] if row else 0


def update_insight_status(
    conn: sqlite3.Connection,
    iid: str,
    status: str,
    retired_at: Optional[str] = None,
) -> None:
    """Apply a lifecycle transition (the only live-row mutation). History keeps
    the prior version forever."""
    if retired_at is not None:
        conn.execute(
            "UPDATE insights SET status = ?, retired_at = ? WHERE id = ?",
            (status, retired_at, iid),
        )
    else:
        conn.execute(
            "UPDATE insights SET status = ? WHERE id = ?", (status, iid)
        )
    conn.commit()


def insert_insight_history(
    conn: sqlite3.Connection, rows: List[InsightHistoryRow]
) -> None:
    """Append a full snapshot of insight state for one build. Idempotent on
    (build_at, insight_id)."""
    for r in rows:
        conn.execute(
            """
            INSERT OR REPLACE INTO insight_history
                (build_at, insight_id, title, insight_type, statement, status,
                 confidence, understanding_ids, initiative_ids, knowledge_ids)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                r.build_at, r.insight_id, r.title, r.insight_type, r.statement,
                r.status, r.confidence, r.understanding_ids, r.initiative_ids,
                r.knowledge_ids,
            ),
        )
    conn.commit()


def latest_insight_snapshot(
    conn: sqlite3.Connection,
) -> List[InsightHistoryRow]:
    """The most recent prior build snapshot, [] on cold start."""
    row = conn.execute(
        "SELECT MAX(build_at) AS t FROM insight_history"
    ).fetchone()
    if row is None or row["t"] is None:
        return []
    rows = conn.execute(
        "SELECT * FROM insight_history WHERE build_at = ? ORDER BY insight_id",
        (row["t"],),
    ).fetchall()
    return [_row_to_insight_history(r) for r in rows]


def insight_history_for(
    conn: sqlite3.Connection, iid: str
) -> List[InsightHistoryRow]:
    """Every snapshot of one insight, oldest first."""
    rows = conn.execute(
        "SELECT * FROM insight_history WHERE insight_id = ? ORDER BY build_at",
        (iid,),
    ).fetchall()
    return [_row_to_insight_history(r) for r in rows]


def insert_insight_evolution(
    conn: sqlite3.Connection, rows: List[InsightEvolutionRow]
) -> None:
    """Append insight evolution events. Idempotent on id."""
    for r in rows:
        conn.execute(
            """
            INSERT OR IGNORE INTO insight_evolution
                (id, build_at, event_type, insight_id, previous_status,
                 new_status, previous_confidence, new_confidence,
                 previous_statement, new_statement, reason, understanding_ids,
                 initiative_ids, knowledge_ids, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                r.id, r.build_at, r.event_type, r.insight_id, r.previous_status,
                r.new_status, r.previous_confidence, r.new_confidence,
                r.previous_statement, r.new_statement, r.reason,
                r.understanding_ids, r.initiative_ids, r.knowledge_ids,
                r.timestamp,
            ),
        )
    conn.commit()


def insight_evolution_all(
    conn: sqlite3.Connection,
) -> List[InsightEvolutionRow]:
    """Every insight evolution event, newest first."""
    rows = conn.execute(
        "SELECT * FROM insight_evolution ORDER BY timestamp DESC, id DESC"
    ).fetchall()
    return [_row_to_insight_evolution(r) for r in rows]


def insight_evolution_for(
    conn: sqlite3.Connection, iid: str
) -> List[InsightEvolutionRow]:
    """Evolution events touching one insight, oldest first."""
    rows = conn.execute(
        "SELECT * FROM insight_evolution WHERE insight_id = ? "
        "ORDER BY timestamp, id",
        (iid,),
    ).fetchall()
    return [_row_to_insight_evolution(r) for r in rows]


def _row_to_insight(r) -> InsightRow:
    return InsightRow(
        id=r["id"],
        title=r["title"],
        insight_type=r["insight_type"],
        statement=r["statement"],
        status=r["status"],
        confidence=r["confidence"],
        started_at=r["started_at"],
        updated_at=r["updated_at"],
        retired_at=r["retired_at"],
        created_at=r["created_at"] or "",
        understanding_ids=r["understanding_ids"] or "",
        initiative_ids=r["initiative_ids"] or "",
        knowledge_ids=r["knowledge_ids"] or "",
        build_at=r["build_at"],
    )


def _row_to_insight_history(r) -> InsightHistoryRow:
    return InsightHistoryRow(
        build_at=r["build_at"],
        insight_id=r["insight_id"],
        title=r["title"],
        insight_type=r["insight_type"],
        statement=r["statement"],
        status=r["status"],
        confidence=r["confidence"],
        understanding_ids=r["understanding_ids"] or "",
        initiative_ids=r["initiative_ids"] or "",
        knowledge_ids=r["knowledge_ids"] or "",
    )


def _row_to_insight_evolution(r) -> InsightEvolutionRow:
    return InsightEvolutionRow(
        id=r["id"],
        build_at=r["build_at"],
        event_type=r["event_type"],
        insight_id=r["insight_id"],
        previous_status=r["previous_status"],
        new_status=r["new_status"],
        previous_confidence=r["previous_confidence"],
        new_confidence=r["new_confidence"],
        previous_statement=r["previous_statement"],
        new_statement=r["new_statement"],
        reason=r["reason"],
        understanding_ids=r["understanding_ids"] or "",
        initiative_ids=r["initiative_ids"] or "",
        knowledge_ids=r["knowledge_ids"] or "",
        timestamp=r["timestamp"],
    )


