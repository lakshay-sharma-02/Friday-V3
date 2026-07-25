"""Tests for the Integration Engine (cross-project integration via the pipeline)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from friday.db import connect


def _db():
    """Create a fresh in-memory database for each test."""
    return connect(":memory:")


# ---------------------------------------------------------------------------
# IntegrationEngine tests
# ---------------------------------------------------------------------------


class TestIntegrationEngine:
    def test_engine_import(self):
        """IntegrationEngine can be imported."""
        from friday.integration import IntegrationEngine
        assert IntegrationEngine is not None

    def test_integrate_result_dataclass(self):
        """IntegrateResult has expected fields."""
        from friday.integration.engine import IntegrateResult
        r = IntegrateResult(
            graph_id="test:123",
            repo_a="repo-a",
            repo_b="repo-b",
            overlap_found=True,
            overlap_kind="shared dependency",
            description="Both use Python.",
            confidence="Medium",
            basis=["Shared: Python"],
            note=None,
        )
        assert r.graph_id == "test:123"
        assert r.repo_a == "repo-a"
        assert r.overlap_found is True
        assert "Python" in r.description

    def test_integrate_result_text_no_overlap(self):
        """to_text works when no overlap found."""
        from friday.integration.engine import IntegrateResult
        r = IntegrateResult(
            graph_id=None,
            repo_a="a", repo_b="b",
            overlap_found=False,
            overlap_kind=None,
            description=None,
            confidence="Weak",
            basis=[],
            note="No LLM available.",
        )
        text = r.to_text()
        assert "No meaningful structural overlap" in text

    def test_integrate_result_text_with_graph(self):
        """to_text includes next steps when graph_id is set."""
        from friday.integration.engine import IntegrateResult
        r = IntegrateResult(
            graph_id="integ:abc123",
            repo_a="foo", repo_b="bar",
            overlap_found=True,
            overlap_kind="complementary",
            description="foo produces input bar consumes.",
            confidence="Strong",
            basis=["Architecture: pipeline"],
            note=None,
        )
        text = r.to_text()
        assert "Next steps" in text
        assert "graph review" in text
        assert "graph review approve" in text

    def test_integrate_calls_synthesis_and_graph(self):
        """IntegrationEngine.integrate() runs synthesis and generates a graph."""
        from friday.integration.engine import IntegrationEngine

        # Set up a real in-memory DB with two repos so synthesis doesn't fail.
        conn = _db()
        conn.execute(
            "INSERT INTO repositories (name, path, ingestion_time) "
            "VALUES (?, ?, ?)",
            ("repo-a", "/tmp/repo-a", "2026-01-01T00:00:00"),
        )
        conn.execute(
            "INSERT INTO repositories (name, path, ingestion_time) "
            "VALUES (?, ?, ?)",
            ("repo-b", "/tmp/repo-b", "2026-01-01T00:00:00"),
        )
        conn.commit()

        engine = IntegrationEngine(conn)
        result = engine.integrate("repo-a", "repo-b")

        # Should produce a result with a graph.
        assert result is not None
        assert result.repo_a == "repo-a"
        assert result.repo_b == "repo-b"

        # The graph should exist in the DB with provenance tag.
        row = conn.execute(
            "SELECT id, source FROM task_graphs WHERE source LIKE 'integration:%'"
        ).fetchone()
        assert row is not None
        assert "repo-a" in row["source"]
        assert "repo-b" in row["source"]

        conn.close()

    def test_integrate_with_no_repos(self):
        """IntegrationEngine gracefully handles missing repos."""
        from friday.integration.engine import IntegrationEngine

        conn = _db()
        engine = IntegrationEngine(conn)

        # With no repos, synthesis still runs (returns no-overlap result).
        result = engine.integrate("nonexistent-a", "nonexistent-b")

        assert result is not None
        # No overlap because repos don't exist in the DB.
        assert not result.overlap_found
        # The graph should still be generated (a minimal one).
        assert result.graph_id is not None

        conn.close()

    def test_integrate_provenance_tagged(self):
        """IntegrationEngine tags graphs with source='integration:...'."""
        from friday.integration.engine import IntegrationEngine

        conn = _db()
        conn.execute(
            "INSERT INTO repositories (name, path, ingestion_time) "
            "VALUES (?, ?, ?)",
            ("alpha", "/tmp/alpha", "2026-01-01T00:00:00"),
        )
        conn.execute(
            "INSERT INTO repositories (name, path, ingestion_time) "
            "VALUES (?, ?, ?)",
            ("beta", "/tmp/beta", "2026-01-01T00:00:00"),
        )
        conn.commit()

        engine = IntegrationEngine(conn)
        result = engine.integrate("alpha", "beta")

        row = conn.execute(
            "SELECT source FROM task_graphs WHERE id = ?",
            (result.graph_id,),
        ).fetchone()
        assert row is not None
        assert row["source"] == "integration:alpha/beta"

        conn.close()


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------


class TestCliIntegration:
    def test_cli_integrate_error_missing_args(self, capsys):
        """CLI handler shows error with no args."""
        from friday.cli_integration import cmd_integrate
        import argparse

        args = argparse.Namespace(repo_a=None, repo_b=None)
        rc = cmd_integrate(args)
        assert rc == 2
        captured = capsys.readouterr()
        assert "error" in captured.err

    def test_cli_integrate_same_repo(self, capsys):
        """CLI handler rejects same-repo integration."""
        from friday.cli_integration import cmd_integrate
        import argparse

        args = argparse.Namespace(repo_a="same", repo_b="same")
        rc = cmd_integrate(args)
        assert rc == 2

    def test_cli_integrate_invalid_repo(self, capsys):
        """CLI handler handles nonexistent repos gracefully."""
        from friday.cli_integration import cmd_integrate
        import argparse

        # This opens the real DB, which is fine — it won't find the repos.
        args = argparse.Namespace(repo_a="zzz_nonexistent_aaa", repo_b="zzz_nonexistent_bbb")
        rc = cmd_integrate(args)
        assert rc == 0
        captured = capsys.readouterr()
        assert "Integration:" in captured.out


# ---------------------------------------------------------------------------
# Pipeline compatibility tests
# ---------------------------------------------------------------------------


class TestPipelineIntegration:
    def test_integrate_graph_appears_in_review(self):
        """Graph generated by IntegrationEngine is persisted with correct source tag."""
        from friday.integration.engine import IntegrationEngine

        conn = _db()
        conn.execute(
            "INSERT INTO repositories (name, path, ingestion_time) "
            "VALUES (?, ?, ?)",
            ("proj-x", "/tmp/proj-x", "2026-01-01T00:00:00"),
        )
        conn.execute(
            "INSERT INTO repositories (name, path, ingestion_time) "
            "VALUES (?, ?, ?)",
            ("proj-y", "/tmp/proj-y", "2026-01-01T00:00:00"),
        )
        conn.commit()

        engine = IntegrationEngine(conn)
        result = engine.integrate("proj-x", "proj-y")

        # Verify the provenance tag in the DB (not on the in-memory TaskGraph).
        row = conn.execute(
            "SELECT id, source FROM task_graphs WHERE id = ?",
            (result.graph_id,),
        ).fetchone()
        assert row is not None, "Graph should exist in task_graphs table"
        assert row["source"] == "integration:proj-x/proj-y", \
            f"Expected source 'integration:proj-x/proj-y', got '{row['source']}'"

        # The graph should be findable via TaskGraphEngine.
        from friday.planning import TaskGraphEngine
        graph_eng = TaskGraphEngine(conn)
        graph = graph_eng.graph_by_id(result.graph_id)
        assert graph is not None
        conn.close()

    def test_integrate_graph_has_tasks(self):
        """Graph generated by IntegrationEngine has expected task structure."""
        from friday.integration.engine import IntegrationEngine

        conn = _db()
        conn.execute(
            "INSERT INTO repositories (name, path, ingestion_time) "
            "VALUES (?, ?, ?)",
            ("app-a", "/tmp/app-a", "2026-01-01T00:00:00"),
        )
        conn.execute(
            "INSERT INTO repositories (name, path, ingestion_time) "
            "VALUES (?, ?, ?)",
            ("app-b", "/tmp/app-b", "2026-01-01T00:00:00"),
        )
        conn.commit()

        engine = IntegrationEngine(conn)
        result = engine.integrate("app-a", "app-b")

        # The graph should have tasks and edges in the DB.
        tasks = conn.execute(
            "SELECT COUNT(*) AS c FROM tasks WHERE graph_id = ?",
            (result.graph_id,),
        ).fetchone()
        assert tasks is not None
        assert tasks["c"] > 0  # At least one task.

        conn.close()
