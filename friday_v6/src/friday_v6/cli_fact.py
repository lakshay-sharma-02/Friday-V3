"""CLI commands for `friday6 fact` — the MemoryFact bridge (Wave 1).

Every fact lives on BOTH sides through one write path:
SQLite rows (structured truth — queries, decay, persona) and a vault
wiki note carrying the ``sources:`` evidence frontmatter (prose a
human reads). This CLI drives that bridge:

- ``friday6 fact store <key> <value>``   — remember (both sides)
- ``friday6 fact recall <key>``          — recall + show the note
- ``friday6 fact list [--subject S]``    — list facts (+ note paths)
- ``friday6 fact forget <key>``          — forget (both sides)

Same conventions as `friday6 memory` (colors, exit codes, JSON
purity). ``--root`` points at the vault, ``--db`` at the SQLite file.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger("friday_v6.cli_fact")

_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_CYAN = "\033[96m"
_GREEN = "\033[92m"
_RED = "\033[91m"

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 3


def _print_logo(title: str = "Fact memory"):
    print()
    print(f"  {_BOLD}{_CYAN}◆ FRIDAY{_RESET} {_DIM}V6 — {title}{_RESET}")
    print(f"  {_DIM}{'─' * 40}{_RESET}")


def _print_friday(text: str):
    print(f"\n{_CYAN}  Friday:{_RESET} {text}")


def _print_error(text: str):
    print(f"  {_RED}✗ {text}{_RESET}")


def _split_key(key: str) -> tuple[str, str]:
    """'subject.predicate' → (subject, predicate); bare key → operator."""
    if "." in key:
        subject, _, predicate = key.partition(".")
        return subject or "operator", predicate
    return "operator", key


def _resolve_bridge(args) -> object:
    """A MemoryFact over --db (SQLite) and --root (vault).

    Same convention as `friday6 memory`: ``db.connect(path=args.db)``
    with None → the default ~/.friday DB, so the structured side is
    ALWAYS on unless the connection genuinely fails (never-crash)."""
    from .vault import MemoryFact, Vault
    conn = None
    try:
        from . import db
        conn = db.connect(path=getattr(args, "db", None))
    except Exception as exc:
        logger.debug(f"fact: db unavailable ({exc})")
        conn = None
    return MemoryFact(conn=conn, vault=Vault(root=getattr(args, "root", None)))


def _close_bridge(bridge) -> None:
    try:
        if getattr(bridge, "_conn", None) is not None:
            bridge._conn.close()
    except Exception:
        pass


def _fmt_fact(fact, note: Optional[str] = None, json_mode: bool = False):
    if json_mode:
        return {
            "subject": fact.subject,
            "predicate": fact.predicate,
            "value": fact.value,
            "source": fact.source,
            "confidence": round(fact.confidence, 3),
            "decay_policy": fact.decay_policy,
            "created_at": fact.created_at,
            "updated_at": fact.updated_at,
            "note": note,
        }
    src = f" {_DIM}(from {fact.source}){_RESET}" if fact.source else ""
    note_bit = f" {_DIM}[note: {note}]{_RESET}" if note else ""
    return (f"  {_GREEN}●{_RESET} {fact.predicate}: {fact.value}{src} "
            f"{_DIM}[conf {fact.confidence:.2f} · "
            f"{fact.decay_policy}]{_RESET}{note_bit}")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_fact_store(args: argparse.Namespace) -> int:
    """`friday6 fact store <key> <value>` — remember on both sides."""
    subject, predicate = _split_key(args.key)
    bridge = _resolve_bridge(args)
    try:
        result = bridge.remember(
            subject, predicate, args.value, source=args.source,
            confidence=args.confidence, decay_policy=args.policy)
    except Exception as exc:
        _print_error(f"could not store: {exc}")
        return EXIT_FAILED
    finally:
        _close_bridge(bridge)
    if result is None:
        _print_error("store failed — neither the DB nor the vault "
                     "accepted it")
        return EXIT_FAILED
    if args.json:
        print(json.dumps({"stored": True, "key": f"{subject}.{predicate}",
                          "note": result.get("note"),
                          "fact": bool(result.get("fact"))}))
        return EXIT_OK
    _print_logo()
    _print_friday(f"Noted — storing {subject}.{predicate}: {args.value} "
                  f"(conf {args.confidence}).")
    if result.get("note"):
        print(f"  {_DIM}vault note: {result['note']}{_RESET}")
    print()
    return EXIT_OK


def cmd_fact_recall(args: argparse.Namespace) -> int:
    """`friday6 fact recall <key>` — the fact + its vault note."""
    subject, predicate = _split_key(args.key)
    bridge = _resolve_bridge(args)
    try:
        fact = bridge.recall_one(subject, predicate)
        note_text = bridge.read_note(subject, predicate)
        note = str(bridge.note_path(subject, predicate))
    except Exception as exc:
        _print_error(f"could not recall: {exc}")
        return EXIT_FAILED
    finally:
        _close_bridge(bridge)
    if fact is None:
        if args.json:
            print(json.dumps({"found": False, "key": f"{subject}.{predicate}",
                              "note": note, "note_text": note_text}))
        else:
            _print_logo()
            _print_friday(f"I don't remember anything about "
                          f"{subject}.{predicate} yet.")
            print()
        return EXIT_FAILED
    if args.json:
        data = _fmt_fact(fact, note=note, json_mode=True)
        data["note_text"] = note_text or ""
        print(json.dumps(data))
        return EXIT_OK
    _print_logo()
    _print_friday(f"Here's what I remember about {subject}.{predicate}:")
    print(_fmt_fact(fact, note=note))
    print()
    return EXIT_OK


def cmd_fact_list(args: argparse.Namespace) -> int:
    """`friday6 fact list` — facts (+ note paths), optionally by subject."""
    bridge = _resolve_bridge(args)
    try:
        facts = bridge.recall(subject=args.subject, limit=args.limit)
        with_notes = [(f, bridge.note_path(f.subject, f.predicate))
                      for f in facts]
    except Exception as exc:
        _print_error(f"could not list: {exc}")
        return EXIT_FAILED
    finally:
        _close_bridge(bridge)
    if args.json:
        print(json.dumps([
            _fmt_fact(f, note=(str(p) if p.exists() else None),
                      json_mode=True)
            for f, p in with_notes
        ], default=str))
        return EXIT_OK
    _print_logo()
    if not facts:
        _print_friday("I don't remember anything yet. "
                      "Try 'friday6 fact store operator.name Lakshay'.")
        print()
        return EXIT_OK
    _print_friday(f"{len(facts)} fact(s):")
    for f, p in with_notes:
        note = str(p) if p.exists() else None
        print(_fmt_fact(f, note=note))
    print()
    return EXIT_OK


def cmd_fact_forget(args: argparse.Namespace) -> int:
    """`friday6 fact forget <key>` — remove DB row(s) + wiki note(s)."""
    subject, predicate = _split_key(args.key)
    bridge = _resolve_bridge(args)
    try:
        removed = bridge.forget(subject, predicate or None)
    except Exception as exc:
        _print_error(f"could not forget: {exc}")
        return EXIT_FAILED
    finally:
        _close_bridge(bridge)
    if not removed:
        if args.json:
            print(json.dumps({"forgotten": False,
                              "key": f"{subject}.{predicate}"}))
        else:
            _print_logo()
            _print_friday(f"I didn't find {subject}.{predicate} to forget.")
            print()
        return EXIT_FAILED
    if args.json:
        print(json.dumps({"forgotten": True, "key": f"{subject}.{predicate}"}))
        return EXIT_OK
    _print_logo()
    _print_friday(f"Forgotten — {subject}.{predicate} (DB row + vault note).")
    print()
    return EXIT_OK


# ---------------------------------------------------------------------------
# Argument parsers
# ---------------------------------------------------------------------------


def build_fact_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "fact", help="Fact memory — DB + vault, one write path",
        description="Friday's fact memory through the MemoryFact bridge: "
                    "every fact is stored in the SQLite memories table AND "
                    "as a vault wiki note with sources: frontmatter.",
    )
    fact_sub = parser.add_subparsers(dest="fact_command")

    p = fact_sub.add_parser("store", help="Remember a fact (both sides)")
    p.add_argument("key", help="Subject.predicate key, e.g. "
                               "operator.prefers_rust")
    p.add_argument("value", help="The fact value")
    p.add_argument("--source", default="cli", help="Provenance (default cli)")
    p.add_argument("--confidence", type=float, default=0.7,
                   help="Confidence 0.0–1.0 (default 0.7)")
    p.add_argument("--policy", default="usage",
                   choices=["none", "time", "usage"],
                   help="Decay policy (default usage)")
    p.add_argument("--root", type=Path, default=None,
                   help="Vault root (default ~/.friday/v6_vault)")
    p.add_argument("--db", type=Path, default=None,
                   help="SQLite DB path (default ~/.friday/friday.db)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_fact_store)

    p = fact_sub.add_parser("recall", help="Recall a fact + its note")
    p.add_argument("key", help="Subject.predicate key")
    p.add_argument("--root", type=Path, default=None)
    p.add_argument("--db", type=Path, default=None)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_fact_recall)

    p = fact_sub.add_parser("list", help="List facts (+ note paths)")
    p.add_argument("--subject", default=None,
                   help="Only facts with this subject (e.g. operator)")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--root", type=Path, default=None)
    p.add_argument("--db", type=Path, default=None)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_fact_list)

    p = fact_sub.add_parser("forget", help="Forget a fact (both sides)")
    p.add_argument("key", help="Subject.predicate key (or bare subject)")
    p.add_argument("--root", type=Path, default=None)
    p.add_argument("--db", type=Path, default=None)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_fact_forget)


def main(argv: list[str] | None = None) -> int:
    """Standalone entry point for `python -m friday_v6.cli_fact`."""
    logging.basicConfig(level=logging.WARNING)
    parser = argparse.ArgumentParser(prog="friday6 fact")
    build_fact_parser(parser)
    args = parser.parse_args(argv)
    if hasattr(args, "func"):
        return args.func(args) or 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
