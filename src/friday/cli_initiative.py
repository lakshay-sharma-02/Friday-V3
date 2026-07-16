"""CLI commands for the Initiatives Engine (Milestone 8.4)."""

from __future__ import annotations

import argparse
import sys

from .db import connect
from .initiative import InitiativeEngine, InitiativeStatus


def cmd_initiatives_build(args: argparse.Namespace) -> int:
    """WRITE: derive initiatives from accumulated understanding."""
    conn = connect()
    eng = InitiativeEngine(conn)
    result = eng.build()
    conn.close()
    print(result.to_text(), end="")
    return 0


def cmd_initiatives_list(args: argparse.Namespace) -> int:
    """READ: list all current initiatives."""
    conn = connect()
    eng = InitiativeEngine(conn)
    items = eng.all_initiatives()
    conn.close()

    if not items:
        print("No initiatives derived yet.\n")
        print("Run:\n")
        print("  friday initiatives build\n")
        return 0

    by_type: dict = {}
    for i in items:
        by_type.setdefault(i.type.value, []).append(i)

    for itype, its in sorted(by_type.items()):
        print(f"\n{itype.replace('_', ' ').title()} ({len(its)}):\n")
        for i in its:
            mark = {
                InitiativeStatus.ACTIVE: "*",
                InitiativeStatus.REVIEW: "#",
                InitiativeStatus.COMPLETED: "✓",
                InitiativeStatus.BLOCKED: "!",
                InitiativeStatus.DORMANT: ".",
                InitiativeStatus.ARCHIVED: "x",
                InitiativeStatus.STARTED: ">",
                InitiativeStatus.CANDIDATE: "?",
            }.get(i.status, "·")
            conf = i.confidence.value[0].upper()
            print(f"  [{mark}] {i.title} ({conf})")
            print(f"      {i.statement}")
            print(
                f"      Repos: {i.repo_count}, Understanding: "
                f"{i.understanding_count}, Status: {i.status.value}"
            )

    print(f"\nTotal: {len(items)}")
    return 0


def resolve_initiative_id(iid: str, eng: InitiativeEngine) -> "tuple[str | None, int | None]":
    """Resolve a reference: full deterministic id, or INTEGER = Nth newest."""
    if iid.isdigit():
        n = int(iid)
        ordered = sorted(eng.all_initiatives(), key=lambda i: i.created_at, reverse=True)
        if 1 <= n <= len(ordered):
            return ordered[n - 1].id, None
        return None, 2
    return iid, None


def cmd_initiatives_explain(args: argparse.Namespace) -> int:
    """READ: explain one initiative with confidence + supporting evidence."""
    iid = getattr(args, "id", None) or getattr(args, "initiative_id", None)
    if not iid:
        print("error: initiative ID required (use --id <id> or provide as argument)",
              file=sys.stderr)
        return 2

    conn = connect()
    eng = InitiativeEngine(conn)
    resolved, err = resolve_initiative_id(iid, eng)
    if err is not None:
        count = len(eng.all_initiatives())
        print(f"error: initiative index {iid} out of range (1-{count} items)",
              file=sys.stderr)
        conn.close()
        return err

    i, breakdown, hist, evo, rels = eng.explain(resolved)
    conn.close()
    if i is None:
        print(f"error: initiative not found: {iid}", file=sys.stderr)
        return 2

    print(f"Initiative: {i.id}\n")
    print(f"Title:       {i.title}")
    if i.statement:
        print(f"Goal:        {i.statement}")
    print(f"Type:        {i.type.value}")
    print(f"Status:      {i.status.value}")
    print(f"Confidence:  {i.confidence.value}")
    print(f"Started:     {i.started_at}")
    print(f"Completed:   {i.completed_at}")
    print(f"Repositories: {i.repo_count} ({', '.join(i.participating_repositories)})")
    print(f"Understanding cited: {i.understanding_count}")
    print(f"Knowledge cited:     {i.knowledge_count}")
    if breakdown:
        print(f"Confidence derivation:")
        print(f"  total contributor weight: {breakdown.get('total_contributor_weight')}")
        print(f"  cross-project multiplier: {breakdown.get('cross_project_multiplier')}")
        print(f"  agreement factor:         {breakdown.get('agreement_factor')}")
    print(f"Created:    {i.created_at}")
    print(f"Updated:    {i.updated_at}")

    print("\nSupporting understanding:")
    for uid in i.understanding_ids:
        print(f"  - {uid}")

    print("\nHistory:")
    if not hist:
        print("  (no prior snapshots)")
    for h in hist:
        n_u = len(h.understanding_ids.split(",")) if h.understanding_ids else 0
        n_r = len(h.participating_repositories.split(",")) if h.participating_repositories else 0
        print(f"  {h.build_at[:19]}  {h.confidence:6}  {h.status:8}  "
              f"{n_u} und / {n_r} repo")

    print("\nEvolution:")
    if not evo:
        print("  (no events)")
    for e in evo:
        print(f"  {e.timestamp[:19]}  {e.event_type:12}  {e.reason}")

    print("\nRelationships (merge/split):")
    if not rels:
        print("  (none)")
    for r in rels:
        print(f"  {r.relationship_type}: parents=[{r.parent_ids}] "
              f"children=[{r.child_ids}]")
    return 0


def cmd_initiatives_timeline(args: argparse.Namespace) -> int:
    """READ: chronological timeline of initiative evolution events."""
    conn = connect()
    eng = InitiativeEngine(conn)
    events = eng.timeline()
    rels = eng.relationships()
    conn.close()

    if not events and not rels:
        print("No initiative evolution yet. Run `friday initiatives build`.")
        return 0

    print("Initiative Evolution Events\n")
    for e in events:
        print(f"{e.timestamp[:19]}  {e.event_type:12}  {e.initiative_id}")
        print(f"    {e.reason}")
    if rels:
        print("\nMerge / Split Edges\n")
        for r in rels:
            print(f"{r.created_at[:19]}  {r.relationship_type:6}  "
                  f"parents=[{r.parent_ids}] children=[{r.child_ids}]")
    print(f"\nTotal events: {len(events)}, edges: {len(rels)}")
    return 0


def cmd_initiatives(args: argparse.Namespace) -> int:
    """Dispatch friday initiatives subcommands."""
    action = getattr(args, "action", None)
    if action == "build":
        return cmd_initiatives_build(args)
    elif action == "explain":
        return cmd_initiatives_explain(args)
    elif action == "timeline":
        return cmd_initiatives_timeline(args)
    else:
        return cmd_initiatives_list(args)
