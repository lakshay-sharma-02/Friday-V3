"""CLI commands for `friday4 mobile` — companion transport (Wave 15).

Usage:
    friday4 mobile serve [--host 127.0.0.1] [--port 8900] [--db PATH]
    friday4 mobile push  [--once] [--db PATH]    # drain the durable queue

The phone is another surface of the same Friday: ``serve`` runs the
pure-stdlib companion API (status / conversation / talk / SSE events),
and ``push`` delivers queued ambient events to the configured
transporter (log by default — a companion app plugs a real push
endpoint in via ``PushNotificationService(transporter=...)``).

Design laws: never crash (missing DB renders neutral output), local
by default, pure stdlib.
"""

from __future__ import annotations

import argparse
import logging

logger = logging.getLogger("friday_v4.cli_mobile")

_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_CYAN = "\033[96m"
_GREEN = "\033[92m"
_RED = "\033[91m"


def _print_logo(title: str):
    print()
    print(f"  {_BOLD}{_CYAN}◆ FRIDAY{_RESET} {_DIM}V4 — {title}{_RESET}")
    print(f"  {_DIM}{'─' * 40}{_RESET}")


def cmd_mobile_serve(args: argparse.Namespace) -> int:
    """Start the companion API server (blocks until Ctrl+C)."""
    from .mobile import create_api_server

    _print_logo("Mobile Companion API")
    try:
        server = create_api_server(host=args.host, port=args.port,
                                   db_path=args.db)
    except OSError as exc:
        print(f"  {_RED}✘ Could not bind {args.host}:{args.port} — {exc}{_RESET}")
        print(f"  {_DIM}  Try: friday4 mobile serve --port 8901{_RESET}")
        return 1
    url = f"http://{args.host}:{server.server_address[1]}/api/status"
    print(f"  {_DIM}Companion API:{_RESET} {_CYAN}{url}{_RESET}")
    print(f"  {_DIM}Endpoints:{_RESET} /api/status · /api/conversation · "
          f"/api/talk · /api/events (SSE push)")
    print(f"  {_DIM}Press Ctrl+C to stop.{_RESET}\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    print(f"  {_GREEN}✔ Companion API stopped.{_RESET}")
    return 0


def cmd_mobile_push(args: argparse.Namespace) -> int:
    """Drain the durable ambient queue to the push transporter.

    Without ``--once`` this loops every ``--poll`` seconds (a tiny
    poll-style push loop for hosts where the companion can't keep an
    SSE connection open); with ``--once`` it delivers a single batch
    (scriptable / cron-friendly).
    """
    import time

    from .mobile import PushNotificationService

    _print_logo("Mobile Push")
    # The daemon owns this schedule now (MobilePushWorker, Wave 15) —
    # when it's running, a manual `friday4 mobile push` mostly drains
    # what the daemon already delivered (same persisted cursor, so no
    # double-delivery — but also usually nothing new). Informational
    # hint, not a block.
    try:
        from .daemon import is_running
        if is_running():
            print(f"  {_YELLOW}◐{_RESET} The daemon is running and already "
                  f"drains the queue on a schedule "
                  f"(shared cursor — no double-delivery).")
    except Exception:
        pass
    service = PushNotificationService(
        db_path=args.db,
        transporter=_print_transporter if args.verbose else None,
        min_priority=args.min_priority)
    print(f"  {_DIM}cursor: {service.cursor}{_RESET}")
    try:
        while True:
            delivered = service.poll_once()
            if delivered:
                print(f"  {_GREEN}✔ delivered {delivered} event(s) — "
                      f"cursor {service.cursor}{_RESET}")
            if args.once:
                break
            for _ in range(max(int(args.poll / 0.5), 1)):
                time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    print(f"  {_DIM}total delivered this run: "
          f"{service.delivered_total}{_RESET}\n")
    return 0


def _print_transporter(notification) -> None:
    from .mobile import Notification
    if not isinstance(notification, Notification):
        return
    print(f"  {_CYAN}[{notification.topic}]{_RESET} "
          f"{notification.payload[:140]}")


def build_mobile_parser(subparsers) -> None:
    """Register `friday4 mobile` (used by the integrated CLI)."""
    parser = subparsers.add_parser(
        "mobile", help="Mobile companion transport",
        description="Run the companion API / push transport so your "
                    "phone is another surface of the same Friday.",
    )
    mobile_sub = parser.add_subparsers(dest="mobile_command")

    serve = mobile_sub.add_parser(
        "serve", help="Run the companion API server",
        description="Serves /api/status, /api/conversation, POST "
                    "/api/talk, and SSE /api/events (durable-queue push).")
    serve.add_argument("--host", default="127.0.0.1",
                       help="Bind address (default: 127.0.0.1)")
    serve.add_argument("--port", type=int, default=8900,
                       help="Port to listen on (default: 8900)")
    serve.add_argument("--db", default=None,
                       help="V4 state DB path (default: ~/.friday/v4.db)")
    serve.set_defaults(func=cmd_mobile_serve)

    push = mobile_sub.add_parser(
        "push", help="Deliver the durable queue to the push transporter",
        description="Replays ambient events since the persisted cursor "
                    "to the transporter (log by default).")
    push.add_argument("--once", action="store_true",
                      help="Deliver one batch and exit (scriptable)")
    push.add_argument("--poll", type=float, default=60.0,
                      help="Seconds between polls in loop mode (default 60)")
    push.add_argument("--min-priority", type=int, default=0,
                      help="Only deliver events at/above this priority "
                           "(0 routine, 1 important, 2 critical)")
    push.add_argument("--verbose", "-v", action="store_true",
                      help="Print each delivered notification")
    push.add_argument("--db", default=None,
                      help="V4 state DB path (default: ~/.friday/v4.db)")
    push.set_defaults(func=cmd_mobile_push)


if __name__ == "__main__":  # pragma: no cover - standalone entry
    logging.basicConfig(level=logging.WARNING)
    parser = argparse.ArgumentParser(prog="friday4 mobile")
    build_mobile_parser(parser)
    args = parser.parse_args()
    if hasattr(args, "func"):
        raise SystemExit(args.func(args) or 0)
    parser.print_help()
