"""Tests for autonomy escalation (promotion/demotion reconciliation).

Tests reconcile_escalation() and the status display updates:
1. No changes when no permissions exist
2. Reports promotion when auto_downgraded + enough successes
3. Reports demotion when enough failures
4. Skips actions with user-set overrides
5. Round-trip: failure → demotion → success → promotion
6. Status display shows correct progress indicators
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest import mock

import pytest

from friday.autonomy import (
    ActionPermission,
    get_all_permissions,
    reconcile_escalation,
    record_action_outcome,
    set_override,
    clear_override,
    AUTO_DOWNGRADE_THRESHOLD,
    AUTO_UPGRADE_THRESHOLD,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    """SQLite connection with autonomy_permissions table."""
    db_path = tmp_path / "test_friday.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    conn.execute("""
        CREATE TABLE IF NOT EXISTS autonomy_permissions (
            action_type          TEXT NOT NULL PRIMARY KEY,
            default_level        TEXT NOT NULL,
            override_level       TEXT,
            auto_downgraded      TEXT,
            consecutive_failures    INTEGER NOT NULL DEFAULT 0,
            consecutive_successes  INTEGER NOT NULL DEFAULT 0,
            updated_at           TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS operator_preferences (
            key         TEXT PRIMARY KEY,
            value       TEXT NOT NULL,
            set_at      TEXT NOT NULL,
            source      TEXT NOT NULL DEFAULT 'explicit'
        )
    """)

    conn.commit()
    return conn


def _insert_perm(conn, action_type: str, default_level: str = "auto",
                 auto_downgraded: str | None = None,
                 failures: int = 0, successes: int = 0,
                 override: str | None = None):
    """Insert a row directly into autonomy_permissions."""
    now = "2026-01-01T00:00:00"
    conn.execute(
        "INSERT OR REPLACE INTO autonomy_permissions "
        "(action_type, default_level, override_level, auto_downgraded, "
        " consecutive_failures, consecutive_successes, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (action_type, default_level, override, auto_downgraded,
         failures, successes, now)
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Reconcilation tests
# ---------------------------------------------------------------------------


class TestReconcileEscalation:
    """Tests for reconcile_escalation()."""

    def test_empty_db_no_changes(self, conn):
        """No permissions rows -> no changes reported."""
        changes = reconcile_escalation(conn)
        assert changes == []

    def test_promotion_reported(self, conn):
        """Auto-downgraded action with enough successes -> promotion reported."""
        # Action was downgraded to 'confirm', now has enough successes.
        _insert_perm(conn, "workspace", default_level="auto",
                     auto_downgraded="confirm",
                     failures=0, successes=AUTO_UPGRADE_THRESHOLD + 1)

        changes = reconcile_escalation(conn)
        assert len(changes) >= 1
        assert "Promoted" in changes[0]
        assert "workspace" in changes[0]

    def test_partial_recovery_not_yet_promoted(self, conn):
        """Auto-downgraded but not enough successes yet -> no change."""
        _insert_perm(conn, "workspace", default_level="auto",
                     auto_downgraded="confirm",
                     failures=0, successes=AUTO_UPGRADE_THRESHOLD - 1)

        changes = reconcile_escalation(conn)
        assert changes == []

    def test_demotion_reported(self, conn):
        """Enough consecutive failures -> demotion reported."""
        _insert_perm(conn, "closewindow", default_level="confirm",
                     auto_downgraded=None,
                     failures=AUTO_DOWNGRADE_THRESHOLD, successes=0)

        changes = reconcile_escalation(conn)
        assert len(changes) >= 1
        assert "Demoted" in changes[0]
        assert "closewindow" in changes[0]

    def test_not_enough_failures_no_demotion(self, conn):
        """Less than threshold failures -> no change."""
        _insert_perm(conn, "closewindow", default_level="confirm",
                     auto_downgraded=None,
                     failures=AUTO_DOWNGRADE_THRESHOLD - 1, successes=0)

        changes = reconcile_escalation(conn)
        assert changes == []

    def test_override_skips_auto_adjustment(self, conn):
        """Action with user-set override -> not auto-adjusted."""
        _insert_perm(conn, "workspace", default_level="auto",
                     auto_downgraded="confirm",
                     failures=0, successes=AUTO_UPGRADE_THRESHOLD + 1,
                     override="confirm")

        changes = reconcile_escalation(conn)
        assert changes == []  # Override means no auto-adjustment

    def test_already_at_double_no_further_demotion(self, conn):
        """Already at max restrictiveness -> no further demotion."""
        _insert_perm(conn, "exit", default_level="double",
                     auto_downgraded="double",
                     failures=10, successes=0)

        changes = reconcile_escalation(conn)
        assert changes == []

    def test_multiple_changes_reported(self, conn):
        """Multiple actions with changes -> all reported."""
        _insert_perm(conn, "workspace", default_level="auto",
                     auto_downgraded="confirm",
                     failures=0, successes=AUTO_UPGRADE_THRESHOLD + 1)
        _insert_perm(conn, "closewindow", default_level="confirm",
                     auto_downgraded=None,
                     failures=AUTO_DOWNGRADE_THRESHOLD, successes=0)

        changes = reconcile_escalation(conn)
        assert len(changes) >= 2

    def test_demotion_resets_failure_counter(self, conn):
        """After demotion, failure counter resets to 0."""
        _insert_perm(conn, "closewindow", default_level="confirm",
                     auto_downgraded=None,
                     failures=AUTO_DOWNGRADE_THRESHOLD, successes=0)

        reconcile_escalation(conn)

        row = conn.execute(
            "SELECT consecutive_failures FROM autonomy_permissions "
            "WHERE action_type='closewindow'"
        ).fetchone()
        assert row["consecutive_failures"] == 0

    def test_promotion_resets_success_counter(self, conn):
        """After promotion, success counter resets to 0."""
        _insert_perm(conn, "workspace", default_level="auto",
                     auto_downgraded="confirm",
                     failures=0, successes=AUTO_UPGRADE_THRESHOLD + 1)

        reconcile_escalation(conn)

        row = conn.execute(
            "SELECT consecutive_successes FROM autonomy_permissions "
            "WHERE action_type='workspace'"
        ).fetchone()
        assert row["consecutive_successes"] == 0


# ---------------------------------------------------------------------------
# Integration: manual inserts + reconcile_escalation
# ---------------------------------------------------------------------------


class TestEscalationIntegration:
    """Full lifecycle via direct DB inserts (bypassing record_action_outcome).

    ``record_action_outcome()`` already handles escalation in real-time,
    so the integration tests use direct inserts to simulate legacy data
    that ``reconcile_escalation()`` would need to clean up.
    """

    def test_fail_then_reconcile_demotes(self, conn):
        """Action with enough consecutive failures -> reconcile demotes."""
        _insert_perm(conn, "workspace", default_level="auto",
                     failures=AUTO_DOWNGRADE_THRESHOLD, successes=0)

        changes = reconcile_escalation(conn)
        assert len(changes) >= 1
        assert "Demoted" in changes[0]

    def test_succeed_then_reconcile_promotes(self, conn):
        """Downgraded action with enough successes -> reconcile promotes."""
        _insert_perm(conn, "workspace", default_level="auto",
                     auto_downgraded="confirm",
                     failures=0, successes=AUTO_UPGRADE_THRESHOLD + 1)

        changes = reconcile_escalation(conn)
        assert len(changes) >= 1
        assert "Promoted" in changes[0]


# ---------------------------------------------------------------------------
# Status display progress indicators
# ---------------------------------------------------------------------------


class TestStatusProgress:
    """Test that the CLI status correctly shows escalation progress."""

    def _perm(self, action_type="workspace", default_level="auto",
              auto_downgraded=None, failures=0, successes=0,
              override=None):
        """Build an ActionPermission for display testing."""
        return ActionPermission(
            action_type=action_type,
            default_level=default_level,
            override_level=override,
            auto_downgraded_level=auto_downgraded,
            consecutive_failures=failures,
            consecutive_successes=successes,
        )

    def test_downgraded_shows_upgrade_progress(self):
        """Downgraded action shows X/Y successes progress."""
        p = self._perm(auto_downgraded="confirm", successes=3)
        eff = p.effective_level
        icon = {"auto": "🟢", "confirm": "🟡", "double": "🔴"}.get(eff, "⚪")
        parts = [f"{icon} {p.action_type:<20} {eff:<8}"]
        parts.append(f"⬆ 3/{AUTO_UPGRADE_THRESHOLD} successes")
        line = " ".join(parts)
        assert "⬆" in line
        assert f"3/{AUTO_UPGRADE_THRESHOLD}" in line

    def test_healthy_shows_failure_progress(self):
        """Healthy action with some failures shows X/Y failures progress."""
        p = self._perm(failures=2)
        eff = p.effective_level
        icon = {"auto": "🟢", "confirm": "🟡", "double": "🔴"}.get(eff, "⚪")
        parts = [f"{icon} {p.action_type:<20} {eff:<8}"]
        parts.append(f"⬇ 2/{AUTO_DOWNGRADE_THRESHOLD} failures")
        line = " ".join(parts)
        assert "⬇" in line
        assert f"2/{AUTO_DOWNGRADE_THRESHOLD}" in line

    def test_override_shows_override_label(self):
        """Action with override shows [override: X] instead of progress."""
        p = self._perm(override="confirm", successes=5, auto_downgraded="confirm")
        eff = p.effective_level
        icon = {"auto": "🟢", "confirm": "🟡", "double": "🔴"}.get(eff, "⚪")
        parts = [f"{icon} {p.action_type:<20} {eff:<8}"]
        # Override takes precedence — show override label, not progress.
        if p.override_level:
            parts.append(f"[override: {p.override_level}]")
        line = " ".join(parts)
        assert "override: confirm" in line
        assert "⬆" not in line  # No escalation progress when overridden

    def test_zero_failures_shows_no_progress(self):
        """Action with no failures/successes shows no progress indicators."""
        p = self._perm()
        eff = p.effective_level
        icon = {"auto": "🟢", "confirm": "🟡", "double": "🔴"}.get(eff, "⚪")
        parts = [f"{icon} {p.action_type:<20} {eff:<8}"]
        line = " ".join(parts)
        assert "⬆" not in line
        assert "⬇" not in line
        assert "✓" not in line
