"""Operator Identity — Phase 1 tests.

Covers:
  1. Evidence-derived fields compute correctly against a seeded DB.
  2. `friday profile set` / `unset` round-trip correctly, each key independently.
  3. No code path other than explicit `set` writes to operator_preferences
     with source='explicit'.
  4. Empty profile produces no errors — all fields are None/empty.
"""

from __future__ import annotations

import json
import tempfile

import pytest

from friday.db import (
    ProposedWorkerRow,
    connect,
    get_all_operator_preferences,
    get_operator_preference,
    insert_proposed_worker,
    set_operator_preference,
    unset_operator_preference,
)
from friday.operator import build_operator_profile


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def db():
    tmp = tempfile.mktemp(suffix=".db")
    conn = connect(tmp)
    yield conn
    conn.close()


def _seed_proposal(conn, status: str, goal: str = "Implement OAuth",
                   gap: str = "rust", manifest: str = "{}"):
    pw = ProposedWorkerRow(
        id=f"proposal:{gap}:{goal}",
        detected_from_goal=goal,
        capability_gap=gap,
        draft_manifest_json=manifest,
        status=status,
        created_at="2026-07-23T00:00:00Z",
        reviewed_at="2026-07-23T01:00:00Z" if status != "pending" else None,
    )
    insert_proposed_worker(conn, pw)


def _seed_graph(conn, status: str, goal: str = "Test goal",
                plan_type: str = "feature", task_count: int = 3,
                edge_count: int = 2):
    import hashlib
    plan_id = f"plan:{goal.lower().replace(' ', '_')}"
    gid = f"taskgraph:plan:{goal.lower().replace(' ', '_')}:{hashlib.md5(goal.encode()).hexdigest()[:8]}"
    # Ensure the referenced plan exists (FK: task_graphs.plan_id -> plans.id)
    conn.execute(
        "INSERT OR IGNORE INTO plans "
        "(id, goal, plan_type, confidence, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (plan_id, goal, plan_type, "high", "planned",
         "2026-07-23T00:00:00Z", "2026-07-23T00:00:00Z"),
    )
    conn.execute(
        "INSERT OR REPLACE INTO task_graphs "
        "(id, goal, plan_id, plan_type, task_count, edge_count, "
        "critical_path_length, parallel_groups, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 1, 0, ?, ?, ?)",
        (gid, goal, plan_id, plan_type,
         task_count, edge_count, status, "2026-07-23T00:00:00Z", "2026-07-23T00:00:00Z"),
    )
    conn.commit()
    return gid


# ===========================================================================
# 1. Evidence-derived fields
# ===========================================================================


class TestEvidenceDerived:
    """Evidence-derived fields compute correctly from seeded DB."""

    def test_empty_db_returns_none_fields(self, db):
        """No proposals or graphs -> all derived fields are None."""
        profile = build_operator_profile(db)
        assert profile.capability_approval_rate is None
        assert profile.graph_review_pattern is None
        assert profile.explicit_preferences == {}

    def test_capability_rate_computes_correctly(self, db):
        """Correct approval rate from mixed proposal states."""
        _seed_proposal(db, "approved", gap="rust")
        _seed_proposal(db, "approved", gap="python")
        _seed_proposal(db, "rejected", gap="superintelligence")
        _seed_proposal(db, "pending", gap="testing")

        profile = build_operator_profile(db)
        cap = profile.capability_approval_rate
        assert cap is not None
        assert cap["approved"] == 2
        assert cap["rejected"] == 1
        assert cap["pending"] == 1
        assert cap["total"] == 4
        assert cap["rate"] == 0.5  # 2/4

    def test_capability_rate_no_proposals_is_none(self, db):
        """No proposals at all -> None, not zero."""
        profile = build_operator_profile(db)
        assert profile.capability_approval_rate is None

    def test_graph_review_computes_correctly(self, db):
        """Approved/rejected graph counts from task_graphs."""
        _seed_graph(db, "approved", "Build Rust project")
        _seed_graph(db, "approved", "Implement OAuth")
        _seed_graph(db, "rejected", "Rewrite everything")

        profile = build_operator_profile(db)
        gr = profile.graph_review_pattern
        assert gr is not None
        assert gr.get("approved") == 2
        assert gr.get("rejected") == 1

    def test_graph_review_no_graphs_is_none(self, db):
        """No task_graphs at all -> None."""
        profile = build_operator_profile(db)
        assert profile.graph_review_pattern is None

    def test_both_derived_fields_together(self, db):
        """Both evidence sources populated simultaneously."""
        _seed_proposal(db, "approved", gap="rust")
        _seed_proposal(db, "rejected", gap="fabricated")
        _seed_graph(db, "approved", "Test")
        _seed_graph(db, "rejected", "Bad idea")

        profile = build_operator_profile(db)
        assert profile.capability_approval_rate is not None
        assert profile.graph_review_pattern is not None
        assert profile.capability_approval_rate["approved"] == 1
        assert profile.graph_review_pattern["approved"] == 1


# ===========================================================================
# 2. Explicit set/unset round-trip
# ===========================================================================


class TestExplicitSetUnset:
    """friday profile set / unset round-trip correctly."""

    def test_set_and_get(self, db):
        """Setting a preference makes it retrievable."""
        set_operator_preference(db, "prefers_additive_changes", "true", source="explicit")
        row = get_operator_preference(db, "prefers_additive_changes")
        assert row is not None
        assert row.key == "prefers_additive_changes"
        assert row.value == "true"
        assert row.source == "explicit"

    def test_set_overwrites(self, db):
        """Setting the same key twice overwrites the value."""
        set_operator_preference(db, "key1", "old", source="explicit")
        set_operator_preference(db, "key1", "new", source="explicit")
        row = get_operator_preference(db, "key1")
        assert row.value == "new"

    def test_unset_removes(self, db):
        """Unsetting a key removes it from the DB."""
        set_operator_preference(db, "temp_key", "temp", source="explicit")
        assert get_operator_preference(db, "temp_key") is not None
        removed = unset_operator_preference(db, "temp_key")
        assert removed is True
        assert get_operator_preference(db, "temp_key") is None

    def test_unset_nonexistent_returns_false(self, db):
        """Unsetting a key that doesn't exist returns False, no error."""
        removed = unset_operator_preference(db, "nonexistent")
        assert removed is False

    def test_multiple_keys_independent(self, db):
        """Each key is independent — setting/unset one doesn't affect others."""
        set_operator_preference(db, "key_a", "val_a", source="explicit")
        set_operator_preference(db, "key_b", "val_b", source="explicit")
        set_operator_preference(db, "key_c", "val_c", source="explicit")

        unset_operator_preference(db, "key_b")

        all_prefs = get_all_operator_preferences(db, source="explicit")
        keys = {r.key for r in all_prefs}
        assert keys == {"key_a", "key_c"}

    def test_explicit_preferences_in_profile(self, db):
        """Explicit preferences show up in OperatorProfile."""
        set_operator_preference(db, "prefers_additive", "true", source="explicit")
        set_operator_preference(db, "max_parallel_workers", "4", source="explicit")

        profile = build_operator_profile(db)
        assert profile.explicit_preferences == {
            "prefers_additive": "true",
            "max_parallel_workers": "4",
        }


# ===========================================================================
# 3. No inference writes
# ===========================================================================


class TestNoInferenceWrites:
    """No code path writes to operator_preferences except explicit set."""

    def test_build_profile_never_writes(self, db):
        """build_operator_profile() is read-only — it never writes to DB."""
        # Count rows before
        before = len(get_all_operator_preferences(db))
        # Build profile (should be a pure read)
        build_operator_profile(db)
        # Count rows after — must be unchanged
        after = len(get_all_operator_preferences(db))
        assert before == after == 0

    def test_no_derived_rows_written(self, db):
        """Evidence-derived fields never write rows to operator_preferences."""
        _seed_proposal(db, "approved")
        _seed_proposal(db, "rejected")
        _seed_graph(db, "approved")
        _seed_graph(db, "rejected")

        profile = build_operator_profile(db)
        assert profile.capability_approval_rate is not None
        assert profile.graph_review_pattern is not None

        # No rows were written — derived fields are computed in memory only
        all_rows = get_all_operator_preferences(db)
        assert len(all_rows) == 0


# ===========================================================================
# 4. Profile shape / structural
# ===========================================================================


class TestProfileShape:
    """OperatorProfile structure and has_profile property."""

    def test_empty_profile_has_no_identity(self, db):
        """Empty profile has has_profile=False."""
        profile = build_operator_profile(db)
        assert profile.has_profile is False

    def test_populated_profile_has_identity(self, db):
        """Profile with data has has_profile=True."""
        _seed_proposal(db, "approved")
        profile = build_operator_profile(db)
        assert profile.has_profile is True

    def test_explicit_prefs_only(self, db):
        """Profile with only explicit prefs has has_profile=True."""
        set_operator_preference(db, "test", "value", source="explicit")
        profile = build_operator_profile(db)
        assert profile.has_profile is True
        assert profile.explicit_preferences == {"test": "value"}
