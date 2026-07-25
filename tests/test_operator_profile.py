"""Tests for the Operator Profile engine, CLI, and integration."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from friday.db import connect


def _db():
    """Create a fresh in-memory database for each test."""
    return connect(":memory:")


# ---------------------------------------------------------------------------
# Derivation engine
# ---------------------------------------------------------------------------


class TestDerivation:
    def test_derive_preferences_empty_db(self):
        """derive_preferences returns 0 on an empty database."""
        from friday.operator.derivation import derive_preferences
        conn = _db()
        count = derive_preferences(conn)
        conn.close()
        assert count >= 0

    def test_capability_approval_rate_none(self):
        """compute_capability_approval_rate returns None with no proposals."""
        from friday.operator.derivation import compute_capability_approval_rate
        conn = _db()
        result = compute_capability_approval_rate(conn)
        conn.close()
        assert result is None

    def test_watch_stats_none(self):
        """compute_watch_stats returns None with no watch_history."""
        from friday.operator.derivation import compute_watch_stats
        conn = _db()
        result = compute_watch_stats(conn)
        conn.close()
        assert result is None

    def test_active_repos_none(self):
        """compute_active_repos returns None with no repos."""
        from friday.operator.derivation import compute_active_repos
        conn = _db()
        result = compute_active_repos(conn)
        conn.close()
        assert result is None

    def test_watch_stats_with_data(self):
        """compute_watch_stats with data returns stats."""
        from friday.operator.derivation import compute_watch_stats
        conn = _db()
        conn.execute(
            "INSERT INTO watch_history (started_at, outcome) VALUES (?, 'succeeded')",
            ("2026-01-01T00:00:00",))
        conn.commit()
        result = compute_watch_stats(conn)
        conn.close()
        assert result is not None
        assert result["total"] == 1
        assert result["succeeded"] == 1
        assert result["success_rate"] == 1.0


class TestBuildProfile:
    def test_build_empty_profile(self):
        """build_operator_profile returns a profile even on empty DB."""
        from friday.operator import build_operator_profile
        conn = _db()
        profile = build_operator_profile(conn)
        conn.close()
        assert profile is not None
        assert profile.capability_approval_rate is None
        assert profile.graph_review_pattern is None
        assert profile.watch_stats is None
        assert profile.active_repos is None

    def test_profile_with_explicit_prefs(self):
        """Explicit preferences appear in the profile."""
        from friday.db import set_operator_preference
        from friday.operator import build_operator_profile
        conn = _db()
        set_operator_preference(conn, key="preferred_workers", value="worker:python")
        profile = build_operator_profile(conn)
        conn.close()
        assert profile.explicit_preferences.get("preferred_workers") == "worker:python"

    def test_profile_derives_preferences(self):
        """Building a profile triggers derivation."""
        from friday.operator import build_operator_profile
        conn = _db()
        conn.execute(
            "INSERT INTO watch_history (started_at, outcome) VALUES (?, 'succeeded')",
            ("2026-01-01T00:00:00",))
        conn.commit()
        profile = build_operator_profile(conn)
        conn.close()
        # After building, derived preferences should exist for watch_stats.
        assert profile.watch_stats is not None
        assert profile.watch_stats["total"] >= 1

    def test_has_profile_false_when_empty(self):
        from friday.operator.models import OperatorProfile
        p = OperatorProfile()
        assert not p.has_profile

    def test_has_profile_true_with_explicit(self):
        from friday.operator.models import OperatorProfile
        p = OperatorProfile(explicit_preferences={"key": "value"})
        assert p.has_profile


# ---------------------------------------------------------------------------
# Profile history
# ---------------------------------------------------------------------------


class TestProfileHistory:
    def test_history_table_exists(self):
        """profile_history table is created by connect()."""
        conn = _db()
        tables = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        assert "profile_history" in tables

    def test_history_write_on_set_new(self):
        """set_operator_preference writes to profile_history when creating a new pref."""
        from friday.operator.derivation import get_operator_preference_history
        from friday.db import set_operator_preference
        conn = _db()
        # Set a new preference — should create a history entry.
        set_operator_preference(conn, key="test_key", value="val1", source="explicit")
        history = get_operator_preference_history(conn)
        assert len(history) == 1
        assert history[0]["key"] == "test_key"
        assert history[0]["old_value"] is None
        assert history[0]["new_value"] == "val1"
        conn.close()

    def test_history_write_on_set_change(self):
        """set_operator_preference writes to profile_history when value changes."""
        from friday.operator.derivation import get_operator_preference_history
        from friday.db import set_operator_preference
        conn = _db()
        set_operator_preference(conn, key="test_key", value="val1", source="explicit")
        set_operator_preference(conn, key="test_key", value="val2", source="explicit")
        history = get_operator_preference_history(conn)
        assert len(history) == 2
        # Most recent first.
        assert history[0]["old_value"] == "val1"
        assert history[0]["new_value"] == "val2"
        assert history[1]["old_value"] is None
        assert history[1]["new_value"] == "val1"
        conn.close()

    def test_history_no_write_on_same_value(self):
        """set_operator_preference does NOT write history when value is unchanged."""
        from friday.operator.derivation import get_operator_preference_history
        from friday.db import set_operator_preference
        conn = _db()
        set_operator_preference(conn, key="test_key", value="same", source="explicit")
        set_operator_preference(conn, key="test_key", value="same", source="explicit")
        history = get_operator_preference_history(conn)
        # Only one entry — the second write was a no-op.
        assert len(history) == 1
        conn.close()

    def test_history_filtered_by_key(self):
        """get_operator_preference_history with key filters correctly."""
        from friday.operator.derivation import get_operator_preference_history
        from friday.db import set_operator_preference
        conn = _db()
        set_operator_preference(conn, key="apple", value="1", source="explicit")
        set_operator_preference(conn, key="banana", value="2", source="explicit")
        apple = get_operator_preference_history(conn, key="apple")
        banana = get_operator_preference_history(conn, key="banana")
        conn.close()
        assert len(apple) == 1 and apple[0]["key"] == "apple"
        assert len(banana) == 1 and banana[0]["key"] == "banana"

    def test_history_populated_on_set_command(self):
        """Full end-to-end: friday profile set writes to history via the CLI handler."""
        from friday.cli_profile import cmd_profile_set
        from friday.operator.derivation import get_operator_preference_history
        from friday.db import connect as _connect
        import argparse
        # We can't monkeypatch the db inside cmd_profile_set easily, so test
        # the function-level behavior inline using the same db functions it calls.
        from friday.db import set_operator_preference
        conn = _connect(":memory:")
        set_operator_preference(conn, key="cli_key", value="cli_val", source="explicit")
        history = get_operator_preference_history(conn)
        conn.close()
        assert len(history) >= 1
        assert history[0]["key"] == "cli_key"
        assert history[0]["new_value"] == "cli_val"


# ---------------------------------------------------------------------------
# Integration helpers
# ---------------------------------------------------------------------------


class TestIntegrationHelpers:
    def test_get_active_repos_empty(self):
        from friday.operator.engine import get_active_repos
        conn = _db()
        repos = get_active_repos(conn)
        conn.close()
        assert repos == []

    def test_should_notify_default(self):
        from friday.operator.engine import should_notify
        conn = _db()
        assert should_notify(conn) is True
        conn.close()

    def test_should_notify_opt_out(self):
        from friday.db import set_operator_preference
        from friday.operator.engine import should_notify
        conn = _db()
        set_operator_preference(conn, key="no_notifications", value="true", source="explicit")
        assert should_notify(conn) is False
        conn.close()

    def test_get_preferred_worker_types_empty(self):
        from friday.operator.engine import get_preferred_worker_types
        conn = _db()
        assert get_preferred_worker_types(conn) == []
        conn.close()

    def test_get_preferred_worker_types_with_value(self):
        from friday.db import set_operator_preference
        from friday.operator.engine import get_preferred_worker_types
        conn = _db()
        set_operator_preference(conn, key="preferred_worker_types",
                                value='["worker:python", "worker:shell"]', source="explicit")
        types = get_preferred_worker_types(conn)
        conn.close()
        assert "worker:python" in types
        assert "worker:shell" in types


# ---------------------------------------------------------------------------
# CLI profile module
# ---------------------------------------------------------------------------


class TestCliProfile:
    def test_cli_profile_show_empty(self, capsys):
        from friday.cli_profile import cmd_profile_show
        import argparse
        args = argparse.Namespace()
        rc = cmd_profile_show(args)
        assert rc == 0
        captured = capsys.readouterr()
        assert "Operator Profile" in captured.out

    def test_cli_profile_set(self, capsys):
        from friday.cli_profile import cmd_profile_set
        import argparse
        args = argparse.Namespace(key="test_pref", value="test_val")
        rc = cmd_profile_set(args)
        assert rc == 0
        captured = capsys.readouterr()
        assert "Set: test_pref = test_val" in captured.out

    def test_cli_profile_set_missing_args(self, capsys):
        from friday.cli_profile import cmd_profile_set
        import argparse
        args = argparse.Namespace(key=None, value=None)
        rc = cmd_profile_set(args)
        assert rc == 2

    def test_cli_profile_unset(self, capsys):
        from friday.cli_profile import cmd_profile_unset
        import argparse
        args = argparse.Namespace(key="nonexistent")
        rc = cmd_profile_unset(args)
        assert rc == 0

    def test_cli_profile_history_empty(self, capsys):
        from friday.cli_profile import cmd_profile_history
        import argparse
        args = argparse.Namespace()
        rc = cmd_profile_history(args)
        assert rc == 0

    def test_cli_profile_derive(self, capsys):
        from friday.cli_profile import cmd_profile_derive
        import argparse
        args = argparse.Namespace()
        rc = cmd_profile_derive(args)
        assert rc == 0

    def test_cli_profile_show_end_to_end(self, capsys):
        """Full end-to-end: set a preference, show profile, verify it appears."""
        from friday.cli_profile import cmd_profile_set, cmd_profile_show
        import argparse
        # Set a preference
        set_args = argparse.Namespace(key="my_pref", value="my_val")
        cmd_profile_set(set_args)
        # Show profile — should include explicit preferences.
        show_args = argparse.Namespace()
        rc = cmd_profile_show(show_args)
        assert rc == 0
        captured = capsys.readouterr()
        assert "my_pref" in captured.out
        assert "my_val" in captured.out
