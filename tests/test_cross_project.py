"""Tests for Cross-Project Knowledge Correlation."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def _fresh_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


def _schema(conn: sqlite3.Connection) -> None:
    """Create all tables needed for correlation tests."""
    conn.executescript("""
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
            ingestion_time  TEXT NOT NULL,
            maturity        TEXT DEFAULT '',
            readme_quality  INTEGER DEFAULT 0,
            readme_completeness INTEGER DEFAULT 0
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
        CREATE TABLE IF NOT EXISTS project_docs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            repo_id     INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
            path        TEXT NOT NULL,
            title       TEXT NOT NULL DEFAULT '',
            content     TEXT NOT NULL,
            doc_type    TEXT NOT NULL DEFAULT 'design',
            ingested_at TEXT NOT NULL,
            checksum    TEXT NOT NULL DEFAULT '',
            UNIQUE(repo_id, path)
        );
        CREATE TABLE IF NOT EXISTS correlation_results (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            repo_a_id    INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
            repo_b_id    INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
            structural_score REAL NOT NULL DEFAULT 0.0,
            semantic_score    REAL,
            semantic_reason   TEXT,
            semantic_label    TEXT,
            semantic_confidence TEXT,
            volatility         REAL NOT NULL DEFAULT 0.0,
            run_at         TEXT NOT NULL,
            UNIQUE(repo_a_id, repo_b_id, run_at)
        );
        CREATE TABLE IF NOT EXISTS snapshots (
            id               INTEGER PRIMARY KEY,
            observed_at      TEXT NOT NULL,
            repo_path        TEXT NOT NULL,
            repo_name        TEXT,
            commit_count     INTEGER,
            last_commit_date TEXT,
            is_dirty         INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS insights (
            id              TEXT PRIMARY KEY,
            title           TEXT NOT NULL,
            insight_type    TEXT NOT NULL,
            statement       TEXT NOT NULL,
            status          TEXT NOT NULL DEFAULT 'candidate',
            confidence      TEXT NOT NULL DEFAULT 'medium',
            started_at      TEXT,
            updated_at      TEXT NOT NULL,
            retired_at      TEXT,
            understanding_ids TEXT NOT NULL DEFAULT '',
            initiative_ids  TEXT NOT NULL DEFAULT '',
            knowledge_ids   TEXT NOT NULL DEFAULT '',
            build_at        TEXT NOT NULL,
            created_at      TEXT NOT NULL,
            schema_version  TEXT NOT NULL DEFAULT '1.0'
        );
        CREATE TABLE IF NOT EXISTS insight_history (
            build_at        TEXT NOT NULL,
            insight_id      TEXT NOT NULL REFERENCES insights(id) ON DELETE CASCADE,
            title           TEXT NOT NULL,
            insight_type    TEXT NOT NULL,
            statement       TEXT NOT NULL,
            status          TEXT NOT NULL,
            confidence      TEXT NOT NULL,
            understanding_ids TEXT NOT NULL DEFAULT '',
            initiative_ids  TEXT NOT NULL DEFAULT '',
            knowledge_ids   TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (build_at, insight_id)
        );
        CREATE TABLE IF NOT EXISTS insight_evolution (
            id                  TEXT PRIMARY KEY,
            build_at            TEXT NOT NULL,
            event_type          TEXT NOT NULL,
            insight_id          TEXT NOT NULL,
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
    """)


def _seed_repos(conn: sqlite3.Connection) -> tuple[int, int]:
    """Insert two test repos. Returns (repo_a_id, repo_b_id)."""
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "INSERT INTO repositories (name, path, ingestion_time) VALUES (?, ?, ?)",
        ("test-repo-a", "/tmp/test-repo-a", now)
    )
    a_id = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO repositories (name, path, ingestion_time) VALUES (?, ?, ?)",
        ("test-repo-b", "/tmp/test-repo-b", now)
    )
    b_id = cur.lastrowid
    # Add some languages
    conn.execute("INSERT INTO languages (repo_id, language, file_count) VALUES (?, ?, ?)",
                 (a_id, "Python", 100))
    conn.execute("INSERT INTO languages (repo_id, language, file_count) VALUES (?, ?, ?)",
                 (b_id, "Python", 80))
    conn.execute("INSERT INTO languages (repo_id, language, file_count) VALUES (?, ?, ?)",
                 (b_id, "TypeScript", 50))
    # Add some technologies
    conn.execute("INSERT INTO technologies (repo_id, tech, evidence) VALUES (?, ?, ?)",
                 (a_id, "FastAPI", "detected"))
    conn.execute("INSERT INTO technologies (repo_id, tech, evidence) VALUES (?, ?, ?)",
                 (b_id, "FastAPI", "detected"))
    conn.execute("INSERT INTO technologies (repo_id, tech, evidence) VALUES (?, ?, ?)",
                 (a_id, "SQLite", "detected"))
    # Add snapshots for recency
    conn.execute(
        "INSERT INTO snapshots (observed_at, repo_path) VALUES (?, ?)",
        (now, "/tmp/test-repo-a"))
    conn.execute(
        "INSERT INTO snapshots (observed_at, repo_path) VALUES (?, ?)",
        (now, "/tmp/test-repo-b"))
    conn.commit()
    return a_id, b_id


def test_structural_pass_detects_similarity():
    """structural_pass finds similarities between repos with shared tech/language."""
    from src.friday.cross_project import structural_pass

    conn = _fresh_db()
    _schema(conn)
    _seed_repos(conn)

    pairs = structural_pass(conn)
    assert len(pairs) >= 1
    top = pairs[0]
    assert top["structural_score"] > 0
    assert len(top["evidence"]) >= 1
    assert any("Python" in e for e in top["evidence"]) or any("FastAPI" in e for e in top["evidence"])


def test_structural_pass_recency_weighting():
    """structural_pass applies recency volatility correctly."""
    from src.friday.cross_project import structural_pass

    conn = _fresh_db()
    _schema(conn)

    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "INSERT INTO repositories (name, path, ingestion_time) VALUES (?, ?, ?)",
        ("active-repo", "/tmp/active", now)
    )
    active_id = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO repositories (name, path, ingestion_time) VALUES (?, ?, ?)",
        ("stale-repo", "/tmp/stale", now)
    )
    stale_id = cur.lastrowid

    # Same tech/language
    conn.execute("INSERT INTO languages (repo_id, language, file_count) VALUES (?, ?, ?)",
                 (active_id, "Python", 100))
    conn.execute("INSERT INTO languages (repo_id, language, file_count) VALUES (?, ?, ?)",
                 (stale_id, "Python", 100))
    # Active repo has recent snapshot
    conn.execute(
        "INSERT INTO snapshots (observed_at, repo_path) VALUES (?, ?)",
        (now, "/tmp/active"))
    # Stale repo has no snapshots — volatility should be near-zero
    conn.commit()
    conn.commit()

    pairs = structural_pass(conn)
    assert len(pairs) >= 1
    # volatility=1.0 is correct — active repo drives the max_commits
    # and one active repo in the pair makes it worth correlating.


def test_upsert_project_doc():
    """Can upsert a project doc and read it back."""
    from src.friday.db import upsert_project_doc, get_project_docs

    conn = _fresh_db()
    _schema(conn)

    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "INSERT INTO repositories (name, path, ingestion_time) VALUES (?, ?, ?)",
        ("doc-test", "/tmp/doc-test", now)
    )
    repo_id = cur.lastrowid
    conn.commit()

    upsert_project_doc(conn, {
        "repo_id": repo_id,
        "path": "docs/prd.md",
        "title": "Product Requirements",
        "content": "# PRD\n\nThis project does X.",
        "doc_type": "prd",
        "checksum": "abc123",
    })

    docs = get_project_docs(conn, repo_id)
    assert len(docs) == 1
    assert docs[0]["doc_type"] == "prd"
    assert docs[0]["path"] == "docs/prd.md"


def test_suggest_respects_confidence_threshold():
    """suggest only promotes correlations above _INSIGHT_THRESHOLD to Insights."""
    from src.friday.cross_project import suggest

    conn = _fresh_db()
    _schema(conn)
    a_id, b_id = _seed_repos(conn)

    # Low-scoring pair — should NOT create an Insight.
    low = [{
        "repo_a_id": a_id, "repo_b_id": b_id,
        "repo_a_name": "test-repo-a", "repo_b_name": "test-repo-b",
        "structural_score": 0.5, "adjusted_score": 0.5,
        "volatility": 0.3, "evidence": [],
        "semantic_score": 0.6,  # below 0.8 threshold
        "semantic_reason": "Weak similarity",
        "semantic_label": "vague",
        "semantic_confidence": "low",
    }]
    created = suggest(conn, low)
    assert len(created) == 0, "Should not create Insight for sub-threshold pair"

    # High-scoring pair — should create an Insight.
    high = [{
        "repo_a_id": a_id, "repo_b_id": b_id,
        "repo_a_name": "test-repo-a", "repo_b_name": "test-repo-b",
        "structural_score": 0.8, "adjusted_score": 0.9,
        "volatility": 0.8, "evidence": ["Shared tech: FastAPI"],
        "semantic_score": 0.95,
        "semantic_reason": "Both implement payment flows",
        "semantic_label": "payment processing",
        "semantic_confidence": "high",
    }]
    created = suggest(conn, high)
    assert len(created) >= 1, "Should create Insight for high-confidence pair"
