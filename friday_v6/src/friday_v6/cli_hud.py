"""CLI commands for `friday6 hud` (Wave 3).

The HUD is another surface of the same Friday — ``friday6 hud`` opens
the Textual face (vitals, live ambient stream, schedule, notices,
permission buttons, and an input box routed through the SAME
``TextCommandHandler`` as voice/CLI/web/phone). Textual is optional:
without it the command degrades to a printed hint and returns 1
(never-crash law — the rest of the CLI stays fully usable).
"""

from __future__ import annotations

import argparse
import logging

logger = logging.getLogger("friday_v6.cli_hud")

_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_CYAN = "\033[96m"
_RED = "\033[91m"


def _print_logo():
    print()
    print(f"  {_BOLD}{_CYAN}◆ FRIDAY{_RESET} {_DIM}V6 — HUD{_RESET}")
    print(f"  {_DIM}{'─' * 40}{_RESET}")


def cmd_hud(args: argparse.Namespace) -> int:
    """`friday6 hud` — launch the Textual HUD (blocking)."""
    from .hud import run_hud
    # ``--db`` is a PATH — the handler/ambient/autonomy layers need a
    # real connection, not a string. Resolve it like every other CLI
    # (``cli_nl``), so the HUD reads/writes the SAME DB as the other
    # surfaces. None → the product default DB.
    conn = None
    db_path = getattr(args, "db", None)
    if db_path:
        try:
            from . import db
            conn = db.connect(str(db_path))
        except Exception as exc:
            logger.warning(f"hud: could not open db {db_path}: {exc}")
    _print_logo()
    try:
        rc = run_hud(
            conn=conn,
            vault_root=(str(getattr(args, "root", None))
                        if getattr(args, "root", None) else None),
            llm=_default_llm(),
        )
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    if rc != 0:
        print(f"  {_RED}✗ HUD unavailable.{_RESET}")
    return rc


def _default_llm():
    """The shared default LLM client (same as cli_nl)."""
    try:
        from .cli_nl import _default_llm
        return _default_llm()
    except Exception:
        return None


def build_hud_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "hud", help="Launch the Textual HUD — Friday's face",
        description="One Textual screen over the vault: vitals, live "
                    "ambient stream, schedule, notices, permission "
                    "buttons, and an input box routed through the same "
                    "brain as every other surface. Requires the "
                    "optional `textual` extra.",
    )
    parser.add_argument("--root", type=str, default=None,
                        help="Vault root (default ~/.friday/v6_vault)")
    parser.add_argument("--db", type=str, default=None,
                        help="SQLite DB path (default ~/.friday/v4.db)")
    parser.set_defaults(func=cmd_hud)


def main(argv: list[str] | None = None) -> int:
    """Standalone entry point for `python -m friday_v6.cli_hud`."""
    logging.basicConfig(level=logging.WARNING)
    import sys
    parser = argparse.ArgumentParser(prog="friday6 hud")
    build_hud_parser(parser)
    args = parser.parse_args(argv)
    if hasattr(args, "func"):
        return args.func(args) or 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
