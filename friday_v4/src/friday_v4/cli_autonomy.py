"""CLI commands for `friday4 autonomy` — Friday's own judgment → action loop.

The operator-facing surface for the autonomy layer: see what Friday is
doing by itself, what it's asking permission for, approve or deny a
pending ask, and manage the operator overrides that make Friday learn
from "no" / "do it differently".

Usage:
    friday4 autonomy status            # loop report (executed / asked / skipped)
    friday4 autonomy pending           # open permission requests
    friday4 autonomy approve <id>      # allow a pending action (like 'yes, run it')
    friday4 autonomy deny <id> [--why "…"]   # decline + record an override
    friday4 autonomy overrides         # what Friday won't propose anymore
    friday4 autonomy clear-overrides [action_type]   # un-block actions
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger("friday_v4.cli_autonomy")

_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_CYAN = "\033[96m"
_GREEN = "\033[92m"
_YELLOW = "\033[93m"
_RED = "\033[91m"

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 3


def _print_logo(title: str = "Autonomy"):
    print()
    print(f"  {_BOLD}{_CYAN}◆ FRIDAY{_RESET} {_DIM}V4 — {title}{_RESET}")
    print(f"  {_DIM}{'─' * 40}{_RESET}")


def _agent(db_path):
    from friday_v4.autonomy import AutonomyAgent
    return AutonomyAgent(db_path=db_path)


def _resolve_db(args):
    try:
        from . import db
        return db.connect(path=getattr(args, "db", None))
    except Exception as exc:
        logger.debug(f"autonomy: db unavailable ({exc})")
        return None


def _close_db(conn) -> None:
    try:
        if conn is not None:
            conn.close()
    except Exception:
        pass


def _status_payload(args) -> Optional[dict]:
    """The loop report + pending/override counts (None on DB failure)."""
    conn = _resolve_db(args)
    try:
        from . import db
        if conn is None:
            return None
        pending = db.pending_permission_requests(conn)
        overrides = db.list_overrides(conn)
        agent = _agent(getattr(args, "db", None))
        last = agent.last_result.to_dict() if agent.last_result else {}
        return {
            "last_cycle": last,
            "pending": len(pending),
            "overrides": len(overrides),
        }
    except Exception as exc:
        logger.debug(f"autonomy status failed: {exc}")
        return None
    finally:
        _close_db(conn)


def cmd_autonomy_status(args: argparse.Namespace) -> int:
    """`friday4 autonomy status` — the loop's latest report."""
    payload = _status_payload(args)
    if payload is None:
        print(f"  {_RED}✗ could not read autonomy state.{_RESET}")
        return EXIT_FAILED

    if args.json:
        print(json.dumps(payload, default=str))
        return EXIT_OK

    _print_logo()
    last = payload.get("last_cycle") or {}
    print(f"  {_BOLD}Last judgment cycle{_RESET}")
    print(f"  {_DIM}{'─' * 40}{_RESET}")
    if not last:
        print(f"  {_DIM}No cycle recorded yet — the daemon runs the loop "
              f"every interval and reports here.{_RESET}")
    else:
        print(f"  {_GREEN}executed{_RESET}  {last.get('executed', 0)}"
              f"  {_DIM}(ran by itself, AUTO-level){_RESET}")
        print(f"  {_YELLOW}asked{_RESET}     {last.get('asked', 0)}"
              f"  {_DIM}(permission requested){_RESET}")
        print(f"  {_RED}skipped{_RESET}   {last.get('skipped', 0)}"
              f"  {_DIM}(override / never / already asked){_RESET}")
        for o in last.get("outcomes", []):
            icon = {"executed": f"{_GREEN}●{_RESET}",
                    "asked": f"{_YELLOW}◉{_RESET}",
                    "skipped_override": f"{_RED}⊘{_RESET}",
                    "skipped_never": f"{_RED}⛔{_RESET}",
                    "skipped_pending": f"{_DIM}○{_RESET}",
                    "noop": f"{_DIM}○{_RESET}"}.get(o.get("disposition"), "•")
            print(f"  {icon} [{o.get('source', '?')}] "
                  f"{o.get('description', '')}")
    print()
    print(f"  {_BOLD}Open permission requests{_RESET}  {payload['pending']}")
    print(f"  {_BOLD}Operator overrides{_RESET}        {payload['overrides']}")
    print()
    if payload["pending"]:
        print(f"  {_DIM}  → 'friday4 autonomy pending' to see them, "
              f"or just say 'yes, run it' / 'no'.{_RESET}")
    print()
    return EXIT_OK


def cmd_autonomy_pending(args: argparse.Namespace) -> int:
    """`friday4 autonomy pending` — the durable permission asks."""
    conn = _resolve_db(args)
    try:
        from . import db
        if conn is None:
            raise RuntimeError("no DB")
        pending = db.pending_permission_requests(conn)
    except Exception as exc:
        print(f"  {_RED}✗ could not read pending requests: {exc}{_RESET}")
        return EXIT_FAILED
    finally:
        _close_db(conn)

    if args.json:
        print(json.dumps(pending, default=str))
        return EXIT_OK

    _print_logo("Autonomy · Pending")
    if not pending:
        print(f"  {_GREEN}✓ Nothing waiting for permission — Friday is "
              f"either running AUTO work or waiting for context.{_RESET}")
        print()
        return EXIT_OK
    for req in pending:
        rid = req.get("id", "?")[:8]
        what = req.get("description") or req.get("command") or "?"
        cmd = req.get("command") or ""
        src = req.get("source", "?")
        print(f"  {_YELLOW}◉{_RESET} [{src}] {what}")
        if cmd and cmd not in what:
            print(f"    {_DIM}run: {cmd}{_RESET}")
        print(f"    {_DIM}id: {rid} · created: "
              f"{str(req.get('created_at', ''))[:19]}{_RESET}")
        print(f"    {_DIM}→ 'friday4 autonomy approve {rid}' or "
              f"say 'yes, run it'{_RESET}")
    print()
    return EXIT_OK


def cmd_autonomy_approve(args: argparse.Namespace) -> int:
    """`friday4 autonomy approve <id>` — allow a pending action."""
    rid = (args.request_id or "").strip()
    if not rid:
        print(f"  {_RED}✗ give me a request id — 'friday4 autonomy "
              f"pending' lists them.{_RESET}")
        return EXIT_USAGE
    try:
        agent = _agent(getattr(args, "db", None))
        outcome = agent.accept(rid, force=getattr(args, "force", False))
    except Exception as exc:
        print(f"  {_RED}✗ could not approve: {exc}{_RESET}")
        return EXIT_FAILED
    if not outcome:
        print(f"  {_YELLOW}⚠ That request is no longer pending.{_RESET}")
        return EXIT_OK
    status = outcome.get("status", "failed")
    if status == "succeeded":
        first = (outcome.get("output") or "").strip().splitlines()[:1]
        print(f"  {_GREEN}✓ Approved and done"
              f"{': ' + first[0][:120] if first else ''}.{_RESET}")
        print(f"  {_DIM}  audit id: {outcome.get('action_id')}{_RESET}")
        return EXIT_OK
    if status == "denied":
        print(f"  {_YELLOW}⚠ That action needs an explicit --force "
              f"override — a bare approval isn't enough.{_RESET}")
        return EXIT_OK
    print(f"  {_RED}✗ That didn't work — {status}.{_RESET}")
    return EXIT_FAILED


def cmd_autonomy_deny(args: argparse.Namespace) -> int:
    """`friday4 autonomy deny <id>` — decline + record an override."""
    rid = (args.request_id or "").strip()
    if not rid:
        print(f"  {_RED}✗ give me a request id — 'friday4 autonomy "
              f"pending' lists them.{_RESET}")
        return EXIT_USAGE
    try:
        agent = _agent(getattr(args, "db", None))
        ok = agent.deny(rid, reason=getattr(args, "why", "") or
                        "operator declined via CLI")
    except Exception as exc:
        print(f"  {_RED}✗ could not deny: {exc}{_RESET}")
        return EXIT_FAILED
    if not ok:
        print(f"  {_YELLOW}⚠ That request is no longer pending.{_RESET}")
        return EXIT_OK
    print(f"  {_GREEN}✓ Declined — Friday will not suggest that action "
          f"again (until you clear the override).{_RESET}")
    return EXIT_OK


def cmd_autonomy_overrides(args: argparse.Namespace) -> int:
    """`friday4 autonomy overrides` — what Friday won't propose anymore."""
    conn = _resolve_db(args)
    try:
        from . import db
        if conn is None:
            raise RuntimeError("no DB")
        overrides = db.list_overrides(conn)
    except Exception as exc:
        print(f"  {_RED}✗ could not read overrides: {exc}{_RESET}")
        return EXIT_FAILED
    finally:
        _close_db(conn)

    if args.json:
        print(json.dumps(overrides, default=str))
        return EXIT_OK

    _print_logo("Autonomy · Overrides")
    if not overrides:
        print(f"  {_GREEN}✓ No overrides — Friday proposes everything it "
              f"judges worth doing.{_RESET}")
        print()
        return EXIT_OK
    print(f"  {_YELLOW}⊘ Friday learned to skip these (from your 'no' / "
          f"'do it differently'):{_RESET}")
    for o in overrides:
        what = f"{o.get('action_type')} {o.get('command')}".strip()
        reason = o.get("reason") or ""
        print(f"  {_YELLOW}⊘{_RESET} {what}"
              f"{f'  {_DIM}({reason}){_RESET}' if reason else ''}")
    print(f"  {_DIM}  → clear with 'friday4 autonomy clear-overrides "
          f"[action_type]' or say 'you can do that again'.{_RESET}")
    print()
    return EXIT_OK


def cmd_autonomy_clear_overrides(args: argparse.Namespace) -> int:
    """`friday4 autonomy clear-overrides [action_type]` — un-block."""
    conn = _resolve_db(args)
    try:
        from . import db
        if conn is None:
            raise RuntimeError("no DB")
        removed = db.clear_overrides(
            conn, action_type=(getattr(args, "action_type", None) or None))
    except Exception as exc:
        print(f"  {_RED}✗ could not clear overrides: {exc}{_RESET}")
        return EXIT_FAILED
    finally:
        _close_db(conn)
    if args.json:
        print(json.dumps({"cleared": removed}, default=str))
        return EXIT_OK
    _print_logo("Autonomy · Overrides")
    print(f"  {_GREEN}✓ Cleared {removed} override(s) — Friday may propose "
          f"those actions again.{_RESET}")
    print()
    return EXIT_OK


def _add_autonomy_commands(subparsers) -> None:
    p = subparsers.add_parser(
        "status", help="The loop's latest report (executed/asked/skipped)")
    p.add_argument("--db", type=Path, default=None)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_autonomy_status)

    p = subparsers.add_parser("pending", help="Open permission requests")
    p.add_argument("--db", type=Path, default=None)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_autonomy_pending)

    p = subparsers.add_parser(
        "approve", help="Allow a pending action (like 'yes, run it')")
    p.add_argument("request_id", help="Request id from 'friday4 autonomy pending'")
    p.add_argument("--force", action="store_true",
                   help="Explicit override for never-level steps")
    p.add_argument("--db", type=Path, default=None)
    p.set_defaults(func=cmd_autonomy_approve)

    p = subparsers.add_parser(
        "deny", help="Decline a pending action + record an override")
    p.add_argument("request_id", help="Request id from 'friday4 autonomy pending'")
    p.add_argument("--why", type=str, default="", help="Reason for the override")
    p.add_argument("--db", type=Path, default=None)
    p.set_defaults(func=cmd_autonomy_deny)

    p = subparsers.add_parser("overrides",
                              help="Actions Friday learned to skip")
    p.add_argument("--db", type=Path, default=None)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_autonomy_overrides)

    p = subparsers.add_parser("clear-overrides",
                              help="Un-block overridden actions")
    p.add_argument("action_type", nargs="?", default=None,
                   help="Optional action type to clear (default: all)")
    p.add_argument("--db", type=Path, default=None)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_autonomy_clear_overrides)


def build_autonomy_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "autonomy", help="Friday's own judgment → action loop",
        description="See what Friday does by itself, approve/deny its "
                    "permission asks, and manage operator overrides.",
    )
    autonomy_sub = parser.add_subparsers(dest="autonomy_command")
    _add_autonomy_commands(autonomy_sub)


def main(argv: list[str] | None = None) -> int:
    """Standalone entry point for `python -m friday_v4.cli_autonomy`."""
    logging.basicConfig(level=logging.WARNING)
    parser = argparse.ArgumentParser(prog="friday4 autonomy")
    sub = parser.add_subparsers(dest="command")
    _add_autonomy_commands(sub)

    args = parser.parse_args(argv)
    if hasattr(args, "func"):
        return args.func(args) or 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
