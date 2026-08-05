"""Confirmation gate — permission levels for every execution.

Wave 9 ``execution/`` safety core. Every action Friday takes passes
through a gate that classifies it into one of three permission levels
(matching the V4 ``actions`` table's ``permission_level`` column):

    AUTO     — read-only (status, diff, list): execute silently.
    CONFIRM  — state-changing (writes, test runs): require y/n.
    NEVER    — prod/deploy/push: blocked unless the operator explicitly
               passes ``force=True`` (a deliberate override).

Design laws:
- Unknown action types default to CONFIRM (safe).
- ``NEVER`` is only bypassable by an explicit operator override, never
  by a scripted ``confirm_fn``.
- This module is pure logic — no DB, no subprocess — so it is trivially
  hermetic and testable.

Usage:
    gate = PermissionGate()
    level = gate.level_for("git", "push origin main")     # NEVER (sniffed)
    ok = gate.check(level, description="git push",
                    confirm_fn=lambda desc: input(f"{desc}? [y/N] ").lower() == "y")
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Callable, Optional

#: Optional confirmation callback: returns True to approve, False to deny.
ConfirmFn = Callable[[str], bool]


class PermissionLevel(str, Enum):
    """How much confirmation an action needs before executing."""

    AUTO = "auto"          # read-only — execute silently
    CONFIRM = "confirm"    # state-changing — require y/n
    NEVER = "never"        # prod/deploy/push — operator override only


#: Default level per action type. ``action_type`` values mirror the V4
#: ``actions`` table. Anything not listed here defaults to CONFIRM.
_DEFAULT_LEVELS: dict[str, PermissionLevel] = {
    # ── Read-only queries → AUTO ──
    "status": PermissionLevel.AUTO,
    "diff": PermissionLevel.AUTO,
    "show": PermissionLevel.AUTO,
    "list": PermissionLevel.AUTO,
    "log": PermissionLevel.AUTO,
    "read": PermissionLevel.AUTO,
    # ── State-changing → CONFIRM ──
    "shell": PermissionLevel.CONFIRM,
    "git": PermissionLevel.CONFIRM,
    "file": PermissionLevel.CONFIRM,
    "python": PermissionLevel.CONFIRM,
    "testing": PermissionLevel.CONFIRM,
    "desktop": PermissionLevel.CONFIRM,
    "ssh": PermissionLevel.CONFIRM,  # remote — always confirm
    "claude": PermissionLevel.CONFIRM,  # agentic delegation — always confirm
}

#: Substrings that escalate any action to NEVER regardless of its default.
#: These are the irreversible, high-blast-radius operations the wave doc
#: explicitly forbids without an operator override.
_DANGEROUS_PATTERNS: tuple[str, ...] = (
    "push",
    "deploy",
    "drop database",
    "drop table",
    "truncate",
    "rm -rf /",
    "rm -rf ~",
    "git reset --hard",
    "git clean -f",
    "--force",
    "--yes",
)


#: Read-only subcommands per action type. When the *first word* of the
#: command is one of these, the action classifies as AUTO (read-only) —
#: e.g. ``file read notes.txt`` or ``git status``. Empty tuple = no
#: subcommand-aware classification (everything stays at the type default).
_READONLY_SUBCOMMANDS: dict[str, tuple[str, ...]] = {
    "git": ("status", "diff", "log", "show", "branch", "remote",
            "ls-files", "ls-remote", "stash", "tag", "rev-parse",
            "check-ignore"),
    "file": ("read",),
}


class PermissionGate:
    """Classify actions into permission levels and enforce them.

    Pure logic (no I/O) — deterministic and hermetic.
    """

    def __init__(self,
                 defaults: Optional[dict[str, PermissionLevel]] = None,
                 dangerous: Optional[tuple[str, ...]] = None) -> None:
        self._defaults = dict(defaults or _DEFAULT_LEVELS)
        self._dangerous = tuple(dangerous or _DANGEROUS_PATTERNS)

    # ── Classification ────────────────────────────────────────────────

    def level_for(self, action_type: str, command: str = "") -> PermissionLevel:
        """The permission level for an action type (+ optional command).

        Resolution order:
          1. Destructive patterns in the command → NEVER (always wins).
          2. A read-only subcommand (e.g. ``git status``, ``file read``)
             → AUTO.
          3. The action-type default (unknown → CONFIRM).

        A read-looking command can never downgrade a destructive one —
        rule 1 is checked first and wins.
        """
        if command and _looks_dangerous(command, self._dangerous):
            return PermissionLevel.NEVER
        readonly = _READONLY_SUBCOMMANDS.get(action_type, ())
        if readonly and _has_readonly_subcommand(command, readonly):
            return PermissionLevel.AUTO
        return self._defaults.get(action_type, PermissionLevel.CONFIRM)

    def classify(self, action_type: str, command: str = "") -> PermissionLevel:
        """Alias of :meth:`level_for` (clearer at call sites)."""
        return self.level_for(action_type, command)

    # ── Enforcement ──────────────────────────────────────────────────

    def check(self, level: PermissionLevel, description: str = "",
              confirm_fn: Optional[ConfirmFn] = None,
              force: bool = False) -> bool:
        """Whether an action at ``level`` may proceed.

        Args:
            level: The classified permission level.
            description: Human-readable action summary (shown to the
                operator by ``confirm_fn``).
            confirm_fn: Callback that returns True to approve. If None,
                CONFIRM actions are denied (safe default).
            force: Explicit operator override. Bypasses CONFIRM and —
                *only for this reason* — NEVER. Scripted callers must not
                pass ``force`` casually.

        Returns:
            True if the action should execute.
        """
        if level == PermissionLevel.AUTO:
            return True
        if level == PermissionLevel.NEVER:
            return force  # explicit operator override only
        # CONFIRM
        if force:
            return True
        if confirm_fn is None:
            return False
        try:
            return confirm_fn(description) is True
        except Exception:
            return False


def _has_readonly_subcommand(command: str, readonly: tuple[str, ...]) -> bool:
    """True when the command's first word is a read-only subcommand.

    ``git status`` → True; ``git status --short`` → True (first word
    still ``status``); ``echo status`` → False (first word ``echo``).
    """
    low = (command or "").lstrip().lower()
    if not low:
        return False
    first = low.split()[0]
    return first in readonly


def _looks_dangerous(command: str, patterns: tuple[str, ...]) -> bool:
    """True if ``command`` contains any destructive pattern.

    Matches whole words where the pattern is a bare word (e.g. ``push``),
    so ``"git push"`` escalates but ``"echo pushups"`` does not. Path
    patterns (``rm -rf /``) match as substrings.
    """
    low = (command or "").lower().strip()
    if not low:
        return False
    for pat in patterns:
        if " " in pat:
            if pat in low:
                return True
            continue
        if re.search(rf"\b{re.escape(pat)}\b", low):
            return True
    return False


__all__ = ["PermissionLevel", "PermissionGate", "ConfirmFn"]
