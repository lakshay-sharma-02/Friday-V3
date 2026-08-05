"""CLI commands for `friday6 memory` — long-term facts + working context (Wave 10).

The text surface for the memory layer: store, recall, forget, list, and
inspect facts (propositions with provenance) plus the ephemeral working
memory context. Same conventions as `friday6 ask` / `friday6 talk`
(colors, exit codes, JSON purity).

Usage:
    friday6 memory store operator.prefers_rust True --source voice:2026-08-01
    friday6 memory recall operator.prefers_rust
    friday6 memory forget operator.name
    friday6 memory list
    friday6 memory status          # counts + working memory context
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger("friday_v6.cli_memory")

_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_CYAN = "\033[96m"
_GREEN = "\033[92m"
_RED = "\033[91m"

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 3


def _print_logo(title: str = "Memory"):
    print()
    print(f"  {_BOLD}{_CYAN}◆ FRIDAY{_RESET} {_DIM}V6 — {title}{_RESET}")
    print(f"  {_DIM}{'─' * 40}{_RESET}")


def _print_friday(text: str):
    print(f"\n{_CYAN}  Friday:{_RESET} {text}")


def _print_error(text: str):
    print(f"  {_RED}✗ {text}{_RESET}")


def _print_dim(text: str):
    print(f"  {_DIM}{text}{_RESET}")


def _resolve_db(args) -> Optional[object]:
    try:
        from . import db
        return db.connect(path=getattr(args, "db", None))
    except Exception as exc:
        logger.debug(f"memory: db unavailable ({exc})")
        return None


def _close_db(conn) -> None:
    try:
        if conn is not None:
            conn.close()
    except Exception:
        pass


def _split_key(key: str) -> tuple[str, str]:
    """'subject.predicate' → (subject, predicate); bare key → operator."""
    if "." in key:
        subject, _, predicate = key.partition(".")
        return subject or "operator", predicate
    return "operator", key


def _fmt_fact(fact, json_mode: bool = False) -> str:
    if json_mode:
        return json.dumps({
            "subject": fact.subject,
            "predicate": fact.predicate,
            "value": fact.value,
            "source": fact.source,
            "confidence": round(fact.confidence, 3),
            "decay_policy": fact.decay_policy,
            "created_at": fact.created_at,
            "updated_at": fact.updated_at,
        }, default=str)
    src = f" {_DIM}(from {fact.source}){_RESET}" if fact.source else ""
    return (f"  {_GREEN}●{_RESET} {fact.predicate}: {fact.value}{src} "
            f"{_DIM}[conf {fact.confidence:.2f} · {fact.decay_policy}]{_RESET}")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_memory_store(args: argparse.Namespace) -> int:
    """`friday6 memory store <key> <value>` — remember a fact."""
    subject, predicate = _split_key(args.key)
    conn = _resolve_db(args)
    try:
        from .memory import FactMemory
        mid = FactMemory(conn).remember(
            subject, predicate, args.value, source=args.source or "",
            confidence=args.confidence, decay_policy=args.policy)
    except Exception as exc:
        _print_error(f"could not store: {exc}")
        return EXIT_FAILED
    finally:
        _close_db(conn)

    if not mid:
        _print_error("store failed")
        return EXIT_FAILED
    if args.json:
        print(json.dumps({"stored": True, "key": f"{subject}.{predicate}",
                          "id": mid}))
        return EXIT_OK
    _print_logo()
    _print_friday(f"Noted — storing {subject}.{predicate}: "
                  f"{args.value} (conf {args.confidence}).")
    print()
    return EXIT_OK


def cmd_memory_recall(args: argparse.Namespace) -> int:
    """`friday6 memory recall <key>` — recall one fact with provenance."""
    subject, predicate = _split_key(args.key)
    conn = _resolve_db(args)
    try:
        from .memory import FactMemory
        fact = FactMemory(conn).recall_one(subject, predicate)
    except Exception as exc:
        _print_error(f"could not recall: {exc}")
        return EXIT_FAILED
    finally:
        _close_db(conn)

    if not fact:
        if args.json:
            print(json.dumps({"found": False}))
        else:
            _print_logo()
            _print_friday(f"I don't remember anything about "
                          f"{subject}.{predicate} yet.")
            print()
        return EXIT_FAILED
    if args.json:
        print(_fmt_fact(fact, json_mode=True))
        return EXIT_OK
    _print_logo()
    _print_friday(f"Here's what I remember about {subject}.{predicate}:")
    print(_fmt_fact(fact))
    print()
    return EXIT_OK


def cmd_memory_forget(args: argparse.Namespace) -> int:
    """`friday6 memory forget <key>` — delete a fact."""
    subject, predicate = _split_key(args.key)
    conn = _resolve_db(args)
    try:
        from .memory import FactMemory
        removed = FactMemory(conn).forget(subject, predicate)
    except Exception as exc:
        _print_error(f"could not forget: {exc}")
        return EXIT_FAILED
    finally:
        _close_db(conn)

    if not removed:
        if args.json:
            print(json.dumps({"forgotten": False}))
        else:
            _print_logo()
            _print_friday(f"I didn't find {subject}.{predicate} to forget.")
            print()
        return EXIT_FAILED
    if args.json:
        print(json.dumps({"forgotten": True, "key": f"{subject}.{predicate}"}))
        return EXIT_OK
    _print_logo()
    _print_friday(f"Forgotten — {subject}.{predicate}.")
    print()
    return EXIT_OK


def cmd_memory_list(args: argparse.Namespace) -> int:
    """`friday6 memory list` — all facts, optionally by subject."""
    conn = _resolve_db(args)
    try:
        from .memory import FactMemory
        facts = FactMemory(conn).recall(subject=args.subject,
                                        limit=args.limit)
    except Exception as exc:
        _print_error(f"could not list: {exc}")
        return EXIT_FAILED
    finally:
        _close_db(conn)

    if args.json:
        print(json.dumps([json.loads(_fmt_fact(f, json_mode=True))
                          for f in facts], default=str))
        return EXIT_OK
    _print_logo()
    if not facts:
        _print_friday("I don't remember anything yet. "
                      "Try 'friday6 memory store operator.name Lakshay'.")
        print()
        return EXIT_OK
    _print_friday(f"{len(facts)} fact(s) in memory:")
    for f in facts:
        print(_fmt_fact(f))
    print()
    return EXIT_OK


def cmd_memory_status(args: argparse.Namespace) -> int:
    """`friday6 memory status` — fact count + working memory context."""
    conn = _resolve_db(args)
    try:
        from .memory import FactMemory, WorkingMemory
        facts = FactMemory(conn)
        wm = WorkingMemory(conn)
        count = facts.count()
        working = wm.current_context()
    except Exception as exc:
        _print_error(f"could not read memory: {exc}")
        return EXIT_FAILED
    finally:
        _close_db(conn)

    if args.json:
        print(json.dumps({"fact_count": count, "working_memory": working},
                         default=str))
        return EXIT_OK
    _print_logo()
    _print_friday(f"{count} long-term fact(s) in memory.")
    if working:
        print()
        print(f"  {_DIM}Current working context:{_RESET}")
        for line in working.splitlines()[1:]:
            print(line)
    print()
    return EXIT_OK


# ---------------------------------------------------------------------------
# Argument parsers
# ---------------------------------------------------------------------------


def build_memory_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "memory", help="Long-term memory — facts with provenance",
        description="Friday's memory layer: store/recall/forget long-term "
                    "facts (propositions with provenance + decay) and "
                    "inspect the ephemeral working-memory context.",
    )
    memory_sub = parser.add_subparsers(dest="memory_command")

    p = memory_sub.add_parser("store", help="Remember a fact")
    p.add_argument("key", help="Subject.predicate key, e.g. "
                               "operator.prefers_rust")
    p.add_argument("value", help="The fact value")
    p.add_argument("--source", default="", help="Provenance, e.g. "
                                                "voice:2026-08-01")
    p.add_argument("--confidence", type=float, default=0.7,
                   help="Confidence 0.0–1.0 (default 0.7)")
    p.add_argument("--policy", default="usage",
                   choices=["none", "time", "usage"],
                   help="Decay policy (default usage)")
    p.add_argument("--db", type=Path, default=None)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_memory_store)

    p = memory_sub.add_parser("recall", help="Recall a fact with provenance")
    p.add_argument("key", help="Subject.predicate key")
    p.add_argument("--db", type=Path, default=None)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_memory_recall)

    p = memory_sub.add_parser("forget", help="Delete a fact")
    p.add_argument("key", help="Subject.predicate key")
    p.add_argument("--db", type=Path, default=None)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_memory_forget)

    p = memory_sub.add_parser("list", help="List facts")
    p.add_argument("--subject", default=None,
                   help="Only facts with this subject (e.g. operator)")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--db", type=Path, default=None)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_memory_list)

    p = memory_sub.add_parser("status", help="Fact count + working context")
    p.add_argument("--db", type=Path, default=None)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_memory_status)


def main(argv: list[str] | None = None) -> int:
    """Standalone entry point for `python -m friday_v6.cli_memory`."""
    logging.basicConfig(level=logging.WARNING)
    parser = argparse.ArgumentParser(prog="friday6 memory")
    build_memory_parser(parser)
    args = parser.parse_args(argv)
    if hasattr(args, "func"):
        return args.func(args) or 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
