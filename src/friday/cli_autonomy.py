"""``friday autonomy`` — graduated autonomy controls.

Subcommands:
  status       Show kill switch state and all action-type permissions.
  enable       Enable autonomy (re-enable action workers).
  disable      Disable autonomy (block action workers only).
  kill         Pull emergency kill switch — blocks ALL executors (nuclear).
  resume       Release emergency kill switch — resume normal operation.
  set          Set a per-action-type permission override.
  reset        Remove a per-action-type permission override.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
from typing import Optional

from .autonomy import (
    clear_override,
    get_all_permissions,
    is_autonomy_enabled,
    is_kill_switch_active,
    set_autonomy_enabled,
    set_kill_switch,
    set_override,
    VALID_LEVELS,
    AUTO_DOWNGRADE_THRESHOLD,
    AUTO_UPGRADE_THRESHOLD,
    record_action_outcome,
)
from .db import connect


def cmd_autonomy(args: argparse.Namespace) -> int:
    """Dispatch to the appropriate autonomy subcommand."""
    sub = getattr(args, "subcommand", None)
    if sub == "status":
        return _status(args)
    elif sub == "enable":
        return _enable(args)
    elif sub == "disable":
        return _disable(args)
    elif sub == "kill":
        return _kill(args)
    elif sub == "resume":
        return _resume(args)
    elif sub == "set":
        return _set_perm(args)
    elif sub == "reset":
        return _reset_perm(args)
    else:
        print("Usage: friday autonomy <status|enable|disable|kill|resume|set|reset>")
        return 2


def _status(args) -> int:
    """Show kill switch state and all action-type permissions."""
    conn = connect()
    enabled = is_autonomy_enabled(conn)
    kill_active = is_kill_switch_active(conn)
    perms = get_all_permissions(conn)

    print("┌─ Graduated Autonomy ─────────────────────────────┐")
    if kill_active:
        print("│  🛑 EMERGENCY KILL SWITCH: ACTIVE                   │")
        print("│  All executor dispatch is blocked.                  │")
        print("│  Run 'friday autonomy resume' to release.           │")
    else:
        print(f"│  Action workers: {'🟢 ENABLED' if enabled else '🔴 DISABLED'}")
    print("├───────────────────────────────────────────────────┤")
    print("│  Per-action-type permissions:                     │")
    for p in perms:
        eff = p.effective_level
        icon = {"auto": "🟢", "confirm": "🟡", "double": "🔴"}.get(eff, "⚪")
        parts = [f"{icon} {p.action_type:<20} {eff:<8}"]

        # Show escalation progress.
        if p.override_level:
            parts.append(f"[override: {p.override_level}]")
        elif p.auto_downgraded_level and p.auto_downgraded_level != p.default_level:
            # Downgraded — show success progress toward promotion.
            progress = f"{p.consecutive_successes}/{AUTO_UPGRADE_THRESHOLD}"
            parts.append(f"⬆ {progress} successes")
        else:
            # At or above default — show failure count toward downgrade.
            if p.consecutive_failures > 0:
                progress = f"{p.consecutive_failures}/{AUTO_DOWNGRADE_THRESHOLD}"
                parts.append(f"⬇ {progress} failures")
            elif p.consecutive_successes > 0:
                parts.append(f"✓ {p.consecutive_successes}s")

        if p.consecutive_failures > 0 and not any("⬇" in pp for pp in parts):
            parts.append(f"⚠ {p.consecutive_failures}f")
        if p.consecutive_successes > 0 and not any("⬆" in pp or "✓" in pp for pp in parts):
            parts.append(f"✓ {p.consecutive_successes}s")

        print(f"│  {' '.join(parts)}")
    print("└───────────────────────────────────────────────────┘")
    return 0


def _enable(args) -> int:
    """Re-enable autonomous actions for action workers."""
    set_autonomy_enabled(True)
    print("Autonomy enabled. Action workers will execute autonomously.")
    return 0


def _disable(args) -> int:
    """Block autonomous actions for action workers only."""
    set_autonomy_enabled(False)
    print("Autonomy disabled. All action workers are blocked.")
    print("Use 'friday autonomy enable' to re-enable.")
    return 0


def _kill(args) -> int:
    """Pull the emergency kill switch — blocks ALL executors (nuclear option).

    This sets a persistent flag in the database that is checked before every
    executor dispatch. Already-running processes are NOT interrupted — they
    may complete or hit their timeout naturally.

    Also sends SIGTERM to the daemon if running, to stop any in-progress
    cycle from starting new work.
    """
    set_kill_switch(True)
    print("🛑 EMERGENCY KILL SWITCH ACTIVATED.")
    print("  All executor dispatch is blocked.")
    print("  Already-running processes will complete or time out.")
    print()
    print("  To release:  friday autonomy resume")

    # Try to stop the daemon too.
    try:
        from .daemon import _read_pid, _is_pid_running
        pid = _read_pid()
        if pid is not None and _is_pid_running(pid):
            os.kill(pid, signal.SIGTERM)
            print(f"  Daemon (PID {pid}): SIGTERM sent.")
    except Exception:
        pass

    return 0


def _resume(args) -> int:
    """Release the emergency kill switch — resume normal operation."""
    set_kill_switch(False)
    print("🟢 Kill switch released. Normal operation resumed.")
    return 0


def _set_perm(args) -> int:
    """Set a per-action-type permission override."""
    action_type = getattr(args, "action_type", None)
    level = getattr(args, "level", None)

    if not action_type or not level:
        print("Usage: friday autonomy set <action_type> <level>", file=sys.stderr)
        print(f"  level must be one of: {', '.join(sorted(VALID_LEVELS))}",
              file=sys.stderr)
        return 2

    level_lower = level.lower().strip()
    if level_lower not in VALID_LEVELS:
        print(f"Invalid level '{level}': must be one of {sorted(VALID_LEVELS)}",
              file=sys.stderr)
        return 2

    try:
        set_override(action_type, level_lower)
        print(f"Override set: {action_type} → {level_lower}")
        return 0
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def _reset_perm(args) -> int:
    """Remove a per-action-type permission override."""
    action_type = getattr(args, "action_type", None)
    if not action_type:
        print("Usage: friday autonomy reset <action_type>", file=sys.stderr)
        return 2

    clear_override(action_type)
    print(f"Override cleared for: {action_type}")
    return 0
