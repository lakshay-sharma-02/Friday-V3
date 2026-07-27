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
    """Display the daemon status in a clean dashboard format."""
    from .presentation.cli_format import (
        header, green, yellow, red, gray, bold, status_dot, card,
    )

    status = get_status()
    running = is_running()
    pid = status.get("pid", "?")
    state_str = status.get("state", "unknown")

    print(header("Daemon", f"PID {pid}" if running else "stopped"))
    print()

    # Health indicator.
    health_dot = status_dot(running)
    state_label = bold(green(f"Running (PID {pid})")) if running else bold(red("Stopped"))
    print(f"  {health_dot} {state_label}")
    print()

    # Timeline card.
    timeline_lines = []
    if status.get("started_at"):
        ts = status["started_at"][:19]
        timeline_lines.append(f"Started          {ts}")
    if status.get("last_cycle_at"):
        ts = status["last_cycle_at"][:19]
        timeline_lines.append(f"Last cycle       {ts}")
    if status.get("cycle_count", 0) > 0:
        timeline_lines.append(f"Total cycles     {status['cycle_count']}")
    if status.get("interval_seconds"):
        secs = status["interval_seconds"]
        timeline_lines.append(f"Interval         {secs}s ({secs // 60}m)")
    if status.get("last_cycle_outcome"):
        outcome = status["last_cycle_outcome"]
        outcome_icon = {"succeeded": green("✓"), "failed": red("✗"), "skipped": yellow("~")}.get(outcome, gray("?"))
        timeline_lines.append(f"Last outcome     {outcome_icon} {outcome}")

    if timeline_lines:
        print(card("Timeline", timeline_lines, color="blue", indent=0))
        print()

    # Ambient findings card.
    sug = status.get("new_suggestions", 0)
    high = status.get("high_severity_suggestions", 0)
    gaps = status.get("new_gaps", 0)
    open_gaps = status.get("open_gaps", 0)
    patterns = status.get("new_patterns", 0)
    intents = status.get("new_intents", 0)
    corrs = status.get("new_correlations", 0)
    skills = status.get("new_skills", 0)
    watched = status.get("watched_repos", 0)

    finding_lines = []
    if watched:
        finding_lines.append(f"Watched repos    {watched}")
    if patterns:
        top = status.get("top_patterns", 0)
        finding_lines.append(f"Patterns         {patterns} mined ({top} frequent)")
    if intents:
        high_conf = status.get("high_conf_intents", 0)
        finding_lines.append(f"Workflows        {intents} labeled ({high_conf} high confidence)")
    if skills:
        finding_lines.append(f"Skills           {skills} formed")
    if sug:
        sev = f" ({high} high-severity)" if high else ""
        finding_lines.append(f"Suggestions      {sug}{sev}")
    if gaps:
        finding_lines.append(f"Gaps             {gaps} new, {open_gaps} open")
    if corrs:
        finding_lines.append(f"Correlations     {corrs} detected")

    if finding_lines:
        print(card("Ambient Findings", finding_lines, color="green", indent=0))
        print()

    # Error card if present.
    if status.get("last_error"):
        print(card("Last Error", [status["last_error"][:200]], color="red", indent=0))
        print()

    # Path info.
    print(gray(f"  PID: {PID_FILE}  Log: {LOG_FILE}  Status: {STATUS_FILE}"))
    print()

    # Next action.
    if running:
        print(gray("  › friday daemon stop    to stop"))
        print(gray("  › friday daemon logs    to see recent activity"))
    else:
        print(gray("  › friday daemon start   to start"))
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
