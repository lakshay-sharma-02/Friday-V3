"""CLI commands for `friday4 mission` — persistent goals (Wave 9 + 18).

The missions layer's debug hatch (the Wiring Law: every layer gets its
``friday4 <layer> …`` command; the product surface stays natural
language through ``friday4 talk``).

Mission planning honors the ``FRIDAY_V4_CLAUDE_PLANNER`` opt-in exactly
like the NL path: with it set, ``create`` / ``replan`` decompose the
goal through the local Claude Code CLI (gated, sandboxed, audited,
read-only); otherwise the deterministic planner stands. The same
:func:`friday_v4.missions.make_planner` construction point is used, so
this CLI never diverges from what talk/voice/web do.

Usage:
    friday4 mission create "ship the auth refactor by Friday"
    friday4 mission list
    friday4 mission status <id>
    friday4 mission replan <id> [--goal G] [--reason R]
    friday4 mission advance <id> [--force] [--manual-result R]
    friday4 mission start|pause|cancel|complete|delete <id>
"""

from __future__ import annotations

import argparse
import json
import os
from typing import Optional

# Terminal UI helpers (shared style with cli_talk).
_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_GREEN = "\033[92m"
_YELLOW = "\033[93m"
_CYAN = "\033[96m"
_RED = "\033[91m"


def _print_logo(title: str):
    print()
    print(f"  {_BOLD}{_CYAN}◆ FRIDAY{_RESET} {_DIM}V4 — {title}{_RESET}")
    print(f"  {_DIM}{'─' * 40}{_RESET}")


def _line(key: str, value: str):
    print(f"  {_DIM}{key:<14}{_RESET} {value}")


def _engine(db_path: Optional[str], cwd: Optional[str]):
    """A MissionEngine on the given DB (default: the V4 state DB).

    The planner default is the single ``make_planner`` construction
    point, so ``FRIDAY_V4_CLAUDE_PLANNER`` gates Claude Code delegation
    here exactly as it does for talk/voice/web.
    """
    from pathlib import Path as _Path

    from . import db
    from .missions import MissionEngine

    path = _Path(db_path) if db_path else db.default_db_path()
    conn = db.connect(path)
    return MissionEngine(conn, cwd=cwd), conn


def _planner_note() -> str:
    """Whether Claude Code planning is active for this process."""
    if os.environ.get("FRIDAY_V4_CLAUDE_PLANNER"):
        return " (Claude Code planning active)"
    return ""


def _status_color(status: str) -> str:
    return {"active": _GREEN, "completed": _GREEN, "failed": _RED,
            "cancelled": _YELLOW}.get(status, "")


def _print_mission(mission) -> None:
    """Render one mission + its steps."""
    color = _status_color(mission.status.value)
    print(f"  {_BOLD}{mission.title}{_RESET} "
          f"{color}[{mission.status.value}]{_RESET} "
          f"{_DIM}({mission.id[:8]}… · priority {mission.priority}){_RESET}")
    for s in mission.steps:
        at = s.action_type or "manual"
        mark = {0: "○", 1: "◐", 2: "●", 3: "✗"}.get(
            {"pending": 0, "running": 1, "completed": 2,
             "failed": 3}.get(s.status.value, 0), "○")
        print(f"    {_DIM}{mark} {s.title} "
              f"[{s.status.value}]{_RESET} "
              f"{_DIM}({at}{' ' + s.command if s.command else ''}){_RESET}")
    pct = int(mission.progress * 100)
    print(f"    {_DIM}progress {pct}% · "
          f"{len(mission.completed_steps)}/{len(mission.steps)} steps{_RESET}\n")


def _mission_dict(mission) -> dict:
    return {
        "id": mission.id,
        "title": mission.title,
        "status": mission.status.value,
        "priority": mission.priority,
        "progress": mission.progress,
        "steps": [{
            "id": s.id, "title": s.title, "status": s.status.value,
            "action_type": s.action_type, "command": s.command,
        } for s in mission.steps],
    }


def cmd_mission_create(args: argparse.Namespace) -> int:
    """Create a mission from a goal (Claude Code when opted in)."""
    engine, conn = _engine(args.db, args.cwd)
    try:
        mission = engine.create(args.goal, title=args.title,
                                priority=args.priority)
        if not mission:
            print(f"  {_RED}✘{_RESET} Couldn't create that mission.")
            return 1
        if args.json:
            print(json.dumps(_mission_dict(mission), indent=2))
            return 0
        _print_logo("Mission created")
        print(f"  {_GREEN}✔{_RESET} '{mission.title}' "
              f"{_DIM}({mission.id}){_RESET}{_planner_note()}")
        _print_mission(mission)
        return 0
    finally:
        conn.close()


def cmd_mission_list(args: argparse.Namespace) -> int:
    """List missions (optionally filtered by status)."""
    engine, conn = _engine(args.db, args.cwd)
    try:
        missions = engine.list(status=args.status)
        if args.json:
            print(json.dumps([_mission_dict(m) for m in missions], indent=2))
            return 0
        _print_logo("Missions")
        if not missions:
            print(f"  {_DIM}No missions"
                  f"{' with status ' + args.status if args.status else ''} "
                  f"yet. Create one: friday4 mission create "
                  f"\"ship the auth refactor by Friday\".{_RESET}")
            return 0
        for m in missions:
            _print_mission(m)
        return 0
    finally:
        conn.close()


def cmd_mission_status(args: argparse.Namespace) -> int:
    """Show one mission with its steps."""
    engine, conn = _engine(args.db, args.cwd)
    try:
        mission = engine.get(args.id)
        if not mission:
            print(f"  {_YELLOW}◐{_RESET} No mission with id {args.id}.")
            return 1
        if args.json:
            print(json.dumps(_mission_dict(mission), indent=2))
            return 0
        _print_logo("Mission")
        _print_mission(mission)
        return 0
    finally:
        conn.close()


def cmd_mission_replan(args: argparse.Namespace) -> int:
    """Re-run the planner on the mission's goal (Claude when opted in)."""
    engine, conn = _engine(args.db, args.cwd)
    try:
        mission = engine.get(args.id)
        if not mission:
            print(f"  {_YELLOW}◐{_RESET} No mission with id {args.id}.")
            return 1
        goal = args.goal or mission.title
        reason = args.reason or "replanned via friday4 mission replan"
        report = engine.replan(args.id, goal, reason=reason, cwd=args.cwd)
        if args.json:
            print(json.dumps({
                "mission_id": report.mission_id,
                "changed": report.changed,
                "reason": report.reason,
                "added": report.added,
                "removed": report.removed,
                "message": report.message,
            }, indent=2))
            return 0
        _print_logo("Mission replanned")
        msg = report.message or "plan updated"
        print(f"  {_GREEN}✔{_RESET} {msg}{_planner_note()}")
        reloaded = engine.get(args.id)
        if reloaded:
            _print_mission(reloaded)
        return 0
    finally:
        conn.close()


def cmd_mission_advance(args: argparse.Namespace) -> int:
    """Run the next step (through the gate) or mark a manual step done."""
    engine, conn = _engine(args.db, args.cwd)
    try:
        outcome = engine.advance(
            args.id, force=args.force, manual_result=args.manual_result or "")
        if args.json:
            print(json.dumps({
                "mission_id": outcome.mission_id, "action": outcome.action,
                "step_id": outcome.step_id, "message": outcome.message,
                "execution": outcome.execution,
            }, indent=2, default=str))
            return 0
        _print_logo("Mission advance")
        icon = {0: _GREEN + "✔", 1: _YELLOW + "◐", 2: _RED + "✘"}.get(
            {"executed": 0, "manual_completed": 0, "none_pending": 1,
             "not_active": 1, "denied": 1, "failed": 2}.get(
                outcome.action, 1), "?")
        print(f"  {icon}{_RESET} {outcome.action} — {outcome.message}")
        return 0
    finally:
        conn.close()


def _lifecycle(args: argparse.Namespace, verb: str) -> int:
    engine, conn = _engine(args.db, args.cwd)
    try:
        fn = {"start": engine.start, "pause": engine.pause,
              "cancel": engine.cancel, "complete": engine.complete,
              "delete": engine.delete}[verb]
        ok = fn(args.id)
        if not ok:
            print(f"  {_YELLOW}◐{_RESET} Couldn't {verb} mission "
                  f"{args.id} (does it exist?).")
            return 1
        print(f"  {_GREEN}✔{_RESET} Mission {args.id} {verb}ed.")
        return 0
    finally:
        conn.close()


def cmd_mission_start(args: argparse.Namespace) -> int:
    return _lifecycle(args, "start")


def cmd_mission_pause(args: argparse.Namespace) -> int:
    return _lifecycle(args, "pause")


def cmd_mission_cancel(args: argparse.Namespace) -> int:
    return _lifecycle(args, "cancel")


def cmd_mission_complete(args: argparse.Namespace) -> int:
    return _lifecycle(args, "complete")


def cmd_mission_delete(args: argparse.Namespace) -> int:
    return _lifecycle(args, "delete")


def build_mission_parser(subparsers) -> None:
    """Register `friday4 mission <cmd>` subcommands."""
    parser = subparsers.add_parser(
        "mission", help="Persistent goals (missions)",
        description="Track goals as persistent missions with ordered, "
                    "schedulable steps. Planning honors "
                    "FRIDAY_V4_CLAUDE_PLANNER (Claude Code decomposition "
                    "when opted in) exactly like friday4 talk.",
    )
    mission_sub = parser.add_subparsers(dest="mission_command")

    p = mission_sub.add_parser("create", help="Create a mission from a goal")
    p.add_argument("goal", help="The goal, in natural language")
    p.add_argument("--title", default=None, help="Display title (default: goal)")
    p.add_argument("--priority", default="medium",
                   choices=["low", "medium", "high", "critical"])
    p.add_argument("--cwd", default=None, help="Working directory (default: cwd)")
    p.add_argument("--db", default=None, help="V4 DB path (default: ~/.friday/v4.db)")
    p.add_argument("--json", action="store_true", help="JSON output")
    p.set_defaults(func=cmd_mission_create)

    p = mission_sub.add_parser("list", help="List missions")
    p.add_argument("--status", default=None,
                   help="Filter: planned|active|paused|completed|cancelled|failed")
    p.add_argument("--cwd", default=None)
    p.add_argument("--db", default=None)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_mission_list)

    p = mission_sub.add_parser("status", help="Show a mission with its steps")
    p.add_argument("id", help="Mission id")
    p.add_argument("--cwd", default=None)
    p.add_argument("--db", default=None)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_mission_status)

    p = mission_sub.add_parser("replan", help="Re-decompose a mission's plan")
    p.add_argument("id", help="Mission id")
    p.add_argument("--goal", default=None, help="New goal (default: the current one)")
    p.add_argument("--reason", default=None, help="Why the plan changed")
    p.add_argument("--cwd", default=None)
    p.add_argument("--db", default=None)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_mission_replan)

    p = mission_sub.add_parser("advance", help="Run the next step")
    p.add_argument("id", help="Mission id")
    p.add_argument("--force", action="store_true",
                   help="Bypass the confirm gate (operator override)")
    p.add_argument("--manual-result", default=None,
                   help="Result for a manual step the operator completed")
    p.add_argument("--cwd", default=None)
    p.add_argument("--db", default=None)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_mission_advance)

    for verb, fn, help_text in (
        ("start", cmd_mission_start, "Activate a mission"),
        ("pause", cmd_mission_pause, "Pause a mission"),
        ("cancel", cmd_mission_cancel, "Cancel a mission"),
        ("complete", cmd_mission_complete, "Mark a mission complete"),
        ("delete", cmd_mission_delete, "Delete a mission"),
    ):
        p = mission_sub.add_parser(verb, help=help_text)
        p.add_argument("id", help="Mission id")
        p.add_argument("--cwd", default=None)
        p.add_argument("--db", default=None)
        p.set_defaults(func=fn)


def main(argv: Optional[list[str]] = None) -> int:
    """Standalone entry (used by tests + `python -m friday_v4.cli_missions`)."""
    parser = argparse.ArgumentParser(prog="friday4 mission")
    subparsers = parser.add_subparsers(dest="command")
    build_mission_parser(subparsers)
    args = parser.parse_args(list(argv) if argv is not None else None)
    if hasattr(args, "func"):
        return args.func(args) or 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
