"""Confirm-before-execute safety gate (Pillar A).

Action-layer workers (Hyprland, browser automation, native GUI) are the first
workers whose *mistake* has a real-world side effect outside Friday's sandbox:
sending a wrong message, closing the wrong window, launching the wrong app is not
the same failure class as a broken shell worker.

This gate enforces:
  - AUTO-EXECUTE for read-only actions (query state, list windows, get info).
  - CONFIRM-BEFORE-EXECUTE for any state-changing action (switch workspace,
    launch app, focus window, close window, move/resize, etc.).
  - DESTRUCTIVE DOUBLE-CONFIRM for actions that destroy state (close window,
    kill process, exit compositor).

The gate is called by the executor dispatch path (resolve_executor) and by any
CLI command that invokes an action worker. It checks the worker's capabilities
and the specific action to determine the required confirmation level.

Usage:
  from .confirm_gate import confirm_action, ActionLevel
  
  level = confirm_action(worker_id, action_type)
  if level == ActionLevel.CONFIRM:
      # Prompt user for y/n
  elif level == ActionLevel.DOUBLE_CONFIRM:
      # Prompt twice
  # ActionLevel.AUTO: no prompt needed
"""

from __future__ import annotations

import sys
from enum import Enum
from typing import Optional


class ActionLevel(Enum):
    """How much confirmation an action needs before executing."""

    AUTO = "auto"                # Read-only: no confirmation needed
    CONFIRM = "confirm"          # State-changing: requires y/n
    DOUBLE_CONFIRM = "double"    # Destructive: requires two y/n prompts


# Worker capabilities that indicate this is an action worker (not a shell/python
# worker that operates inside Friday's own workspace).
_ACTION_CAPABILITIES = frozenset({
    "Window Management",
    "Workspace Control",
    "Application Launcher",
})

# Action types (from the action field of the worker payload) mapped to their
# confirmation level. Unknown actions default to CONFIRM.
_ACTION_LEVELS: dict[str, ActionLevel] = {
    # Read-only — auto-execute
    "query": ActionLevel.AUTO,
    "clients": ActionLevel.AUTO,
    "workspaces": ActionLevel.AUTO,
    "monitors": ActionLevel.AUTO,
    "activewindow": ActionLevel.AUTO,
    "activeworkspace": ActionLevel.AUTO,
    "cursorpos": ActionLevel.AUTO,
    "binds": ActionLevel.AUTO,
    "devices": ActionLevel.AUTO,

    # Browser read-only — auto-execute
    "read": ActionLevel.AUTO,
    "title": ActionLevel.AUTO,
    "url": ActionLevel.AUTO,
    "screenshot": ActionLevel.AUTO,

    # State-changing — require confirmation
    "workspace": ActionLevel.CONFIRM,
    "exec": ActionLevel.CONFIRM,
    "focuswindow": ActionLevel.CONFIRM,
    "movetoworkspace": ActionLevel.CONFIRM,
    "movetoworkspacesilent": ActionLevel.CONFIRM,
    "movewindow": ActionLevel.CONFIRM,
    "resizewindow": ActionLevel.CONFIRM,
    "fullscreen": ActionLevel.CONFIRM,
    "togglefloating": ActionLevel.CONFIRM,
    "pin": ActionLevel.CONFIRM,
    "focusmonitor": ActionLevel.CONFIRM,
    "movecursortocorner": ActionLevel.CONFIRM,

    # Destructive — require double confirmation
    "closewindow": ActionLevel.DOUBLE_CONFIRM,
    "kill": ActionLevel.DOUBLE_CONFIRM,
    "exit": ActionLevel.DOUBLE_CONFIRM,
}


def is_action_worker(worker_id: str,
                    capabilities: Optional[list[str]] = None) -> bool:
    """True if the worker has action capabilities (desktop control).

    Checks two signals:
    1. Worker ID follows the action-worker naming pattern
       (worker:hyprctl, worker:browser, worker:gui, worker:input).
    2. Worker declares any of the known action capabilities
       (Window Management, Workspace Control, Application Launcher).

    Either signal alone is sufficient — this ensures meta-generated workers
    with action capabilities but non-standard ids are also caught.
    """
    wid = (worker_id or "").lower()
    # Check worker_id prefix pattern.
    if any(wid.startswith(p) for p in ("worker:hyprctl", "worker:browser",
                                        "worker:gui", "worker:input")):
        return True
    # Check capabilities (catches meta-generated workers with action caps).
    if capabilities:
        caps_lower = {c.lower().strip() for c in capabilities}
        if caps_lower & {c.lower() for c in _ACTION_CAPABILITIES}:
            return True
    return False


def get_action_level(action: str) -> ActionLevel:
    """Return the confirmation level for a given action type."""
    action_lower = (action or "").lower().strip()
    return _ACTION_LEVELS.get(action_lower, ActionLevel.CONFIRM)


def prompt_confirm(action: str, target: str, worker_id: str,
                   skip_prompt: bool = False,
                   conn=None) -> bool:
    """Prompt the user to confirm an action. Returns True if confirmed.

    Args:
        action: The hyprctl action type (e.g. 'workspace', 'exec', 'closewindow').
        target: The target of the action (e.g. '3', 'firefox', 'class:kitty').
        worker_id: The worker that will execute the action (for display).
        skip_prompt: If True, auto-confirm (for scripted/--yes mode).
        conn: Optional DB connection for autonomy checks (used by tests).

    Returns:
        True if the action should proceed, False to cancel.
    """
    # Graduated autonomy: check kill switch FIRST.
    # When autonomy is disabled, ALL action workers are blocked regardless
    # of the action level or skip_prompt flag. This is the emergency override.
    from ..autonomy import is_autonomy_enabled
    if not is_autonomy_enabled(conn):
        print(f"\n🔒 [AUTONOMY DISABLED] {worker_id} blocked: {action} {target}")
        print("  Use 'friday autonomy enable' to re-enable autonomous actions.")
        return False

    # Graduated autonomy: check per-action-type override + auto-downgrade.
    # The effective level is resolved by precedence:
    #   user override > auto-downgrade > hardcoded default
    from ..autonomy import get_action_permission
    auto_perm = get_action_permission(action, conn)
    effective_override = auto_perm.effective_level

    if skip_prompt:
        # Even with --yes, respect the kill switch (checked above).
        return True

    # Determine the confirmation level: use the effective permission level
    # from graduated autonomy system (which accounts for overrides and
    # auto-downgrades), falling back to the hardcoded action level.
    level = get_action_level(action)
    if effective_override != level.value:
        level = ActionLevel(effective_override)

    if level == ActionLevel.AUTO:
        return True

    action_desc = f"{action} {target}" if target else action
    source = worker_id or "action worker"

    if level == ActionLevel.CONFIRM:
        print(f"\n[ACTION REQUIRED] {source} wants to: {action_desc}")
        try:
            response = input("  Proceed? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        return response in ("y", "yes")

    if level == ActionLevel.DOUBLE_CONFIRM:
        print(f"\n⚠️  [DESTRUCTIVE ACTION] {source} wants to: {action_desc}")
        print("  This action CANNOT be undone.")
        try:
            first = input("  Are you sure? [y/N] ").strip().lower()
            if first not in ("y", "yes"):
                return False
            second = input("  REALLY sure? This will close/terminate. [y/N] ").strip().lower()
            return second in ("y", "yes")
        except (EOFError, KeyboardInterrupt):
            print()
            return False

    return False
