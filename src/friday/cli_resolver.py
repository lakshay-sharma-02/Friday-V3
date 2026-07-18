"""CLI commands for the Capability Resolver (Milestone 9.3).

`friday resolve "<goal>"`  -> Plan -> Task Graph -> Assignments.
`friday resolver`          -> list all assignments.
`friday resolver explain <id>` -> task, worker, score, matched/missing caps,
                                  confidence, reason, alternatives.
`friday resolver export`       -> JSON export of all assignments.

This module only ADDS a layer. Task Graph, Worker Registry, and every lower
layer are unchanged. No execution, scheduling, or worker invocation.
"""

from __future__ import annotations

import argparse
import json
import sys

from .db import connect, now_iso
from .planning import TaskGraphEngine
from .worker.engine import WorkerRegistry
from .resolver import CapabilityResolver, ResolutionStatus, SelectionStrategy


def _resolve_assignment_ref(ref: str, resolver: CapabilityResolver):
    """Resolve a reference: full assignment_id, or INTEGER = Nth newest."""
    if ref.isdigit():
        n = int(ref)
        items = sorted(resolver.assignments(), key=lambda a: a.created_at,
                       reverse=True)
        if 1 <= n <= len(items):
            return items[n - 1], None
        return None, 2
    # Try as graph_id (returns all assignments for that graph).
    by_graph = resolver.assignments(graph_id=ref)
    if by_graph:
        return by_graph, "graph"
    # Try as assignment_id.
    a = resolver.assignment_for_task(ref)
    if a is not None:
        return a, None
    return None, 3


def cmd_resolve(args: argparse.Namespace) -> int:
    """WRITE: Plan -> Task Graph -> Assignments for a goal."""
    raw = getattr(args, "goal", None)
    goal = " ".join(raw) if isinstance(raw, (list, tuple)) else (raw or "")
    if not goal.strip():
        print('error: a goal is required: friday resolve "<goal>"',
              file=sys.stderr)
        return 2
    conn = connect()

    # 1. Derive plan + compile task graph (reuses existing engines).
    graph_eng = TaskGraphEngine(conn)
    g = graph_eng.generate(goal)

    # 2. Resolve every task in the graph to a worker.
    resolver = CapabilityResolver(conn)
    result = resolver.resolve_graph(g.id)
    conn.close()

    # 3. Print summary.
    print(f"Resolution: {result.graph_id}\n")
    print(f"Goal:          {g.goal}")
    print(f"Tasks:         {len(result.assignments)}")
    print(f"Assigned:      {result.assigned}")
    print(f"Unresolved:    {result.unresolved}")
    print(f"Strategy:      {result.strategy.value}")
    print(f"Resolved at:   {result.resolved_at}\n")

    for r in result.results:
        status_mark = "+" if r.status == ResolutionStatus.ASSIGNED else "!"
        worker = r.worker_id or "(none)"
        print(f"  [{status_mark}] {r.task_title}")
        print(f"      worker:   {worker}")
        print(f"      caps:     {', '.join(r.matched_capabilities) or '-'}")
        if r.missing_capabilities:
            print(f"      missing:  {', '.join(r.missing_capabilities)}")
        print(f"      strategy: {r.selection_strategy.value}")
        print(f"      conf:     {r.confidence}")
        print(f"      reason:   {r.reason}")
        if r.alternatives:
            print(f"      alts:     {len(r.alternatives)} runner(s)-up")
        print()

    return 0


def cmd_resolver_list(args: argparse.Namespace) -> int:
    """READ: list all resolver assignments."""
    conn = connect()
    resolver = CapabilityResolver(conn)
    items = resolver.assignments()
    conn.close()
    if not items:
        print("No assignments yet.\n")
        print("Run:\n")
        print('  friday resolve "<goal>"\n')
        return 0
    print(f"Resolver assignments ({len(items)}):\n")
    for a in items:
        mark = "+" if a.status == ResolutionStatus.ASSIGNED else "!"
        worker = a.worker_id or "(none)"
        print(f"  [{mark}] {a.assignment_id}")
        print(f"      worker:   {worker}")
        print(f"      status:   {a.status.value}  conf: {a.confidence}")
        print(f"      strategy: {a.selection_strategy.value}")
    return 0


def cmd_resolver_explain(args: argparse.Namespace) -> int:
    """READ: explain one assignment in full."""
    ref = getattr(args, "id", None) or getattr(args, "assignment_id", None)
    if not ref:
        print("error: assignment ID required (use --id <id> or provide as argument)",
              file=sys.stderr)
        return 2
    conn = connect()
    resolver = CapabilityResolver(conn)
    resolved, err = _resolve_assignment_ref(ref, resolver)

    if err is not None:
        conn.close()
        if err == 2:
            count = len(resolver.assignments())
            print(f"error: index {ref} out of range (1-{count} items)",
                  file=sys.stderr)
        else:
            print(f"error: assignment not found: {ref}", file=sys.stderr)
        return 2

    # If it's a list (graph_id match), explain each.
    if isinstance(resolved, list):
        for a in resolved:
            _print_assignment_detail(a, resolver)
        conn.close()
        return 0

    _print_assignment_detail(resolved, resolver)
    conn.close()
    return 0


def _print_assignment_detail(a, resolver):
    """Pretty-print one assignment."""
    print(f"Assignment: {a.assignment_id}\n")
    print(f"  Task:              {a.task_id}")
    print(f"  Worker:            {a.worker_id or '(none)'}")
    print(f"  Status:            {a.status.value}")
    print(f"  Confidence:        {a.confidence}")
    print(f"  Strategy:          {a.selection_strategy.value}")
    print(f"  Reason:            {a.reason}")

    matched = a.matched_capabilities
    missing = a.missing_capabilities
    print(f"  Matched caps:      {', '.join(matched) if matched else '-'}")
    print(f"  Missing caps:      {', '.join(missing) if missing else '-'}")

    print(f"  Schema version:    {a.schema_version}")
    print(f"  Created:           {a.created_at}")
    print(f"  Updated:           {a.updated_at}")

    # History for this assignment.
    hist = resolver.history(assignment_id=a.assignment_id)
    print(f"\n  History snapshots: {len(hist)}")
    for h in hist:
        print(f"    {h['resolved_at'][:19]}  worker={h['worker_id'] or '(none)'} "
              f"status={h['status']} conf={h['confidence']}")

    # Evolution events for this graph.
    evo = resolver.evolution(graph_id=a.graph_id)
    task_evo = [e for e in evo if e.get("task_id") == a.task_id]
    if task_evo:
        print(f"  Evolution events:  {len(task_evo)}")
        for e in task_evo:
            print(f"    {e['evolved_at'][:19]}  {e['change_type']}: "
                  f"{e['from_worker_id'] or '(none)'} -> "
                  f"{e['to_worker_id'] or '(none)'}")

    # Alternative workers (from resolution — we'd need to re-resolve for those,
    # but the stored reason often says how many were eligible).
    print()


def cmd_resolver_export(args: argparse.Namespace) -> int:
    """READ: export all assignments as JSON."""
    conn = connect()
    resolver = CapabilityResolver(conn)
    items = resolver.assignments()
    conn.close()
    data = {
        "schema_version": "1.0",
        "assignment_count": len(items),
        "assignments": [a.to_dict() for a in items],
    }
    print(json.dumps(data, indent=2))
    return 0


def cmd_resolver(args: argparse.Namespace) -> int:
    """Dispatch friday resolver subcommands.

    `friday resolver`               -> list all assignments
    `friday resolver explain <id>`  -> explain one assignment
    `friday resolver export`        -> JSON export
    """
    token = getattr(args, "token", None)
    if token == "export":
        return cmd_resolver_export(args)
    if token == "explain":
        args.id = getattr(args, "assignment_id", None)
        return cmd_resolver_explain(args)
    if token:
        # Treat as explain <id>.
        args.id = token
        return cmd_resolver_explain(args)
    return cmd_resolver_list(args)
