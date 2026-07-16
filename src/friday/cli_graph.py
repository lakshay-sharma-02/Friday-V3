"""CLI commands for the Task Graph Compiler (Milestone 9.1).

`friday graph "<goal>"`  -> derive Plan (frozen PlanEngine) then compile +
persist + show a Task Graph summary.
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

from .db import connect
from .planning import TaskGraphEngine


def _resolve_graph_id(gid: str, eng: TaskGraphEngine):
    """Resolve a reference: full deterministic id, or INTEGER = Nth newest."""
    if gid.isdigit():
        n = int(gid)
        ordered = sorted(eng.all_graphs(), key=lambda r: r.created_at,
                         reverse=True)
        if 1 <= n <= len(ordered):
            return ordered[n - 1].id, None
        return None, 2
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


def cmd_graph(args: argparse.Namespace) -> int:
    """Dispatch friday graph subcommands."""
    goal = getattr(args, "goal", None)
    action = getattr(args, "action", None)
    graph_id = getattr(args, "graph_id", None)
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
