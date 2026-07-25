"""friday daemon — CLI subcommand for the ambient daemon."""

from __future__ import annotations

import argparse
import sys
from typing import NoReturn

from .daemon import (
    FRIDAY_DIR,
    LOG_FILE,
    STATUS_FILE,
    get_status,
    is_running,
    logs,
    restart,
    start,
    stop,
)

INTERVAL_HELP = "Seconds between observation cycles (default: 900 = 15 min)"


def cmd_daemon(args: argparse.Namespace) -> int:
    """Dispatch friday daemon subcommands."""
    action = getattr(args, "action", None) or "status"

    if action == "start":
        interval = getattr(args, "interval", 900)
        no_notify = getattr(args, "no_notify", False)
        return start(interval_seconds=interval, no_notify=no_notify)

    elif action == "stop":
        return stop()

    elif action == "restart":
        interval = getattr(args, "interval", 900)
        no_notify = getattr(args, "no_notify", False)
        return restart(interval_seconds=interval, no_notify=no_notify)

    elif action == "status":
        return _show_status()

    elif action == "logs":
        n = getattr(args, "lines", 50)
        return _show_logs(n)

    else:
        print(f"Unknown daemon action: {action}", file=sys.stderr)
        return 2


def _show_status() -> int:
    """Display the daemon status."""
    status = get_status()
    running = is_running()

    print("Friday Daemon\n")
    if running:
        print(f"  State:    running (PID {status.get('pid', '?')})")
    else:
        print(f"  State:    stopped (last: {status.get('state', 'unknown')})")
    print(f"  PID file: {PID_FILE}")
    print(f"  Log:      {LOG_FILE}")
    print(f"  Status:   {STATUS_FILE}")
    print()

    if status.get("started_at"):
        print(f"  Started:       {status['started_at']}")
    if status.get("last_cycle_at"):
        print(f"  Last cycle:    {status['last_cycle_at']}")
    if status.get("last_cycle_outcome"):
        outcome = status["last_cycle_outcome"]
        mark = {"succeeded": "✓", "failed": "✗", "skipped": "~"}.get(outcome, "?")
        print(f"  Last outcome:  {mark} {outcome}")
    if status.get("cycle_count", 0) > 0:
        print(f"  Total cycles:  {status['cycle_count']}")
    if status.get("interval_seconds"):
        print(f"  Interval:      {status['interval_seconds']}s ({status['interval_seconds'] // 60}m)")
    if status.get("watched_repos", 0) > 0:
        print(f"  Watched repos: {status['watched_repos']}")
    if status.get("new_suggestions", 0) > 0 or status.get("new_gaps", 0) > 0:
        sug = status.get("new_suggestions", 0)
        high = status.get("high_severity_suggestions", 0)
        gaps = status.get("new_gaps", 0)
        open_gaps = status.get("open_gaps", 0)
        parts = []
        if sug:
            parts.append(f"{sug} suggestion(s)")
        if high:
            parts.append(f"{high} high-severity")
        if gaps:
            parts.append(f"{gaps} new gap(s)")
        if open_gaps:
            parts.append(f"{open_gaps} open gap(s)")
        print(f"  Ambient findings: {', '.join(parts)}")
    if status.get("last_error"):
        print(f"  Last error:    {status['last_error'][:200]}")
    print()

    if running:
        print(f"Use 'friday daemon stop' to stop the daemon.")
    else:
        print(f"Use 'friday daemon start' to start the daemon.")
    print()

    return 0


def _show_logs(n: int) -> int:
    """Show the last N lines of the daemon log."""
    lines = logs(tail=n)
    if lines is None:
        print(f"No daemon log found at {LOG_FILE}.", file=sys.stderr)
        return 1
    if not lines:
        print("Daemon log is empty.")
        return 0
    print(f"Last {len(lines)} lines of {LOG_FILE}:\n")
    for line in lines:
        print(line)
    return 0


PID_FILE = FRIDAY_DIR / "daemon.pid"
