"""CRUD queries for the Friday knowledge base.

Split from the original monolithic db.py — all query functions live here.
Shared definitions (schema, classes, migrations, connect) are in core.py.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Union

from .core import (
    Repository, LangRow, TechRow, RelationshipRow, ArchitectureRow,
    ComponentRow, EntryPointRow, connect, now_iso, commit_if_top,
    insert_layer_history, atomic,
)

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
    head_sha: Optional[str] = None
    manifest_hash: Optional[str] = None


def insert_snapshot(conn: sqlite3.Connection, snap: SnapshotRow) -> None:
    """Append one observation row. Snapshots are never updated or deleted."""
    conn.execute(
        """
        INSERT INTO snapshots
            (observed_at, repo_path, repo_name, default_branch, commit_count,
             last_commit_date, is_dirty, readme_hash, architecture_hash, identity_hash,
             head_sha, manifest_hash)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            snap.head_sha,
            snap.manifest_hash,
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
    same fact written twice in one run is idempotent. With PRIMARY KEY(id) in
    place (M9.2.5), `INSERT OR REPLACE` collapses identical re-inserts instead
    of appending duplicates. `scope` qualifies the subject (e.g. a repository
    path) without overloading `subject`.
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
    schema_version: str = "1.0"


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
    schema_version: str = "1.0"


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




def insert_understanding(conn: sqlite3.Connection, rows: List[UnderstandingRow]) -> None:
    """Insert or replace understanding entries. Idempotent on id."""
    for r in rows:
        conn.execute(
            """
            INSERT INTO understanding
                (id, type, subject, statement, confidence, status,
                 knowledge_ids, created_at, updated_at, build_at, retired_at,
                 schema_version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                type=excluded.type, subject=excluded.subject,
                statement=excluded.statement, confidence=excluded.confidence,
                status=excluded.status, knowledge_ids=excluded.knowledge_ids,
                updated_at=excluded.updated_at, build_at=excluded.build_at,
                retired_at=excluded.retired_at, schema_version=excluded.schema_version
            """,
            (
                r.id, r.type, r.subject, r.statement, r.confidence, r.status,
                r.knowledge_ids, r.created_at, r.updated_at, r.build_at,
                r.retired_at, r.schema_version,
            ),
        )
    commit_if_top(conn)


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
    commit_if_top(conn)


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
    schema_version: str = "1.0"


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
    created_at: str = ""


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
            INSERT INTO initiatives
                (id, title, initiative_type, status, confidence, statement,
                 started_at, updated_at, completed_at, participating_repositories,
                 understanding_ids, knowledge_ids, build_at, created_at,
                 schema_version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                title=excluded.title, initiative_type=excluded.initiative_type,
                status=excluded.status, confidence=excluded.confidence,
                statement=excluded.statement, started_at=excluded.started_at,
                updated_at=excluded.updated_at, completed_at=excluded.completed_at,
                participating_repositories=excluded.participating_repositories,
                understanding_ids=excluded.understanding_ids,
                knowledge_ids=excluded.knowledge_ids, build_at=excluded.build_at,
                created_at=excluded.created_at, schema_version=excluded.schema_version
            """,
            (
                r.id, r.title, r.initiative_type, r.status, r.confidence,
                r.statement,
                r.started_at, r.updated_at, r.completed_at,
                r.participating_repositories, r.understanding_ids,
                r.knowledge_ids, r.build_at, r.created_at, r.schema_version,
            ),
        )
    commit_if_top(conn)


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
    commit_if_top(conn)


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
    commit_if_top(conn)


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
    schema_version: str = "1.0"


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
    created_at: str = ""
    schema_version: str = "1.0"


def insert_insight(conn: sqlite3.Connection, rows: List[InsightRow]) -> None:
    """Insert or replace insight entries. Idempotent on id (stable per rule)."""
    for r in rows:
        conn.execute(
            """
            INSERT INTO insights
                (id, title, insight_type, statement, status, confidence,
                 started_at, updated_at, retired_at, understanding_ids,
                 initiative_ids, knowledge_ids, build_at, created_at,
                 schema_version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                title=excluded.title, insight_type=excluded.insight_type,
                statement=excluded.statement, status=excluded.status,
                confidence=excluded.confidence, started_at=excluded.started_at,
                updated_at=excluded.updated_at, retired_at=excluded.retired_at,
                understanding_ids=excluded.understanding_ids,
                initiative_ids=excluded.initiative_ids,
                knowledge_ids=excluded.knowledge_ids, build_at=excluded.build_at,
                created_at=excluded.created_at, schema_version=excluded.schema_version
            """,
            (
                r.id, r.title, r.insight_type, r.statement, r.status,
                r.confidence, r.started_at, r.updated_at, r.retired_at,
                r.understanding_ids, r.initiative_ids, r.knowledge_ids,
                r.build_at, r.created_at, r.schema_version,
            ),
        )
    commit_if_top(conn)


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
    commit_if_top(conn)


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
# ===========================================================================
# Planning Engine storage (Milestone 9.0) — write-only layer on top of
# Insights/Initiatives/Understanding/Knowledge. Append-only. The Brain reads
# `plans` (new); every lower layer is unchanged.
# ===========================================================================


@dataclass
class PlanRow:
    """One derived engineering plan (structured, evidence-backed)."""

    id: str
    goal: str
    plan_type: str
    confidence: str
    status: str
    milestones: str
    dependencies: str
    risks: str
    verification: str
    rollback: str
    estimated_complexity: str
    estimated_effort: str
    plan_text: str
    created_at: str
    updated_at: str
    affected_initiative_ids: str = ""
    affected_insight_ids: str = ""
    affected_understanding_ids: str = ""
    affected_knowledge_ids: str = ""
    schema_version: str = "1.0"


@dataclass
class PlanHistoryRow:
    """One snapshot of a plan as of a single generation."""

    generated_at: str
    plan_id: str
    goal: str
    plan_type: str
    confidence: str
    status: str
    milestones: str
    dependencies: str
    risks: str
    verification: str
    rollback: str
    estimated_complexity: str
    estimated_effort: str
    affected_initiative_ids: str = ""
    affected_insight_ids: str = ""
    affected_understanding_ids: str = ""
    affected_knowledge_ids: str = ""




def insert_plan(conn: sqlite3.Connection, rows: List[PlanRow]) -> None:
    """Insert or replace plan entries. Idempotent on id (stable per goal)."""
    for r in rows:
        conn.execute(
            """
            INSERT INTO plans
                (id, goal, plan_type, confidence, status,
                 affected_initiative_ids, affected_insight_ids,
                 affected_understanding_ids, affected_knowledge_ids,
                 milestones, dependencies, risks, verification, rollback,
                 estimated_complexity, estimated_effort, plan_text,
                 schema_version, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                goal=excluded.goal, plan_type=excluded.plan_type,
                confidence=excluded.confidence, status=excluded.status,
                affected_initiative_ids=excluded.affected_initiative_ids,
                affected_insight_ids=excluded.affected_insight_ids,
                affected_understanding_ids=excluded.affected_understanding_ids,
                affected_knowledge_ids=excluded.affected_knowledge_ids,
                milestones=excluded.milestones, dependencies=excluded.dependencies,
                risks=excluded.risks, verification=excluded.verification,
                rollback=excluded.rollback,
                estimated_complexity=excluded.estimated_complexity,
                estimated_effort=excluded.estimated_effort,
                plan_text=excluded.plan_text, schema_version=excluded.schema_version,
                updated_at=excluded.updated_at
            """,
            (
                r.id, r.goal, r.plan_type, r.confidence, r.status,
                r.affected_initiative_ids, r.affected_insight_ids,
                r.affected_understanding_ids, r.affected_knowledge_ids,
                r.milestones, r.dependencies, r.risks, r.verification,
                r.rollback, r.estimated_complexity, r.estimated_effort,
                r.plan_text, r.schema_version, r.created_at, r.updated_at,
            ),
        )
    commit_if_top(conn)


def get_all_plans(conn: sqlite3.Connection) -> List[PlanRow]:
    """Every plan entry, newest-first by updated_at."""
    rows = conn.execute("SELECT * FROM plans ORDER BY updated_at DESC").fetchall()
    return [_row_to_plan(r) for r in rows]


def get_plan_by_id(conn: sqlite3.Connection, pid: str) -> Optional[PlanRow]:
    row = conn.execute("SELECT * FROM plans WHERE id = ?", (pid,)).fetchone()
    return _row_to_plan(row) if row else None


def get_plans_by_type(conn: sqlite3.Connection, ptype: str) -> List[PlanRow]:
    rows = conn.execute(
        "SELECT * FROM plans WHERE plan_type = ? ORDER BY updated_at DESC",
        (ptype,),
    ).fetchall()
    return [_row_to_plan(r) for r in rows]


def count_plans(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS c FROM plans").fetchone()
    return row["c"] if row else 0


def update_plan_status(conn: sqlite3.Connection, pid: str, status: str) -> None:
    """Apply a lifecycle transition. History keeps the prior version forever."""
    conn.execute("UPDATE plans SET status = ? WHERE id = ?", (status, pid))
    conn.commit()


def insert_plan_history(conn: sqlite3.Connection, rows: List[PlanHistoryRow]) -> None:
    """Append a full snapshot of plan state for one generation. Idempotent on
    (generated_at, plan_id)."""
    for r in rows:
        conn.execute(
            """
            INSERT OR REPLACE INTO plan_history
                (generated_at, plan_id, goal, plan_type, confidence, status,
                 affected_initiative_ids, affected_insight_ids,
                 affected_understanding_ids, affected_knowledge_ids,
                 milestones, dependencies, risks, verification, rollback,
                 estimated_complexity, estimated_effort)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                r.generated_at, r.plan_id, r.goal, r.plan_type, r.confidence,
                r.status, r.affected_initiative_ids, r.affected_insight_ids,
                r.affected_understanding_ids, r.affected_knowledge_ids,
                r.milestones, r.dependencies, r.risks, r.verification,
                r.rollback, r.estimated_complexity, r.estimated_effort,
            ),
        )
    commit_if_top(conn)


def latest_plan_snapshot(conn: sqlite3.Connection) -> List[PlanHistoryRow]:
    """The most recent prior generation snapshot, [] on cold start."""
    row = conn.execute(
        "SELECT MAX(generated_at) AS t FROM plan_history"
    ).fetchone()
    if row is None or row["t"] is None:
        return []
    rows = conn.execute(
        "SELECT * FROM plan_history WHERE generated_at = ? ORDER BY plan_id",
        (row["t"],),
    ).fetchall()
    return [_row_to_plan_history(r) for r in rows]


def plan_history_for(conn: sqlite3.Connection, pid: str) -> List[PlanHistoryRow]:
    """Every snapshot of one plan, oldest first."""
    rows = conn.execute(
        "SELECT * FROM plan_history WHERE plan_id = ? ORDER BY generated_at",
        (pid,),
    ).fetchall()
    return [_row_to_plan_history(r) for r in rows]
# ===========================================================================
# Task Graph Compiler storage (Milestone 9.1) — write-only layer on top of
# the Planning Engine. Append-only. The Brain reads `task_graphs` (new); every
# lower layer (including plans) is unchanged.
# ===========================================================================


@dataclass
class TaskGraphRow:
    """One compiled task graph (a deterministic DAG of tasks)."""

    id: str
    goal: str
    plan_id: str
    plan_type: str
    task_count: int
    edge_count: int
    critical_path_length: int
    parallel_groups: int
    status: str
    created_at: str
    updated_at: str
    source: Optional[str] = None  # provenance: e.g. "suggestion:<id>", "initiative:<id>"


@dataclass
class TaskRow:
    """One executable task node in a compiled graph."""

    id: str
    graph_id: str
    plan_id: str
    milestone_order: int
    title: str
    description: str
    task_type: str
    required_capabilities: str
    complexity: str
    priority: str
    estimated_effort: str
    dependencies: str
    inputs: str
    outputs: str
    acceptance_criteria: str
    verification: str
    rollback: str
    evidence: str
    symbolic: str = "{}"
    status: str = "pending"
    confidence: str = "medium"
    sequence: int = 0


@dataclass
class TaskEdgeRow:
    """One dependency edge in a compiled graph (from_task depends on to_task)."""

    id: str
    graph_id: str
    from_task: str
    to_task: str
    kind: str


@dataclass
class TaskHistoryRow:
    """One append-only snapshot of a graph as of a single compilation."""

    generated_at: str
    graph_id: str
    goal: str
    task_count: int
    edge_count: int
    critical_path_length: int
    parallel_groups: int
    tasks_json: str
    edges_json: str




def insert_task_graph(conn: sqlite3.Connection, graphs: List[TaskGraphRow],
                      tasks: List[TaskRow], edges: List[TaskEdgeRow]) -> None:
    """Persist one compiled graph: header, tasks, edges (idempotent on ids).

    All three groups are written atomically — a crash mid-write leaves no
    partial graph (e.g. a header with zero tasks).
    """
    conn.commit()  # close any open implicit transaction from prior raw writes
    conn.execute("BEGIN TRANSACTION")
    try:
        for g in graphs:
            conn.execute(
                """
                INSERT INTO task_graphs
                    (id, goal, plan_id, plan_type, task_count, edge_count,
                     critical_path_length, parallel_groups, status, created_at,
                     updated_at, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    goal=excluded.goal, plan_id=excluded.plan_id,
                    plan_type=excluded.plan_type, task_count=excluded.task_count,
                    edge_count=excluded.edge_count,
                    critical_path_length=excluded.critical_path_length,
                    parallel_groups=excluded.parallel_groups, status=excluded.status,
                    updated_at=excluded.updated_at
                """,
                (g.id, g.goal, g.plan_id, g.plan_type, g.task_count, g.edge_count,
                 g.critical_path_length, g.parallel_groups, g.status,
                 g.created_at, g.updated_at, g.source),
            )
            # Scrub stale tasks + edges before re-inserting, so recompilation
            # doesn't leave orphan rows from a prior generation with different
            # task count.
            conn.execute("DELETE FROM tasks WHERE graph_id=?", (g.id,))
            conn.execute("DELETE FROM task_edges WHERE graph_id=?", (g.id,))
        for t in tasks:
            conn.execute(
                """
                INSERT INTO tasks
                    (id, graph_id, plan_id, milestone_order, title, description,
                     task_type, required_capabilities, complexity, priority,
                     estimated_effort, dependencies, inputs, outputs,
                     acceptance_criteria, verification, rollback, evidence,
                     symbolic, status, confidence, sequence)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    graph_id=excluded.graph_id, plan_id=excluded.plan_id,
                    milestone_order=excluded.milestone_order, title=excluded.title,
                    description=excluded.description, task_type=excluded.task_type,
                    required_capabilities=excluded.required_capabilities,
                    complexity=excluded.complexity, priority=excluded.priority,
                    estimated_effort=excluded.estimated_effort,
                    dependencies=excluded.dependencies, inputs=excluded.inputs,
                    outputs=excluded.outputs,
                    acceptance_criteria=excluded.acceptance_criteria,
                    verification=excluded.verification, rollback=excluded.rollback,
                    evidence=excluded.evidence, symbolic=excluded.symbolic,
                    status=excluded.status,
                    confidence=excluded.confidence, sequence=excluded.sequence
                """,
                (t.id, t.graph_id, t.plan_id, t.milestone_order, t.title,
                 t.description, t.task_type, t.required_capabilities, t.complexity,
                 t.priority, t.estimated_effort, t.dependencies, t.inputs, t.outputs,
                 t.acceptance_criteria, t.verification, t.rollback, t.evidence,
                 t.symbolic, t.status, t.confidence, t.sequence),
            )
        for e in edges:
            conn.execute(
                """
                INSERT OR REPLACE INTO task_edges
                    (id, graph_id, from_task, to_task, kind)
                VALUES (?, ?, ?, ?, ?)
                """,
                (e.id, e.graph_id, e.from_task, e.to_task, e.kind),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def get_all_task_graphs(conn: sqlite3.Connection) -> List[TaskGraphRow]:
    rows = conn.execute(
        "SELECT * FROM task_graphs ORDER BY updated_at DESC").fetchall()
    return [_row_to_task_graph(r) for r in rows]


def get_task_graph_by_id(conn: sqlite3.Connection, gid: str) -> Optional[TaskGraphRow]:
    row = conn.execute(
        "SELECT * FROM task_graphs WHERE id = ?", (gid,)).fetchone()
    return _row_to_task_graph(row) if row else None


def get_tasks_for_graph(conn: sqlite3.Connection, gid: str) -> List[TaskRow]:
    rows = conn.execute(
        "SELECT * FROM tasks WHERE graph_id = ? ORDER BY sequence", (gid,)
    ).fetchall()
    return [_row_to_task(r) for r in rows]


def get_edges_for_graph(conn: sqlite3.Connection, gid: str) -> List[TaskEdgeRow]:
    rows = conn.execute(
        "SELECT * FROM task_edges WHERE graph_id = ?", (gid,)).fetchall()
    return [_row_to_task_edge(r) for r in rows]


def count_task_graphs(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS c FROM task_graphs").fetchone()
    return row["c"] if row else 0


def update_task_graph_source(conn: sqlite3.Connection, gid: str,
                              source: str) -> None:
    """Set the provenance/source tag on a graph (e.g. 'suggestion:<id>')."""
    conn.execute(
        "UPDATE task_graphs SET source = ? WHERE id = ?", (source, gid))
    conn.commit()


def update_task_graph_status(conn: sqlite3.Connection, gid: str,
                             status: str) -> None:
    conn.execute(
        "UPDATE task_graphs SET status = ? WHERE id = ?", (status, gid))
    conn.commit()


def insert_task_history(conn: sqlite3.Connection,
                        rows: List[TaskHistoryRow]) -> None:
    for r in rows:
        conn.execute(
            """
            INSERT OR REPLACE INTO task_history
                (generated_at, graph_id, goal, task_count, edge_count,
                 critical_path_length, parallel_groups, tasks_json, edges_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (r.generated_at, r.graph_id, r.goal, r.task_count, r.edge_count,
             r.critical_path_length, r.parallel_groups, r.tasks_json,
             r.edges_json),
        )
    conn.commit()


def latest_task_graph_snapshot(conn: sqlite3.Connection) -> List[TaskHistoryRow]:
    row = conn.execute(
        "SELECT MAX(generated_at) AS t FROM task_history").fetchone()
    if row is None or row["t"] is None:
        return []
    rows = conn.execute(
        "SELECT * FROM task_history WHERE generated_at = ? ORDER BY graph_id",
        (row["t"],)).fetchall()
    return [_row_to_task_history(r) for r in rows]


def task_history_for(conn: sqlite3.Connection, gid: str) -> List[TaskHistoryRow]:
    rows = conn.execute(
        "SELECT * FROM task_history WHERE graph_id = ? ORDER BY generated_at",
        (gid,)).fetchall()
    return [_row_to_task_history(r) for r in rows]
def _row_to_task_graph(r) -> TaskGraphRow:
    source = r["source"] if "source" in r.keys() else None
    return TaskGraphRow(
        id=r["id"], goal=r["goal"], plan_id=r["plan_id"],
        plan_type=r["plan_type"], task_count=r["task_count"],
        edge_count=r["edge_count"],
        critical_path_length=r["critical_path_length"],
        parallel_groups=r["parallel_groups"], status=r["status"],
        created_at=r["created_at"], updated_at=r["updated_at"],
        source=source,
    )


def _row_to_task(r) -> TaskRow:
    return TaskRow(
        id=r["id"], graph_id=r["graph_id"], plan_id=r["plan_id"],
        milestone_order=r["milestone_order"], title=r["title"],
        description=r["description"], task_type=r["task_type"],
        required_capabilities=r["required_capabilities"],
        complexity=r["complexity"], priority=r["priority"],
        estimated_effort=r["estimated_effort"], dependencies=r["dependencies"],
        inputs=r["inputs"], outputs=r["outputs"],
        acceptance_criteria=r["acceptance_criteria"],
        verification=r["verification"], rollback=r["rollback"],
        evidence=r["evidence"], symbolic=r["symbolic"], status=r["status"],
        confidence=r["confidence"], sequence=r["sequence"],
    )


def _row_to_task_edge(r) -> TaskEdgeRow:
    return TaskEdgeRow(
        id=r["id"], graph_id=r["graph_id"], from_task=r["from_task"],
        to_task=r["to_task"], kind=r["kind"],
    )


def _row_to_task_history(r) -> TaskHistoryRow:
    return TaskHistoryRow(
        generated_at=r["generated_at"], graph_id=r["graph_id"], goal=r["goal"],
        task_count=r["task_count"], edge_count=r["edge_count"],
        critical_path_length=r["critical_path_length"],
        parallel_groups=r["parallel_groups"], tasks_json=r["tasks_json"],
        edges_json=r["edges_json"],
    )


def _row_to_plan(r) -> PlanRow:
    return PlanRow(
        id=r["id"],
        goal=r["goal"],
        plan_type=r["plan_type"],
        confidence=r["confidence"],
        status=r["status"],
        milestones=r["milestones"] or "",
        dependencies=r["dependencies"] or "",
        risks=r["risks"] or "",
        verification=r["verification"] or "",
        rollback=r["rollback"] or "",
        estimated_complexity=r["estimated_complexity"] or "",
        estimated_effort=r["estimated_effort"] or "",
        plan_text=r["plan_text"] or "",
        created_at=r["created_at"] or "",
        updated_at=r["updated_at"] or "",
        affected_initiative_ids=r["affected_initiative_ids"] or "",
        affected_insight_ids=r["affected_insight_ids"] or "",
        affected_understanding_ids=r["affected_understanding_ids"] or "",
        affected_knowledge_ids=r["affected_knowledge_ids"] or "",
    )


def _row_to_plan_history(r) -> PlanHistoryRow:
    return PlanHistoryRow(
        generated_at=r["generated_at"],
        plan_id=r["plan_id"],
        goal=r["goal"],
        plan_type=r["plan_type"],
        confidence=r["confidence"],
        status=r["status"],
        milestones=r["milestones"] or "",
        dependencies=r["dependencies"] or "",
        risks=r["risks"] or "",
        verification=r["verification"] or "",
        rollback=r["rollback"] or "",
        estimated_complexity=r["estimated_complexity"] or "",
        estimated_effort=r["estimated_effort"] or "",
        affected_initiative_ids=r["affected_initiative_ids"] or "",
        affected_insight_ids=r["affected_insight_ids"] or "",
        affected_understanding_ids=r["affected_understanding_ids"] or "",
        affected_knowledge_ids=r["affected_knowledge_ids"] or "",
    )
# ===========================================================================
# Worker Registry storage (Milestone 9.2) — write-only layer describing workers.
# Append-only history + version log. Every lower layer unchanged. No execution.
# ===========================================================================


@dataclass
class WorkerRow:
    """One registered worker (a capability profile; NEVER an execution)."""

    id: str
    name: str
    kind: str
    description: str = ""
    capabilities: str = ""
    supported_languages: str = ""
    supported_task_types: str = ""
    supported_plan_types: str = ""
    limitations: str = ""
    estimated_speed: str = ""
    estimated_cost: str = ""
    context_window: int = 0
    parallelism: int = 1
    requires_network: bool = False
    requires_filesystem: bool = False
    requires_git: bool = False
    requires_python: bool = False
    requires_shell: bool = False
    confidence: str = "medium"
    version: str = "1.0.0"
    status: str = "active"
    schema_version: str = "1.0"
    created_at: str = ""
    updated_at: str = ""
    availability: str = "available"
    manifest_ref: Optional[str] = None
    worker_kind: str = "function"


@dataclass
class WorkerHistoryRow:
    """One append-only snapshot of a worker per registration event."""

    registered_at: str
    worker_id: str
    name: str
    kind: str
    version: str
    status: str
    capabilities: str = ""
    limitations: str = ""
    event_type: str = "registered"
    note: Optional[str] = None


@dataclass
class WorkerVersionRow:
    """One append-only version record for a worker."""

    worker_id: str
    version: str
    registered_at: str
    changelog: Optional[str] = None


def insert_worker(conn: sqlite3.Connection, w: WorkerRow) -> None:
    """Insert or replace a worker by id (idempotent on id)."""
    conn.execute(
        """
        INSERT INTO workers
            (id, name, kind, description, capabilities, supported_languages,
             supported_task_types, supported_plan_types, limitations,
             estimated_speed, estimated_cost, context_window, parallelism,
             requires_network, requires_filesystem, requires_git,
             requires_python, requires_shell, confidence, version, status,
             schema_version, created_at, updated_at, availability, manifest_ref,
             worker_kind)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            name=excluded.name, kind=excluded.kind, description=excluded.description,
            capabilities=excluded.capabilities,
            supported_languages=excluded.supported_languages,
            supported_task_types=excluded.supported_task_types,
            supported_plan_types=excluded.supported_plan_types,
            limitations=excluded.limitations, estimated_speed=excluded.estimated_speed,
            estimated_cost=excluded.estimated_cost,
            context_window=excluded.context_window, parallelism=excluded.parallelism,
            requires_network=excluded.requires_network,
            requires_filesystem=excluded.requires_filesystem,
            requires_git=excluded.requires_git, requires_python=excluded.requires_python,
            requires_shell=excluded.requires_shell, confidence=excluded.confidence,
            version=excluded.version, status=excluded.status,
            schema_version=excluded.schema_version, updated_at=excluded.updated_at,
            availability=excluded.availability, manifest_ref=excluded.manifest_ref,
            worker_kind=excluded.worker_kind
        """,
        (
            w.id, w.name, w.kind, w.description, w.capabilities,
            w.supported_languages, w.supported_task_types, w.supported_plan_types,
            w.limitations, w.estimated_speed, w.estimated_cost, w.context_window,
            w.parallelism, int(w.requires_network), int(w.requires_filesystem),
            int(w.requires_git), int(w.requires_python), int(w.requires_shell),
            w.confidence, w.version, w.status, w.schema_version,
            w.created_at, w.updated_at, w.availability, w.manifest_ref,
            w.worker_kind,
        ),
    )
    # Re-sync normalized capability rows.
    conn.execute(
        "DELETE FROM worker_capabilities WHERE worker_id = ?", (w.id,)
    )
    conn.executemany(
        "INSERT OR IGNORE INTO worker_capabilities (worker_id, capability) "
        "VALUES (?, ?)",
        [(w.id, c) for c in (w.capabilities.split(",") if w.capabilities else [])],
    )
    commit_if_top(conn)


def replace_worker_capabilities(
    conn: sqlite3.Connection, worker_id: str, capabilities: list[str]
) -> None:
    conn.execute(
        "DELETE FROM worker_capabilities WHERE worker_id = ?", (worker_id,)
    )
    conn.executemany(
        "INSERT OR IGNORE INTO worker_capabilities (worker_id, capability) "
        "VALUES (?, ?)",
        [(worker_id, c) for c in capabilities],
    )
    conn.commit()


def get_worker(conn: sqlite3.Connection, wid: str) -> Optional[WorkerRow]:
    row = conn.execute("SELECT * FROM workers WHERE id = ?", (wid,)).fetchone()
    return _row_to_worker(row) if row else None


def get_worker_by_name(conn: sqlite3.Connection, name: str) -> Optional[WorkerRow]:
    row = conn.execute(
        "SELECT * FROM workers WHERE name = ?", (name,)
    ).fetchone()
    return _row_to_worker(row) if row else None


def get_all_workers(conn: sqlite3.Connection) -> List[WorkerRow]:
    rows = conn.execute("SELECT * FROM workers ORDER BY name").fetchall()
    return [_row_to_worker(r) for r in rows]


def count_workers(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS c FROM workers").fetchone()
    return row["c"] if row else 0


def workers_with_capability(
    conn: sqlite3.Connection, capability: str
) -> List[WorkerRow]:
    """Capability Resolver query hook: every worker exposing `capability`.
    Case-insensitive (the vocabulary stores canonical-capitalized forms)."""
    rows = conn.execute(
        """
        SELECT w.* FROM workers w
        JOIN worker_capabilities c ON c.worker_id = w.id
        WHERE LOWER(c.capability) = LOWER(?)
        ORDER BY w.name
        """,
        (capability,),
    ).fetchall()
    return [_row_to_worker(r) for r in rows]


def update_worker_status(
    conn: sqlite3.Connection, wid: str, status: str
) -> None:
    """The only live-row mutation: enable/disable a worker. History keeps the
    prior version forever."""
    conn.execute("UPDATE workers SET status = ? WHERE id = ?", (status, wid))
    conn.commit()


def update_worker_version(
    conn: sqlite3.Connection, wid: str, version: str
) -> None:
    """Advance a worker's live version (the only other live-row mutation)."""
    conn.execute("UPDATE workers SET version = ? WHERE id = ?", (version, wid))
    conn.commit()


def update_worker_availability(conn: sqlite3.Connection, worker_id: str,
                               availability: str) -> None:
    """Update ONLY the availability column (runtime install state). Distinct
    from `status` (active/disabled); availability is available|unavailable|error."""
    conn.execute(
        "UPDATE workers SET availability = ? WHERE id = ?",
        (availability, worker_id))
    conn.commit()


def insert_worker_history(conn: sqlite3.Connection, rows: List[WorkerHistoryRow]) -> None:
    """Append a snapshot of worker state per registration/upgrade/disable event.
    Idempotent on (registered_at, worker_id)."""
    for r in rows:
        conn.execute(
            """
            INSERT OR REPLACE INTO worker_history
                (registered_at, worker_id, name, kind, version, status,
                 capabilities, limitations, event_type, note)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                r.registered_at, r.worker_id, r.name, r.kind, r.version,
                r.status, r.capabilities, r.limitations, r.event_type, r.note,
            ),
        )
    conn.commit()


def insert_worker_version(conn: sqlite3.Connection, rows: List[WorkerVersionRow]) -> None:
    """Append a version record. Idempotent on (worker_id, version)."""
    for r in rows:
        conn.execute(
            """
            INSERT OR IGNORE INTO worker_versions
                (worker_id, version, registered_at, changelog)
            VALUES (?, ?, ?, ?)
            """,
            (r.worker_id, r.version, r.registered_at, r.changelog),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Worker Genesis: proposed_workers table access
# ---------------------------------------------------------------------------


@dataclass
class ProposedWorkerRow:
    """One proposed worker (capability gap proposal awaiting review)."""
    id: str
    detected_from_goal: str
    capability_gap: str
    draft_manifest_json: str
    status: str  # pending | approved | rejected
    created_at: str
    reviewed_at: Optional[str] = None


def insert_proposed_worker(
    conn: sqlite3.Connection, pw: ProposedWorkerRow
) -> None:
    """Insert a proposed worker. Idempotent on id.

    Uses commit_if_top so this function can be safely called from within
    an outer atomic() transaction block without prematurely committing.
    """
    conn.execute(
        """
        INSERT OR IGNORE INTO proposed_workers
            (id, detected_from_goal, capability_gap, draft_manifest_json,
             status, created_at, reviewed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            pw.id, pw.detected_from_goal, pw.capability_gap,
            pw.draft_manifest_json, pw.status, pw.created_at, pw.reviewed_at,
        ),
    )
    commit_if_top(conn)


def get_proposed_worker(
    conn: sqlite3.Connection, pid: str
) -> Optional[ProposedWorkerRow]:
    row = conn.execute(
        "SELECT * FROM proposed_workers WHERE id = ?", (pid,)
    ).fetchone()
    if row is None:
        return None
    return ProposedWorkerRow(
        id=row["id"], detected_from_goal=row["detected_from_goal"],
        capability_gap=row["capability_gap"],
        draft_manifest_json=row["draft_manifest_json"],
        status=row["status"], created_at=row["created_at"],
        reviewed_at=row["reviewed_at"],
    )


def get_proposed_workers(
    conn: sqlite3.Connection, status: Optional[str] = None
) -> List[ProposedWorkerRow]:
    """Get proposed workers, optionally filtered by status."""
    if status:
        rows = conn.execute(
            "SELECT * FROM proposed_workers WHERE status = ? ORDER BY created_at DESC",
            (status,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM proposed_workers ORDER BY created_at DESC"
        ).fetchall()
    return [
        ProposedWorkerRow(
            id=r["id"], detected_from_goal=r["detected_from_goal"],
            capability_gap=r["capability_gap"],
            draft_manifest_json=r["draft_manifest_json"],
            status=r["status"], created_at=r["created_at"],
            reviewed_at=r["reviewed_at"],
        )
        for r in rows
    ]


def update_proposed_worker_status(
    conn: sqlite3.Connection, pid: str, status: str,
    reviewed_at: Optional[str] = None,
) -> None:
    """Update a proposal's status (pending -> approved | rejected)."""
    if reviewed_at is None:
        reviewed_at = now_iso()
    conn.execute(
        "UPDATE proposed_workers SET status = ?, reviewed_at = ? WHERE id = ?",
        (status, reviewed_at, pid),
    )
    commit_if_top(conn)


def delete_proposed_worker(
    conn: sqlite3.Connection, pid: str
) -> None:
    """Remove a proposed worker by id (for cleanup)."""
    conn.execute("DELETE FROM proposed_workers WHERE id = ?", (pid,))
    commit_if_top(conn)


# ---------------------------------------------------------------------------
# Operator Identity: operator_preferences (Phase 1 — model + explicit CLI only)
# ---------------------------------------------------------------------------


@dataclass
class OperatorPreferenceRow:
    """One operator preference (explicitly set or evidence-derived)."""
    key: str
    value: str
    set_at: str
    source: str  # 'explicit' | 'derived'


def set_operator_preference(
    conn: sqlite3.Connection, key: str, value: str,
    source: str = "explicit",
) -> None:
    """Insert or update one operator preference.

    Uses source='explicit' for `friday profile set` commands; source='derived'
    for evidence-computed fields (never triggered by inference — see operator.py
    for the derive-on-read-only discipline).

    Records the change in profile_history for audit. The old value is captured
    before the upsert so the history shows the actual transition. When the value
    is unchanged, no history row is written (avoids log noise on re-derivation).

    Use commit_if_top for safe composition inside atomic() blocks.
    """
    # Capture the old value before upsert.
    old_row = conn.execute(
        "SELECT value FROM operator_preferences WHERE key = ?",
        (key,),
    ).fetchone()
    old_value = old_row["value"] if old_row else None

    conn.execute(
        """
        INSERT INTO operator_preferences (key, value, set_at, source)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            value=excluded.value, set_at=excluded.set_at, source=excluded.source
        """,
        (key, value, now_iso(), source),
    )

    # Write to profile_history only when the value actually changed.
    if old_value != value:
        conn.execute(
            "INSERT INTO profile_history (key, old_value, new_value, source, changed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (key, old_value, value, source, now_iso()),
        )

    commit_if_top(conn)


def get_operator_preference(
    conn: sqlite3.Connection, key: str
) -> Optional[OperatorPreferenceRow]:
    """Get one preference by key, or None."""
    row = conn.execute(
        "SELECT * FROM operator_preferences WHERE key = ?", (key,)
    ).fetchone()
    if row is None:
        return None
    return OperatorPreferenceRow(
        key=row["key"], value=row["value"],
        set_at=row["set_at"], source=row["source"],
    )


def get_all_operator_preferences(
    conn: sqlite3.Connection, source: Optional[str] = None
) -> list[OperatorPreferenceRow]:
    """Get all operator preferences, optionally filtered by source."""
    if source:
        rows = conn.execute(
            "SELECT * FROM operator_preferences WHERE source = ? ORDER BY key",
            (source,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM operator_preferences ORDER BY key"
        ).fetchall()
    return [
        OperatorPreferenceRow(
            key=r["key"], value=r["value"],
            set_at=r["set_at"], source=r["source"],
        )
        for r in rows
    ]


def unset_operator_preference(conn: sqlite3.Connection, key: str) -> bool:
    """Delete one preference. Returns True if a row was actually removed."""
    cur = conn.execute(
        "DELETE FROM operator_preferences WHERE key = ?", (key,)
    )
    commit_if_top(conn)
    return cur.rowcount > 0


# ===========================================================================
# Phase A: Conversation Log — append-only IdentityEngine exchange log.
# ===========================================================================


@dataclass
class ConversationLogRow:
    id: int
    channel: str
    channel_id: str
    routing: str
    user_message: str
    friday_reply: str
    conversation_at: str
    processed: int


def log_exchange(
    conn: sqlite3.Connection,
    channel: str,
    channel_id: str,
    user_message: str,
    friday_reply: str,
    routing: str = "",
) -> int:
    """Append one exchange to the conversation_log.

    Returns the row id of the newly inserted row.
    """
    import datetime as dt

    cur = conn.execute(
        """INSERT INTO conversation_log
           (channel, channel_id, routing, user_message, friday_reply, conversation_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (channel, channel_id, routing, user_message, friday_reply,
         dt.datetime.now(dt.timezone.utc).isoformat()),
    )
    commit_if_top(conn)
    return cur.lastrowid or 0


def get_conversation_history(
    conn: sqlite3.Connection,
    limit: int = 50,
    channel: str | None = None,
    unprocessed_only: bool = False,
) -> list[ConversationLogRow]:
    """Fetch recent conversation log entries, newest first.

    Args:
        limit: Max rows to return.
        channel: Optional channel filter ("telegram", "slack", "cli", etc.).
        unprocessed_only: If True, only return entries where processed=0.
    """
    clauses = []
    params = []
    if channel:
        clauses.append("channel = ?")
        params.append(channel)
    if unprocessed_only:
        clauses.append("processed = 0")
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    rows = conn.execute(
        f"""SELECT id, channel, channel_id, routing, user_message, friday_reply,
                  conversation_at, processed
           FROM conversation_log
           {where}
           ORDER BY conversation_at DESC
           LIMIT ?""",
        (*params, limit),
    ).fetchall()
    return [ConversationLogRow(**r) for r in rows]


def get_unprocessed_conversations(
    conn: sqlite3.Connection, limit: int = 100
) -> list[ConversationLogRow]:
    """Fetch conversation_log entries that haven't been LLM-extracted yet."""
    return get_conversation_history(conn, limit=limit, unprocessed_only=True)


def mark_conversation_processed(conn: sqlite3.Connection, log_id: int) -> None:
    """Mark a conversation_log entry as processed by the LLM extraction."""
    conn.execute(
        "UPDATE conversation_log SET processed = 1 WHERE id = ?",
        (log_id,),
    )
    commit_if_top(conn)


def worker_history_for(
    conn: sqlite3.Connection, wid: str
) -> List[WorkerHistoryRow]:
    """Every snapshot of one worker, newest first."""
    rows = conn.execute(
        "SELECT * FROM worker_history WHERE worker_id = ? ORDER BY registered_at DESC",
        (wid,),
    ).fetchall()
    return [_row_to_worker_history(r) for r in rows]


def worker_versions_for(
    conn: sqlite3.Connection, wid: str
) -> List[WorkerVersionRow]:
    rows = conn.execute(
        "SELECT * FROM worker_versions WHERE worker_id = ? ORDER BY registered_at",
        (wid,),
    ).fetchall()
    return [_row_to_worker_version(r) for r in rows]


def _row_to_worker(r) -> WorkerRow:
    return WorkerRow(
        id=r["id"], name=r["name"], kind=r["kind"],
        description=r["description"] or "",
        capabilities=r["capabilities"] or "",
        supported_languages=r["supported_languages"] or "",
        supported_task_types=r["supported_task_types"] or "",
        supported_plan_types=r["supported_plan_types"] or "",
        limitations=r["limitations"] or "",
        estimated_speed=r["estimated_speed"] or "",
        estimated_cost=r["estimated_cost"] or "",
        context_window=r["context_window"] or 0,
        parallelism=r["parallelism"] or 1,
        requires_network=bool(r["requires_network"]),
        requires_filesystem=bool(r["requires_filesystem"]),
        requires_git=bool(r["requires_git"]),
        requires_python=bool(r["requires_python"]),
        requires_shell=bool(r["requires_shell"]),
        confidence=r["confidence"] or "medium",
        version=r["version"] or "1.0.0",
        status=r["status"] or "active",
        created_at=r["created_at"] or "",
        updated_at=r["updated_at"] or "",
        availability=r["availability"] or "available",
        manifest_ref=r["manifest_ref"] if "manifest_ref" in r.keys() else None,
        worker_kind=(r["worker_kind"] if "worker_kind" in r.keys() else "function") or "function",
    )


def _row_to_worker_history(r) -> WorkerHistoryRow:
    return WorkerHistoryRow(
        registered_at=r["registered_at"], worker_id=r["worker_id"],
        name=r["name"], kind=r["kind"], version=r["version"],
        status=r["status"], capabilities=r["capabilities"] or "",
        limitations=r["limitations"] or "", event_type=r["event_type"] or "registered",
        note=r["note"],
    )


def _row_to_worker_version(r) -> WorkerVersionRow:
    return WorkerVersionRow(
        worker_id=r["worker_id"], version=r["version"],
        registered_at=r["registered_at"], changelog=r["changelog"],
    )


# ===========================================================================
# M9.3 Capability Resolver — persistence helpers (append-only history)
# ===========================================================================

@dataclass
class ResolverAssignmentRow:
    """One persisted Task -> Worker assignment (resolver_assignments)."""
    assignment_id: str
    graph_id: str
    task_id: str
    worker_id: Optional[str]
    status: str
    confidence: str
    reason: str
    matched_capabilities: str
    missing_capabilities: str
    selection_strategy: str
    schema_version: str
    created_at: str
    updated_at: str


def insert_resolver_assignment(conn: sqlite3.Connection, row: dict) -> None:
    """Insert one assignment, or UPDATE an existing one in place.

    Uses UPDATE (not INSERT OR REPLACE) on re-resolution so the original row id
    is preserved — INSERT OR REPLACE would DELETE+INSERT, which cascades to
    resolver_history and breaks append-only history. The assignment's prior
    state lives in history; the live row is simply advanced.
    """
    existing = conn.execute(
        "SELECT 1 FROM resolver_assignments WHERE assignment_id = ?",
        (row["assignment_id"],),
    ).fetchone()
    if existing is None:
        conn.execute(
            """
            INSERT INTO resolver_assignments
                (assignment_id, graph_id, task_id, worker_id, status, confidence,
                 reason, matched_capabilities, missing_capabilities,
                 selection_strategy, schema_version, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (row["assignment_id"], row["graph_id"], row["task_id"], row["worker_id"],
             row["status"], row["confidence"], row["reason"],
             row["matched_capabilities"], row["missing_capabilities"],
             row["selection_strategy"], row["schema_version"],
             row["created_at"], row["updated_at"]),
        )
    else:
        conn.execute(
            """
            UPDATE resolver_assignments
                SET worker_id = ?, status = ?, confidence = ?, reason = ?,
                    matched_capabilities = ?, missing_capabilities = ?,
                    selection_strategy = ?, schema_version = ?, updated_at = ?
                WHERE assignment_id = ?
            """,
            (row["worker_id"], row["status"], row["confidence"], row["reason"],
             row["matched_capabilities"], row["missing_capabilities"],
             row["selection_strategy"], row["schema_version"],
             row["updated_at"], row["assignment_id"]),
        )


def insert_resolver_history(conn: sqlite3.Connection, row: dict) -> None:
    """Append one resolution-run snapshot (append-only, never updated)."""
    conn.execute(
        """
        INSERT INTO resolver_history
            (resolved_at, assignment_id, graph_id, task_id, worker_id, status,
             confidence, score_total, matched_capabilities,
             missing_capabilities, selection_strategy)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (row["resolved_at"], row["assignment_id"], row["graph_id"],
         row["task_id"], row["worker_id"], row["status"], row["confidence"],
         row["score_total"], row["matched_capabilities"],
         row["missing_capabilities"], row["selection_strategy"]),
    )


def insert_resolver_evolution(conn: sqlite3.Connection, row: dict) -> None:
    """Record one assignment-change event (append-only)."""
    conn.execute(
        """
        INSERT INTO resolver_evolution
            (evolved_at, graph_id, task_id, from_worker_id, to_worker_id,
             change_type, reason)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (row["evolved_at"], row["graph_id"], row["task_id"],
         row["from_worker_id"], row["to_worker_id"], row["change_type"],
         row["reason"]),
    )


def get_resolver_assignments(conn: sqlite3.Connection,
                             graph_id: Optional[str] = None
                             ) -> List[ResolverAssignmentRow]:
    if graph_id is None:
        rows = conn.execute(
            "SELECT * FROM resolver_assignments ORDER BY graph_id, task_id"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM resolver_assignments WHERE graph_id = ? "
            "ORDER BY task_id", (graph_id,)).fetchall()
    return [_row_to_resolver_assignment(r) for r in rows]


def get_resolver_assignment(conn: sqlite3.Connection,
                           assignment_id: str) -> Optional[ResolverAssignmentRow]:
    row = conn.execute(
        "SELECT * FROM resolver_assignments WHERE assignment_id = ?",
        (assignment_id,)).fetchone()
    return _row_to_resolver_assignment(row) if row else None


def get_resolver_assignment_by_task(conn: sqlite3.Connection,
                                    task_id: str
                                    ) -> Optional[ResolverAssignmentRow]:
    """Lookup a resolver assignment by its task id (not assignment_id).

    Orders by `updated_at` (the live row's recency); `resolver_assignments`
    has no `resolved_at` column — per-run recency lives in `resolver_history`.
    """
    row = conn.execute(
        "SELECT * FROM resolver_assignments WHERE task_id = ? "
        "ORDER BY updated_at DESC", (task_id,)).fetchone()
    return _row_to_resolver_assignment(row) if row else None


def get_resolver_history(conn: sqlite3.Connection,
                        assignment_id: Optional[str] = None
                        ) -> list:
    if assignment_id is None:
        rows = conn.execute(
            "SELECT * FROM resolver_history ORDER BY resolved_at"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM resolver_history WHERE assignment_id = ? "
            "ORDER BY resolved_at", (assignment_id,)).fetchall()
    return [dict(r) for r in rows]


def get_resolver_evolution(conn: sqlite3.Connection,
                          graph_id: Optional[str] = None) -> list:
    if graph_id is None:
        rows = conn.execute(
            "SELECT * FROM resolver_evolution ORDER BY evolved_at"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM resolver_evolution WHERE graph_id = ? "
            "ORDER BY evolved_at", (graph_id,)).fetchall()
    return [dict(r) for r in rows]


def count_resolver_assignments(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS c FROM resolver_assignments").fetchone()
    return row["c"] if row else 0


def _row_to_resolver_assignment(r) -> ResolverAssignmentRow:
    return ResolverAssignmentRow(
        assignment_id=r["assignment_id"], graph_id=r["graph_id"],
        task_id=r["task_id"], worker_id=r["worker_id"], status=r["status"],
        confidence=r["confidence"], reason=r["reason"] or "",
        matched_capabilities=r["matched_capabilities"] or "[]",
        missing_capabilities=r["missing_capabilities"] or "[]",
        selection_strategy=r["selection_strategy"],
        schema_version=r["schema_version"] if "schema_version" in r.keys() else "1.0",
        created_at=r["created_at"], updated_at=r["updated_at"],
    )


# ===========================================================================
# M9.4 Task Scheduler — persistence helpers (append-only history/evolution)
# ===========================================================================

@dataclass
class SchedulerTaskRow:
    """One persisted scheduled task (scheduler_tasks)."""
    schedule_id: str
    graph_id: str
    assignment_id: str
    task_id: str
    worker_id: Optional[str]
    phase: str
    status: str
    priority: int
    wave: int
    dependency_count: int
    estimated_start: Optional[int]
    estimated_finish: Optional[int]
    blocked_reason: str
    confidence: str
    selection_strategy: str
    schema_version: str
    created_at: str
    updated_at: str


def insert_scheduler_run(conn: sqlite3.Connection, row: dict) -> None:
    """Insert or replace one scheduler run record (one per graph run)."""
    conn.execute(
        """
        INSERT INTO scheduler_runs
            (run_id, graph_id, goal, wave_count, task_count,
             critical_path_length, max_parallelism, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(run_id) DO UPDATE SET
            goal=excluded.goal, wave_count=excluded.wave_count,
            task_count=excluded.task_count,
            critical_path_length=excluded.critical_path_length,
            max_parallelism=excluded.max_parallelism,
            status=excluded.status, updated_at=excluded.updated_at
        """,
        (row["run_id"], row["graph_id"], row["goal"], row["wave_count"],
         row["task_count"], row["critical_path_length"],
         row["max_parallelism"], row["status"],
         row["created_at"], row["updated_at"]),
    )


def insert_scheduler_task(conn: sqlite3.Connection, row: dict) -> None:
    """Insert one scheduled task, or UPDATE an existing one in place.

    UPDATE (not INSERT OR REPLACE) preserves the row id so scheduler_history
    (FK ON DELETE SET NULL) is never cascade-deleted. Append-only history keeps
    the prior state.
    """
    existing = conn.execute(
        "SELECT 1 FROM scheduler_tasks WHERE schedule_id = ?",
        (row["schedule_id"],),
    ).fetchone()
    if existing is None:
        conn.execute(
            """
            INSERT INTO scheduler_tasks
                (schedule_id, graph_id, assignment_id, task_id, worker_id,
                 phase, status, priority, wave, dependency_count,
                 estimated_start, estimated_finish, blocked_reason,
                 confidence, selection_strategy, schema_version,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (row["schedule_id"], row["graph_id"], row["assignment_id"],
             row["task_id"], row["worker_id"], row["phase"], row["status"],
             row["priority"], row["wave"], row["dependency_count"],
             row["estimated_start"], row["estimated_finish"],
             row["blocked_reason"], row["confidence"],
             row["selection_strategy"], row["schema_version"],
             row["created_at"], row["updated_at"]),
        )
    else:
        conn.execute(
            """
            UPDATE scheduler_tasks
                SET graph_id = ?, assignment_id = ?, worker_id = ?,
                    phase = ?, status = ?, priority = ?, wave = ?,
                    dependency_count = ?, estimated_start = ?,
                    estimated_finish = ?, blocked_reason = ?,
                    confidence = ?, selection_strategy = ?, schema_version = ?,
                    updated_at = ?
                WHERE schedule_id = ?
            """,
            (row["graph_id"], row["assignment_id"], row["worker_id"],
             row["phase"], row["status"], row["priority"], row["wave"],
             row["dependency_count"], row["estimated_start"],
             row["estimated_finish"], row["blocked_reason"], row["confidence"],
             row["selection_strategy"], row["schema_version"],
             row["updated_at"], row["schedule_id"]),
        )


def insert_scheduler_history(conn: sqlite3.Connection, row: dict) -> None:
    """Append one scheduling-run snapshot (append-only, never updated)."""
    conn.execute(
        """
        INSERT INTO scheduler_history
            (scheduled_at, schedule_id, graph_id, task_id, worker_id, wave,
             status, priority, assignment_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (row["scheduled_at"], row["schedule_id"], row["graph_id"],
         row["task_id"], row["worker_id"], row["wave"], row["status"],
         row["priority"], row["assignment_id"]),
    )


def insert_scheduler_evolution(conn: sqlite3.Connection, row: dict) -> None:
    """Record one scheduler decision change (append-only)."""
    conn.execute(
        """
        INSERT INTO scheduler_evolution
            (evolved_at, schedule_id, graph_id, task_id, from_wave, to_wave,
             from_state, to_state, change_type, reason)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (row["evolved_at"], row["schedule_id"], row["graph_id"],
         row["task_id"], row["from_wave"], row["to_wave"],
         row["from_state"], row["to_state"], row["change_type"],
         row["reason"]),
    )


def get_scheduler_tasks(conn: sqlite3.Connection,
                        graph_id: Optional[str] = None) -> List[dict]:
    if graph_id is None:
        rows = conn.execute(
            "SELECT * FROM scheduler_tasks ORDER BY graph_id, wave, priority DESC, task_id"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM scheduler_tasks WHERE graph_id = ? "
            "ORDER BY wave, priority DESC, task_id", (graph_id,)).fetchall()
    return [dict(r) for r in rows]


def get_scheduler_task(conn: sqlite3.Connection, schedule_id: str) -> Optional[dict]:
    row = conn.execute(
        "SELECT * FROM scheduler_tasks WHERE schedule_id = ?",
        (schedule_id,)).fetchone()
    return dict(row) if row else None


def get_scheduler_runs(conn: sqlite3.Connection,
                       graph_id: Optional[str] = None) -> List[dict]:
    if graph_id is None:
        rows = conn.execute(
            "SELECT * FROM scheduler_runs ORDER BY created_at DESC"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM scheduler_runs WHERE graph_id = ? "
            "ORDER BY created_at DESC", (graph_id,)).fetchall()
    return [dict(r) for r in rows]


def get_scheduler_history(conn: sqlite3.Connection,
                          graph_id: Optional[str] = None) -> List[dict]:
    if graph_id is None:
        rows = conn.execute(
            "SELECT * FROM scheduler_history ORDER BY scheduled_at"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM scheduler_history WHERE graph_id = ? "
            "ORDER BY scheduled_at", (graph_id,)).fetchall()
    return [dict(r) for r in rows]


def get_scheduler_evolution(conn: sqlite3.Connection,
                            graph_id: Optional[str] = None) -> List[dict]:
    if graph_id is None:
        rows = conn.execute(
            "SELECT * FROM scheduler_evolution ORDER BY evolved_at"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM scheduler_evolution WHERE graph_id = ? "
            "ORDER BY evolved_at", (graph_id,)).fetchall()
    return [dict(r) for r in rows]


# ===========================================================================
# M9.5 Execution Runtime — persistence helpers
# ===========================================================================

def insert_runtime_session(conn: sqlite3.Connection, row: dict) -> None:
    conn.execute(
        """
        INSERT INTO runtime_sessions
            (session_id, schedule_id, state, started_at, finished_at,
             schema_version, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (row["session_id"], row["schedule_id"], row["state"],
         row["started_at"], row.get("finished_at"),
         row.get("schema_version", "1.0"),
         row["created_at"], row["updated_at"]),
    )


def update_runtime_session(conn: sqlite3.Connection, row: dict) -> None:
    conn.execute(
        """
        UPDATE runtime_sessions
        SET state = ?, finished_at = ?, updated_at = ?
        WHERE session_id = ?
        """,
        (row["state"], row.get("finished_at"), row["updated_at"],
         row["session_id"]),
    )


def get_runtime_sessions(conn: sqlite3.Connection,
                         schedule_id: Optional[str] = None) -> List[dict]:
    if schedule_id is None:
        rows = conn.execute(
            "SELECT * FROM runtime_sessions ORDER BY created_at DESC"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM runtime_sessions WHERE schedule_id = ? "
            "ORDER BY created_at DESC", (schedule_id,)).fetchall()
    return [dict(r) for r in rows]


def get_runtime_session(conn: sqlite3.Connection,
                        session_id: str) -> Optional[dict]:
    row = conn.execute(
        "SELECT * FROM runtime_sessions WHERE session_id = ?",
        (session_id,)).fetchone()
    return dict(row) if row else None


def insert_runtime_event(conn: sqlite3.Connection, row: dict) -> None:
    conn.execute(
        """
        INSERT INTO runtime_events
            (event_id, session_id, kind, task_id, worker_id, detail, at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (row["event_id"], row["session_id"], row["kind"],
         row.get("task_id", ""), row.get("worker_id"),
         row.get("detail", ""), row["at"]),
    )


def get_runtime_events(conn: sqlite3.Connection,
                       session_id: str) -> List[dict]:
    rows = conn.execute(
        "SELECT * FROM runtime_events WHERE session_id = ? ORDER BY eid",
        (session_id,)).fetchall()
    return [dict(r) for r in rows]


def insert_runtime_task(conn: sqlite3.Connection, row: dict) -> None:
    """Insert or UPDATE a task's latest state in place (the only mutable
    runtime table). A crash mid-run leaves a consistent last-known state."""
    existing = conn.execute(
        "SELECT 1 FROM runtime_tasks WHERE execution_id = ?",
        (row["execution_id"],)).fetchone()
    if existing is None:
        conn.execute(
            """
            INSERT INTO runtime_tasks
                (execution_id, session_id, schedule_id, task_id, worker_id,
                 wave, attempt, status, started_at, finished_at, duration_ms,
                 exit_code, error, output_reference, schema_version,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (row["execution_id"], row["session_id"], row["schedule_id"],
             row["task_id"], row.get("worker_id"),
             row.get("wave", 1), row.get("attempt", 1), row["status"],
             row.get("started_at"), row.get("finished_at"),
             row.get("duration_ms"), row.get("exit_code"), row.get("error", ""),
             row.get("output_reference"),
             row.get("schema_version", "1.0"),
             row["created_at"], row["updated_at"]),
        )
    else:
        conn.execute(
            """
            UPDATE runtime_tasks
            SET session_id = ?, schedule_id = ?, worker_id = ?, wave = ?,
                attempt = ?, status = ?, started_at = ?, finished_at = ?,
                duration_ms = ?, exit_code = ?, error = ?, output_reference = ?,
                updated_at = ?
            WHERE execution_id = ?
            """,
            (row["session_id"], row["schedule_id"], row.get("worker_id"),
             row.get("wave", 1), row.get("attempt", 1), row["status"],
             row.get("started_at"), row.get("finished_at"),
             row.get("duration_ms"), row.get("exit_code"), row.get("error", ""),
             row.get("output_reference"), row["updated_at"],
             row["execution_id"]),
        )


def get_runtime_tasks(conn: sqlite3.Connection,
                      session_id: Optional[str] = None) -> List[dict]:
    if session_id is None:
        rows = conn.execute(
            "SELECT * FROM runtime_tasks ORDER BY session_id, wave, task_id"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM runtime_tasks WHERE session_id = ? "
            "ORDER BY wave, task_id", (session_id,)).fetchall()
    return [dict(r) for r in rows]


def get_runtime_task(conn: sqlite3.Connection,
                     execution_id: str) -> Optional[dict]:
    row = conn.execute(
        "SELECT * FROM runtime_tasks WHERE execution_id = ?",
        (execution_id,)).fetchone()
    return dict(row) if row else None


def insert_runtime_result(conn: sqlite3.Connection, row: dict) -> None:
    """Append-only outcome of one execution attempt."""
    conn.execute(
        """
        INSERT INTO runtime_results
            (execution_id, session_id, task_id, worker_id, success, stdout,
             stderr, artifacts, exit_code, duration_ms, error, payload,
             verification_passed, verification_evidence, recorded_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (row["execution_id"], row["session_id"], row["task_id"],
         row.get("worker_id"),
         1 if row.get("success") else 0,
         row.get("stdout", ""), row.get("stderr", ""), row.get("artifacts", "[]"),
         row.get("exit_code"), row.get("duration_ms", 0), row.get("error", ""),
         row.get("payload"),
         row.get("verification_passed"), row.get("verification_evidence", "{}"),
         row["recorded_at"]),
    )


def get_runtime_results(conn: sqlite3.Connection,
                        session_id: str) -> List[dict]:
    rows = conn.execute(
        "SELECT * FROM runtime_results WHERE session_id = ? ORDER BY result_id",
        (session_id,)).fetchall()
    return [dict(r) for r in rows]


def insert_runtime_history(conn: sqlite3.Connection, row: dict) -> None:
    conn.execute(
        """
        INSERT INTO runtime_history
            (session_id, schedule_id, task_id, worker_id, status, attempt, at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (row["session_id"], row["schedule_id"], row["task_id"],
         row.get("worker_id"), row["status"], row.get("attempt", 1),
         row["at"]),
    )


def get_runtime_history(conn: sqlite3.Connection,
                        session_id: Optional[str] = None) -> List[dict]:
    if session_id is None:
        rows = conn.execute(
            "SELECT * FROM runtime_history ORDER BY hid"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM runtime_history WHERE session_id = ? ORDER BY hid",
            (session_id,)).fetchall()
    return [dict(r) for r in rows]


def insert_runtime_evolution(conn: sqlite3.Connection, row: dict) -> None:
    conn.execute(
        """
        INSERT INTO runtime_evolution
            (evolved_at, session_id, task_id, from_state, to_state,
             change_type, reason)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (row["evolved_at"], row["session_id"], row["task_id"],
         row.get("from_state"), row.get("to_state"),
         row["change_type"], row.get("reason", "")),
    )


def get_runtime_evolution(conn: sqlite3.Connection,
                          session_id: Optional[str] = None) -> List[dict]:
    if session_id is None:
        rows = conn.execute(
            "SELECT * FROM runtime_evolution ORDER BY evolved_at"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM runtime_evolution WHERE session_id = ? "
            "ORDER BY evolved_at", (session_id,)).fetchall()
    return [dict(r) for r in rows]


# ===========================================================================
# Meta-Engine storage (Phase 7) — capability gaps + self-improvement runs.
# ===========================================================================


def get_capability_gaps(conn, status=None):
    if status:
        rows = conn.execute(
            "SELECT * FROM capability_gaps WHERE status = ? ORDER BY score DESC",
            (status,)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM capability_gaps ORDER BY score DESC").fetchall()
    return [dict(r) for r in rows]


def get_capability_gap(conn, gap_id):
    row = conn.execute(
        "SELECT * FROM capability_gaps WHERE id = ?", (gap_id,)).fetchone()
    return dict(row) if row else None


def insert_capability_gap(conn, row):
    from datetime import datetime, timezone
    cur = conn.execute(
        """INSERT INTO capability_gaps
           (description, evidence_refs, frequency, score, status,
            attempt_count, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (row["description"], row.get("evidence_refs", "[]"),
         row.get("frequency", 0), row.get("score", 0.0),
         row.get("status", "open"), row.get("attempt_count", 0),
         row.get("created_at", datetime.now(timezone.utc).isoformat()),
         row.get("updated_at", datetime.now(timezone.utc).isoformat())))
    conn.commit()
    return cur.lastrowid


def update_capability_gap(conn, gap_id, **kw):
    sets = ", ".join(f"{k} = ?" for k in kw)
    vals = list(kw.values()) + [gap_id]
    conn.execute(f"UPDATE capability_gaps SET {sets} WHERE id = ?", vals)
    conn.commit()


def get_si_runs(conn, gap_id=None):
    if gap_id is not None:
        rows = conn.execute(
            "SELECT * FROM self_improvement_runs WHERE gap_id = ? ORDER BY created_at DESC",
            (gap_id,)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM self_improvement_runs ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


def get_si_run(conn, run_id):
    row = conn.execute(
        "SELECT * FROM self_improvement_runs WHERE id = ?", (run_id,)).fetchone()
    return dict(row) if row else None


def insert_si_run(conn, row):
    from datetime import datetime, timezone
    cur = conn.execute(
        """INSERT INTO self_improvement_runs
           (gap_id, plan_id, sandbox_path, diff_path, verification_result,
            verification_log, deployed, human_approved, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (row["gap_id"], row.get("plan_id", ""), row.get("sandbox_path", ""),
         row.get("diff_path", ""), row.get("verification_result", "{}"),
         row.get("verification_log", ""),
         1 if row.get("deployed") else 0,
         1 if row.get("human_approved") else 0,
         row.get("created_at", datetime.now(timezone.utc).isoformat()),
         row.get("updated_at", datetime.now(timezone.utc).isoformat())))
    conn.commit()
    return cur.lastrowid


def update_si_run(conn, run_id, **kw):
    sets = ", ".join(f"{k} = ?" for k in kw)
    vals = list(kw.values()) + [run_id]
    conn.execute(f"UPDATE self_improvement_runs SET {sets} WHERE id = ?", vals)
    conn.commit()


# ===========================================================================
# Pillar B Stage 2 — Sequence Mining (mined_patterns table helpers)
# ===========================================================================


def insert_mined_pattern(conn, row):
    """Persist one mined pattern row. Returns the new row id."""
    from datetime import datetime, timezone
    cur = conn.execute(
        """INSERT INTO mined_patterns
           (sequence_json, count, distinct_sessions, first_seen, last_seen,
            common_workspace, common_project, confidence, mined_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (row["sequence_json"], row.get("count", 0),
         row.get("distinct_sessions", 0),
         row.get("first_seen", ""), row.get("last_seen", ""),
         row.get("common_workspace", ""), row.get("common_project", ""),
         row.get("confidence", "derived"),
         row.get("mined_at", datetime.now(timezone.utc).isoformat())))
    conn.commit()
    return cur.lastrowid


def get_mined_patterns(conn, min_count=0, limit=50):
    """Return mined patterns, most frequent first, optionally filtered by min_count."""
    if min_count > 0:
        rows = conn.execute(
            "SELECT * FROM mined_patterns WHERE count >= ? "
            "ORDER BY count DESC LIMIT ?", (min_count, limit)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM mined_patterns ORDER BY count DESC LIMIT ?",
            (limit,)).fetchall()
    return [dict(r) for r in rows]


def clear_mined_patterns(conn):
    """Delete all mined patterns (for re-mining)."""
    conn.execute("DELETE FROM mined_patterns")
    conn.commit()


# ===========================================================================
# Pillar B Stage 3 — Workflow Intents (LLM-labeled workflow descriptions)
# ===========================================================================


def insert_workflow_intent(conn, row):
    """Persist one workflow intent. Returns the new row id."""
    from datetime import datetime, timezone
    cur = conn.execute(
        """INSERT INTO workflow_intents
           (pattern_id, intent_label, intent_description, steps_text,
            confidence, pattern_summary, labeled_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (row["pattern_id"], row["intent_label"],
         row.get("intent_description", ""),
         row.get("steps_text", "[]"),
         row.get("confidence", "low"),
         row.get("pattern_summary", ""),
         row.get("labeled_at", datetime.now(timezone.utc).isoformat())))
    conn.commit()
    return cur.lastrowid


def get_workflow_intents(conn, min_confidence=None, limit=50):
    """Return workflow intents, newest first, optionally filtered by min confidence."""
    if min_confidence:
        rows = conn.execute(
            "SELECT * FROM workflow_intents WHERE confidence >= ? "
            "ORDER BY labeled_at DESC LIMIT ?",
            (min_confidence, limit)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM workflow_intents ORDER BY labeled_at DESC LIMIT ?",
            (limit,)).fetchall()
    return [dict(r) for r in rows]


def get_workflow_intents_for_pattern(conn, pattern_id):
    """Return all workflow intents for a specific pattern."""
    rows = conn.execute(
        "SELECT * FROM workflow_intents WHERE pattern_id = ? "
        "ORDER BY labeled_at DESC", (pattern_id,)).fetchall()
    return [dict(r) for r in rows]


def clear_workflow_intents(conn):
    """Delete all workflow intents (re-label on re-mine)."""
    conn.execute("DELETE FROM workflow_intents")
    conn.commit()


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


# ---------------------------------------------------------------------------
# Shadow Mode — track proposed skill shadow runs for the improvement loop
# ---------------------------------------------------------------------------


def insert_shadow_run(conn, row: dict) -> int:
    """Record one shadow execution run.

    Args:
        conn: Open SQLite connection.
        row: Dict with keys: skill_id, run_at, step_count, steps_matched,
             steps_mismatched, exemplar_comparison (JSON str),
             overall_match_score, outcome, promoted.

    Returns:
        The new shadow_runs row id.
    """
    cur = conn.execute(
        """INSERT INTO shadow_runs
           (skill_id, run_at, step_count, steps_matched, steps_mismatched,
            exemplar_comparison, overall_match_score, outcome, promoted)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (row["skill_id"], row["run_at"], row.get("step_count", 0),
         row.get("steps_matched", 0), row.get("steps_mismatched", 0),
         row.get("exemplar_comparison", "{}"),
         row.get("overall_match_score", 0.0),
         row.get("outcome", "matched"),
         row.get("promoted", 0))
    )
    conn.commit()
    return cur.lastrowid


def count_recent_shadow_runs(conn, skill_id: int, limit: int = 5) -> int:
    """Count the most recent shadow runs for a skill that were clean matches.

    Returns the count of consecutive 'matched' shadow runs (from newest
    backwards, stopping at the first non-matched outcome).
    """
    rows = conn.execute(
        "SELECT outcome FROM shadow_runs WHERE skill_id = ? "
        "ORDER BY id DESC LIMIT ?",
        (skill_id, limit)
    ).fetchall()
    count = 0
    for r in rows:
        if r["outcome"] == "matched":
            count += 1
        else:
            break
    return count


def get_shadow_runs_for_skill(conn, skill_id: int, limit: int = 20) -> list[dict]:
    """Return recent shadow runs for a skill, newest first."""
    rows = conn.execute(
        "SELECT * FROM shadow_runs WHERE skill_id = ? "
        "ORDER BY id DESC LIMIT ?",
        (skill_id, limit)
    ).fetchall()
    return [dict(r) for r in rows]


def get_all_shadow_runs_summary(conn) -> list[dict]:
    """Return per-skill shadow run summary (agg over shadow_runs)."""
    rows = conn.execute(
        """SELECT sr.skill_id,
                  COUNT(*) AS total_runs,
                  SUM(CASE WHEN sr.outcome = 'matched' THEN 1 ELSE 0 END) AS matched_runs,
                  AVG(sr.overall_match_score) AS avg_match_score,
                  SUM(sr.promoted) AS promoted_count
           FROM shadow_runs sr
           GROUP BY sr.skill_id
           ORDER BY total_runs DESC"""
    ).fetchall()
    return [dict(r) for r in rows]


# ===========================================================================
# Cross-Project Correlation — project docs + per-pair correlation results
# ===========================================================================


def upsert_project_doc(conn, row: dict) -> int:
    """Insert or update a project doc by (repo_id, path). Returns row id."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        """INSERT INTO project_docs (repo_id, path, title, content, doc_type, ingested_at, checksum)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(repo_id, path) DO UPDATE SET
               title=excluded.title, content=excluded.content,
               doc_type=excluded.doc_type, ingested_at=excluded.ingested_at,
               checksum=excluded.checksum""",
        (row["repo_id"], row["path"], row.get("title", ""), row["content"],
         row.get("doc_type", "design"), now, row.get("checksum", "")))
    conn.commit()
    return cur.lastrowid


def get_project_docs(conn, repo_id: int) -> list[dict]:
    """Return all project docs for a repo."""
    rows = conn.execute(
        "SELECT * FROM project_docs WHERE repo_id = ? ORDER BY ingested_at DESC",
        (repo_id,)).fetchall()
    return [dict(r) for r in rows]


def get_all_project_docs(conn) -> list[dict]:
    """Return all project docs across all repos."""
    rows = conn.execute(
        "SELECT * FROM project_docs ORDER BY ingested_at DESC").fetchall()
    return [dict(r) for r in rows]


def insert_correlation_result(conn, row: dict) -> int:
    """Persist one correlation result. Returns row id."""
    from datetime import datetime, timezone
    cur = conn.execute(
        """INSERT INTO correlation_results
           (repo_a_id, repo_b_id, structural_score, semantic_score,
            semantic_reason, semantic_label, semantic_confidence,
            volatility, run_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (row["repo_a_id"], row["repo_b_id"],
         row.get("structural_score", 0.0),
         row.get("semantic_score"),
         row.get("semantic_reason"),
         row.get("semantic_label"),
         row.get("semantic_confidence"),
         row.get("volatility", 0.0),
         datetime.now(timezone.utc).isoformat()))
    conn.commit()
    return cur.lastrowid


def get_recent_correlations(conn, min_structural: float = 0.0, limit: int = 20) -> list[dict]:
    """Return most recent correlation results, optionally filtered by structural score."""
    if min_structural > 0:
        rows = conn.execute(
            "SELECT * FROM correlation_results WHERE structural_score >= ? "
            "ORDER BY run_at DESC LIMIT ?", (min_structural, limit)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM correlation_results ORDER BY run_at DESC LIMIT ?",
            (limit,)).fetchall()
    return [dict(r) for r in rows]


def get_repo_commit_count_recent(conn, repo_id: int, days: int = 30) -> int:
    """Count commits in the last N days for a repo using snapshot history."""
    from datetime import datetime, timezone, timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    # Use git snapshots to estimate activity — count distinct snapshot dates.
    row = conn.execute(
        "SELECT COUNT(DISTINCT date(observed_at)) as c FROM snapshots "
        "WHERE repo_path = (SELECT path FROM repositories WHERE id = ?) "
        "AND observed_at >= ?", (repo_id, cutoff)).fetchone()
    return row["c"] if row else 0
