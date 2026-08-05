"""CLI commands for `friday6 relationship` — how close Friday and you are (Wave 10 §3.3).

The text surface for the relationship layer: depth computed from *real
interaction data* (exchanges, sessions, missions, facts), mapped to a
tone + verbosity + briefing length. Depth is monotonic — more
interaction → deeper, never suddenly shallower.

Usage:
    friday6 relationship status              # depth, level, tone, signals
    friday6 relationship status --json       # machine-readable
    friday6 relationship refresh             # recompute + persist now
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger("friday_v6.cli_relationship")

_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_CYAN = "\033[96m"
_GREEN = "\033[92m"
_YELLOW = "\033[93m"
_RED = "\033[91m"

EXIT_OK = 0
EXIT_FAILED = 1

_TONE_COLORS = {
    "neutral": _DIM,
    "warm": _GREEN,
    "friendly": _GREEN,
    "close": _CYAN,
    "casual": _CYAN,
    "formal": _YELLOW,
}


def _print_logo(title: str = "Relationship"):
    print()
    print(f"  {_BOLD}{_CYAN}◆ FRIDAY{_RESET} {_DIM}V4 — {title}{_RESET}")
    print(f"  {_DIM}{'─' * 40}{_RESET}")


def _resolve_db(args) -> Optional[object]:
    try:
        from . import db
        return db.connect(path=getattr(args, "db", None))
    except Exception as exc:
        logger.debug(f"relationship: db unavailable ({exc})")
        return None


def _close_db(conn) -> None:
    try:
        if conn is not None:
            conn.close()
    except Exception:
        pass


def cmd_relationship_status(args: argparse.Namespace) -> int:
    """`friday6 relationship status` — the relationship view."""
    conn = _resolve_db(args)
    try:
        from .relationship import RelationshipEngine
        status = RelationshipEngine(conn).status()
    except Exception as exc:
        print(f"  {_RED}✗ could not read relationship: {exc}{_RESET}")
        return EXIT_FAILED
    finally:
        _close_db(conn)

    if args.json:
        print(json.dumps(status, default=str))
        return EXIT_OK

    _print_logo()
    depth = status["depth"]
    bar = "█" * int(round(depth * 10)) + "░" * (10 - int(round(depth * 10)))
    tone = status["tone"]
    tone_color = _TONE_COLORS.get(tone, _DIM)
    print(f"  {_BOLD}Depth{_RESET}    {_CYAN}{bar}{_RESET} {depth:.2f} "
          f"({status['level']})")
    print(f"  {_BOLD}Tone{_RESET}     {tone_color}{tone}{_RESET} "
          f"· verbosity {status['verbosity']} · "
          f"briefing: {status['briefing']}")
    direction = status.get("tone_direction") or {}
    if direction.get("tone") or direction.get("verbosity"):
        req = direction.get("request") or ""
        req_txt = f" — {req!r}" if req else ""
        print(f"  {_DIM}Direction{_RESET} explicit override ("
              f"tone {direction.get('tone') or '—'}, "
              f"verbosity {direction.get('verbosity') or '—'}){req_txt}")
        print(f"  {_DIM}         set {_RESET}{_DIM}"
              f"{(direction.get('set_at') or '')[:16]}{_RESET}")
    print()
    print(f"  {_DIM}Signals (real interaction data):{_RESET}")
    for key, label in (("exchanges", "conversations"),
                       ("sessions", "sessions"),
                       ("missions_completed", "missions completed"),
                       ("facts", "things you told me")):
        print(f"  {_DIM}  {label:<20}{_RESET} {status['signals'].get(key, 0)}")
    print()
    if depth < 0.15:
        print(f"  {_DIM}We're still getting to know each other — keep talking "
              f"and the relationship deepens.{_RESET}")
    print()
    return EXIT_OK


def cmd_relationship_refresh(args: argparse.Namespace) -> int:
    """`friday6 relationship refresh` — recompute + persist now."""
    conn = _resolve_db(args)
    try:
        from .relationship import RelationshipEngine
        status = RelationshipEngine(conn).refresh()
    except Exception as exc:
        print(f"  {_RED}✗ could not refresh relationship: {exc}{_RESET}")
        return EXIT_FAILED
    finally:
        _close_db(conn)

    if args.json:
        print(json.dumps(status, default=str))
        return EXIT_OK
    _print_logo()
    print(f"  {_GREEN}✓ Relationship refreshed — depth {status['depth']:.2f} "
          f"({status['level']}), tone {status['tone']}.{_RESET}")
    print()
    return EXIT_OK


def cmd_relationship_tone(args: argparse.Namespace) -> int:
    """`friday6 relationship tone [tone] [--verbosity N] [--reset]`.

    Wave 17 adaptive identity: set/clear the explicit tone-direction
    (the CLI is a debug hatch — "be more casual" through `friday6 talk`
    is the product path). Stored with the operator's words so Friday
    can explain why she talks the way she does.
    """
    conn = _resolve_db(args)
    try:
        from .relationship import RelationshipEngine, DIRECTION_TONES
        engine = RelationshipEngine(conn)
        if getattr(args, "reset", False):
            status = engine.clear_direction()
            changed = "cleared"
        else:
            tone = getattr(args, "tone", None)
            verbosity = getattr(args, "verbosity", None)
            if tone is None and verbosity is None:
                print(f"  {_YELLOW}⚠ give a tone to set "
                      f"({'|'.join(DIRECTION_TONES)}) or --reset.{_RESET}")
                return 3
            if tone is not None and tone not in DIRECTION_TONES:
                print(f"  {_RED}✗ unknown tone {tone!r} — "
                      f"use one of {'|'.join(DIRECTION_TONES)}.{_RESET}")
                return 3
            if verbosity is not None and not (1 <= verbosity <= 5):
                print(f"  {_RED}✗ verbosity must be 1..5.{_RESET}")
                return 3
            status = engine.set_direction(tone=tone, verbosity=verbosity,
                                          request="friday6 relationship tone")
            changed = "set"
    except Exception as exc:
        print(f"  {_RED}✗ could not update tone: {exc}{_RESET}")
        return EXIT_FAILED
    finally:
        _close_db(conn)

    if args.json:
        print(json.dumps(status, default=str))
        return EXIT_OK
    _print_logo()
    print(f"  {_GREEN}✓ Tone direction {changed} — effective tone "
          f"{status['tone']}, verbosity {status['verbosity']}.{_RESET}")
    print()
    return EXIT_OK


def _add_relationship_commands(subparsers) -> None:
    p = subparsers.add_parser("status", help="Show the relationship view")
    p.add_argument("--db", type=Path, default=None)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_relationship_status)

    p = subparsers.add_parser("refresh",
                              help="Recompute depth from real data now")
    p.add_argument("--db", type=Path, default=None)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_relationship_refresh)

    p = subparsers.add_parser("tone",
                              help="Set/clear the explicit tone direction")
    p.add_argument("tone", nargs="?", default=None,
                   help="Tone to adopt (casual/formal/warm/friendly/"
                        "close/neutral)")
    p.add_argument("--verbosity", type=int, default=None,
                   help="Verbosity 1..5 override")
    p.add_argument("--reset", action="store_true",
                   help="Clear the explicit direction (be yourself again)")
    p.add_argument("--db", type=Path, default=None)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_relationship_tone)


def build_relationship_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "relationship", help="How close Friday and you are",
        description="Relationship depth from real interaction data — tone, "
                    "verbosity, briefing length. Monotonic: more interaction "
                    "→ deeper, never suddenly shallower.",
    )
    rel_sub = parser.add_subparsers(dest="relationship_command")
    _add_relationship_commands(rel_sub)


def main(argv: list[str] | None = None) -> int:
    """Standalone entry point for `python -m friday_v6.cli_relationship`."""
    logging.basicConfig(level=logging.WARNING)
    parser = argparse.ArgumentParser(prog="friday6 relationship")
    sub = parser.add_subparsers(dest="command")
    _add_relationship_commands(sub)

    args = parser.parse_args(argv)
    if hasattr(args, "func"):
        return args.func(args) or 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
