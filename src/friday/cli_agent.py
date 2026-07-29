"""CLI for agent management — `friday agent status`, `friday agent history`, `friday agent cancel`.

Provides the management interface for the AgenticExecutor (``agent.py``).
Lets the operator inspect running agent sessions, view history, and cancel
in-progress tasks.
"""

from __future__ import annotations

import argparse
import sys

from .db import connect
from .presentation.cli_format import header, green, yellow, red, gray, cyan, bold


def cmd_agent(args: argparse.Namespace) -> int:
    """Dispatch agent subcommands."""
    sub = (args.subcommand or "status").lower()
    if sub == "status":
        return _agent_status()
    if sub == "history":
        return _agent_history(args)
    if sub == "cancel":
        return _agent_cancel()
    print(f"Unknown agent subcommand: {sub}", file=sys.stderr)
    return 2


def _agent_status() -> int:
    """Show current agent session status."""
    conn = connect()
    try:
        from .agent import get_active_session
        session = get_active_session(conn)
    except Exception as exc:
        print(red(f"Error: {exc}"))
        return 1
    finally:
        conn.close()

    if not session:
        print(yellow("No active agent session."))
        print(gray("  Run `friday do <task>` to start one."))
        return 0

    print(header("Agent Status", session.session_id))
    print()
    print(f"  Task:     {bold(session.task[:80])}")
    print(f"  Status:   {green('Running') if session.status == 'running' else yellow(session.status)}")
    print(f"  Steps:    {len([s for s in session.steps if s.success])}/{len(session.steps)} completed")
    print(f"  Duration: {session.duration_ms / 1000:.1f}s")
    if session.adapted:
        print(f"  Adapted:  {yellow('Yes')}")
    print()

    if session.steps:
        print(gray("  Steps:"))
        for s in session.steps:
            icon = green("✓") if s.success else red("✗")
            adapted = gray(" [adapted]") if s.adapted else ""
            print(f"    {icon} Step {s.index + 1}: {s.description}{adapted}")
            print(f"       Tool: {cyan(s.tool)} | {s.duration_ms}ms")
            if s.stdout:
                first_line = s.stdout.strip().split("\n")[0][:100]
                if first_line:
                    print(f"       > {gray(first_line)}")
            if s.error:
                print(f"       {red(s.error[:120])}")
            print()

    return 0


def _agent_history(args: argparse.Namespace) -> int:
    """Show agent session history."""
    limit = min(getattr(args, "limit", 20), 100)
    conn = connect()
    try:
        from .agent import get_session_history
        sessions = get_session_history(conn, limit=limit)
    except Exception as exc:
        print(red(f"Error: {exc}"))
        return 1
    finally:
        conn.close()

    if not sessions:
        print(yellow("No agent session history."))
        return 0

    print(header("Agent History", f"{len(sessions)} session(s)"))
    print()
    for s in sessions:
        icon = green("✓") if s.status == "succeeded" else red("✗") if s.status == "failed" else yellow("⋯")
        adapted = gray(" [adapted]") if s.adapted else ""
        print(f"  {icon} {cyan(s.session_id)} {gray(s.created_at[:19])}{adapted}")
        print(f"     {s.task[:100]}")
        print(f"     {s.status} — {len([st for st in s.steps if st.success])}/{len(s.steps)} steps ({s.duration_ms / 1000:.1f}s)")
        print()

    return 0


def _agent_cancel() -> int:
    """Cancel the currently running agent session."""
    conn = connect()
    try:
        from .agent import cancel_active_session
        cancelled = cancel_active_session(conn)
    except Exception as exc:
        print(red(f"Error: {exc}"))
        return 1
    finally:
        conn.close()

    if cancelled:
        print(green("Cancelled active agent session."))
    else:
        print(yellow("No active session to cancel."))
    return 0
