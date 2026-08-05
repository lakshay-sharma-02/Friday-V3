"""CLI commands for `friday6 capability` — what Friday can do (Wave 16, Law 7).

The debug-hatch surface for the capability registry (the product path
is `friday6 talk "what can you do"`). Lists, describes, and counts the
registered capabilities — builtins (executors, providers, intents,
surfaces) plus learned skills (self-extension).

Usage:
    friday6 capability list                 # all registered capabilities
    friday6 capability list --json
    friday6 capability describe executor:shell
    friday6 capability count
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger("friday_v6.cli_capability")

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


def _print_logo(title: str = "Capabilities"):
    print()
    print(f"  {_BOLD}{_CYAN}◆ FRIDAY{_RESET} {_DIM}V4 — {title}{_RESET}")
    print(f"  {_DIM}{'─' * 40}{_RESET}")


def _resolve_db(args) -> Optional[object]:
    try:
        from . import db
        return db.connect(path=getattr(args, "db", None), read_only=True)
    except Exception as exc:
        logger.debug(f"capability: db unavailable ({exc})")
        return None


def _close_db(conn) -> None:
    try:
        if conn is not None:
            conn.close()
    except Exception:
        pass


def cmd_capability_list(args: argparse.Namespace) -> int:
    """`friday6 capability list` — everything Friday can do."""
    conn = _resolve_db(args)
    try:
        from .capability import CapabilityRegistry
        caps = CapabilityRegistry(conn).list()
    except Exception as exc:
        print(f"  {_RED}✗ could not read capabilities: {exc}{_RESET}")
        return EXIT_FAILED
    finally:
        _close_db(conn)

    if args.json:
        print(json.dumps([c.to_dict() for c in caps], default=str))
        return EXIT_OK

    _print_logo()
    if not caps:
        print(f"  {_DIM}No capabilities registered yet.{_RESET}")
        print()
        return EXIT_OK
    by_layer: dict[str, list] = {}
    for c in caps:
        by_layer.setdefault(c.layer, []).append(c)
    total = len(caps)
    print(f"  {_BOLD}{total}{_RESET}{_DIM} registered capabilities{_RESET}\n")
    for layer in ("executor", "provider", "intent", "surface", "skill"):
        group = by_layer.get(layer)
        if not group:
            continue
        print(f"  {_BOLD}{layer}{_RESET} {_DIM}({len(group)}){_RESET}")
        for c in sorted(group, key=lambda x: x.id):
            perm = c.permission_level
            flag = "" if perm == "auto" else f" {_YELLOW}[{perm}]{_RESET}"
            print(f"  {_GREEN}●{_RESET} {_BOLD}{c.name}{_RESET} — "
                  f"{c.description}{flag}")
        print()
    return EXIT_OK


def cmd_capability_describe(args: argparse.Namespace) -> int:
    """`friday6 capability describe <id>` — one capability in detail."""
    cap_id = (args.capability_id or "").strip()
    if not cap_id:
        print(f"  {_RED}✗ give a capability id (e.g. executor:shell).{_RESET}")
        return EXIT_USAGE
    conn = _resolve_db(args)
    try:
        from .capability import CapabilityRegistry
        description = CapabilityRegistry(conn).describe(cap_id)
    except Exception as exc:
        print(f"  {_RED}✗ could not describe capability: {exc}{_RESET}")
        return EXIT_FAILED
    finally:
        _close_db(conn)

    if args.json:
        print(json.dumps({"id": cap_id, "description": description},
                         default=str))
        return EXIT_OK
    _print_logo()
    if not description:
        print(f"  {_RED}✗ no capability with id {cap_id!r}.{_RESET}\n"
              f"  {_DIM}  Run 'friday6 capability list' to see them.{_RESET}")
        return EXIT_USAGE
    print(f"  {_BOLD}{cap_id}{_RESET}\n  {description}")
    print()
    return EXIT_OK


def cmd_capability_count(args: argparse.Namespace) -> int:
    """`friday6 capability count` — how many things Friday can do."""
    conn = _resolve_db(args)
    try:
        from .capability import CapabilityRegistry
        summary = CapabilityRegistry(conn).summary()
    except Exception as exc:
        print(f"  {_RED}✗ could not count capabilities: {exc}{_RESET}")
        return EXIT_FAILED
    finally:
        _close_db(conn)

    if args.json:
        print(json.dumps(summary, default=str))
        return EXIT_OK
    _print_logo()
    print(f"  {_BOLD}{summary.get('total', 0)}{_RESET}{_DIM} registered "
          f"capabilities{_RESET}")
    for layer, count in sorted((summary.get("by_layer") or {}).items()):
        print(f"  {_DIM}  {layer:<10}{_RESET} {count}")
    print()
    return EXIT_OK


def _add_capability_commands(subparsers) -> None:
    p = subparsers.add_parser("list", help="List registered capabilities")
    p.add_argument("--db", type=Path, default=None)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_capability_list)

    p = subparsers.add_parser("describe", help="Describe one capability")
    p.add_argument("capability_id", help="Capability id (e.g. executor:shell)")
    p.add_argument("--db", type=Path, default=None)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_capability_describe)

    p = subparsers.add_parser("count", help="Count registered capabilities")
    p.add_argument("--db", type=Path, default=None)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_capability_count)


def build_capability_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "capability", help="What Friday can do (the capability registry)",
        description="Friday's capability registry — every executor, "
                    "provider, intent, surface, and learned skill, "
                    "registered and discoverable. The product path is "
                    "just asking: 'what can you do'.",
    )
    cap_sub = parser.add_subparsers(dest="capability_command")
    _add_capability_commands(cap_sub)


def main(argv: list[str] | None = None) -> int:
    """Standalone entry point for `python -m friday_v6.cli_capability`."""
    logging.basicConfig(level=logging.WARNING)
    parser = argparse.ArgumentParser(prog="friday6 capability")
    sub = parser.add_subparsers(dest="command")
    _add_capability_commands(sub)

    args = parser.parse_args(argv)
    if hasattr(args, "func"):
        return args.func(args) or 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
