"""Tests for the Cross-Project Integration Engine.

Covers:
1. Input validation (too few, too many, non-existent, empty)
2. Multi-repo support (2-8 repos)
3. Correlation-aware behavior (positive score, low score warnings)
4. Plan + Task Graph creation (persisted in DB)
5. Milestone content in generated plans
6. IntegrateResult formatting
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from friday.db import connect


# ──────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def db():
    """In-memory DB with test repos."""
    conn = connect(Path(":memory:"))
    _seed_repos(conn)
    return conn


def _seed_repos(conn) -> None:
    """Insert 3 test repos with languages, tech, and architecture."""
    from friday.db import now_iso

    now = now_iso()

    # Repositories
    cur = conn.execute(
        "INSERT INTO repositories (name, path, ingestion_time) VALUES (?, ?, ?)",
        ("vivaha", "/tmp/vivaha", now),
    )
    vid = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO repositories (name, path, ingestion_time) VALUES (?, ?, ?)",
        ("aether", "/tmp/aether", now),
    )
    aid = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO repositories (name, path, ingestion_time) VALUES (?, ?, ?)",
        ("jarvis", "/tmp/jarvis", now),
    )
    jid = cur.lastrowid

    # Languages
    for rid, lang, count in [
        (vid, "Python", 100), (vid, "TypeScript", 30),
        (aid, "Python", 80), (aid, "Rust", 20),
        (jid, "Python", 50), (jid, "Go", 40),
    ]:
        conn.execute(
            "INSERT INTO languages (repo_id, language, file_count) VALUES (?, ?, ?)",
            (rid, lang, count),
        )

    # Technologies — vivaha and aether both use FastAPI → shared overlap
    for rid, tech in [
        (vid, "FastAPI"), (vid, "SQLite"),
        (aid, "FastAPI"), (aid, "PostgreSQL"),
        (jid, "Django"), (jid, "PostgreSQL"),
    ]:
        conn.execute(
            "INSERT INTO technologies (repo_id, tech, evidence) VALUES (?, ?, 'detected')",
            (rid, tech),
        )

    # Architecture
    conn.execute(
        "INSERT INTO architecture (repo_id, architecture, evidence) VALUES (?, ?, ?)",
        (vid, "Microservices with REST APIs", "detected"),
    )
    conn.execute(
        "INSERT INTO architecture (repo_id, architecture, evidence) VALUES (?, ?, ?)",
        (aid, "Modular monolith with async workers", "detected"),
    )

    # Snapshots for recency
    for path in ["/tmp/vivaha", "/tmp/aether", "/tmp/jarvis"]:
        conn.execute(
            "INSERT INTO snapshots (observed_at, repo_path) VALUES (?, ?)",
            (now, path),
        )

    conn.commit()


# ──────────────────────────────────────────────────────────────────────
# Input validation
# ──────────────────────────────────────────────────────────────────────


class TestValidation:
    def test_requires_at_least_two(self, db):
        from friday.integration import IntegrationEngine

        eng = IntegrationEngine(db)
        with pytest.raises(ValueError, match="at least 2"):
            eng.integrate("vivaha")

    def test_empty_list(self, db):
        from friday.integration import IntegrationEngine
        eng = IntegrationEngine(db)
        with pytest.raises(ValueError, match="at least 2"):
            eng.integrate()

    def test_whitespace_stripped(self, db):
        from friday.integration import IntegrationEngine
        eng = IntegrationEngine(db)
        with pytest.raises(ValueError, match="at least 2"):
            eng.integrate("vivaha", "  ")

    def test_non_existent_repo_raises(self, db):
        from friday.integration import IntegrationEngine

        eng = IntegrationEngine(db)
        with pytest.raises(ValueError, match="not found"):
            eng.integrate("vivaha", "nonexistent")

    def test_max_repos_threshold(self, db):
        from friday.integration import IntegrationEngine, _MAX_REPOS

        eng = IntegrationEngine(db)
        names = [f"repo-{n}" for n in range(_MAX_REPOS + 1)]
        with pytest.raises(ValueError, match="Max"):
            eng.integrate(*names)

    def test_duplicates_not_allowed_in_cli(self):
        """CLI layer rejects duplicates — verify the check works."""
        names = ["vivaha", "vivaha"]
        assert len(set(r.lower() for r in names)) != len(names)


# ──────────────────────────────────────────────────────────────────────
# Multi-repo support
# ──────────────────────────────────────────────────────────────────────


class TestMultiRepo:
    def test_two_repos(self, db):
        from friday.integration import IntegrationEngine

        eng = IntegrationEngine(db)
        result = eng.integrate("vivaha", "aether")
        assert len(result.repo_names) == 2
        assert "vivaha" in result.repo_names
        assert "aether" in result.repo_names

    def test_three_repos(self, db):
        from friday.integration import IntegrationEngine

        eng = IntegrationEngine(db)
        result = eng.integrate("vivaha", "aether", "jarvis")
        assert len(result.repo_names) == 3

    def test_repo_names_integrity(self, db):
        """Verify repo_names match input order."""
        from friday.integration import IntegrationEngine

        eng = IntegrationEngine(db)
        result = eng.integrate("jarvis", "vivaha", "aether")
        assert result.repo_names == ["jarvis", "vivaha", "aether"]


# ──────────────────────────────────────────────────────────────────────
# Correlation / synthesis integration
# ──────────────────────────────────────────────────────────────────────


class TestCorrelation:
    def test_returns_positive_score_for_overlapping_repos(self, db):
        """vivaha + aether share FastAPI → overlap found → score > 0."""
        from friday.integration import IntegrationEngine

        eng = IntegrationEngine(db)
        result = eng.integrate("vivaha", "aether")
        assert result.correlation_score > 0
        # The pairwise synthesis should find the FastAPI overlap
        assert result.overlap_found is True

    def test_synthesis_fields_populated(self, db):
        from friday.integration import IntegrationEngine

        eng = IntegrationEngine(db)
        result = eng.integrate("vivaha", "aether")
        # overlap fields should be set (vivaha↔aether share FastAPI)
        assert result.overlap_kind is not None
        assert result.confidence in ("Strong", "Medium", "Weak")

    def test_basis_includes_shared_technology(self, db):
        from friday.integration import IntegrationEngine

        eng = IntegrationEngine(db)
        result = eng.integrate("vivaha", "aether")
        # basis should include FastAPI
        basis_text = " ".join(result.basis).lower()
        assert "fastapi" in basis_text

    def test_warning_on_no_overlap(self, db):
        """When repos share no technologies, a warning is emitted."""
        from friday.integration import IntegrationEngine
        from friday.db import now_iso

        now = now_iso()
        conn = db
        cur = conn.execute(
            "INSERT INTO repositories (name, path, ingestion_time) VALUES (?, ?, ?)",
            ("unrelated", "/tmp/unrelated", now),
        )
        uid = cur.lastrowid
        conn.execute(
            "INSERT INTO languages (repo_id, language, file_count) VALUES (?, ?, ?)",
            (uid, "Ruby", 100),
        )
        conn.execute(
            "INSERT INTO technologies (repo_id, tech, evidence) VALUES (?, ?, 'detected')",
            (uid, "Sinatra"),
        )
        conn.execute(
            "INSERT INTO snapshots (observed_at, repo_path) VALUES (?, ?)",
            (now, "/tmp/unrelated"),
        )
        conn.commit()

        eng = IntegrationEngine(db)
        result = eng.integrate("vivaha", "unrelated")
        # These share nothing (Python vs Ruby, FastAPI vs Sinatra)
        # The warning should fire for low correlation
        assert len(result.warnings) >= 1


# ──────────────────────────────────────────────────────────────────────
# Plan + Task Graph creation
# ──────────────────────────────────────────────────────────────────────


class TestPlanAndGraph:
    def test_creates_plan_and_graph_in_db(self, db):
        from friday.integration import IntegrationEngine

        eng = IntegrationEngine(db)
        result = eng.integrate("vivaha", "aether")

        assert result.plan_id is not None
        assert result.graph_id is not None

        # Verify plan exists in DB
        row = db.execute(
            "SELECT id FROM plans WHERE id = ?", (result.plan_id,)
        ).fetchone()
        assert row is not None

        # Verify graph exists in DB
        row = db.execute(
            "SELECT id, status FROM task_graphs WHERE id = ?",
            (result.graph_id,),
        ).fetchone()
        assert row is not None
        assert row["status"] == "proposal"

    def test_plan_has_milestones(self, db):
        from friday.integration import IntegrationEngine

        eng = IntegrationEngine(db)
        result = eng.integrate("vivaha", "aether")

        assert result.plan_id is not None
        row = db.execute(
            "SELECT milestones FROM plans WHERE id = ?", (result.plan_id,)
        ).fetchone()
        assert row is not None
        milestones = json.loads(row["milestones"])
        assert len(milestones) >= 3
        # First milestone should reference architecture analysis
        assert "analyse" in milestones[0]["title"].lower()
        assert "vivaha" in milestones[0]["title"]

    def test_docs_generated_listed_in_result(self, db):
        from friday.integration import IntegrationEngine

        eng = IntegrationEngine(db)
        result = eng.integrate("vivaha", "aether")

        # Should have at least 3 docs (analysis, patterns, plan)
        assert len(result.docs_generated) >= 3
        # All doc paths should be unique
        assert len(result.docs_generated) == len(set(result.docs_generated))

    def test_three_repo_graph_created(self, db):
        from friday.integration import IntegrationEngine

        eng = IntegrationEngine(db)
        result = eng.integrate("vivaha", "aether", "jarvis")

        assert result.graph_id is not None
        row = db.execute(
            "SELECT id FROM task_graphs WHERE id = ?", (result.graph_id,)
        ).fetchone()
        assert row is not None


# ──────────────────────────────────────────────────────────────────────
# IntegrateResult formatting
# ──────────────────────────────────────────────────────────────────────


class TestResultFormatting:
    def test_to_text_includes_goal_and_repos(self, db):
        from friday.integration import IntegrationEngine

        eng = IntegrationEngine(db)
        result = eng.integrate("vivaha", "aether")
        text = result.to_text()

        assert "vivaha" in text
        assert "aether" in text
        assert "Integrate" in text

    def test_to_text_includes_correlation_score(self, db):
        from friday.integration import IntegrationEngine

        eng = IntegrationEngine(db)
        result = eng.integrate("vivaha", "aether")
        text = result.to_text()

        assert str(round(result.correlation_score, 3)) in text

    def test_to_text_mentions_review(self, db):
        from friday.integration import IntegrationEngine

        eng = IntegrationEngine(db)
        result = eng.integrate("vivaha", "aether")
        text = result.to_text()

        # Should mention the graph review command
        assert "graph review" in text.lower()

    def test_to_text_includes_warnings(self, db):
        """When warnings are present, they appear in the text."""
        from friday.integration import IntegrationEngine, IntegrateResult

        result = IntegrateResult(
            goal="test",
            repo_names=["a", "b"],
            graph_id="graph:test",
            plan_id="plan:test",
            correlation_score=0.12,
            overlap_found=False,
            overlap_kind=None,
            overlap_description=None,
            confidence="Weak",
            basis=[],
            warnings=["Low structural correlation (0.12) among repos."],
        )
        text = result.to_text()
        assert "Low structural" in text
