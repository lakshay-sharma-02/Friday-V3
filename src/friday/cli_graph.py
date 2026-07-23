"""CLI commands for the Task Graph Compiler (Milestone 9.1).

`friday graph "<goal>"`  -> derive Plan (frozen PlanEngine) then compile +
persist + show a Task Graph summary.
`friday graph generate <initiative-id>` -> derive a Task Graph proposal from an
    approved initiative.
`friday graph review`   -> list pending graph proposals awaiting review.
`friday graphs`          -> list compiled graphs.
`friday graph explain <id>` -> tasks, edges, critical path, parallel tasks,
                               acceptance criteria, verification, rollback.
`friday graph export <id>`   -> Worker-Engine JSON export.

This module only ADDS a layer. The Brain, Planning, and every lower layer are
unchanged. No routing/retrieval/judgment changes anywhere.
"""

from __future__ import annotations

import argparse
import json
import sys

from .db import connect, now_iso
from .planning import TaskGraphEngine


def _resolve_graph_id(gid: str, eng: TaskGraphEngine):
    """Resolve a reference: full deterministic id, INTEGER = Nth newest, or
    last-segment short form (e.g. proposal graph short IDs)."""
    if gid.isdigit():
        n = int(gid)
        ordered = sorted(eng.all_graphs(), key=lambda r: r.created_at,
                         reverse=True)
        if 1 <= n <= len(ordered):
            return ordered[n - 1].id, None
        return None, 2
    # Exact match first (fast path for goal-based graphs with simple IDs).
    if eng.graph_by_id(gid) is not None:
        return gid, None
    # Fallback: last-segment match for proposal short IDs
    # e.g. "maintenance_Python_Engineering_Initiative" matches
    # "initiative_graph:maintenance_Python_Engineering_Initiative".
    for r in eng.all_graphs():
        short = r.id.split(":")[-1] if ":" in r.id else r.id
        if short == gid:
            return r.id, None
    return gid, None


def cmd_graph_generate(args: argparse.Namespace) -> int:
    """WRITE: derive Plan -> compile Task Graph -> persist -> show summary."""
    raw = getattr(args, "goal", None)
    goal = " ".join(raw) if isinstance(raw, (list, tuple)) else (raw or "")
    if not goal.strip():
        print('error: a goal is required: friday graph "<goal>"',
              file=sys.stderr)
        return 2
    conn = connect()
    eng = TaskGraphEngine(conn)
    g = eng.generate(goal)
    conn.close()
    sys.stdout.write(g.summary())
    return 0


def cmd_graph_generate_from_initiative(args: argparse.Namespace) -> int:
    """WRITE: generate a Task Graph proposal from an approved initiative.

    The graph is built ONLY from the initiative's actual supporting evidence.
    Status is "proposal" — reviewable, non-executing.
    """
    iid = getattr(args, "initiative_id", None)
    if not iid:
        print("error: initiative ID required: friday graph generate <id>",
              file=sys.stderr)
        return 2
    conn = connect()
    eng = TaskGraphEngine(conn)
    try:
        g = eng.generate_from_initiative(iid)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        conn.close()
        return 2
    conn.close()
    print()
    sys.stdout.write(g.summary())
    print()
    print("This graph is a PROPOSAL — it does not execute anything.")
    print(f"Review it: friday graph review")
    print(f"Approve:    friday graph review approve {g.id.split(':')[-1]}")
    return 0


def cmd_graph_review(args: argparse.Namespace) -> int:
    """Manage graph proposals awaiting review."""
    conn = connect()
    eng = TaskGraphEngine(conn)

    action = getattr(args, "review_action", None)
    target = getattr(args, "review_target", None)

    if action == "approve" and target:
        # Resolve target: could be a short id (last segment) or full id.
        gid = _resolve_proposal_id(target, eng)
        if gid is None:
            print(f"error: no proposal found matching '{target}'", file=sys.stderr)
            conn.close()
            return 2
        conn.execute(
            "UPDATE task_graphs SET status=? WHERE id=?",
            ("approved", gid))
        conn.commit()
        print(f"Approved: {gid}")
        print("Note: this graph is approved for review only. It does not execute.")
        conn.close()
        return 0

    if action == "reject" and target:
        gid = _resolve_proposal_id(target, eng)
        if gid is None:
            print(f"error: no proposal found matching '{target}'", file=sys.stderr)
            conn.close()
            return 2
        conn.execute(
            "UPDATE task_graphs SET status=? WHERE id=?",
            ("rejected", gid))
        conn.commit()
        print(f"Rejected: {gid}")
        conn.close()
        return 0

    if action and action not in ("approve", "reject"):
        # Treat as a target ID to show detail.
        target = action

    if target:
        return _show_proposal_detail(conn, eng, target)

    # List all proposals.
    items = eng.all_graphs()
    proposals = [r for r in items if r.status == "proposal"]
    if not proposals:
        print("No graph proposals awaiting review.")
        print("Generate one: friday graph generate <initiative-id>")
        conn.close()
        return 0

    print(f"Graph proposals awaiting review — {len(proposals)}\n")
    for r in sorted(proposals, key=lambda x: x.updated_at, reverse=True):
        short_id = r.id.split(":")[-1] if ":" in r.id else r.id
        source_tag = f" | source={r.source}" if r.source else ""
        print(f"  {r.goal}")
        print(f"      id={short_id} | tasks={r.task_count} "
              f"edges={r.edge_count} | plan={r.plan_type}{source_tag}")
        print(f"      -> friday graph explain {short_id} for details")
        print()
    print("Actions:")
    print("  friday graph review <id>           Show full detail")
    print("  friday graph review approve <id>   Approve (review only, no execution)")
    print("  friday graph review reject <id>    Reject")
    conn.close()
    return 0


def _resolve_proposal_id(ref: str, eng: TaskGraphEngine) -> str:
    """Resolve a short reference to a full proposal graph id."""
    items = eng.all_graphs()
    proposals = [r for r in items if r.status == "proposal"]
    # Exact match on full id.
    for r in proposals:
        if r.id == ref:
            return r.id
    # Match on short id (last segment).
    for r in proposals:
        short = r.id.split(":")[-1] if ":" in r.id else r.id
        if short == ref:
            return r.id
    return None


def _show_proposal_detail(conn, eng: TaskGraphEngine, ref: str) -> int:
    """Show one proposal with its tasks and evidence trace."""
    items = eng.all_graphs()
    proposals = [r for r in items if r.status == "proposal"]
    matched = None
    for r in proposals:
        short = r.id.split(":")[-1] if ":" in r.id else r.id
        if r.id == ref or short == ref:
            matched = r.id
            break
    if matched is None:
        print(f"error: no proposal found: {ref}", file=sys.stderr)
        conn.close()
        return 2

    g = eng.graph_by_id(matched)
    if g is None:
        print(f"error: proposal not found: {matched}", file=sys.stderr)
        conn.close()
        return 2

    print(f"Graph Proposal: {g.id}\n")
    print(f"Initiative: {g.goal}")
    print(f"Plan:        {g.plan_id} ({g.plan_type})")
    print(f"Status:      {g.status}")
    print(f"Tasks:       {len(g.tasks)}")
    print(f"Edges:       {len(g.edges)}")
    print()
    print("Tasks (each traced to initiative evidence):")
    for t in sorted(g.tasks, key=lambda x: x.sequence):
        print(f"  {t.sequence:>2}. [{t.priority:8}] {t.task_type:13} {t.title}")
        print(f"        confidence: {t.confidence} | complexity: {t.complexity}")
        print(f"        evidence citations: {len(t.evidence)} record(s)")
        if t.evidence:
            for eid in t.evidence[:5]:
                short_eid = eid.split(":")[-1] if ":" in eid else eid
                print(f"          - {short_eid}")
            if len(t.evidence) > 5:
                print(f"          ... and {len(t.evidence) - 5} more")
        deps = t.dependencies
        if deps:
            print(f"        depends on: {', '.join(d.split('#')[-1] for d in deps)}")
        if t.acceptance_criteria:
            print(f"        acceptance: {'; '.join(t.acceptance_criteria)}")
        print()

    print("Actions:")
    print(f"  friday graph review approve {g.id.split(':')[-1]}")
    print(f"  friday graph review reject {g.id.split(':')[-1]}")
    conn.close()
    return 0


def cmd_graphs_list(args: argparse.Namespace) -> int:
    """READ: list compiled task graphs."""
    conn = connect()
    eng = TaskGraphEngine(conn)
    items = eng.all_graphs()
    conn.close()
    if not items:
        print("No task graphs compiled yet.\n")
        print('Run:\n')
        print('  friday graph "Implement OAuth"\n')
        return 0
    items = sorted(items, key=lambda r: r.updated_at, reverse=True)
    for r in items:
        print(f"  {r.id}")
        print(f"      goal={r.goal} ({r.plan_type}) tasks={r.task_count} "
              f"edges={r.edge_count} critical_path={r.critical_path_length} "
              f"parallel_groups={r.parallel_groups}")
    print(f"\nGraphs: {len(items)}")
    return 0


def cmd_graph_explain(args: argparse.Namespace) -> int:
    """READ: explain one task graph in full."""
    gid = getattr(args, "id", None) or getattr(args, "graph_id", None)
    if not gid:
        print("error: graph ID required (use --id <id> or provide as argument)",
              file=sys.stderr)
        return 2
    conn = connect()
    eng = TaskGraphEngine(conn)
    resolved, err = _resolve_graph_id(gid, eng)
    if err is not None:
        count = len(eng.all_graphs())
        print(f"error: graph index {gid} out of range (1-{count} items)",
              file=sys.stderr)
        conn.close()
        return err
    g = eng.graph_by_id(resolved)
    conn.close()
    if g is None:
        print(f"error: graph not found: {gid}", file=sys.stderr)
        return 2

    print(f"Task Graph: {g.id}\n")
    print(f"Goal:         {g.goal}")
    print(f"Plan:         {g.plan_id} ({g.plan_type})")
    print(f"Status:       {g.status}")
    print(f"Tasks:        {len(g.tasks)}")
    print(f"Edges:        {len(g.edges)}")
    print(f"Critical path:{len(g.critical_path)} tasks")
    print(f"Parallel:     {g.parallel_groups} groups, "
          f"{len(g.parallel_tasks)} tasks")

    print("\nTasks (in execution order):")
    for t in sorted(g.tasks, key=lambda x: x.sequence):
        print(f"  {t.sequence:>2}. [{t.priority:8}] {t.task_type:13} {t.title}")
        print(f"        caps: {', '.join(t.required_capabilities) or '-'} "
              f"| complexity: {t.complexity} | confidence: {t.confidence}")
        deps = t.dependencies
        if deps:
            print(f"        depends on: {', '.join(d.split('#')[-1] for d in deps)}")
        print(f"        acceptance: {'; '.join(t.acceptance_criteria)}")

    print("\nEdges (dependencies):")
    if not g.edges:
        print("  (no dependencies)")
    for e in g.edges:
        print(f"  - {e['from'].split('#')[-1]} -> {e['to'].split('#')[-1]} "
              f"({e.get('kind', 'depends_on')})")

    print("\nCritical path:")
    if g.critical_path:
        print("  " + " -> ".join(g._title(p) for p in g.critical_path))
    else:
        print("  (none)")

    print("\nParallel tasks:")
    if g.parallel_tasks:
        for tid in g.parallel_tasks:
            for t in g.tasks:
                if t.id == tid:
                    print(f"  - {t.title} ({t.task_type})")
    else:
        print("  (none)")

    print("\nPer-task verification & rollback:")
    for t in sorted(g.tasks, key=lambda x: x.sequence):
        print(f"  {t.sequence:>2}. {t.title}")
        for v in t.verification:
            print(f"        verify: {v.get('method')} — {v.get('detail','')}")
        for rb in t.rollback:
            print(f"        rollback: {rb.get('strategy')} — {rb.get('detail','')}")

    # Separate connection for the history read (mirrors cli_planning): the main
    # connection can be GC-closed by an unconsumed cursor once the long read +
    # print loop above completes, so history uses a fresh handle.
    from .db import task_history_for
    conn2 = connect()
    hist = task_history_for(conn2, g.id)
    conn2.close()
    print(f"\nHistory snapshots: {len(hist)}")
    for h in hist:
        print(f"  {h.generated_at[:19]}  tasks={h.task_count} "
              f"edges={h.edge_count}")
    return 0


def cmd_graph_export(args: argparse.Namespace) -> int:
    """READ: export the graph as Worker-Engine JSON."""
    gid = getattr(args, "id", None) or getattr(args, "graph_id", None)
    if not gid:
        print("error: graph ID required (use --id <id> or provide as argument)",
              file=sys.stderr)
        return 2
    conn = connect()
    eng = TaskGraphEngine(conn)
    resolved, err = _resolve_graph_id(gid, eng)
    if err is not None:
        count = len(eng.all_graphs())
        print(f"error: graph index {gid} out of range (1-{count} items)",
              file=sys.stderr)
        conn.close()
        return err
    g = eng.graph_by_id(resolved)
    conn.close()
    if g is None:
        print(f"error: graph not found: {gid}", file=sys.stderr)
        return 2
    print(json.dumps(g.to_json(), indent=2))
    return 0


def _join_parts(parts) -> Optional[str]:
    """Join graph_id parts back into a string (handles nargs="*" → list).

    When argparse receives positional arguments with nargs="*", they arrive as a
    list. This joins them back into a single string. If the list is empty or
    None, returns None.
    """
    if parts is None:
        return None
    if isinstance(parts, list):
        parts = " ".join(p for p in parts if p)
    parts = parts.strip()
    return parts if parts else None


def cmd_graph(args: argparse.Namespace) -> int:
    """Dispatch friday graph subcommands."""
    goal = getattr(args, "goal", None)
    action = getattr(args, "action", None)
    graph_id = _join_parts(getattr(args, "graph_id", None))
    review_action = getattr(args, "review_action", None)
    review_target = getattr(args, "review_target", None)

    # Phase 5: `graph generate <initiative-id>`
    # The initiative ID may contain spaces (e.g. "maintenance:Typescript
    # Engineering Initiative"), so it's split across action + graph_id.
    # Join them back into a single ID.
    if goal == "generate":
        parts = []
        if action:
            parts.append(action)
        if graph_id:
            parts.append(graph_id)
        args.initiative_id = " ".join(parts) if parts else ""
        return cmd_graph_generate_from_initiative(args)
    # Phase 5: `graph review [approve|reject <id>]`
    if goal == "review":
        args.review_action = action
        args.review_target = graph_id
        return cmd_graph_review(args)
    # `graph explain <id>` / `graph export <id>` / `graph list`
    if goal in ("explain", "export", "list"):
        real_id = action or graph_id or getattr(args, "id", None)
        if goal == "explain":
            args.id = real_id
            return cmd_graph_explain(args)
        elif goal == "export":
            args.id = real_id
            return cmd_graph_export(args)
        else:
            return cmd_graphs_list(args)
    if action == "explain":
        args.id = graph_id
        return cmd_graph_explain(args)
    elif action == "export":
        args.id = graph_id
        return cmd_graph_export(args)
    elif action == "list":
        return cmd_graphs_list(args)
    else:
        if goal:
            return cmd_graph_generate(args)
        return cmd_graphs_list(args)
