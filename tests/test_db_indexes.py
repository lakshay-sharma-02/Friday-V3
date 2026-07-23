"""Tests for database indexes (purely additive — no schema changes).

Each test confirms:
1. The index exists via PRAGMA index_list
2. For hot-path queries, EXPLAIN QUERY PLAN shows SEARCH (indexed lookup)
   instead of SCAN (full table scan), proving the index is usable.
"""

import sqlite3
import pytest

from src.friday.db import SCHEMA


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    c.executescript(SCHEMA)
    yield c
    c.close()


# ---------------------------------------------------------------------------
# Expected indexes: name -> (table, column)
# ---------------------------------------------------------------------------

EXPECTED_INDEXES: dict[str, tuple[str, str]] = {
    "idx_tasks_graph_id": ("tasks", "graph_id"),
    "idx_task_edges_graph_id": ("task_edges", "graph_id"),
    "idx_task_history_graph_id": ("task_history", "graph_id"),
    "idx_task_evolution_graph_id": ("task_evolution", "graph_id"),
    "idx_task_graphs_status": ("task_graphs", "status"),
    "idx_resolver_evolution_graph_id": ("resolver_evolution", "graph_id"),
    "idx_resolver_history_assignment_id": ("resolver_history", "assignment_id"),
    "idx_scheduler_tasks_graph_id": ("scheduler_tasks", "graph_id"),
    "idx_scheduler_history_graph_id": ("scheduler_history", "graph_id"),
    "idx_scheduler_history_schedule_id": ("scheduler_history", "schedule_id"),
    "idx_scheduler_runs_graph_id": ("scheduler_runs", "graph_id"),
    "idx_scheduler_evolution_graph_id": ("scheduler_evolution", "graph_id"),
    "idx_worker_history_worker_id": ("worker_history", "worker_id"),
    "idx_proposed_workers_status": ("proposed_workers", "status"),
    "idx_knowledge_history_knowledge_id": ("knowledge_history", "knowledge_id"),
    "idx_evolution_events_knowledge_id": ("evolution_events", "knowledge_id"),
    "idx_understanding_history_understanding_id": ("understanding_history", "understanding_id"),
    "idx_understanding_evolution_understanding_id": ("understanding_evolution", "understanding_id"),
    "idx_initiative_history_initiative_id": ("initiative_history", "initiative_id"),
    "idx_initiative_evolution_initiative_id": ("initiative_evolution", "initiative_id"),
    "idx_insight_history_insight_id": ("insight_history", "insight_id"),
    "idx_insight_evolution_insight_id": ("insight_evolution", "insight_id"),
    "idx_plan_history_plan_id": ("plan_history", "plan_id"),
    "idx_plan_evolution_plan_id": ("plan_evolution", "plan_id"),
    "idx_relationships_repo_a": ("relationships", "repo_a"),
    "idx_relationships_repo_b": ("relationships", "repo_b"),
    "idx_runtime_sessions_schedule_id": ("runtime_sessions", "schedule_id"),
    "idx_runtime_events_session_id": ("runtime_events", "session_id"),
    "idx_runtime_tasks_session_id": ("runtime_tasks", "session_id"),
    "idx_runtime_results_session_id": ("runtime_results", "session_id"),
    "idx_runtime_results_execution_id": ("runtime_results", "execution_id"),
    "idx_runtime_history_session_id": ("runtime_history", "session_id"),
    "idx_runtime_evolution_session_id": ("runtime_evolution", "session_id"),
}


def _list_indexes(conn, table: str) -> set[str]:
    """Return set of index names for a table."""
    rows = conn.execute(f"PRAGMA index_list({table!r})").fetchall()
    return {r["name"] for r in rows}


def _table_exists(conn, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Test 1: Every expected index exists
# ---------------------------------------------------------------------------


def test_all_expected_indexes_exist(conn):
    """Each index in EXPECTED_INDEXES must exist in the database."""
    checked = 0
    for idx_name, (table, column) in sorted(EXPECTED_INDEXES.items()):
        if not _table_exists(conn, table):
            pytest.skip(f"table {table} does not exist (schema not applied)")
        indexes = _list_indexes(conn, table)
        assert idx_name in indexes, (
            f"Index {idx_name} not found on {table}. "
            f"Existing indexes: {sorted(indexes)}"
        )
        checked += 1
    assert checked > 0, "no indexes were checked (no tables exist?)"


# ---------------------------------------------------------------------------
# Test 2: EXPLAIN QUERY PLAN BEFORE (no indexes = SCAN) vs AFTER (with indexes = SEARCH)
# ---------------------------------------------------------------------------

# Schema WITHOUT indexes to prove the baseline is SCAN.
# Split on the first CREATE INDEX statement rather than a comment for durability.
_SCHEMA_INDEXES_START = SCHEMA.find("\nCREATE INDEX IF NOT EXISTS")
SCHEMA_NO_INDEXES = SCHEMA[:_SCHEMA_INDEXES_START] if _SCHEMA_INDEXES_START >= 0 else SCHEMA


@pytest.fixture
def conn_no_indexes():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    c.executescript(SCHEMA_NO_INDEXES)
    yield c
    c.close()


# Each entry: (query_name, sql, params)
HOT_PATH_QUERIES = [
    ("tasks by graph_id", "SELECT * FROM tasks WHERE graph_id = ?", ("g1",)),
    ("task_edges by graph_id", "SELECT * FROM task_edges WHERE graph_id = ?", ("g1",)),
    ("task_graphs by status", "SELECT COUNT(*) FROM task_graphs WHERE status = ?", ("proposal",)),
    ("proposed_workers by status",
     "SELECT * FROM proposed_workers WHERE status = ? ORDER BY created_at DESC",
     ("pending",)),
    ("knowledge_history by knowledge_id",
     "SELECT * FROM knowledge_history WHERE knowledge_id = ? ORDER BY build_at",
     ("k1",)),
]


def test_explain_query_plan_before_after():
    """Show that each hot-path query transitions from SCAN (no indexes)
    to SEARCH (with indexes), proving the index actually changes the plan."""
    # BEFORE: schema without indexes
    c_before = sqlite3.connect(":memory:")
    c_before.row_factory = sqlite3.Row
    c_before.execute("PRAGMA foreign_keys = ON")
    c_before.executescript(SCHEMA_NO_INDEXES)

    # AFTER: schema with indexes
    c_after = sqlite3.connect(":memory:")
    c_after.row_factory = sqlite3.Row
    c_after.execute("PRAGMA foreign_keys = ON")
    c_after.executescript(SCHEMA)

    for name, sql, params in HOT_PATH_QUERIES:
        plan_before = " ".join(
            r["detail"] for r in c_before.execute(f"EXPLAIN QUERY PLAN {sql}", params))
        plan_after = " ".join(
            r["detail"] for r in c_after.execute(f"EXPLAIN QUERY PLAN {sql}", params))

        # BEFORE must be SCAN (full table scan)
        assert "SCAN" in plan_before, (
            f"BEFORE: Expected SCAN for '{name}' but got: {plan_before}")
        # AFTER must be SEARCH (indexed lookup)
        assert "SEARCH" in plan_after, (
            f"AFTER: Expected SEARCH for '{name}' but got: {plan_after}")

    c_before.close()
    c_after.close()


def test_explain_query_plan_selected_queries(conn):
    """For each hot-path query (with indexes active), verify SEARCH."""
    for name, sql, params in HOT_PATH_QUERIES:
        plan_rows = conn.execute(f"EXPLAIN QUERY PLAN {sql}", params).fetchall()
        plan_text = " ".join(r["detail"] for r in plan_rows)
        assert "SEARCH" in plan_text and "USING" in plan_text, (
            f"Query '{name}' did not use an index.\n"
            f"  SQL: {sql}\n"
            f"  Plan: {plan_text}\n"
            f"  Expected: SEARCH ... USING INDEX/COVERING INDEX\n"
            f"  Got: {plan_text}"
        )


# ---------------------------------------------------------------------------
# Test 3: Relationship OR query plan
# ---------------------------------------------------------------------------


def test_relationships_or_query_uses_index(conn):
    """WHERE repo_a = ? OR repo_b = ? should use at least one index."""
    plan = conn.execute(
        "EXPLAIN QUERY PLAN "
        "SELECT * FROM relationships WHERE repo_a = ? OR repo_b = ?",
        (1, 1),
    ).fetchall()
    plan_text = " ".join(r["detail"] for r in plan)
    assert "SEARCH" in plan_text and ("USING INDEX" in plan_text or "USING COVERING INDEX" in plan_text), (
        f"Relationships OR query did not use index.\nPlan: {plan_text}"
    )


# ---------------------------------------------------------------------------
# Test 4: All auto-indexed PK/UNIQUE columns are NOT redundantly indexed
# ---------------------------------------------------------------------------


def test_no_redundant_pk_indexes(conn):
    """Columns covered by PK/UNIQUE leftmost prefix must not have a separate
    redundant index (the PK/UNIQUE already covers those lookups)."""
    # These should NOT have a separate single-column index since they are
    # covered by their table's composite PK (leftmost column) or single PK.
    covered_by_pk = {
        "languages": ["repo_id"],           # PK(repo_id, language)
        "technologies": ["repo_id"],        # PK(repo_id, tech)
        "components": ["repo_id"],          # PK(repo_id, name)
        "entry_points": ["repo_id"],        # PK(repo_id, kind, detail)
        "architecture": ["repo_id"],        # PK(repo_id) — single col
        "worker_capabilities": ["worker_id"],  # PK(worker_id, capability)
        "worker_versions": ["worker_id"],   # PK(worker_id, version)
        "resolver_assignments": ["graph_id"],  # UNIQUE(graph_id, task_id)
        "scheduler_tasks": ["schedule_id"], # PK(schedule_id)
        "runtime_sessions": ["session_id"], # PK(session_id)
    }
    for table, cols in covered_by_pk.items():
        if not _table_exists(conn, table):
            continue
        indexes = _list_indexes(conn, table)
        for col in cols:
            # Check for any index that has this column as its only indexed col
            # (i.e. NOT the composite PK itself)
            for idx_name in indexes:
                # Skip SQLite auto-generated PK indexes (named sqlite_autoindex_...)
                if idx_name.startswith("sqlite_autoindex"):
                    continue
                idx_info = conn.execute(
                    f"PRAGMA index_info({idx_name!r})"
                ).fetchall()
                idx_cols = [r["name"] for r in idx_info]
                if len(idx_cols) == 1 and idx_cols[0] == col:
                    pytest.fail(
                        f"Redundant single-column index {idx_name} on {table}({col}) "
                        f"— column is already covered by PK/UNIQUE leftmost prefix"
                    )
