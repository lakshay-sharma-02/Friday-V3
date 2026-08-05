"""CLI commands for `friday6 execute` / `friday6 actions` — Wave 9 execution.

The surface for the execution layer: you can finally tell Friday to *do*
something and watch it pass through the gate → sandbox → audit pipeline.

Usage:
    friday6 execute <action_type> <command...> [--cwd DIR] [--db PATH]
                    [--force] [--yes] [--json] [--goal TEXT]
    friday6 actions [--limit N] [--type T] [--db PATH] [--json]

Action types: shell | git | file | python | testing | ssh (Wave 12) |
claude (Wave 18 — delegate a complex task to the Claude Code CLI, e.g.
`friday6 execute claude "figure out why the build fails and fix it"`)

Permission levels (see execution/gate.py):
    AUTO     — read-only (git status, file read): runs silently
    CONFIRM  — state-changing: prompts y/N unless --force/--yes
    NEVER    — push/deploy/rm -rf /: requires an explicit --force

The audit trail (actions table) is the durable record of everything —
including denied attempts. `friday6 actions` reads it back.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger("friday_v6.cli_execute")

_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_GREEN = "\033[92m"
_YELLOW = "\033[93m"
_CYAN = "\033[96m"
_RED = "\033[91m"

#: Exit codes (stable for scripting).
EXIT_OK = 0
EXIT_DENIED = 2      # gate refused (or operator declined)
EXIT_FAILED = 1      # ran but failed / unknown action type
EXIT_USAGE = 3


def _print_logo(title: str = "Execution"):
    print()
    print(f"  {_BOLD}{_CYAN}◆ FRIDAY{_RESET} {_DIM}V4 — {title}{_RESET}")
    print(f"  {_DIM}{'─' * 40}{_RESET}")


def _print_row(key: str, value: str, ok: Optional[bool] = None):
    icon = {True: f"{_GREEN}✔{_RESET}", False: f"{_RED}✘{_RESET}"}.get(ok, " ")
    print(f"  {icon} {_DIM}{key:<16}{_RESET}{value}")


def _print_dim(text: str):
    print(f"  {_DIM}{text}{_RESET}")


def _print_error(text: str):
    print(f"  {_RED}✗ {text}{_RESET}")


def _print_ok(text: str):
    print(f"  {_GREEN}✓ {text}{_RESET}")


# ---------------------------------------------------------------------------
# Confirm prompt
# ---------------------------------------------------------------------------


def _confirm_prompt(description: str) -> bool:
    """Interactive y/N confirmation for CONFIRM-level actions.

    Reads from stdin; returns False on EOF/KeyboardInterrupt (safe).
    """
    try:
        print(f"\n  {_YELLOW}→ Friday wants to:{_RESET} {description}")
        answer = input(f"  {_BOLD}Proceed? [y/N] {_RESET}").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer in ("y", "yes")


def _resolve_db(args) -> Optional[object]:
    """Open the V4 state DB for auditing; None when absent (no-op audit).

    Never raises — a missing DB just means the action runs without a
    durable audit row (and we say so).
    """
    try:
        from . import db
        path = getattr(args, "db", None)
        return db.connect(path=path)
    except Exception as exc:
        logger.debug(f"execute: db unavailable ({exc}) — running without audit")
        return None


def _close_db(conn) -> None:
    try:
        if conn is not None:
            conn.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_execute(args: argparse.Namespace) -> int:
    """Run an action through the gate → sandbox → audit pipeline."""
    from .execution import execute

    action_type = args.action_type
    command = " ".join(args.command).strip() if args.command else ""
    if not command:
        print(f"  {_RED}✗ empty command for action type '{action_type}'{_RESET}")
        return EXIT_USAGE

    conn = _resolve_db(args)
    try:
        # ``--force`` OR ``--yes`` is the explicit operator override
        # (both bypass the CONFIRM prompt; NEVER still needs them too).
        # JSON mode never prompts (it would corrupt the machine-readable
        # document) — a CONFIRM action without an override fails closed.
        force = bool(args.force or args.yes)
        confirm_fn = None if (force or args.json) else _confirm_prompt

        result = execute(
            action_type,
            command,
            cwd=args.cwd,
            conn=conn,
            confirm_fn=confirm_fn,
            force=force,
            goal=args.goal,
        )
    finally:
        _close_db(conn)

    if args.json:
        print(json.dumps(result.to_dict(), indent=2, default=str))
        # Machine-readable exit code: 0 success, 2 denied, 1 failed.
        if result.status == "succeeded":
            return EXIT_OK
        if result.status == "denied":
            return EXIT_DENIED
        return EXIT_FAILED

    _print_logo("Execute")
    _print_row("action", f"{action_type} — {command}")
    _print_row("permission", result.permission_level)
    _print_row("status", result.status, ok=(result.status == "succeeded"))
    if result.action_id:
        _print_row("audit id", result.action_id)

    if result.output:
        print(f"  {_DIM}{'─' * 40}{_RESET}")
        for line in result.output.splitlines()[:30]:
            print(f"  {_DIM}{line[:140]}{_RESET}")

    print()
    if result.status == "succeeded":
        _print_ok("done.")
    elif result.status == "denied":
        if result.permission_level == "never":
            _print_error("denied by gate — action is NEVER-permitted; "
                         f"an explicit --force is required")
        else:
            _print_error(f"denied by gate — operator confirmation required "
                         f"(or use --force for an explicit override)")
        return EXIT_DENIED
    elif result.status == "timed_out":
        _print_error("timed out (sandbox timeout).")
        return EXIT_FAILED
    else:
        _print_error("failed.")
        return EXIT_FAILED
    return EXIT_OK


def cmd_actions(args: argparse.Namespace) -> int:
    """List the audit trail (recent actions, newest first)."""
    conn = _resolve_db(args)
    try:
        from .execution import AuditLogger
        audit = AuditLogger(conn)
        rows = audit.recent(limit=args.limit, action_type=args.action_type)
    except Exception as exc:
        _print_error(f"could not read audit trail: {exc}")
        return EXIT_FAILED
    finally:
        _close_db(conn)

    if args.json:
        print(json.dumps(rows, indent=2, default=str))
        return EXIT_OK

    _print_logo("Actions (audit trail)")
    if not rows:
        _print_dim("no actions recorded yet — run `friday6 execute` first")
        return EXIT_OK

    _STATUS_COLORS = {
        "succeeded": _GREEN, "denied": _RED, "failed": _RED,
        "timed_out": _YELLOW, "pending": _YELLOW,
    }
    for row in rows:
        status = row.get("status", "?")
        color = _STATUS_COLORS.get(status, _RESET)
        level = row.get("permission_level", "")
        command = row.get("command", "") or row.get("goal", "")
        if len(command) > 60:
            command = command[:57] + "..."
        print(f"  {color}{status:<9}{_RESET} "
              f"{_DIM}{row.get('action_type','?'):<8}{_RESET} "
              f"{command}")
        created = (row.get("created_at") or "")[:16]
        _print_dim(f"      {level:<8} {created}  id={row.get('id','')[:12]}…")
    print()
    return EXIT_OK


# ---------------------------------------------------------------------------
# Argument parsers
# ---------------------------------------------------------------------------


def build_execute_parser(subparsers) -> None:
    """Register `friday6 execute` and `friday6 actions`."""
    parser = subparsers.add_parser(
        "execute", help="Run an action through the safety pipeline",
        description="Friday executes an action (shell/git/file/python/"
                    "testing/ssh) through gate → sandbox → audit. CONFIRM "
                    "actions prompt unless --force; NEVER actions require "
                    "--force explicitly.",
    )
    parser.add_argument(
        "action_type",
        choices=["shell", "git", "file", "python", "testing", "ssh",
                 "claude"],
        help="Action type (the executor to use; claude = Claude Code CLI)",
    )
    parser.add_argument("command", nargs="+", help="The command to run")
    parser.add_argument("--cwd", type=Path, default=None,
                        help="Working directory (sandbox roots follow it; "
                             "default: cwd)")
    parser.add_argument("--db", type=Path, default=None,
                        help="V4 state DB path (default: ~/.friday/v4.db "
                             "or $FRIDAY_V4_DB)")
    parser.add_argument("--goal", default="",
                        help="Human-readable goal for the audit row")
    parser.add_argument("--force", action="store_true",
                        help="Explicit operator override — bypasses the "
                             "confirm gate (required for NEVER actions)")
    parser.add_argument("--yes", "-y", action="store_true",
                        help="Alias for --force (non-interactive confirm)")
    parser.add_argument("--json", action="store_true",
                        help="Machine-readable JSON output (never prompts — "
                             "CONFIRM actions fail closed unless --force)")
    parser.set_defaults(func=cmd_execute)

    actions = subparsers.add_parser(
        "actions", help="List the execution audit trail",
        description="Read back the durable audit trail (every attempt, "
                    "including denials).",
    )
    actions.add_argument("--limit", type=int, default=25,
                         help="Max rows (default: 25)")
    actions.add_argument("--type", dest="action_type", default=None,
                         help="Filter by action type (shell/git/file/...)")
    actions.add_argument("--db", type=Path, default=None,
                         help="V4 state DB path")
    actions.add_argument("--json", action="store_true",
                         help="Machine-readable JSON output")
    actions.set_defaults(func=cmd_actions)


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Standalone entry point for `python -m friday_v6.cli_execute`."""
    logging.basicConfig(level=logging.WARNING)

    parser = argparse.ArgumentParser(prog="friday6 execute")
    subparsers = parser.add_subparsers(dest="command")
    build_execute_parser(subparsers)

    args = parser.parse_args(argv)
    if hasattr(args, "func"):
        return args.func(args) or 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
