"""Graduated Autonomy — kill switch, per-action permissions, confidence escalation.

Three mechanisms control Friday's autonomous execution:

1. **Kill switch** (global on/off) — Stored in ``operator_preferences`` as
   ``autonomy_enabled`` (``"true"`` / ``"false"``). When disabled, every action
   worker is blocked before dispatch — Friday explains why and refuses.

2. **Per-action-type permissions** — Stored in ``autonomy_permissions`` table.
   Override the hardcoded ``ActionLevel`` from ``confirm_gate``. Users can
   raise/lower any action's confirmation requirement. Persisted across restarts.

3. **Confidence-based auto-downgrade** — After ``AUTO_DOWNGRADE_THRESHOLD``
   consecutive failures for an action type, the system auto-downgrades its
   permission from the user-set or default level to one level more restrictive
   (AUTO → CONFIRM, CONFIRM → DOUBLE_CONFIRM). Consecutive successes reset
   the counter and undo the downgrade.

Precedence (highest to lowest):
  1. Kill switch disabled → block everything (emergency override)
  2. User-set override in autonomy_permissions (explicit user intent)
  3. Auto-downgrade level (system-adjusted when consecutive failures exceed threshold)
  4. Hardcoded default from confirm_gate._ACTION_LEVELS
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from typing import Optional

from .db import connect, now_iso

# ──────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────

# Preference keys in operator_preferences.
_AUTONOMY_KEY = "autonomy_enabled"
_KILL_SWITCH_KEY = "kill_switch"

# Default is on — Friday starts with full autonomy.
_AUTONOMY_DEFAULT = "true"
_KILL_SWITCH_DEFAULT = "false"

# Consecutive failures before auto-downgrade kicks in.
AUTO_DOWNGRADE_THRESHOLD = 3

# Consecutive successes needed to undo an auto-downgrade.
AUTO_UPGRADE_THRESHOLD = 5

# Valid permission levels (mirrors confirm_gate.ActionLevel values).
VALID_LEVELS = frozenset({"auto", "confirm", "double"})


# ──────────────────────────────────────────────────────────────────────
# Dataclasses
# ──────────────────────────────────────────────────────────────────────


@dataclass
class ActionPermission:
    """Current effective permission for one action type."""

    action_type: str
    default_level: str  # hardcoded baseline from confirm_gate
    override_level: Optional[str]  # user-set override, if any
    auto_downgraded_level: Optional[str]  # system-adjusted level, if any
    consecutive_failures: int = 0
    consecutive_successes: int = 0

    @property
    def effective_level(self) -> str:
        """Resolve the effective permission level by precedence.

        1. User override (highest).
        2. Auto-downgrade level (system adjusted).
        3. Default level (hardcoded).
        """
        if self.override_level is not None:
            return self.override_level
        if self.auto_downgraded_level is not None:
            return self.auto_downgraded_level
        return self.default_level


# ──────────────────────────────────────────────────────────────────────
# Kill switch
# ──────────────────────────────────────────────────────────────────────


def is_kill_switch_active(conn=None) -> bool:
    """Check the global emergency kill switch.

    Returns True when the kill switch has been pulled — ALL executors are
    blocked, not just action workers. This is the nuclear option that stops
    everything: new daemon cycles, executor dispatch, runtime sessions.

    Unlike ``is_autonomy_enabled()`` which only blocks action workers
    (Hyprland/Browser), the kill switch blocks the entire Friday execution
    pipeline. Running processes are NOT killed (they may complete or timeout)
    — no new work starts.
    """
    if conn is None:
        conn = connect()
        should_close = True
    else:
        should_close = False
    try:
        row = conn.execute(
            "SELECT value FROM operator_preferences WHERE key = ?",
            (_KILL_SWITCH_KEY,),
        ).fetchone()
        if row is None:
            return False  # default: not active
        return row["value"].strip().lower() == "true"
    finally:
        if should_close:
            conn.close()


def set_kill_switch(active: bool, conn=None) -> None:
    """Set the global emergency kill switch.

    When ``active=True``, ALL executor dispatch is blocked immediately.
    Already-running processes are NOT interrupted (they may complete or
    hit their timeout) — this prevents NEW actions from starting.

    Call with ``active=False`` to resume normal operation.
    """
    if conn is None:
        conn = connect()
        should_close = True
    else:
        should_close = False
    try:
        val = "true" if active else "false"
        conn.execute(
            "INSERT OR REPLACE INTO operator_preferences (key, value, set_at, source) "
            "VALUES (?, ?, ?, 'explicit')",
            (_KILL_SWITCH_KEY, val, now_iso()),
        )
        conn.commit()
    finally:
        if should_close:
            conn.close()


def is_autonomy_enabled(conn=None) -> bool:
    """Check the global autonomy enable flag.

    Returns True when autonomy is enabled (default). Returns False when
    autonomy has been disabled — action workers (Hyprland/Browser) are
    blocked but other executors still operate.

    For the full nuclear stop that blocks ALL executors, use
    ``is_kill_switch_active()`` instead.
    """
    if conn is None:
        conn = connect()
        should_close = True
    else:
        should_close = False
    try:
        row = conn.execute(
            "SELECT value FROM operator_preferences WHERE key = ?",
            (_AUTONOMY_KEY,),
        ).fetchone()
        if row is None:
            return True  # default: enabled
        return row["value"].strip().lower() == "true"
    finally:
        if should_close:
            conn.close()


def set_autonomy_enabled(enabled: bool, conn=None) -> None:
    """Set the global autonomy enable flag.

    When called with ``enabled=False``, all action workers will be blocked
    until autonomy is re-enabled.
    """
    if conn is None:
        conn = connect()
        should_close = True
    else:
        should_close = False
    try:
        val = "true" if enabled else "false"
        conn.execute(
            "INSERT OR REPLACE INTO operator_preferences (key, value, set_at, source) "
            "VALUES (?, ?, ?, 'explicit')",
            (_AUTONOMY_KEY, val, now_iso()),
        )
        conn.commit()
    finally:
        if should_close:
            conn.close()


# ──────────────────────────────────────────────────────────────────────
# Per-action-type permission CRUD
# ──────────────────────────────────────────────────────────────────────


@functools.lru_cache(maxsize=1)
def _all_hardcoded_levels() -> dict[str, str]:
    """Return the hardcoded action level map from confirm_gate.

    Imported lazily to avoid circular imports at module load time.
    Cached (``lru_cache(maxsize=1)``) since the action levels are
    immutable at runtime — the cache avoids re-importing and
    re-iterating on every permission CRUD call.
    """
    from .runtime.confirm_gate import _ACTION_LEVELS

    return {
        action: level.value
        for action, level in _ACTION_LEVELS.items()
    }


def _downgrade_one_level(current: str) -> str:
    """Return the next-more-restrictive level.

    auto → confirm → double (double is terminal — can't downgrade further).
    """
    if current == "auto":
        return "confirm"
    if current == "confirm":
        return "double"
    return "double"  # already at max restrictiveness


def _upgrade_one_level(current: str) -> str:
    """Return the next-less-restrictive level.

    double → confirm → auto (auto is terminal — can't upgrade further).
    """
    if current == "double":
        return "confirm"
    if current == "confirm":
        return "auto"
    return "auto"


def get_action_permission(action_type: str, conn=None) -> ActionPermission:
    """Look up the current effective permission for an action type.

    Reads the autonomy_permissions row (if any) and resolves the effective
    level by precedence: override > auto-downgrade > default.
    """
    if conn is None:
        conn = connect()
        should_close = True
    else:
        should_close = False
    try:
        default_level = _all_hardcoded_levels().get(action_type.lower(), "confirm")

        row = conn.execute(
            "SELECT override_level, auto_downgraded, consecutive_failures, "
            "consecutive_successes, updated_at "
            "FROM autonomy_permissions WHERE action_type = ?",
            (action_type.lower(),),
        ).fetchone()

        if row is None:
            return ActionPermission(
                action_type=action_type,
                default_level=default_level,
                override_level=None,
                auto_downgraded_level=None,
                consecutive_failures=0,
                consecutive_successes=0,
            )

        return ActionPermission(
            action_type=action_type,
            default_level=default_level,
            override_level=row["override_level"],
            auto_downgraded_level=row["auto_downgraded"],
            consecutive_failures=row["consecutive_failures"] or 0,
            consecutive_successes=row["consecutive_successes"] or 0,
        )
    finally:
        if should_close:
            conn.close()


def set_override(action_type: str, level: str, conn=None) -> None:
    """Set a user-defined override for one action type.

    ``level`` must be one of ``auto``, ``confirm``, ``double``.
    Setting an override clears any auto-downgrade for that action type.
    """
    at = action_type.lower().strip()
    lv = level.lower().strip()
    if lv not in VALID_LEVELS:
        raise ValueError(f"Invalid level '{level}': must be one of {sorted(VALID_LEVELS)}")
    if conn is None:
        conn = connect()
        should_close = True
    else:
        should_close = False
    try:
        now = now_iso()
        conn.execute(
            "INSERT INTO autonomy_permissions "
            "(action_type, default_level, override_level, auto_downgraded, "
            "consecutive_failures, consecutive_successes, updated_at) "
            "VALUES (?, ?, ?, NULL, 0, 0, ?) "
            "ON CONFLICT(action_type) DO UPDATE SET "
            "override_level=excluded.override_level, "
            "auto_downgraded=NULL, "
            "consecutive_failures=0, "
            "consecutive_successes=0, "
            "updated_at=excluded.updated_at",
            (at, _all_hardcoded_levels().get(at, "confirm"), lv, now),
        )
        conn.commit()
    finally:
        if should_close:
            conn.close()


def clear_override(action_type: str, conn=None) -> None:
    """Remove a user-defined override, reverting to default or auto-downgrade."""
    at = action_type.lower().strip()
    if conn is None:
        conn = connect()
        should_close = True
    else:
        should_close = False
    try:
        now = now_iso()
        conn.execute(
            "UPDATE autonomy_permissions SET "
            "override_level=NULL, updated_at=? "
            "WHERE action_type=?",
            (now, at),
        )
        conn.commit()
    finally:
        if should_close:
            conn.close()


def get_all_permissions(conn=None) -> list[ActionPermission]:
    """Return the effective permission for every known action type.

    Includes action types that have no row in autonomy_permissions — they'll
    show default_level and no override/downgrade.
    """
    if conn is None:
        conn = connect()
        should_close = True
    else:
        should_close = False
    try:
        hardcoded = _all_hardcoded_levels()
        db_rows = {
            r["action_type"]: r
            for r in conn.execute(
                "SELECT action_type, override_level, auto_downgraded, "
                "consecutive_failures, consecutive_successes "
                "FROM autonomy_permissions"
            ).fetchall()
        }
        results: list[ActionPermission] = []
        for action, default_value in sorted(hardcoded.items()):
            db = db_rows.get(action)
            if db:
                results.append(
                    ActionPermission(
                        action_type=action,
                        default_level=default_value,
                        override_level=db["override_level"],
                        auto_downgraded_level=db["auto_downgraded"],
                        consecutive_failures=db["consecutive_failures"] or 0,
                        consecutive_successes=db["consecutive_successes"] or 0,
                    )
                )
            else:
                results.append(
                    ActionPermission(
                        action_type=action,
                        default_level=default_value,
                        override_level=None,
                        auto_downgraded_level=None,
                    )
                )
        return results
    finally:
        if should_close:
            conn.close()


# ──────────────────────────────────────────────────────────────────────
# Confidence-based escalation (auto-downgrade / auto-upgrade)
# ──────────────────────────────────────────────────────────────────────


def record_action_outcome(
    action_type: str,
    success: bool,
    conn=None,
) -> None:
    """Record one action execution outcome for confidence-based escalation.

    On success: increments consecutive_successes. When it reaches
    ``AUTO_UPGRADE_THRESHOLD``, undoes any auto-downgrade.

    On failure: increments consecutive_failures. When it reaches
    ``AUTO_DOWNGRADE_THRESHOLD``, auto-downgrades the effective level and
    resets the failure counter.

    Does NOT clear a user-set override — auto-downgrade only affects the
    auto_downgraded_level column, which has lower precedence than override.
    """
    at = action_type.lower().strip()
    if conn is None:
        conn = connect()
        should_close = True
    else:
        should_close = False
    try:
        now = now_iso()

        # Upsert the row if it doesn't exist.
        default_level = _all_hardcoded_levels().get(at, "confirm")
        conn.execute(
            "INSERT INTO autonomy_permissions "
            "(action_type, default_level, consecutive_failures, "
            "consecutive_successes, updated_at) "
            "VALUES (?, ?, 0, 0, ?) "
            "ON CONFLICT(action_type) DO NOTHING",
            (at, default_level, now),
        )

        row = conn.execute(
            "SELECT override_level, auto_downgraded, consecutive_failures, "
            "consecutive_successes FROM autonomy_permissions WHERE action_type=?",
            (at,),
        ).fetchone()
        if row is None:
            return

        override = row["override_level"]
        current_downgraded = row["auto_downgraded"]
        failures = row["consecutive_failures"] or 0
        successes = row["consecutive_successes"] or 0

        if success:
            # On success: count successes, reset failures.
            successes += 1
            new_failures = 0

            # Check if we've earned an upgrade (undo auto-downgrade).
            if current_downgraded and successes >= AUTO_UPGRADE_THRESHOLD:
                # Determine what level to revert to:
                # override > default (skip one downgrade level).
                base = override or default_level
                upgraded = _upgrade_one_level(base)
                # The downgrade was from base → current_downgraded.
                # Undo it: set auto_downgraded one notch back toward base.
                new_downgraded = _upgrade_one_level(current_downgraded)
                # Don't go past the base level.
                if new_downgraded == base or _level_index(new_downgraded) < _level_index(base):
                    new_downgraded = None  # fully recovered
                conn.execute(
                    "UPDATE autonomy_permissions SET "
                    "auto_downgraded=?, consecutive_failures=?, "
                    "consecutive_successes=?, updated_at=? "
                    "WHERE action_type=?",
                    (new_downgraded, new_failures, successes, now, at),
                )
            else:
                conn.execute(
                    "UPDATE autonomy_permissions SET "
                    "consecutive_failures=?, consecutive_successes=?, updated_at=? "
                    "WHERE action_type=?",
                    (new_failures, successes, now, at),
                )
        else:
            # On failure: count failures, reset successes.
            failures += 1
            new_successes = 0

            # Check if we've hit the downgrade threshold.
            if failures >= AUTO_DOWNGRADE_THRESHOLD:
                # Determine the current base level (what we downgrade FROM).
                base = override or default_level

                # Apply one downgrade step.
                downgraded = _downgrade_one_level(
                    current_downgraded or base
                )
                conn.execute(
                    "UPDATE autonomy_permissions SET "
                    "auto_downgraded=?, consecutive_failures=0, "
                    "consecutive_successes=?, updated_at=? "
                    "WHERE action_type=?",
                    (downgraded, new_successes, now, at),
                )
            else:
                conn.execute(
                    "UPDATE autonomy_permissions SET "
                    "consecutive_failures=?, consecutive_successes=?, updated_at=? "
                    "WHERE action_type=?",
                    (failures, new_successes, now, at),
                )
        conn.commit()
    finally:
        if should_close:
            conn.close()


def _level_index(level: str) -> int:
    """Return numeric index for comparison: auto=0, confirm=1, double=2."""
    return {"auto": 0, "confirm": 1, "double": 2}.get(level, 1)


# ──────────────────────────────────────────────────────────────────────
# Escalation reconciliation (runs as part of daemon cycle)
# ──────────────────────────────────────────────────────────────────────


def reconcile_escalation(conn=None) -> list[str]:
    """Check all action types for pending promotions/demotions and log changes.

    Reads every row in ``autonomy_permissions`` and checks whether any have
    crossed the escalation thresholds. Returns a list of human-readable
    change messages describing what happened (empty list if nothing changed).

    The actual counter updates and level changes happen inside
    ``record_action_outcome()`` at executor dispatch time. This function is
    a reporting pass that surfaces what changed since the last daemon cycle
    — it does NOT modify any permission levels itself.

    Returns:
        List of strings like "Promoted 'workspace': CONFIRM → AUTO (10 successes)"
        or "Demoted 'closewindow': AUTO → CONFIRM (3 failures)". Empty if no changes.
    """
    if conn is None:
        conn = connect()
        should_close = True
    else:
        should_close = False
    try:
        changes: list[str] = []

        rows = conn.execute(
            "SELECT action_type, default_level, override_level, "
            "auto_downgraded, consecutive_failures, consecutive_successes "
            "FROM autonomy_permissions"
        ).fetchall()

        for r in rows:
            at = r["action_type"]
            override = r["override_level"]
            auto_downgraded = r["auto_downgraded"]
            failures = r["consecutive_failures"] or 0
            successes = r["consecutive_successes"] or 0

            if override:
                continue  # User set an override — don't auto-adjust

            # Check if we're due for a promotion (undo auto-downgrade).
            if auto_downgraded and successes >= AUTO_UPGRADE_THRESHOLD:
                default = r["default_level"] or "confirm"
                new_level = _upgrade_one_level(auto_downgraded)
                if new_level == default or _level_index(new_level) < _level_index(default):
                    new_level = None  # fully recovered

                conn.execute(
                    "UPDATE autonomy_permissions SET "
                    "auto_downgraded=?, consecutive_successes=0, updated_at=? "
                    "WHERE action_type=?",
                    (new_level, now_iso(), at),
                )
                conn.commit()

                direction = "fully recovered" if new_level is None else f"partially recovered ({auto_downgraded} → {new_level})"
                changes.append(
                    f"Promoted '{at}': {auto_downgraded} → {new_level or 'default'} "
                    f"({direction}, {successes} consecutive successes)"
                )
                continue  # Don't also demote in the same cycle

            # Check if we're due for a demotion.
            if failures >= AUTO_DOWNGRADE_THRESHOLD:
                default = r["default_level"] or "confirm"
                base = auto_downgraded or default
                if base == "double":
                    continue  # Already at max restrictiveness
                new_downgraded = _downgrade_one_level(base)

                conn.execute(
                    "UPDATE autonomy_permissions SET "
                    "auto_downgraded=?, consecutive_failures=0, updated_at=? "
                    "WHERE action_type=?",
                    (new_downgraded, now_iso(), at),
                )
                conn.commit()

                changes.append(
                    f"Demoted '{at}': {base} → {new_downgraded} "
                    f"({failures} consecutive failures)"
                )

        conn.commit()
        return changes
    finally:
        if should_close:
            conn.close()
