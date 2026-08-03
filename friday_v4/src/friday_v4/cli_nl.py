"""CLI command for `friday4 talk "..."` — natural language, Friday acts.

The text surface for the Wave 13a ONE NLU point: you say it like a
person, Friday understands it via ``nlu.resolve()`` (LLM-first,
rules fallback) and *does* it through the execution layer (gate →
sandbox → audit) or the missions layer.

Usage:
    friday4 talk "run the tests"                 # one-shot: say it, it runs
    friday4 talk                                 # interactive REPL
    friday4 talk "ship the auth refactor" --force
    friday4 talk --json "git status"             # machine-readable
    friday4 talk --manual "m_abcd" "done"        # mark a manual mission step

Exit codes (same contract as `friday4 execute`):
    0 success / 1 failed / 2 denied / 3 usage
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Optional

from .nl_router import TextCommandHandler, TalkResult

logger = logging.getLogger("friday_v4.cli_nl")

_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_GREEN = "\033[92m"
_YELLOW = "\033[93m"
_CYAN = "\033[96m"
_RED = "\033[91m"

EXIT_OK = 0
EXIT_DENIED = 2
EXIT_FAILED = 1
EXIT_USAGE = 3


def _print_logo(title: str = "Talk"):
    print()
    print(f"  {_BOLD}{_CYAN}◆ FRIDAY{_RESET} {_DIM}V4 — {title}{_RESET}")
    print(f"  {_DIM}{'─' * 40}{_RESET}")


def _print_friday(text: str):
    print(f"\n{_CYAN}  Friday:{_RESET} {text}")


def _print_error(text: str):
    print(f"  {_RED}✗ {text}{_RESET}")


def _print_dim(text: str):
    print(f"  {_DIM}{text}{_RESET}")


def _default_llm():
    """The ONE NLU point's LLM client, or None (deterministic fallback)."""
    try:
        from .nlu import LLMClient
        return LLMClient()
    except Exception:
        return None


def _confirm_prompt(description: str) -> bool:
    """y/N confirmation for CONFIRM actions (EOF/ctrl-C → safe deny)."""
    try:
        print(f"\n  {_YELLOW}→ Friday wants to:{_RESET} {description}")
        answer = input(f"  {_BOLD}Proceed? [y/N] {_RESET}").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer in ("y", "yes")


def _resolve_db(args) -> Optional[object]:
    """Open the V4 state DB (never raises — None degrades gracefully)."""
    try:
        from . import db
        return db.connect(path=getattr(args, "db", None))
    except Exception as exc:
        logger.debug(f"talk: db unavailable ({exc})")
        return None


def _close_db(conn) -> None:
    try:
        if conn is not None:
            conn.close()
    except Exception:
        pass


def _exit_code(result: TalkResult) -> int:
    if result.status == "denied" or result.action == "denied":
        return EXIT_DENIED
    if result.action in ("failed", "executed") and result.status == "failed":
        return EXIT_FAILED
    return EXIT_OK


def _show_result(result: TalkResult, json_mode: bool) -> int:
    if json_mode:
        print(json.dumps(result.to_dict(), indent=2, default=str))
        return _exit_code(result)
    _print_friday(result.response)
    if result.action_id:
        _print_dim(f"  audit id: {result.action_id}")
    if result.mission_id:
        _print_dim(f"  mission id: {result.mission_id}")
    print()
    return _exit_code(result)


def cmd_talk(args: argparse.Namespace) -> int:
    """`friday4 talk [text...]` — one-shot, or interactive when no text."""
    if args.manual:
        return _cmd_manual(args)
    if not args.text:
        return _cmd_repl(args)

    conn = _resolve_db(args)
    try:
        from .desktop.wm_abstraction import desktop_text_command
        handler = TextCommandHandler(
            conn, cwd=str(args.cwd) if args.cwd else None,
            llm=_default_llm(),
            desktop_handler=desktop_text_command)
        force = bool(args.force or args.yes)
        # JSON mode never prompts (would corrupt the document) — a
        # CONFIRM action without an override fails closed.
        confirm_fn = None if (force or args.json) else _confirm_prompt
        result = handler.handle(
            " ".join(args.text), confirm_fn=confirm_fn, force=force)
    finally:
        _close_db(conn)
    return _show_result(result, args.json)


def _cmd_manual(args: argparse.Namespace) -> int:
    """`friday4 talk --manual <mission_id> <result...>`."""
    conn = _resolve_db(args)
    try:
        handler = TextCommandHandler(conn)
        result = handler.handle_manual(args.manual,
                                       result=args.text and " ".join(args.text))
    finally:
        _close_db(conn)
    return _show_result(result, args.json)


def _cmd_repl(args: argparse.Namespace) -> int:
    """Interactive loop — the conversational surface."""
    _print_logo()
    conn = _resolve_db(args)
    from .desktop.wm_abstraction import desktop_text_command
    handler = TextCommandHandler(conn,
                                 cwd=str(args.cwd) if args.cwd else None,
                                 llm=_default_llm(),
                                 desktop_handler=desktop_text_command)
    print(f"  {_DIM}Say it like a person — 'run the tests', 'git status', "
          f"'ship the auth refactor', 'focus code editor', 'scan my repo'.{_RESET}")
    print(f"  {_DIM}Type 'exit' to quit, 'help' for commands.{_RESET}\n")
    code = EXIT_OK
    try:
        while True:
            try:
                utterance = input(f"{_GREEN}  You:{_RESET} ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not utterance:
                continue
            if utterance.lower() in ("exit", "quit", "stop"):
                break
            if utterance.lower() in ("help", "?"):
                _print_dim("  Try: run the tests · git status · read main.py "
                           "· lint · ship the auth refactor · what can you do")
                continue
            result = handler.handle(utterance, confirm_fn=_confirm_prompt)
            code = _show_result(result, json_mode=False)
    finally:
        _close_db(conn)
    print()
    return code


def cmd_research(args: argparse.Namespace) -> int:
    """`friday4 research analyze/correlate/briefing/narrative/report`.

    Law 1: the CLI is a debug hatch — the *product* path is
    ``friday4 talk "analyze vivaha vs MindWell"`` (the NL router routes
    RESEARCH intents here). This subcommand exists so operators can
    drive the Wave 11 surfaces directly.
    """
    from .cli_research import main as research_main
    return research_main([
        args.research_command, *(args.research_args or []),
    ])


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def build_talk_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "talk", help="Say it in natural language — Friday does it",
        description="Understands your words (via the Wave 9 NLU layer) and "
                    "acts: runs tests, checks git, edits files, starts "
                    "missions. With no text, opens an interactive session.",
    )
    parser.add_argument("text", nargs="*",
                        help="What you want done, in plain language")
    parser.add_argument("--manual", metavar="MISSION_ID", default=None,
                        help="Mark the mission's manual step done: "
                             "friday4 talk --manual m_abcd \"done\"")
    parser.add_argument("--db", type=Path, default=None,
                        help="V4 state DB path (default: ~/.friday/v4.db)")
    parser.add_argument("--cwd", type=Path, default=None,
                        help="Working directory for executed actions "
                             "(default: current directory)")
    parser.add_argument("--force", action="store_true",
                        help="Explicit operator override (bypasses confirm)")
    parser.add_argument("--yes", "-y", action="store_true",
                        help="Alias for --force (non-interactive confirm)")
    parser.add_argument("--json", action="store_true",
                        help="Machine-readable JSON (never prompts — "
                             "CONFIRM actions fail closed unless --force)")
    parser.set_defaults(func=cmd_talk)

    research_parser = subparsers.add_parser(
        "research", help="Wave 11 research surfaces (analyze/correlate/"
                         "briefing/narrative/report)",
        description="Direct access to the Wave 11 research & reflection "
                    "surfaces. The NL path is `friday4 talk \"analyze X\"` "
                    "— this is the debug hatch.",
    )
    research_parser.add_argument("research_command",
                                 choices=["analyze", "correlate",
                                          "briefing", "narrative", "report"],
                                 help="Which research surface")
    research_parser.add_argument("research_args", nargs=argparse.REMAINDER,
                                 help="Arguments for that surface")
    research_parser.set_defaults(func=cmd_research)


def main(argv: list[str] | None = None) -> int:
    """Standalone entry point for `python -m friday_v4.cli_nl`."""
    logging.basicConfig(level=logging.WARNING)
    parser = argparse.ArgumentParser(prog="friday4 talk")
    build_talk_parser(parser)
    args = parser.parse_args(argv)
    if hasattr(args, "func"):
        return args.func(args) or 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
