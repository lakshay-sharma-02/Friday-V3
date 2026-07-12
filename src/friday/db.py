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
from typing import Optional


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
    priority INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS architecture (
    repo_id         INTEGER PRIMARY KEY REFERENCES repositories(id) ON DELETE CASCADE,
    architecture    TEXT NOT NULL,
    evidence        TEXT NOT NULL,
    data_flow       TEXT,
    known_patterns  TEXT,
    complexity      TEXT
);

CREATE TABLE IF NOT EXISTS components (
    repo_id   INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    name      TEXT NOT NULL,
    evidence  TEXT NOT NULL,
    PRIMARY KEY (repo_id, name)
);

CREATE TABLE IF NOT EXISTS entry_points (
    repo_id   INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    kind      TEXT NOT NULL,
    detail    TEXT NOT NULL,
    evidence  TEXT NOT NULL,
    PRIMARY KEY (repo_id, kind, detail)
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


@dataclass
class ArchitectureRow:
    repo_id: int
    architecture: str
    evidence: str
    data_flow: Optional[str]
    known_patterns: Optional[str]
    complexity: Optional[str]


@dataclass
class ComponentRow:
    repo_id: int
    name: str
    evidence: str


@dataclass
class EntryPointRow:
    repo_id: int
    kind: str
    detail: str
    evidence: str


def connect(path: Optional[Path] = None) -> sqlite3.Connection:
    path = path or db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Apply additive schema changes idempotently (M2: identity-card columns)."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(repositories)")}
    for col, ctype in (
        ("maturity", "TEXT"),
        ("readme_quality", "TEXT"),
        ("readme_completeness", "TEXT"),
    ):
        if col not in cols:
            conn.execute(f"ALTER TABLE repositories ADD COLUMN {col} {ctype}")
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
        """INSERT INTO relationships (repo_a, repo_b, kind, evidence, priority)
           VALUES (?, ?, ?, ?, ?)""",
        [(r.repo_a, r.repo_b, r.kind, r.evidence, r.priority) for r in rels],
    )
    conn.commit()


def replace_all_relationships(conn: sqlite3.Connection, rels: list[RelationshipRow]) -> None:
    """Wipe and rewrite the entire relationships table (used at ingest)."""
    conn.execute("DELETE FROM relationships")
    conn.executemany(
        """INSERT INTO relationships (repo_a, repo_b, kind, evidence, priority)
           VALUES (?, ?, ?, ?, ?)""",
        [(r.repo_a, r.repo_b, r.kind, r.evidence, r.priority) for r in rels],
    )
    conn.commit()


def get_relationships(conn: sqlite3.Connection, repo_id: int) -> list[RelationshipRow]:
    rows = conn.execute(
        """SELECT repo_a, repo_b, kind, evidence, priority
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
        )
        for r in rows
    ]


def get_all_relationships(conn: sqlite3.Connection) -> list[RelationshipRow]:
    rows = conn.execute(
        "SELECT repo_a, repo_b, kind, evidence, priority FROM relationships ORDER BY priority DESC, kind"
    ).fetchall()
    return [
        RelationshipRow(
            repo_a=r["repo_a"],
            repo_b=r["repo_b"],
            kind=r["kind"],
            evidence=r["evidence"],
            priority=r["priority"],
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
) -> None:
    conn.execute(
        """
        INSERT INTO architecture
            (repo_id, architecture, evidence, data_flow, known_patterns, complexity)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(repo_id) DO UPDATE SET
            architecture=excluded.architecture,
            evidence=excluded.evidence,
            data_flow=excluded.data_flow,
            known_patterns=excluded.known_patterns,
            complexity=excluded.complexity
        """,
        (repo_id, architecture, evidence, data_flow, known_patterns, complexity),
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
    )


def replace_components(
    conn: sqlite3.Connection, repo_id: int, components: list[ComponentRow]
) -> None:
    conn.execute("DELETE FROM components WHERE repo_id = ?", (repo_id,))
    conn.executemany(
        "INSERT OR REPLACE INTO components (repo_id, name, evidence) VALUES (?, ?, ?)",
        [(repo_id, c.name, c.evidence) for c in components],
    )
    conn.commit()


def get_components(conn: sqlite3.Connection, repo_id: int) -> list[ComponentRow]:
    rows = conn.execute(
        "SELECT repo_id, name, evidence FROM components WHERE repo_id = ? ORDER BY name",
        (repo_id,),
    ).fetchall()
    return [ComponentRow(repo_id=r["repo_id"], name=r["name"], evidence=r["evidence"]) for r in rows]


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
        "SELECT repo_id, name, evidence FROM components ORDER BY repo_id, name"
    ).fetchall()
    return [ComponentRow(repo_id=r["repo_id"], name=r["name"], evidence=r["evidence"]) for r in rows]


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
