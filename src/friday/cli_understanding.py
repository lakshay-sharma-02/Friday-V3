"""CLI commands for the Understanding Engine (Milestone 8.3)."""

from __future__ import annotations

import argparse
import sys

from .db import connect
from .understanding import UnderstandingEngine, UnderstandingStatus


def cmd_understanding_build(args: argparse.Namespace) -> int:
    """WRITE: derive understanding from accumulated knowledge."""
    conn = connect()
    eng = UnderstandingEngine(conn)
    result = eng.build()
    conn.close()
    print(result.to_text(), end="")
    return 0


def cmd_understanding_list(args: argparse.Namespace) -> int:
    """READ: list all current understandings."""
    conn = connect()
    eng = UnderstandingEngine(conn)
    items = eng.all_understanding()
    conn.close()

    if not items:
        print("No understanding derived yet.\n")
        print("Run:\n")
        print("  friday understanding build\n")
        return 0

    by_type: dict = {}
    for u in items:
        by_type.setdefault(u.type.value, []).append(u)

    for utype, its in sorted(by_type.items()):
        print(f"\n{utype.replace('_', ' ').title()} ({len(its)}):\n")
        for u in its:
            mark = "✓" if u.status == UnderstandingStatus.STABLE else "·"
            conf = u.confidence.value[0].upper()
            print(f"  [{mark}] {u.subject} ({conf})")
            print(f"      {u.statement}")
            print(f"      Knowledge cited: {u.knowledge_count}, Status: {u.status.value}")

    print(f"\nTotal: {len(items)}")
    return 0


def resolve_understanding_id(uid: str, eng: UnderstandingEngine) -> "tuple[str | None, int | None]":
    """Resolve a reference: full deterministic id, or INTEGER = Nth newest."""
    if uid.isdigit():
        n = int(uid)
        ordered = sorted(eng.all_understanding(), key=lambda u: u.created_at, reverse=True)
        if 1 <= n <= len(ordered):
            return ordered[n - 1].id, None
        return None, 2
    return uid, None


def cmd_understanding_explain(args: argparse.Namespace) -> int:
    """READ: explain one understanding with confidence + supporting knowledge."""
    uid = getattr(args, "id", None) or getattr(args, "understanding_id", None)
    if not uid:
        print("error: understanding ID required (use --id <id> or provide as argument)",
              file=sys.stderr)
        return 2

    conn = connect()
    eng = UnderstandingEngine(conn)
    resolved, err = resolve_understanding_id(uid, eng)
    if err is not None:
        count = len(eng.all_understanding())
        print(f"error: understanding index {uid} out of range (1-{count} items)",
              file=sys.stderr)
        conn.close()
        return err

    u, breakdown, hist, evo = eng.explain(resolved)
    conn.close()
    if u is None:
        print(f"error: understanding not found: {uid}", file=sys.stderr)
        return 2

    print(f"Understanding: {u.id}\n")
    print(f"Type:       {u.type.value}")
    print(f"Subject:    {u.subject}")
    print(f"Statement:  {u.statement}")
    print(f"Confidence: {u.confidence.value}")
    print(f"Status:     {u.status.value}")
    print(f"Knowledge cited: {u.knowledge_count}")
    if breakdown:
        print(f"Confidence derivation:")
        print(f"  total contributor weight: {breakdown.get('total_contributor_weight')}")
        print(f"  cross-source multiplier:  {breakdown.get('cross_source_multiplier')}")
        print(f"  agreement factor:         {breakdown.get('agreement_factor')}")
    print(f"Created:    {u.created_at}")
    print(f"Updated:    {u.updated_at}")

    print("\nSupporting knowledge:")
    for kid in u.knowledge_ids:
        print(f"  - {kid}")

    print("\nHistory:")
    if not hist:
        print("  (no prior snapshots)")
    for h in hist:
        n_k = len(h.knowledge_ids.split(",")) if h.knowledge_ids else 0
        print(f"  {h.build_at[:19]}  {h.confidence:6}  {h.status:8}  {n_k} kn")

    print("\nEvolution:")
    if not evo:
        print("  (no events)")
    for e in evo:
        print(f"  {e.timestamp[:19]}  {e.event_type:12}  {e.reason}")
    return 0


def cmd_understanding_evolution(args: argparse.Namespace) -> int:
    """READ: timeline of understanding evolution events."""
    conn = connect()
    eng = UnderstandingEngine(conn)
    events = eng.evolution_timeline()
    conn.close()

    if not events:
        print("No understanding evolution yet. Run `friday understanding build`.")
        return 0

    print("Understanding Evolution Events\n")
    for e in events:
        print(f"{e.timestamp[:19]}  {e.event_type:12}  {e.understanding_id}")
        print(f"    {e.reason}")
    print(f"\nTotal events: {len(events)}")
    return 0


def cmd_understanding(args: argparse.Namespace) -> int:
    """Dispatch friday understanding subcommands."""
    action = getattr(args, "action", None)
    if action == "build":
        return cmd_understanding_build(args)
    elif action == "explain":
        return cmd_understanding_explain(args)
    elif action == "evolution":
        return cmd_understanding_evolution(args)
    else:
        return cmd_understanding_list(args)
