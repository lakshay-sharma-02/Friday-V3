"""CLI for Guided Walkthroughs — ``friday guide``.

Usage::

    friday guide create \"Deploy fix\" --step \"git pull\" --step \"run tests\"
    friday guide list
    friday guide status <id>
    friday guide step <id> done|fail|abort|pause|resume
"""

from __future__ import annotations

import argparse
import json
from .presentation.cli_format import header, green, yellow, red, gray, cyan


def cmd_guide(args: argparse.Namespace) -> int:
    """Manage guided walkthrough sessions."""
    from .db import connect
    from .guide import (
        create_guide, load_session, list_active_sessions,
        advance_guide, get_current_step, format_step,
    )

    action = getattr(args, "action", "list")

    conn = connect()
    try:
        if action == "create":
            title = getattr(args, "title", "Guided procedure")
            steps_raw = getattr(args, "step", [])
            if not steps_raw:
                print(red("  error: specify at least one --step"))
                return 1
            steps = [{"instruction": s} for s in steps_raw]
            channel = getattr(args, "channel", "cli")
            session = create_guide(conn, title, steps, channel=channel)
            print(green(f"  Guide created: {session.id}"))
            print(f"  Title: {session.title}")
            print(f"  Steps: {session.total_steps}")
            print(f"  Channel: {session.channel}")
            print()
            step = get_current_step(session)
            if step:
                print(format_step(session, step))
            return 0

        if action == "list":
            sessions = list_active_sessions(conn)
            if not sessions:
                print(gray("  No active guide sessions."))
                print(gray("  Create one: friday guide create \"Deploy fix\" --step \"git pull\""))
                return 0
            print(header("Active Guides", f"{len(sessions)} session(s)"))
            print()
            for s in sessions:
                status_mark = {
                    "running": green("●"), "paused": yellow("○"),
                    "completed": green("✓"), "aborted": red("✗"),
                }.get(s.status, gray("?"))
                print(f"  {status_mark} {s.id[:20]:20s} {s.title[:40]:40s} "
                      f"Step {s.current_step}/{s.total_steps}  [{s.channel}]")
            return 0

        if action == "status":
            session_id = getattr(args, "id", "")
            if not session_id:
                print(red("  error: specify a guide session ID"))
                return 1
            session = load_session(conn, session_id)
            if not session:
                print(red(f"  Guide session not found: {session_id}"))
                return 1
            print(header("Guide Session", session.id[:20]))
            print(f"  Title:   {session.title}")
            print(f"  Status:  {session.status}")
            print(f"  Step:    {session.current_step}/{session.total_steps}")
            print(f"  Channel: {session.channel}")
            print()
            step = get_current_step(session)
            if step:
                print(format_step(session, step))
            return 0

        if action in ("step", "advance"):
            session_id = getattr(args, "id", "")
            step_action = getattr(args, "step_action", "done")
            if not session_id:
                print(red("  error: specify a guide session ID"))
                return 1
            session = advance_guide(conn, session_id, step_action)
            if not session:
                print(red(f"  Guide session not found: {session_id}"))
                return 1
            if session.status in ("completed", "aborted"):
                print(green(f"  Guide {session.status}!"))
                return 0
            if session.status == "paused":
                print(yellow("  Guide paused. Resume: friday guide step <id> resume"))
                return 0
            step = get_current_step(session)
            if step:
                print(format_step(session, step))
            return 0

        print(red(f"  Unknown action: {action}"))
        return 1
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Subparser registration
# ---------------------------------------------------------------------------


def add_subparser(sub) -> None:
    """Add the ``guide`` subcommand parser."""
    p = sub.add_parser("guide", help="Guided walkthroughs — step-by-step procedures.")
    subp = p.add_subparsers(dest="action", required=True)

    # create
    pc = subp.add_parser("create", help="Create a new guided walkthrough.")
    pc.add_argument("title", help="Title for the guide.")
    pc.add_argument("--step", "-s", action="append", default=[], help="A step instruction (repeatable).")
    pc.add_argument("--channel", default="cli", help="Delivery channel (cli, telegram, slack, discord).")
    pc.set_defaults(func=cmd_guide)

    # list
    subp.add_parser("list", help="List active guide sessions.").set_defaults(func=cmd_guide)

    # status
    ps = subp.add_parser("status", help="Show guide session status.")
    ps.add_argument("id", help="Guide session ID.")
    ps.set_defaults(func=cmd_guide)

    # step (advance)
    pa = subp.add_parser("step", help="Advance a guide step (done, fail, abort, pause, resume).")
    pa.add_argument("id", help="Guide session ID.")
    pa.add_argument("step_action", nargs="?", default="done",
                    choices=["done", "fail", "abort", "pause", "resume"],
                    help="Action for the current step.")
    pa.set_defaults(func=cmd_guide)
