"""CLI commands for `friday4 persona` — identity as a verbatim view (Wave 10).

The text surface for the persona layer. Per the operator's direction
there are **no keywords and no extraction**: the identity is a verbatim
view over the conversation log — what you actually told Friday, quoted
word-for-word with provenance.

Usage:
    friday4 persona profile              # what Friday knows about you
    friday4 persona remember "call me Lakshay"   # record verbatim
    friday4 persona profile --json
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger("friday_v4.cli_persona")

_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_CYAN = "\033[96m"
_GREEN = "\033[92m"
_RED = "\033[91m"

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 3


def _print_logo(title: str = "Persona"):
    print()
    print(f"  {_BOLD}{_CYAN}◆ FRIDAY{_RESET} {_DIM}V4 — {title}{_RESET}")
    print(f"  {_DIM}{'─' * 40}{_RESET}")


def _print_friday(text: str):
    print(f"\n{_CYAN}  Friday:{_RESET} {text}")


def _print_error(text: str):
    print(f"  {_RED}✗ {text}{_RESET}")


def _resolve_db(args) -> Optional[object]:
    try:
        from . import db
        return db.connect(path=getattr(args, "db", None))
    except Exception as exc:
        logger.debug(f"persona: db unavailable ({exc})")
        return None


def _close_db(conn) -> None:
    try:
        if conn is not None:
            conn.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_persona_profile(args: argparse.Namespace) -> int:
    """`friday4 persona profile` — what Friday knows about the operator."""
    conn = _resolve_db(args)
    try:
        from .persona import IdentityEngine
        profile = IdentityEngine(conn).profile()
    except Exception as exc:
        _print_error(f"could not read profile: {exc}")
        return EXIT_FAILED
    finally:
        _close_db(conn)

    statements = profile.get("statements") or []
    if args.json:
        print(json.dumps(profile, default=str))
        return EXIT_OK

    _print_logo()
    if not statements:
        _print_friday("I don't know who you are yet — you haven't told me "
                      "anything. Say something like \"call me Lakshay\" and "
                      "I'll remember it verbatim.")
        print()
        return EXIT_OK
    _print_friday(f"I remember {len(statements)} thing(s) you told me:")
    print()
    for s in statements:
        when = (s.get("when") or "")[:16]
        suffix = f" {_DIM}({when}){_RESET}" if when else ""
        print(f"  {_GREEN}●{_RESET} \"{s.get('content', '')}\"{suffix}")
    print()
    return EXIT_OK


def cmd_persona_remember(args: argparse.Namespace) -> int:
    """`friday4 persona remember <text>` — record a statement verbatim.

    No parsing: the exact words are stored in the conversation log (the
    same source the reasoning conversation provider and identity answers
    read). This is explicit, operator-initiated memory — never extracted
    from speech automatically.
    """
    text = (args.text or "").strip()
    if not text:
        _print_error("nothing to remember — give me the words you want "
                     "me to keep, e.g. 'remember \"call me Lakshay\"'.")
        return EXIT_USAGE
    conn = _resolve_db(args)
    try:
        from .persona import IdentityEngine
        ack = IdentityEngine(conn).remember(text, surface="cli")
    except Exception as exc:
        _print_error(f"could not remember: {exc}")
        return EXIT_FAILED
    finally:
        _close_db(conn)

    if args.json:
        print(json.dumps({"remembered": bool(ack), "text": text},
                         default=str))
        return EXIT_OK if ack else EXIT_FAILED
    if ack:
        _print_logo()
        _print_friday(ack)
        print()
        return EXIT_OK
    _print_error("could not record that — is the database available?")
    return EXIT_FAILED


# ---------------------------------------------------------------------------
# Argument parsers
# ---------------------------------------------------------------------------


def _add_persona_commands(subparsers) -> None:
    """The `profile` / `remember` commands, shared by the integrated CLI
    (`friday4 persona <cmd>`) and the standalone entry point."""
    p = subparsers.add_parser("profile", help="Show what Friday knows")
    p.add_argument("--db", type=Path, default=None)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_persona_profile)

    p = subparsers.add_parser("remember", help="Record a statement verbatim")
    p.add_argument("text", help="The exact words to remember, e.g. "
                                "'call me Lakshay'")
    p.add_argument("--db", type=Path, default=None)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_persona_remember)


def build_persona_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "persona", help="Identity — what Friday knows about you",
        description="Friday's identity layer: a verbatim view over the "
                    "conversation log — what you told Friday, quoted "
                    "word-for-word. No keyword extraction, ever.",
    )
    persona_sub = parser.add_subparsers(dest="persona_command")
    _add_persona_commands(persona_sub)


def main(argv: list[str] | None = None) -> int:
    """Standalone entry point for `python -m friday_v4.cli_persona`."""
    logging.basicConfig(level=logging.WARNING)
    parser = argparse.ArgumentParser(prog="friday4 persona")
    sub = parser.add_subparsers(dest="command")
    _add_persona_commands(sub)

    args = parser.parse_args(argv)
    if hasattr(args, "func"):
        return args.func(args) or 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
