"""CLI commands for the Insight Engine (Milestone 8.5)."""

from __future__ import annotations

import argparse
import sys

from .db import connect
from .insight import InsightEngine, InsightStatus


def cmd_insights_build(args: argparse.Namespace) -> int:
    """WRITE: derive insights from accumulated understanding/initiatives."""
    conn = connect()
    eng = InsightEngine(conn)
    result = eng.build()
    conn.close()
    print(result.to_text(), end="")
    return 0


def cmd_insights_list(args: argparse.Namespace) -> int:
    """READ: list active insights (non-retired)."""
    conn = connect()
    eng = InsightEngine(conn)
    items = eng.active_insights()
    conn.close()

    if not items:
        print("No active insights derived yet.\n")
        print("Run:\n")
        print("  friday insights build\n")
        return 0

    by_type: dict = {}
    for i in items:
        by_type.setdefault(i.type.value, []).append(i)

    for itype, its in sorted(by_type.items()):
        print(f"\n{itype.replace('_', ' ').title()} ({len(its)}):\n")
        for i in its:
            mark = {
                InsightStatus.STABLE: "*",
                InsightStatus.VERIFIED: "#",
                InsightStatus.OBSERVED: ">",
                InsightStatus.CANDIDATE: "?",
                InsightStatus.RETIRED: "x",
            }.get(i.status, "·")
            conf = i.confidence.value[0].upper()
            print(f"  [{mark}] {i.title} ({conf})")
            print(f"      {i.statement}")
            print(
                f"      Understanding: {i.understanding_count}, "
                f"Initiatives: {i.initiative_count}, "
                f"Knowledge: {i.knowledge_count}, Status: {i.status.value}"
            )

    print(f"\nActive: {len(items)}")
    return 0


def resolve_insight_id(iid: str, eng: InsightEngine) -> "tuple[str | None, int | None]":
    """Resolve a reference: full deterministic id, or INTEGER = Nth newest."""
    if iid.isdigit():
        n = int(iid)
        ordered = sorted(eng.all_insights(), key=lambda i: i.created_at, reverse=True)
        if 1 <= n <= len(ordered):
            return ordered[n - 1].id, None
        return None, 2
    return iid, None


def cmd_insights_explain(args: argparse.Namespace) -> int:
    """READ: explain one insight with confidence + supporting evidence."""
    iid = getattr(args, "id", None) or getattr(args, "insight_id", None)
    if not iid:
        print("error: insight ID required (use --id <id> or provide as argument)",
              file=sys.stderr)
        return 2

    conn = connect()
    eng = InsightEngine(conn)
    resolved, err = resolve_insight_id(iid, eng)
    if err is not None:
        count = len(eng.all_insights())
        print(f"error: insight index {iid} out of range (1-{count} items)",
              file=sys.stderr)
        conn.close()
        return err

    i, breakdown, u_ids, i_ids, k_ids, evo = eng.explain(resolved)
    conn.close()
    if i is None:
        print(f"error: insight not found: {iid}", file=sys.stderr)
        return 2

    print(f"Insight: {i.id}\n")
    print(f"Title:         {i.title}")
    print(f"Type:          {i.type.value}")
    print(f"Status:        {i.status.value}")
    print(f"Confidence:    {i.confidence.value}")
    print(f"Started:       {i.started_at}")
    print(f"Retired:       {i.retired_at}")
    print(f"Statement:     {i.statement}")
    if breakdown:
        print(f"Confidence derivation:")
        print(f"  total contributor weight: {breakdown.get('total_contributor_weight')}")
        print(f"  cross-project multiplier: {breakdown.get('cross_project_multiplier')}")
        print(f"  agreement factor:         {breakdown.get('agreement_factor')}")
    print(f"Created:    {i.created_at}")
    print(f"Updated:    {i.updated_at}")

    print("\nSupporting understanding:")
    for uid in u_ids:
        print(f"  - {uid}")
    print("\nSupporting initiatives:")
    for iid_ in i_ids:
        print(f"  - {iid_}")
    print("\nSupporting knowledge:")
    for kid in k_ids:
        print(f"  - {kid}")

    print("\nHistory:")
    from .db import insight_history_for
    conn2 = connect()
    hist = insight_history_for(conn2, i.id or "")
    conn2.close()
    if not hist:
        print("  (no prior snapshots)")
    for h in hist:
        n_u = len(h.understanding_ids.split(",")) if h.understanding_ids else 0
        n_i = len(h.initiative_ids.split(",")) if h.initiative_ids else 0
        n_k = len(h.knowledge_ids.split(",")) if h.knowledge_ids else 0
        print(f"  {h.build_at[:19]}  {h.confidence:6}  {h.status:8}  "
              f"{n_u} u / {n_i} i / {n_k} k")

    print("\nEvolution:")
    if not evo:
        print("  (no events)")
    for e in evo:
        print(f"  {e.timestamp[:19]}  {e.event_type:12}  {e.reason}")
    return 0


def cmd_insights_evolution(args: argparse.Namespace) -> int:
    """READ: chronological timeline of insight evolution events."""
    conn = connect()
    eng = InsightEngine(conn)
    events = eng.evolution()
    conn.close()

    if not events:
        print("No insight evolution yet. Run `friday insights build`.")
        return 0

    print("Insight Evolution Events\n")
    for e in events:
        print(f"{e.timestamp[:19]}  {e.event_type:12}  {e.insight_id}")
        print(f"    {e.reason}")
    print(f"\nTotal events: {len(events)}")
    return 0


def cmd_insights(args: argparse.Namespace) -> int:
    """Dispatch friday insights subcommands."""
    action = getattr(args, "action", None)
    if action == "build":
        return cmd_insights_build(args)
    elif action == "explain":
        return cmd_insights_explain(args)
    elif action == "evolution":
        return cmd_insights_evolution(args)
    else:
        return cmd_insights_list(args)
