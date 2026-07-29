"""CLI for Persistent Missions — ``friday mission <command>``.

Usage::

    friday mission start "Refactor auth module"
    friday mission list
    friday mission show <id>
    friday mission cancel <id>
    friday mission history
"""

from __future__ import annotations

import argparse
import json

from .presentation.cli_format import header, green, yellow, red, gray


def cmd_mission(args: argparse.Namespace) -> int:
    """Dispatch to the appropriate mission subcommand."""
    sub = getattr(args, "subcommand", "list")
    if sub == "start":
        return _start(args)
    elif sub == "list":
        return _list(args)
    elif sub == "show":
        return _show(args)
    elif sub == "cancel":
        return _cancel(args)
    elif sub == "history":
        return _history(args)
    else:
        print(red("  Usage: friday mission <start|list|show|cancel|history>"))
        return 2


def _start(args: argparse.Namespace) -> int:
    """Start a new persistent mission."""
    from .db import connect
    from .mission import MissionEngine

    goal = args.goal
    if not goal.strip():
        print(red("  error: mission goal cannot be empty"))
        return 1

    conn = connect()
    try:
        engine = MissionEngine(conn)
        mission = engine.start_mission(goal)
        print(green(f"  ✅ Mission started: {mission.mission_id}"))
        print(f"     Goal:  {mission.goal}")
        print(f"     Steps: {mission.total_steps}")
        print(f"     Run `friday mission list` to track progress.")
        print(f"     The daemon will advance it each cycle.")
    finally:
        conn.close()
    return 0


def _list(args: argparse.Namespace) -> int:
    """Show active missions."""
    from .db import connect
    from .mission import MissionEngine

    conn = connect()
    try:
        engine = MissionEngine(conn)
        missions = engine.get_active_missions()
    finally:
        conn.close()

    if not missions:
        print(gray("  No active missions."))
        print(gray("  Start one: `friday mission start \"my goal\"`"))
        return 0

    print(header("Active Missions", f"{len(missions)} running"))
    print()
    for m in missions:
        print(f"  {m.format_short()}")
        print()
    print(gray("  Show detail:  friday mission show <id>"))
    print(gray("  Cancel:       friday mission cancel <id>"))
    return 0


def _show(args: argparse.Namespace) -> int:
    """Show mission details."""
    from .db import connect
    from .mission import MissionEngine

    mission_id = args.mission_id
    if not mission_id:
        print(red("  error: mission ID required"))
        return 1

    conn = connect()
    try:
        engine = MissionEngine(conn)
        mission = engine.get_mission(mission_id)
    finally:
        conn.close()

    if not mission:
        print(red(f"  error: mission not found: {mission_id}"))
        return 1

    print(mission.format())
    return 0


def _cancel(args: argparse.Namespace) -> int:
    """Cancel a mission."""
    from .db import connect
    from .mission import MissionEngine

    mission_id = args.mission_id
    if not mission_id:
        print(red("  error: mission ID required"))
        return 1

    conn = connect()
    try:
        engine = MissionEngine(conn)
        if engine.cancel_mission(mission_id):
            print(green(f"  ✅ Mission cancelled: {mission_id}"))
        else:
            print(red(f"  error: could not cancel {mission_id} — "
                       "not found or already completed"))
            return 1
    finally:
        conn.close()
    return 0


def _history(args: argparse.Namespace) -> int:
    """Show mission history (all statuses)."""
    from .db import connect
    from .mission import MissionEngine

    limit = getattr(args, "limit", 20)

    conn = connect()
    try:
        engine = MissionEngine(conn)
        missions = engine.get_all_missions(limit=limit)
    finally:
        conn.close()

    if not missions:
        print(gray("  No missions yet."))
        return 0

    print(header("Mission History", f"{len(missions)} total"))
    print()
    for m in missions:
        print(f"  {m.format_short()}")
    print()
    print(gray("  Show detail:  friday mission show <id>"))
    return 0


# ---------------------------------------------------------------------------
# Subparser registration
# ---------------------------------------------------------------------------


def add_subparser(sub) -> None:
    """Add the ``mission`` subcommand parser."""
    p = sub.add_parser(
        "mission",
        help="Persistent missions — start, track, or cancel multi-cycle goals.",
    )
    p.add_argument(
        "subcommand", nargs="?", default="list",
        choices=["start", "list", "show", "cancel", "history"],
        help="'start' (create new), 'list' (active), 'show <id>', "
             "'cancel <id>', or 'history' (all).",
    )
    p.add_argument(
        "goal", nargs="?", default="",
        help="Mission goal (required for 'start').",
    )
    p.add_argument(
        "mission_id", nargs="?", default="",
        help="Mission ID (required for 'show' and 'cancel').",
    )
    p.add_argument(
        "--limit", type=int, default=20,
        help="Max missions to show in history (default: 20).",
    )
    p.set_defaults(func=cmd_mission)
