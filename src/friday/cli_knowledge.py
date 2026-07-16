"""CLI commands for the Knowledge Engine (Milestone 8.1 / 8.2)."""

from __future__ import annotations

import argparse
import sys

from .db import connect
from .knowledge import KnowledgeEngine, KnowledgeStatus, evolve, history_timeline
from .knowledge.evolution import evolution_timeline


def cmd_knowledge_build(args: argparse.Namespace) -> int:
    """WRITE: build knowledge, then derive Knowledge Evolution records."""
    conn = connect()
    eng = KnowledgeEngine(conn)
    result = eng.build()
    n_events = evolve(conn)
    conn.close()
    print(result.to_text(), end="")
    print(f"Evolution events recorded: {n_events}\n")
    return 0


def cmd_knowledge_list(args: argparse.Namespace) -> int:
    """READ: list all knowledge entries."""
    conn = connect()
    eng = KnowledgeEngine(conn)
    knowledge = eng.all_knowledge()
    conn.close()

    if not knowledge:
        print("No knowledge accumulated yet.\n")
        print("Run:\n")
        print("  friday knowledge build\n")
        return 0

    # Group by type
    by_type = {}
    for k in knowledge:
        by_type.setdefault(k.type.value, []).append(k)

    for ktype, items in sorted(by_type.items()):
        print(f"\n{ktype.replace('_', ' ').title()} ({len(items)}):\n")
        for k in items:
            status_mark = "✓" if k.status == KnowledgeStatus.STABLE else "·"
            conf = k.confidence.value[0].upper()  # W/M/S
            print(f"  [{status_mark}] {k.subject} ({conf})")
            print(f"      {k.statement}")
            print(f"      Evidence: {k.evidence_count}, Verified: {k.verification_count}x")

    print(f"\nTotal: {len(knowledge)}")
    return 0


def resolve_knowledge_id(knowledge_id: str, eng) -> "tuple[str | None, int | None]":
    """Resolve a knowledge reference to a concrete timestamp ID.

    Accepts either a full timestamp-based knowledge ID, or an INTEGER meaning
    the Nth newest knowledge item (1 = most recent). Both forms supported
    (Part G); timestamp IDs are never removed.

    Returns (resolved_id, error_code). error_code is None on success, or an
    exit code (2) when the integer is out of range / missing.
    """
    if knowledge_id.isdigit():
        n = int(knowledge_id)
        ordered = sorted(eng.all_knowledge(),
                         key=lambda k: k.created_at, reverse=True)
        if 1 <= n <= len(ordered):
            return ordered[n - 1].id, None
        return None, 2
    return knowledge_id, None


def cmd_knowledge_explain(args: argparse.Namespace) -> int:
    """READ: explain one knowledge entry in detail.

    Accepts either a full timestamp-based knowledge ID, or an INTEGER meaning
    the Nth newest knowledge item (1 = most recent). Both forms supported
    (Part G); timestamp IDs are never removed.
    """
    # Handle both --id flag and positional argument
    knowledge_id = getattr(args, 'id', None) or getattr(args, 'knowledge_id', None)

    if not knowledge_id:
        print("error: knowledge ID required (use --id <id> or provide ID as argument)", file=sys.stderr)
        return 2

    conn = connect()
    eng = KnowledgeEngine(conn)

    resolved, err = resolve_knowledge_id(knowledge_id, eng)
    if err is not None:
        count = len(eng.all_knowledge())
        print(f"error: knowledge index {knowledge_id} out of range "
              f"(1-{count} items)", file=sys.stderr)
        conn.close()
        return err

    k = eng.knowledge_by_id(resolved)
    conn.close()

    if not k:
        print(f"error: knowledge not found: {knowledge_id}", file=sys.stderr)
        return 2

    print(f"Knowledge: {k.id}\n")
    print(f"Type:       {k.type.value}")
    print(f"Subject:    {k.subject}")
    print(f"Statement:  {k.statement}")
    print(f"Confidence: {k.confidence.value}")
    print(f"Status:     {k.status.value}")
    print(f"Evidence:   {k.evidence_count} observation(s)")
    print(f"Verified:   {k.verification_count} time(s)")
    print(f"Created:    {k.created_at}")
    print(f"Updated:    {k.updated_at}")
    if k.last_verified:
        print(f"Last verified: {k.last_verified}")

    if args.verbose and k.evidence_ids:
        print(f"\nEvidence IDs:")
        for eid in k.evidence_ids[:10]:
            print(f"  {eid}")
        if len(k.evidence_ids) > 10:
            print(f"  ... and {len(k.evidence_ids) - 10} more")

    # Knowledge history (Part A) — append-only timeline.
    print("\nHistory:")
    hist = history_timeline(conn, k.id)
    if not hist:
        print("  (no prior snapshots)")
    for h in hist:
        print(f"  {h.build_at[:19]}  {h.confidence:6}  {h.status:8}  {h.evidence_ids.count(',')+1 if h.evidence_ids else 0} ev")

    return 0


def cmd_knowledge_verify(args: argparse.Namespace) -> int:
    """READ: verify knowledge integrity and show statistics."""
    conn = connect()
    eng = KnowledgeEngine(conn)
    knowledge = eng.all_knowledge()
    conn.close()

    if not knowledge:
        print("No knowledge to verify.")
        return 0

    print("Knowledge Verification\n")

    # Count by status
    by_status = {}
    for k in knowledge:
        by_status.setdefault(k.status.value, []).append(k)

    for status, items in sorted(by_status.items()):
        print(f"{status.title()}: {len(items)}")

    # Count by confidence
    by_conf = {}
    for k in knowledge:
        by_conf.setdefault(k.confidence.value, []).append(k)

    print()
    for conf, items in sorted(by_conf.items()):
        print(f"{conf.title()} confidence: {len(items)}")

    # Find knowledge without evidence
    no_evidence = [k for k in knowledge if not k.evidence_ids]
    if no_evidence:
        print(f"\nWarning: {len(no_evidence)} knowledge entries have no evidence")

    # Find knowledge needing verification
    candidates = [k for k in knowledge if k.status == KnowledgeStatus.CANDIDATE]
    print(f"\nCandidates needing verification: {len(candidates)}")

    return 0


def cmd_knowledge_history(args: argparse.Namespace) -> int:
    """READ: timeline of every knowledge item's evolution (Part A/J)."""
    conn = connect()
    eng = KnowledgeEngine(conn)
    knowledge = eng.all_knowledge()
    conn.close()

    if not knowledge:
        print("No knowledge accumulated yet.\n")
        return 0

    print("Knowledge History (append-only)\n")
    conn2 = connect()
    for k in sorted(knowledge, key=lambda x: x.subject):
        hist = history_timeline(conn2, k.id)
        print(f"{k.subject} ({k.type.value})")
        if not hist:
            print("  (no snapshots)")
            continue
        for h in hist:
            n_ev = len(h.evidence_ids.split(",")) if h.evidence_ids else 0
            print(f"  {h.build_at[:19]}  conf={h.confidence:6}  status={h.status:8}  ev={n_ev}")
    conn2.close()
    print()
    return 0


def cmd_knowledge_evolution(args: argparse.Namespace) -> int:
    """READ: recent evolution events (Part D/J)."""
    conn = connect()
    events = evolution_timeline(conn)
    conn.close()

    if not events:
        print("No evolution events yet. Run `friday knowledge build`.")
        return 0

    print("Knowledge Evolution Events\n")
    for e in events:
        related = f"  [related: {e.related_ids}]" if e.related_ids else ""
        print(f"{e.timestamp[:19]}  {e.event_type:10}  {e.knowledge_id}")
        print(f"    {e.reason}{related}")
    print(f"\nTotal events: {len(events)}")
    return 0


def cmd_knowledge(args: argparse.Namespace) -> int:
    """Dispatch friday knowledge subcommands."""
    action = getattr(args, "action", None)
    if action == "build":
        return cmd_knowledge_build(args)
    elif action == "explain":
        return cmd_knowledge_explain(args)
    elif action == "verify":
        return cmd_knowledge_verify(args)
    elif action == "history":
        return cmd_knowledge_history(args)
    elif action == "evolution":
        return cmd_knowledge_evolution(args)
    else:
        # Default: list
        return cmd_knowledge_list(args)
