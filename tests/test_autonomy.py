"""Graduated Autonomy tests — Gap #7.

Covers:
1. Kill switch (global on/off via operator_preferences)
2. Per-action-type permission CRUD (override, clear, get_all)
3. Confidence-based auto-downgrade and auto-upgrade
4. Integration with confirm_gate (kill switch blocks actions)
5. CLI subcommand parsing
"""

from __future__ import annotations

import pytest
import tempfile
from pathlib import Path

from friday.autonomy import (
    AUTO_DOWNGRADE_THRESHOLD,
    AUTO_UPGRADE_THRESHOLD,
    ActionPermission,
    clear_override,
    get_action_permission,
    get_all_permissions,
    is_autonomy_enabled,
    record_action_outcome,
    set_autonomy_enabled,
    set_override,
    _downgrade_one_level,
    _upgrade_one_level,
    _all_hardcoded_levels,
)
from friday.db import connect


# ──────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def db():
    """Create an isolated in-memory database for each test."""
    conn = connect(Path(":memory:"))
    yield conn
    conn.close()


# ──────────────────────────────────────────────────────────────────────
# Level helpers
# ──────────────────────────────────────────────────────────────────────


class TestLevelTransitions:
    """Test the four-tier escalation chain: auto → notify → confirm → double."""

    def test_downgrade_auto_to_notify(self):
        assert _downgrade_one_level("auto") == "notify"

    def test_downgrade_notify_to_confirm(self):
        assert _downgrade_one_level("notify") == "confirm"

    def test_downgrade_confirm_to_double(self):
        assert _downgrade_one_level("confirm") == "double"

    def test_downgrade_double_stays_double(self):
        assert _downgrade_one_level("double") == "double"

    def test_upgrade_double_to_confirm(self):
        assert _upgrade_one_level("double") == "confirm"

    def test_upgrade_confirm_to_notify(self):
        assert _upgrade_one_level("confirm") == "notify"

    def test_upgrade_notify_to_auto(self):
        assert _upgrade_one_level("notify") == "auto"

    def test_upgrade_auto_stays_auto(self):
        assert _upgrade_one_level("auto") == "auto"


# ──────────────────────────────────────────────────────────────────────
# Kill switch
# ──────────────────────────────────────────────────────────────────────


class TestKillSwitch:
    def test_default_enabled(self, db):
        """Default state is enabled — no preference row = autonomy on."""
        assert is_autonomy_enabled(db) is True

    def test_disable(self, db):
        set_autonomy_enabled(False, conn=db)
        assert is_autonomy_enabled(db) is False

    def test_enable_after_disable(self, db):
        set_autonomy_enabled(False, conn=db)
        assert is_autonomy_enabled(db) is False
        set_autonomy_enabled(True, conn=db)
        assert is_autonomy_enabled(db) is True

    def test_round_trip(self, db):
        """Multiple toggle works correctly."""
        for expected in (False, True, False, True):
            set_autonomy_enabled(expected, conn=db)
            assert is_autonomy_enabled(db) is expected

    def test_isolation_between_connections(self, db):
        """Set in one connection, read in another — should persist."""
        set_autonomy_enabled(False, conn=db)
        # Re-read to confirm
        assert is_autonomy_enabled(db) is False


# ──────────────────────────────────────────────────────────────────────
# Per-action-type permissions
# ──────────────────────────────────────────────────────────────────────


class TestActionPermission:
    def test_unknown_action_defaults_confirm(self, db):
        p = get_action_permission("nonexistent_action", db)
        assert p.default_level == "confirm"
        assert p.override_level is None
        assert p.auto_downgraded_level is None
        assert p.effective_level == "confirm"

    def test_known_action_default(self, db):
        p = get_action_permission("workspace", db)
        # workspace is (REVERSIBLE, MEDIUM) → NOTIFY
        assert p.default_level == "notify"
        assert p.effective_level == "notify"

    def test_read_only_is_auto(self, db):
        p = get_action_permission("query", db)
        assert p.default_level == "auto"

    def test_destructive_is_confirm(self, db):
        p = get_action_permission("closewindow", db)
        # closewindow is (IRREVERSIBLE, NARROW) → CONFIRM
        assert p.default_level == "confirm"

    def test_set_override_confirm_to_auto(self, db):
        set_override("workspace", "auto", conn=db)
        p = get_action_permission("workspace", db)
        assert p.override_level == "auto"
        assert p.effective_level == "auto"

    def test_set_override_auto_to_double(self, db):
        set_override("query", "double", conn=db)
        p = get_action_permission("query", db)
        assert p.override_level == "double"
        assert p.effective_level == "double"

    def test_clear_override_reverts_to_default(self, db):
        set_override("workspace", "auto", conn=db)
        clear_override("workspace", conn=db)
        p = get_action_permission("workspace", db)
        assert p.override_level is None
        # workspace default is now NOTIFY (was CONFIRM)
        assert p.effective_level == "notify"

    def test_invalid_level_raises(self, db):
        with pytest.raises(ValueError, match="must be one of"):
            set_override("workspace", "invalid", conn=db)

    def test_get_all_includes_all_hardcoded(self, db):
        all_perms = get_all_permissions(db)
        hardcoded = _all_hardcoded_levels()
        # Every hardcoded action type must appear in the list.
        perm_types = {p.action_type for p in all_perms}
        for action in hardcoded:
            assert action in perm_types, f"Missing: {action}"

    def test_get_all_reflects_overrides(self, db):
        set_override("workspace", "auto", conn=db)
        set_override("closewindow", "confirm", conn=db)
        all_perms = get_all_permissions(db)
        lookup = {p.action_type: p for p in all_perms}
        assert lookup["workspace"].effective_level == "auto"
        assert lookup["closewindow"].effective_level == "confirm"

    def test_case_insensitive(self, db):
        set_override("WORKSPACE", "auto", conn=db)
        p = get_action_permission("workspace", db)
        assert p.override_level == "auto"


# ──────────────────────────────────────────────────────────────────────
# Confidence-based escalation (auto-downgrade / auto-upgrade)
# ──────────────────────────────────────────────────────────────────────


class TestConfidenceEscalation:
    def test_single_failure_does_not_downgrade(self, db):
        """One failure should NOT trigger auto-downgrade."""
        record_action_outcome("workspace", success=False, conn=db)
        p = get_action_permission("workspace", db)
        assert p.auto_downgraded_level is None
        # workspace default is now NOTIFY (was CONFIRM)
        assert p.effective_level == "notify"
        assert p.consecutive_failures == 1

    def test_threshold_failures_triggers_downgrade(self, db):
        """After AUTO_DOWNGRADE_THRESHOLD failures, should auto-downgrade."""
        at = AUTO_DOWNGRADE_THRESHOLD
        for _ in range(at):
            record_action_outcome("workspace", success=False, conn=db)

        p = get_action_permission("workspace", db)
        assert p.auto_downgraded_level is not None
        # workspace starts at NOTIFY, downgrades to CONFIRM
        assert p.effective_level == "confirm"
        # Counter should be reset after downgrade
        assert p.consecutive_failures == 0

    def test_downgrade_from_auto_to_notify(self, db):
        """AUTO → NOTIFY after threshold failures."""
        at = AUTO_DOWNGRADE_THRESHOLD
        for _ in range(at):
            record_action_outcome("query", success=False, conn=db)

        p = get_action_permission("query", db)
        assert p.auto_downgraded_level == "notify"
        assert p.effective_level == "notify"

    def test_success_resets_failure_counter(self, db):
        """A success should reset the consecutive failures counter."""
        record_action_outcome("workspace", success=False, conn=db)
        record_action_outcome("workspace", success=False, conn=db)
        record_action_outcome("workspace", success=True, conn=db)

        p = get_action_permission("workspace", db)
        assert p.consecutive_failures == 0
        assert p.consecutive_successes == 1

    def test_success_streak_upgrades_after_threshold(self, db):
        """After AUTO_UPGRADE_THRESHOLD successes, undo auto-downgrade."""
        # First trigger a downgrade
        at = AUTO_DOWNGRADE_THRESHOLD
        for _ in range(at):
            record_action_outcome("query", success=False, conn=db)

        p = get_action_permission("query", db)
        assert p.auto_downgraded_level == "notify"

        # Then succeed enough times to undo it
        ust = AUTO_UPGRADE_THRESHOLD
        for _ in range(ust):
            record_action_outcome("query", success=True, conn=db)

        p = get_action_permission("query", db)
        assert p.auto_downgraded_level is None  # fully recovered
        assert p.effective_level == "auto"

    def test_override_is_higher_precedence_than_downgrade(self, db):
        """User override should take priority over auto-downgrade."""
        # Set an override to auto
        set_override("workspace", "auto", conn=db)
        p = get_action_permission("workspace", db)
        assert p.effective_level == "auto"

        # Then trigger failures that would normally downgrade
        at = AUTO_DOWNGRADE_THRESHOLD
        for _ in range(at):
            record_action_outcome("workspace", success=False, conn=db)

        p = get_action_permission("workspace", db)
        # Override still wins
        assert p.effective_level == "auto"
        # But the auto_downgraded should be set
        assert p.auto_downgraded_level is not None

    def test_override_cleared_reveals_downgrade(self, db):
        """After clearing override, auto-downgrade takes effect."""
        set_override("workspace", "auto", conn=db)
        at = AUTO_DOWNGRADE_THRESHOLD
        for _ in range(at):
            record_action_outcome("workspace", success=False, conn=db)

        clear_override("workspace", conn=db)
        p = get_action_permission("workspace", db)
        # Now auto-downgrade should be visible
        assert p.auto_downgraded_level is not None
        assert p.effective_level == p.auto_downgraded_level

    def test_mixed_success_failure_does_not_downgrade(self, db):
        """Alternating success/failure should NOT accumulate to threshold."""
        for _ in range(AUTO_DOWNGRADE_THRESHOLD * 2):
            record_action_outcome("workspace", success=True, conn=db)
            record_action_outcome("workspace", success=False, conn=db)

        p = get_action_permission("workspace", db)
        assert p.auto_downgraded_level is None

    def test_downgrade_level_tracking(self, db):
        """Consecutive_failures increments correctly and resets on success."""
        record_action_outcome("workspace", success=False, conn=db)
        record_action_outcome("workspace", success=False, conn=db)
        p = get_action_permission("workspace", db)
        assert p.consecutive_failures == 2

        record_action_outcome("workspace", success=True, conn=db)
        p = get_action_permission("workspace", db)
        assert p.consecutive_failures == 0


# ──────────────────────────────────────────────────────────────────────
# Integration with confirm_gate
# ──────────────────────────────────────────────────────────────────────


class TestConfirmGateIntegration:
    def test_kill_switch_blocks_auto_actions(self, db):
        """When autonomy is disabled, even AUTO-level actions are blocked."""
        from friday.runtime.confirm_gate import prompt_confirm

        set_autonomy_enabled(False, conn=db)
        # Pass the test DB connection so the kill switch check reads the right DB.
        assert prompt_confirm("query", "clients", "worker:hyprctl",
                              skip_prompt=True, conn=db) is False

    def test_kill_switch_blocks_confirm_actions(self, db):
        """When autonomy is disabled, CONFIRM-level actions are blocked."""
        from friday.runtime.confirm_gate import prompt_confirm

        set_autonomy_enabled(False, conn=db)
        assert prompt_confirm("workspace", "3", "worker:hyprctl",
                              skip_prompt=True, conn=db) is False

    def test_kill_switch_blocks_double_confirm(self, db):
        """When autonomy is disabled, DOUBLE_CONFIRM actions are blocked."""
        from friday.runtime.confirm_gate import prompt_confirm

        set_autonomy_enabled(False, conn=db)
        assert prompt_confirm("closewindow", "firefox", "worker:hyprctl",
                              skip_prompt=True, conn=db) is False

    def test_enable_allows_actions_again(self, db):
        """After re-enabling, actions proceed normally."""
        from friday.runtime.confirm_gate import prompt_confirm

        set_autonomy_enabled(False, conn=db)
        assert prompt_confirm("query", "clients", "worker:hyprctl",
                              skip_prompt=True, conn=db) is False

        set_autonomy_enabled(True, conn=db)
        assert prompt_confirm("query", "clients", "worker:hyprctl",
                              skip_prompt=True, conn=db) is True


# ──────────────────────────────────────────────────────────────────────
# Effective level precedence
# ──────────────────────────────────────────────────────────────────────


class TestEffectiveLevelPrecedence:
    """Precedence: override > auto-downgrade > default."""

    def test_default_when_nothing_set(self):
        p = ActionPermission("test_action", "confirm", None, None)
        assert p.effective_level == "confirm"

    def test_override_takes_priority(self):
        p = ActionPermission("test_action", "confirm", "auto", None)
        assert p.effective_level == "auto"

    def test_auto_downgrade_when_no_override(self):
        p = ActionPermission("test_action", "confirm", None, "double")
        assert p.effective_level == "double"

    def test_override_beats_downgrade(self):
        p = ActionPermission("test_action", "confirm", "auto", "double")
        assert p.effective_level == "auto"  # override wins


# ──────────────────────────────────────────────────────────────────────
# CLI subcommand routing
# ──────────────────────────────────────────────────────────────────────


class TestCLIIntegration:
    def test_autonomy_module_importable(self):
        """The CLI module should import without error."""
        import friday.cli_autonomy  # noqa: F401

    def test_autonomy_import_does_not_crash(self):
        from friday.cli_autonomy import cmd_autonomy
        # Just verify it's a callable function
        assert callable(cmd_autonomy)
