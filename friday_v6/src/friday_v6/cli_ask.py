"""CLI command for `friday6 ask` — evidence-cited answers (Wave 9).

The text surface for the reasoning layer: ask a question in plain
language and get an answer backed by real V4 state — never a guess.
Same conventions as `friday6 execute` / `friday6 talk` (colors, exit
codes, JSON purity).

Usage:
    friday6 ask "what's the status of my projects?"
    friday6 ask "what did I do recently?" --json
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger("friday_v6.cli_ask")

_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_CYAN = "\033[96m"
_RED = "\033[91m"

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 3


def _print_logo():
    print()
    print(f"  {_BOLD}{_CYAN}◆ FRIDAY{_RESET} {_DIM}V4 — Ask{_RESET}")
    print(f"  {_DIM}{'─' * 40}{_RESET}")


def _print_friday(text: str):
    print(f"\n{_CYAN}  Friday:{_RESET} {text}")


def _print_dim(text: str):
    print(f"  {_DIM}{text}{_RESET}")


def _print_error(text: str):
    print(f"  {_RED}✗ {text}{_RESET}")


def _resolve_db(args) -> Optional[object]:
    try:
        from . import db
        return db.connect(path=getattr(args, "db", None))
    except Exception as exc:
        logger.debug(f"ask: db unavailable ({exc})")
        return None


def _close_db(conn) -> None:
    try:
        if conn is not None:
            conn.close()
    except Exception:
        pass


def _recent_history(conn) -> list[dict]:
    """Recent exchanges (oldest first) for follow-up context (never raises).

    ``friday6 ask`` is conversation-capable (Wave 13): a follow-up like
    "and the tests?" is resolved against what was asked earlier. The
    history feeds the LLM synthesis prompt via ``reasoning.answer``.
    """
    if conn is None:
        return []
    from . import db
    return db.recent_exchange_history(conn)


def _log_exchange(conn, question: str, answer_text: str) -> None:
    """Record the Q&A in the conversation log (never raises).

    Wave 15 — one presence: the Q&A joins the shared session (the same
    thread talk/voice/web append to), so asks are part of the one
    conversation and time-window recall sees them.
    """
    if conn is None:
        return
    try:
        from . import db
        sid = db.get_or_create_shared_session(conn)
        if not sid:
            return
        db.log_exchange(conn, sid, "user", question, intent="ask")
        db.log_exchange(conn, sid, "friday", answer_text, intent="ask")
    except Exception:
        pass


def cmd_ask(args: argparse.Namespace) -> int:
    """`friday6 ask <question...>` — answer with cited evidence."""
    if not args.question:
        _print_error("ask needs a question, e.g. friday6 ask \"what's the "
                     "status of my projects?\"")
        return EXIT_USAGE

    conn = _resolve_db(args)
    try:
        from .reasoning import answer
        question = " ".join(args.question)
        ans = answer(question, conn=conn, history=_recent_history(conn))
        _log_exchange(conn, question, ans.text)
    except Exception as exc:
        _print_error(f"could not answer: {exc}")
        return EXIT_FAILED
    finally:
        _close_db(conn)

    if args.json:
        print(json.dumps(ans.to_dict(), indent=2, default=str))
        return EXIT_OK if ans.known else EXIT_FAILED

    _print_logo()
    _print_friday(ans.text)
    for citation in ans.citations:
        _print_dim(f"  ↳ {citation}")
    if not ans.known:
        _print_dim("  (no evidence yet — I never fabricate answers)")
    print()
    return EXIT_OK if ans.known else EXIT_FAILED


def build_ask_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "ask", help="Ask Friday a question — answers cite evidence",
        description="Ask a question in plain language. Friday answers "
                    "from real state (missions, actions, memories) and "
                    "cites the evidence — or honestly says it doesn't "
                    "know yet.",
    )
    parser.add_argument("question", nargs="+",
                        help="The question, e.g. \"what's the status of "
                             "my projects?\"")
    parser.add_argument("--db", type=Path, default=None,
                        help="V4 state DB path (default: ~/.friday/v4.db)")
    parser.add_argument("--json", action="store_true",
                        help="Machine-readable JSON output")
    parser.set_defaults(func=cmd_ask)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING)
    parser = argparse.ArgumentParser(prog="friday6 ask")
    build_ask_parser(parser)
    args = parser.parse_args(argv)
    if hasattr(args, "func"):
        return args.func(args) or 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
