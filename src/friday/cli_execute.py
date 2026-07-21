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
import json
import sys
from pathlib import Path

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
    import time as _time
    t0 = _time.monotonic()

    # Phase 4: a fresh database must be executable without manual seeding.
    # Runtime initialization auto-bootstraps the Worker Registry (built-in +
    # external AI adapter profiles) idempotently, before resolution/scheduling.
    try:
        from .worker.engine import ensure_runtime_bootstrapped
        ensure_runtime_bootstrapped(conn)
    except Exception:
        pass

    # 1. Plan the goal into a task graph.
    graph_eng = TaskGraphEngine(conn)
    g = graph_eng.generate(goal)

    # 2. Resolve every task to a worker (capability evidence, deterministic).
    #    Pass the workspace so symbolic tasks are enriched against the repo.
    resolver = CapabilityResolver(conn)
    result = resolver.resolve_graph(g.id, workspace=workspace)

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
    planner_time_ms = int((_time.monotonic() - t0) * 1000)

    def _resolve(wid: str):
        return resolve_executor(wid, workspace)

    engine = RuntimeEngine(conn, worker_resolver=_resolve, workspace=workspace,
                           fallback=True)
    report = engine.run(sched_result.schedule)
    # NOTE: keep `conn` open until the journal is built (step 5) — the journal
    # reads the runtime_tasks/runtime_results rows written above, and the
    # engine already committed them on this same connection. Closing + opening
    # a fresh default-path connection would read an empty/other database.

    # 5. Mission journal + metrics (Phase 4). Persist a structured journal and
    #    print the metrics block. No analysis — a faithful read-out.
    from .runtime.journal import build_journal, write_journal, collect_metrics, \
        format_metrics

    # Executor cooperation: the schedule already assigned each task to a worker.
    sched_tasks = getattr(sched_result, "schedule", sched_result).tasks
    executor_assignments = [
        {"task_id": t.task_id, "worker_id": t.worker_id or ""}
        for t in sched_tasks]
    graph = {
        "nodes": [t.task_id for t in sched_tasks],
        "edges": [{"from": d, "to": t.task_id}
                  for t in sched_tasks for d in t.dependencies],
    }

    journal = build_journal(
        report.session_id, conn, report, goal=goal, graph_id=g.id,
        planner_time_ms=planner_time_ms,
        verification_time_ms=report.verification_time_ms,
        graph=graph, executor_assignments=executor_assignments,
        stopped_at=report.stopped_at, stop_reason=report.stop_reason)
    journal_path = write_journal(
        journal, Path(workspace) / f"mission_journal_{report.session_id}.json")
    conn.close()
    metrics = collect_metrics(journal)
    print(format_metrics(metrics))
    # Rolling average across this workspace's mission journals.
    _print_average_metrics(workspace)
    print(f"\nMission journal: {journal_path}")

    # 6. Render with Mission Control (Rich dashboard if interactive TTY,
    #    plain text otherwise).
    return _render_report(report, goal)


def _print_average_metrics(workspace: str) -> None:
    """Print a rolling average of execution metrics over prior mission journals
    in the workspace (no LLM, no network — just a faithful aggregate)."""
    try:
        journals = []
        for p in Path(workspace).glob("mission_journal_*.json"):
            try:
                journals.append(json.loads(p.read_text(encoding="utf-8")))
            except Exception:
                continue
        if len(journals) <= 1:
            return
        def _avg(key):
            vals = [j.get("summary", {}).get("execution_time_ms", 0)
                    for j in journals if "summary" in j]
            return sum(vals) / len(vals) if vals else 0
        completed = sum(1 for j in journals
                        if j.get("summary", {}).get("completed"))
        print(
            f"\nRolling average over {len(journals)} missions: "
            f"execution={_avg('execution_time_ms'):.0f} ms, "
            f"completion={completed}/{len(journals)}")
    except Exception:
        pass

