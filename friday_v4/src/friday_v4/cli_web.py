"""CLI commands for `friday4 web` — the local dashboard server.

Usage:
    friday4 web [--host 127.0.0.1] [--port 8899]

Serves the pure-stdlib dashboard over the V4 subsystems (daemon,
security, intelligence, proactive, V3 bridge, voice) at the given
address. Local-first by default; opening a browser tab is attempted
automatically.
"""

from __future__ import annotations

import argparse
import logging

logger = logging.getLogger("friday_v4.cli_web")

_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_CYAN = "\033[96m"
_GREEN = "\033[92m"
_RED = "\033[91m"


def cmd_web(args: argparse.Namespace) -> int:
    """Start the dashboard server (blocks until Ctrl+C)."""
    from .web.server import serve

    print()
    print(f"  {_BOLD}{_CYAN}◆ FRIDAY{_RESET} {_DIM}V4 — Web Dashboard{_RESET}")
    print(f"  {_DIM}{'─' * 40}{_RESET}")
    try:
        serve(host=args.host, port=args.port)
    except OSError as exc:
        print(f"  {_RED}✘ Could not bind {args.host}:{args.port} — {exc}{_RESET}")
        print(f"  {_DIM}  Try: friday4 web --port 8900{_RESET}")
        return 1
    print(f"  {_GREEN}✔ Dashboard stopped.{_RESET}")
    return 0


def build_web_parser(subparsers) -> None:
    """Register `friday4 web` (used by the integrated CLI)."""
    parser = subparsers.add_parser(
        "web", help="Local web dashboard",
        description="Start a local dashboard visualizing daemon, security, "
                    "intelligence, proactive, V3, and voice status.",
    )
    parser.add_argument("--host", default="127.0.0.1",
                        help="Bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8899,
                        help="Port to listen on (default: 8899)")
    parser.set_defaults(func=cmd_web)


if __name__ == "__main__":  # pragma: no cover - standalone entry
    logging.basicConfig(level=logging.WARNING)
    parser = argparse.ArgumentParser(prog="friday4 web")
    parser.add_argument("--host", default="127.0.0.1",
                        help="Bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8899,
                        help="Port to listen on (default: 8899)")
    args = parser.parse_args()
    raise SystemExit(cmd_web(args))
