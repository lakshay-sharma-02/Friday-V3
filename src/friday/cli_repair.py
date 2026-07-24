"""CLI commands for the Repair Loop (Law 16).

`friday repair pending`                       -> list drafted proposals awaiting approval
`friday repair pending <id>`                  -> show one proposal with evidence
`friday repair approve <id>`                  -> approve -> re-enters Planning -> new graph
`friday repair reject <id>`                   -> dismiss

Thin dispatch over repair/engine.py. No repair logic here.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Optional

from .db import connect, now_iso
from .repair import (
    approve_repair,
    detect_repair_candidates,
    evaluate_repair,
    get_all_candidates,
    get_pending_proposals,
    propose_repair,
)


def _show_candidate(c: dict) -> str:
    """Format one repair candidate for display."""
    lines = [
        f"  Task:       {c['task_id']}",
        f"  Graph:      {c['graph_id']}",
        f"  Failure:    {c['failure_reason']}",
        f"  Capability: {c['capability'] or '(unknown)'}",
        f"  Depth:      {c['repair_depth']}",
        f"  Decision:   {c['decision']}",
        f"  Evidence:   {len(c['evidence_ids'])} id(s)",
    ]
    if c.get("proposal_id"):
        lines.insert(0, f"  Proposal:   {c['proposal_id']}")
    return "\n".join(lines)


def cmd_repair_pending(args: argparse.Namespace) -> int:
    """List pending repair proposals or show one in detail."""
    conn = connect()

    # Also run detection to surface new candidates.
    candidates = detect_repair_candidates(conn)
    for c in candidates:
        propose_repair(conn, c)

    proposal_id = getattr(args, "proposal_id", None)
    if proposal_id:
        # Show one proposal in detail.
        row = conn.execute(
            "SELECT * FROM repair_proposals WHERE id = ?", (proposal_id,)
        ).fetchone()
        conn.close()
        if row is None:
            print(f"error: no such proposal: {proposal_id}", file=sys.stderr)
            return 2
        print(f"Repair Proposal: {row['id']}")
        print(f"  Graph:      {row['original_graph_id']}")
        print(f"  Task:       {row['original_task_id']}")
        print(f"  Failure:    {row['failure_reason']}")
        print(f"  Capability: {row['capability'] or '(unknown)'}")
        print(f"  Depth:      {row['repair_depth']}")
        print(f"  Decision:   {row['decision']}")
        print(f"  Status:     {row['status']}")
        print(f"  Goal:       {row['proposed_goal']}")
        print(f"  Created:    {row['created_at']}")
        evidence_ids = json.loads(row["evidence_ids"] or "[]")
        if evidence_ids:
            print(f"  Evidence ({len(evidence_ids)}):")
            for eid in evidence_ids[:5]:
                print(f"    - {eid}")
            if len(evidence_ids) > 5:
                print(f"    ... and {len(evidence_ids) - 5} more")
        print()
        print("Actions:")
        print(f"  friday repair approve {row['id']}")
        print(f"  friday repair reject {row['id']}")
        return 0

    # List all pending proposals.
    proposals = get_pending_proposals(conn)
    conn.close()

    if not proposals:
        print("No pending repair proposals.")
        candidates = get_all_candidates(conn)
        if candidates:
            print(f"\n{candidates} unaddressed failure(s) found (escalated or pending).")
        return 0

    print(f"Pending repair proposals ({len(proposals)}):\n")
    for p in proposals:
        print(f"  {p['id']}")
        print(f"    Goal:    {p['proposed_goal']}")
        print(f"    Task:    {p['original_task_id']}")
        print(f"    Failure: {p['failure_reason']}")
        print(f"    Depth:   {p['repair_depth']}")
        if p["capability"]:
            print(f"    Cap:     {p['capability']}")
        print()
    print("Actions:")
    print("  friday repair approve <id>   Approve a proposal")
    print("  friday repair reject <id>    Reject a proposal")
    print("  friday repair pending <id>   Show full detail")
    return 0


def cmd_repair_approve(args: argparse.Namespace) -> int:
    """Approve a repair proposal -> creates a new graph with source=repair:<...>."""
    proposal_id = getattr(args, "proposal_id", None)
    if not proposal_id:
        print("error: proposal id required (friday repair approve <id>)",
              file=sys.stderr)
        return 2

    conn = connect()
    graph_id = approve_repair(conn, proposal_id)
    conn.close()

    if graph_id is None:
        print(f"error: could not approve proposal {proposal_id}", file=sys.stderr)
        return 2

    print(f"Approved. New task graph created: {graph_id}")
    print(f"Source: repair:<original>:<task>")
    print()
    print("To execute the repair:")
    print(f"  friday execute \"<select-repair-graph>\"")
    return 0


def cmd_repair_reject(args: argparse.Namespace) -> int:
    """Reject a repair proposal."""
    proposal_id = getattr(args, "proposal_id", None)
    if not proposal_id:
        print("error: proposal id required (friday repair reject <id>)",
              file=sys.stderr)
        return 2

    conn = connect()
    now = now_iso()
    conn.execute(
        "UPDATE repair_proposals SET status = 'rejected', reviewed_at = ? WHERE id = ?",
        (now, proposal_id),
    )
    conn.execute(
        """INSERT INTO repair_history
           (proposal_id, event_type, detail, recorded_at)
           VALUES (?, ?, ?, ?)""",
        (proposal_id, "rejected", "Rejected by human", now),
    )
    conn.commit()
    conn.close()
    print(f"Rejected: {proposal_id}")
    return 0


def cmd_repair(args: argparse.Namespace) -> int:
    """Dispatch friday repair subcommands."""
    action = getattr(args, "action", None) or "pending"
    rest = getattr(args, "rest", None) or []

    if action == "pending":
        args.proposal_id = rest[0] if rest else None
        return cmd_repair_pending(args)
    elif action == "approve":
        args.proposal_id = rest[0] if rest else None
        return cmd_repair_approve(args)
    elif action == "reject":
        args.proposal_id = rest[0] if rest else None
        return cmd_repair_reject(args)
    else:
        print(f"error: unknown repair action: {action}", file=sys.stderr)
        print("usage: friday repair [pending|approve|reject] [<id>]", file=sys.stderr)
        return 2
