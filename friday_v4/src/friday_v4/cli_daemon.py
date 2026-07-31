"""CLI commands for `friday4 daemon` — the ambient FRIDAY process.

Usage:
    friday4 daemon start [--voice] [--no-notifications] [--poll N]
    friday4 daemon status
    friday4 daemon stop
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time

logger = logging.getLogger("friday_v4.cli_daemon")

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


def _status_line(key: str, value: str, ok: bool = True):
    icon = _GREEN + "✔" if ok else _RED + "✘"
    print(f"  {icon}{_RESET} {key:<18} {_DIM}{value}{_RESET}")


def cmd_daemon_start(args: argparse.Namespace):
    """Start the daemon (foreground; Ctrl+C stops cleanly)."""
    from .daemon import DaemonConfig, DaemonService, is_running

    if is_running():
        print(f"  {_RED}✘{_RESET} Daemon already running (pid {is_running() and '? — see v4_daemon.pid'})")
        return 1

    _print_logo("Daemon")
    config = DaemonConfig(
        voice=args.voice,
        notifications=not args.no_notifications,
        poll_interval=args.poll,
    )
    service = DaemonService(config=config)
    print(f"  {_DIM}Voice: {config.voice} | Notifications: {config.notifications}"
          f" | Poll: {config.poll_interval}s{_RESET}")
    print(f"  {_DIM}Press Ctrl+C to stop.{_RESET}\n")

    # Let logging reach the terminal so the daemon feels alive.
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    try:
        service.run()
    except KeyboardInterrupt:
        service.stop()
        service._shutdown_components()
    print(f"  {_GREEN}✔{_RESET} Daemon stopped.")
    return 0


def cmd_daemon_status(args: argparse.Namespace):
    """Show daemon state and subcomponent health."""
    from .daemon import is_running, read_status

    _print_logo("Daemon")
    running = is_running()
    status = read_status()

    state = status.get("state", "stopped")
    ok = running and state == "running"
    _status_line("state", state + (" (not running)" if not ok else ""), ok=ok)
    if status.get("started_at"):
        uptime = status.get("uptime_seconds", 0)
        _status_line("uptime", f"{uptime / 60:.1f} min" if uptime >= 60 else f"{uptime:.0f}s")
    _status_line("pid", str(status.get("pid", "—")))
    _status_line("notifications", str(status.get("notification_count", 0)))

    comps = status.get("components", {})
    if comps:
        print(f"\n  {_DIM}Components{_RESET}")
        for name, healthy in comps.items():
            _status_line(name, "up" if healthy else "down", ok=healthy)

    if not running and status:
        print(f"\n  {_YELLOW}◐{_RESET} Status file is stale — daemon is not running.")
    elif not running:
        print(f"\n  {_YELLOW}◐{_RESET} Daemon not running. Start with `friday4 daemon start`.")
    return 0 if ok else 1


def cmd_daemon_stop(args: argparse.Namespace):
    """Stop a running daemon by pid file."""
    from .daemon import DaemonService, read_pid

    pid = read_pid()
    if not pid:
        DaemonService.clear_state_files()
        print(f"  {_YELLOW}◐{_RESET} No daemon pid file — nothing to stop.")
        return 1
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        DaemonService.clear_state_files()
        print(f"  {_YELLOW}◐{_RESET} Daemon (pid {pid}) not running — cleaned stale files.")
        return 1
    # Wait briefly for a clean exit.
    for _ in range(20):
        try:
            os.kill(pid, 0)
            time.sleep(0.25)
        except OSError:
            break
    print(f"  {_GREEN}✔{_RESET} Daemon (pid {pid}) stopped.")
    return 0


def build_daemon_parser(subparsers) -> None:
    """Register `friday4 daemon <cmd>` subcommands."""
    parser = subparsers.add_parser(
        "daemon", help="Ambient FRIDAY background service",
        description="Run Friday V4 as a persistent ambient service: desktop "
                    "observer, ambient + proactive notifications, intelligence "
                    "sampling, and optional hotword voice.",
    )
    daemon_sub = parser.add_subparsers(dest="daemon_command")

    p = daemon_sub.add_parser("start", help="Start the daemon (foreground)")
    p.add_argument("--voice", action="store_true",
                   help="Also start hotword voice listening")
    p.add_argument("--no-notifications", action="store_true",
                   help="Disable ambient + suggestion notifications")
    p.add_argument("--poll", type=float, default=10.0,
                   help="Ambient feed poll interval in seconds (default: 10)")
    p.set_defaults(func=cmd_daemon_start)

    p = daemon_sub.add_parser("status", help="Show daemon status")
    p.set_defaults(func=cmd_daemon_status)

    p = daemon_sub.add_parser("stop", help="Stop a running daemon")
    p.set_defaults(func=cmd_daemon_stop)
