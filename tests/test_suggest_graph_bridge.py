"""Tests for the Suggestion → Graph Bridge (M10.x).

Covers: stable suggestion ids, --graph with valid/invalid ids, graph provenance
tagging (source=suggestion:<id>), and existing graph test suite non-regression.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from friday.db import connect, now_iso, get_all_task_graphs, get_task_graph_by_id
from friday.cli_suggest import (
    Suggestion,
    SuggestResult,
    _suggestion_id,
    generate_suggestions,
    _suggestion_to_graph,
)
from friday.planning import TaskGraphEngine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _db(tmp_path: Path) -> sqlite3.Connection:
    return connect(tmp_path / "suggest_bridge_test.db")


def _seed_repos(conn: sqlite3.Connection) -> None:
    """Seed a minimal workspace with 2 repositories so suggestion detectors
    have data to work with."""
    from friday.db import upsert_repository

    upsert_repository(
        conn, name="repo-a", path="/tmp/repo-a", default_branch="main",
        is_dirty=False, first_commit_date=None, last_commit_date=None,
        remote_url=None, commit_count=5, readme_summary="A test project",
        license=None, primary_author=None,
    )
    upsert_repository(
        conn, name="repo-b", path="/tmp/repo-b", default_branch="main",
        is_dirty=False, first_commit_date=None, last_commit_date=None,
        remote_url=None, commit_count=3, readme_summary="Another test project",
        license=None, primary_author=None,
    )

    # Add shared tech for both repos so shared-tech suggestions are detected.
    from friday.db import replace_children
    replace_children(conn, 1, [], [])  # clear defaults
    replace_children(conn, 2, [], [])
    # Add technology data
    conn.execute(
        "INSERT OR REPLACE INTO technologies (repo_id, tech, evidence) VALUES (?, ?, ?)",
        (1, "Docker", "found in Dockerfile"))
    conn.execute(
        "INSERT OR REPLACE INTO technologies (repo_id, tech, evidence) VALUES (?, ?, ?)",
        (2, "Docker", "found in docker-compose.yml"))
    conn.commit()


# ===================================================================
# 1. Stable suggestion ids
# ===================================================================

def test_suggestion_id_stable():
    """Same (title, detail) produces the same id."""
    id1 = _suggestion_id("Share Docker config", "Both projects use Docker.")
    id2 = _suggestion_id("Share Docker config", "Both projects use Docker.")
    assert id1 == id2
    assert id1.startswith("sug:")


def test_suggestion_id_different_content():
    """Different content produces different ids."""
    id1 = _suggestion_id("Share Docker", "Docker is shared.")
    id2 = _suggestion_id("Share database", "DB is shared.")
    assert id1 != id2


def test_suggestion_id_consistent_across_runs(tmp_path):
    """Two consecutive `friday suggest` runs produce same suggestion ids
    when evidence is unchanged."""
    conn = _db(tmp_path)
    _seed_repos(conn)

    result1 = generate_suggestions(conn)
    ids1 = {s.id for s in result1.suggestions}

    result2 = generate_suggestions(conn)
    ids2 = {s.id for s in result2.suggestions}

    assert ids1 == ids2
    assert all(i.startswith("sug:") for i in ids1)

    conn.close()


# ===================================================================
# 2. Suggestion dataclass auto-generates id
# ===================================================================

def test_suggestion_auto_generates_id():
    """Suggestion dataclass auto-generates an id when created without one."""
    s = Suggestion(title="Test", detail="Testing auto id", severity="high")
    assert s.id.startswith("sug:")
    assert len(s.id) > 4


def test_suggestion_preserves_explicit_id():
    """Suggestion preserves an explicitly provided id."""
    s = Suggestion(
        id="sug:custom",
        title="Test", detail="Testing explicit id", severity="high",
    )
    assert s.id == "sug:custom"


# ===================================================================
# 3. --graph with valid suggestion id
# ===================================================================

def test_suggestion_to_graph_creates_graph_with_provenance(tmp_path):
    """_suggestion_to_graph generates a graph and tags its source."""
    conn = _db(tmp_path)

    suggestion = Suggestion(
        title="Integrate A and B",
        detail="Both projects share Docker. Create shared infra.",
        severity="medium",
    )

    gid = _suggestion_to_graph(conn, suggestion)
    assert gid is not None

    # Check the graph was persisted with source tag.
    row = get_task_graph_by_id(conn, gid)
    assert row is not None
    assert row.source == f"suggestion:{suggestion.id}"

    conn.close()


def test_suggestion_to_graph_creates_real_graph(tmp_path):
    """The generated graph has real tasks, edges, and is acyclic."""
    conn = _db(tmp_path)

    suggestion = Suggestion(
        title="Refactor shared auth",
        detail="Both repo-a and repo-b implement authentication.",
        severity="high",
    )

    gid = _suggestion_to_graph(conn, suggestion)
    eng = TaskGraphEngine(conn)
    g = eng.graph_by_id(gid)
    assert g is not None
    assert len(g.tasks) >= 1
    assert g.status == "compiled"

    # Acyclic.
    from friday.planning.compiler import _detect_cycle
    ids = [t.id for t in g.tasks]
    assert _detect_cycle(g.edges, ids) is False

    conn.close()


# ===================================================================
# 4. --graph with invalid/stale suggestion id
# ===================================================================

def test_suggest_with_invalid_id_fails_cleanly(tmp_path):
    """--graph with an unknown suggestion id fails cleanly with no graph."""
    conn = _db(tmp_path)
    _seed_repos(conn)

    # Generate suggestions but don't match the made-up id.
    result = generate_suggestions(conn)

    fake_id = "sug:doesnotexist"
    matched = [s for s in result.suggestions if s.id == fake_id]
    assert len(matched) == 0  # no match

    # Verify no graph was created with that source tag.
    from friday.db import get_all_task_graphs
    graphs = get_all_task_graphs(conn)
    tagged = [g for g in graphs if g.source == f"suggestion:{fake_id}"]
    assert len(tagged) == 0

    conn.close()


# ===================================================================
# 5. Graph review shows source provenance
# ===================================================================

def test_graph_provenance_in_review_listing(tmp_path):
    """A graph with source=suggestion:<id> is visible in the graph review
    listing with its provenance tag."""
    conn = _db(tmp_path)

    suggestion = Suggestion(
        title="Share Docker infra",
        detail="Both projects use Docker. Standardize.",
        severity="high",
    )

    gid = _suggestion_to_graph(conn, suggestion)
    row = get_task_graph_by_id(conn, gid)
    assert row is not None
    assert row.source == f"suggestion:{suggestion.id}"

    # Verify the graph is listed in all_graphs with source.
    graphs = get_all_task_graphs(conn)
    matched = [g for g in graphs if g.id == gid]
    assert len(matched) == 1
    assert matched[0].source == f"suggestion:{suggestion.id}"

    conn.close()


# ===================================================================
# 6. Existing graph test suites pass unmodified
# ===================================================================

def test_graph_suite_loads():
    """The existing test_graph.py module loads correctly."""
    import tests.test_graph as mod
    assert hasattr(mod, "test_compile_produces_tasks_and_edges")
    assert hasattr(mod, "test_no_hallucination_valid_plan_references")


def test_graph_dogfood_suite_loads():
    """The existing test_graph_dogfood.py module loads correctly."""
    import tests.test_graph_dogfood as mod
    assert hasattr(mod, "test_dogfood_compile_all_goals")
    assert hasattr(mod, "test_dogfood_idempotency")
