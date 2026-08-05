"""CLI commands for `friday6 vault` and `friday6 index` (Wave 0).

The text surface for the vault — Friday's linked-markdown memory:

- ``friday6 vault ls``              — wiki notes, newest first
- ``friday6 vault find TERMS...``   — search (FTS index, grep fallback)
- ``friday6 vault note NAME [TEXT]`` — write a wiki note (``-`` = stdin)
- ``friday6 index rebuild``         — rebuild the FTS cache
- ``friday6 index status``          — index state

Same conventions as `friday6 memory` / `friday6 ask` (colors, exit
codes, JSON purity). Everything accepts ``--root`` to point at a
specific vault (hermetic tests use tmp dirs; the product default is
``~/.friday/v6_vault``).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger("friday_v6.cli_vault")

_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_CYAN = "\033[96m"
_GREEN = "\033[92m"
_RED = "\033[91m"

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 3


def _print_logo(title: str = "Vault"):
    print()
    print(f"  {_BOLD}{_CYAN}◆ FRIDAY{_RESET} {_DIM}V6 — {title}{_RESET}")
    print(f"  {_DIM}{'─' * 40}{_RESET}")


def _print_friday(text: str):
    print(f"\n{_CYAN}  Friday:{_RESET} {text}")


def _print_error(text: str):
    print(f"  {_RED}✗ {text}{_RESET}")


def _resolve_vault(args) -> object:
    """A Vault rooted at ``--root`` (or the product default)."""
    from .vault import Vault
    return Vault(root=getattr(args, "root", None))


def _resolve_index(args) -> object:
    """A VaultIndex rooted at ``--root`` (or the product default)."""
    from .vault import VaultIndex
    return VaultIndex(root=getattr(args, "root", None))


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_vault_ls(args: argparse.Namespace) -> int:
    """`friday6 vault ls` — wiki notes, newest first."""
    vault = _resolve_vault(args)
    try:
        notes = vault.list_wiki()
    except Exception as exc:
        _print_error(f"could not list the vault: {exc}")
        return EXIT_FAILED
    if args.json:
        print(json.dumps([
            {"name": p.stem, "path": str(p)} for p in notes
        ]))
        return EXIT_OK
    _print_logo("Vault")
    if not notes:
        _print_friday("The vault is empty — write a note with "
                      "'friday6 vault note <name> <text>'.")
        print()
        return EXIT_OK
    _print_friday(f"{len(notes)} wiki note(s), newest first:")
    for p in notes:
        print(f"  {_GREEN}●{_RESET} {p.stem} {_DIM}({p.name}){_RESET}")
    print()
    return EXIT_OK


def cmd_vault_find(args: argparse.Namespace) -> int:
    """`friday6 vault find TERMS...` — index-first, grep fallback.

    Wave 0 exit criterion: answers from the FTS index when it exists,
    and from grep when the index was deleted (cache, not truth). One
    search code path: ``Vault.search_with_source``."""
    terms = " ".join(args.terms)
    vault = _resolve_vault(args)
    try:
        lines, source = vault.search_with_source(terms, args.limit)
    except Exception as exc:
        _print_error(f"could not search the vault: {exc}")
        return EXIT_FAILED
    used_index = source == "index"
    if not lines:
        if args.json:
            print(json.dumps({"terms": terms, "hits": []}))
        else:
            _print_logo("Vault find")
            _print_friday(f"Nothing in the vault matches '{terms}'.")
            print()
        return EXIT_FAILED
    if args.json:
        print(json.dumps({
            "terms": terms,
            "indexed": used_index,
            "hits": lines,
        }))
        return EXIT_OK
    _print_logo("Vault find")
    source = "index" if used_index else "grep"
    _print_friday(f"{len(lines)} match(es) for '{terms}' "
                  f"({_DIM}{source}{_RESET}):")
    for line in lines:
        print(f"  {_GREEN}●{_RESET} {line}")
    if not used_index:
        print()
        print(f"  {_DIM}(grep — run 'friday6 index rebuild' for "
              f"faster search){_RESET}")
    print()
    return EXIT_OK


def cmd_vault_note(args: argparse.Namespace) -> int:
    """`friday6 vault note NAME [TEXT]` — write a wiki note.

    TEXT may be ``-`` (or omitted on a piped stdin) to read from
    standard input, so notes can come from any pipeline."""
    vault = _resolve_vault(args)
    content = args.text
    if content in (None, "-"):
        # Omitted text on an interactive terminal would block on EOF —
        # fail fast with usage instead of hanging the session.
        if content is None and sys.stdin.isatty():
            _print_error("no text given — pass text or pipe stdin "
                         "('friday6 vault note NAME -')")
            return EXIT_USAGE
        try:
            content = sys.stdin.read()
        except Exception as exc:
            _print_error(f"could not read note from stdin: {exc}")
            return EXIT_FAILED
    content = (content or "").strip()
    if not content:
        _print_error("nothing to write — pass text or pipe stdin "
                     "('friday6 vault note NAME -')")
        return EXIT_USAGE
    try:
        path = vault.note(args.name, content)
    except Exception as exc:
        _print_error(f"could not write the note: {exc}")
        return EXIT_FAILED
    if args.json:
        print(json.dumps({"written": True, "name": args.name,
                          "path": str(path)}))
        return EXIT_OK
    _print_logo("Vault note")
    _print_friday(f"Noted — {args.name} ({path.name}). "
                  f"Say 'friday6 vault find {args.name}' to recall it.")
    print()
    return EXIT_OK


def cmd_index_rebuild(args: argparse.Namespace) -> int:
    """`friday6 index rebuild` — full FTS reindex of the vault."""
    index = _resolve_index(args)
    try:
        count = index.rebuild()
    except Exception as exc:
        _print_error(f"could not rebuild the index: {exc}")
        return EXIT_FAILED
    if args.json:
        print(json.dumps({"rebuilt": True, "docs": count,
                          "fts5": index.fts_available()}))
        return EXIT_OK
    _print_logo("Index rebuild")
    if count == 0 and not index.fts_available():
        _print_friday("FTS5 isn't available on this Python build — "
                      "search falls back to grep.")
    else:
        _print_friday(f"Index rebuilt — {count} document(s) indexed.")
    print()
    return EXIT_OK


def cmd_index_status(args: argparse.Namespace) -> int:
    """`friday6 index status` — index state (cache, not truth)."""
    index = _resolve_index(args)
    try:
        status = index.status()
    except Exception as exc:
        _print_error(f"could not read index status: {exc}")
        return EXIT_FAILED
    if args.json:
        print(json.dumps(status))
        return EXIT_OK
    _print_logo("Index status")
    state = "ready" if status["exists"] else "missing (grep fallback)"
    _print_friday(f"FTS5: {status['fts5']} · index: {state} · "
                  f"{status['docs']} document(s).")
    print(f"  {_DIM}db: {status['db']}{_RESET}")
    print()
    return EXIT_OK


# ---------------------------------------------------------------------------
# Argument parsers
# ---------------------------------------------------------------------------


def _add_root_json(parser) -> None:
    parser.add_argument("--root", type=Path, default=None,
                        help="Vault root (default ~/.friday/v6_vault)")
    parser.add_argument("--json", action="store_true",
                        help="Machine-readable output")


def build_vault_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "vault", help="Vault — linked-markdown memory",
        description="Friday's vault: wiki notes, grep/full-text search, "
                    "append-only raw log. Memory as plain files.",
    )
    vault_sub = parser.add_subparsers(dest="vault_command")

    p = vault_sub.add_parser("ls", help="List wiki notes, newest first")
    _add_root_json(p)
    p.set_defaults(func=cmd_vault_ls)

    p = vault_sub.add_parser("find", help="Search the vault (index, grep fallback)")
    p.add_argument("terms", nargs="+", help="Search terms")
    p.add_argument("--limit", type=int, default=20)
    _add_root_json(p)
    p.set_defaults(func=cmd_vault_find)

    p = vault_sub.add_parser("note", help="Write a wiki note")
    p.add_argument("name", help="Note name (becomes <name>.md)")
    p.add_argument("text", nargs="?", default=None,
                   help="Note content, or '-' to read stdin")
    _add_root_json(p)
    p.set_defaults(func=cmd_vault_note)


def build_index_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "index", help="Vault FTS index — a rebuildable cache",
        description="The vault's full-text index. A cache, never the "
                    "truth: grep always works without it.",
    )
    index_sub = parser.add_subparsers(dest="index_command")

    p = index_sub.add_parser("rebuild", help="Rebuild the index (full reindex)")
    _add_root_json(p)
    p.set_defaults(func=cmd_index_rebuild)

    p = index_sub.add_parser("status", help="Index state")
    _add_root_json(p)
    p.set_defaults(func=cmd_index_status)


def main(argv: list[str] | None = None) -> int:
    """Standalone entry point for `python -m friday_v6.cli_vault`."""
    logging.basicConfig(level=logging.WARNING)
    parser = argparse.ArgumentParser(prog="friday6 vault")
    build_vault_parser(parser)
    build_index_parser(parser)
    args = parser.parse_args(argv)
    if hasattr(args, "func"):
        return args.func(args) or 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
