"""friday execute "<goal>" — orchestrate plan -> resolve -> schedule -> run.

Thin glue command. Composes EXISTING engines (no new subsystem):
  TaskGraphEngine  (M9.0)  plan a goal into a task graph
  CapabilityResolver (M9.3) assign each task to a worker
  Scheduler        (M9.4)  build a deterministic execution schedule
  RuntimeEngine    (M9.5)  execute the schedule (worker.execute + verify)
  resolve_worker   (M10)   map a registry worker_id to its execution adapter

Friday stays the orchestrator. External AI workers are selected by the
resolver on capability evidence and executed via their adapters; their output
is verified by the dispatcher before being reported.
"""

from __future__ import annotations

import argparse
import sys

from .db import connect
from .planning import TaskGraphEngine
from .resolver import CapabilityResolver
from .scheduler.engine import TaskScheduler
from .runtime import RuntimeEngine, resolve_executor


def cmd_execute(args: argparse.Namespace, conn=None) -> int:
    """Plan a goal, resolve workers, schedule, and run it end-to-end."""
    raw = getattr(args, "goal", None)
    goal = " ".join(raw) if isinstance(raw, (list, tuple)) else (raw or "")
    if not goal.strip():
        print('error: a goal is required: friday execute "<goal>"',
              file=sys.stderr)
        return 2

    workspace = getattr(args, "workspace", None) or "."
    conn = conn or connect()

    # 1. Plan the goal into a task graph.
    graph_eng = TaskGraphEngine(conn)
    g = graph_eng.generate(goal)

    # 2. Resolve every task to a worker (capability evidence, deterministic).
    resolver = CapabilityResolver(conn)
    result = resolver.resolve_graph(g.id)

    # 3. Schedule (waves + state) for the resolved graph.
    scheduler = TaskScheduler(conn)
    try:
        sched_result = scheduler.schedule_graph(g.id)
    except Exception as e:  # cycle / missing assignment / invalid graph
        print(f"error: scheduling failed: {e}", file=sys.stderr)
        conn.close()
        return 2

    # 4. Execute via the Runtime. Executor adapters resolved lazily from the
    #    registry id -> execution adapter (native + external M10 adapters).
    def _resolve(wid: str):
        return resolve_executor(wid, workspace)

    engine = RuntimeEngine(conn, worker_resolver=_resolve)
    report = engine.run(sched_result.schedule)
    conn.close()

    # 5. Report.
    print(f"Goal:        {goal}")
    print(f"Graph:       {g.id}")
    print(f"Session:     {report.session_id}")
    print(f"Tasks:       {report.executed}")
    print(f"Success:     {report.succeeded}")
    print(f"Failed:      {report.failed}")
    print(f"Duration:    {report.duration_ms / 1000:.1f}s")
    if report.failed:
        print("\nFailures:")
        for t in report.tasks:
            if t.get("status") == "failed":
                print(f"  - {t.get('task_id')}: {t.get('error') or 'execution failed'}")
    return 0 if report.failed == 0 else 1
