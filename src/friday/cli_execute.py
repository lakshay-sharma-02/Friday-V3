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


def _render_report(report, goal: str) -> int:
    """Render execution report with Mission Control UI or plain text."""
    from .presentation.formatters.execution import execution_result_to_view, task_to_worker_view
    from .presentation.renderers.execution import render_execution_summary

    workers = [
        task_to_worker_view(
            worker_id=t.get("task_id", ""),
            name=(t.get("worker_id") or "unknown").replace("worker:", "").title(),
            status=t.get("status", "pending"),
            current_task=t.get("task_id", ""),
        )
        for t in report.tasks[:10]
    ]

    phase = "complete" if report.failed == 0 else "implementation"
    progress = 1.0 if report.failed == 0 else 0.0

    mv = execution_result_to_view(
        mission_id=report.session_id,
        goal=goal,
        phase=phase,
        progress=progress,
        workers=workers,
        elapsed_seconds=int(report.duration_ms / 1000),
    )

    use_rich = _try_rich_import()
    if use_rich and report.duration_ms > 500:
        from rich.console import Console
        from rich.live import Live
        from .presentation.renderers.mission import render_mission_view

        console = Console()
        with Live(render_mission_view(mv), refresh_per_second=15,
                  console=console):
            import time
            time.sleep(0.3)
        console.print(render_execution_summary(mv))
    elif use_rich:
        from rich.console import Console
        from .presentation.renderers.mission import render_mission_view

        Console().print(render_mission_view(mv))
        Console().print(render_execution_summary(mv))
    else:
        print(f"Goal:        {goal}")
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


def _try_rich_import() -> bool:
    """Check if Rich is available and stdout is a TTY."""
    import sys
    if not sys.stdout.isatty():
        return False
    try:
        import rich  # noqa: F401
        return True
    except ImportError:
        return False


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

    # 5. Render with Mission Control (Rich dashboard if interactive TTY,
    #    plain text otherwise).
    return _render_report(report, goal)
