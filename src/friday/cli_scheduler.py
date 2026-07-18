"""CLI commands for the Task Scheduler (Milestone 9.4).

`friday schedule "<goal>"` -> Plan -> Task Graph -> Resolve -> Schedule.
`friday scheduler`          -> list schedules (one run record per graph).
`friday scheduler explain <id>` -> waves, dependencies, worker, priority,
                                  critical path, blocked reason.
`friday scheduler export`       -> JSON export of all scheduled tasks.

This module only ADDS a layer. Task Graph, Worker Registry, Capability Resolver
are unchanged. No execution, scheduling of worker timing, or worker invocation.
"""

from __future__ import annotations

import argparse
import json
import sys

from .db import connect
from .planning import TaskGraphEngine
from .resolver import CapabilityResolver
from .scheduler import TaskScheduler, CycleDetectedError, \
    MissingAssignmentError, InvalidGraphError
from .scheduler.timeline import build_timeline, wave_summary, critical_path_status


def _resolve_schedule_ref(ref: str, sched: TaskScheduler):
    """Resolve a reference: run_id, graph_id, or INTEGER = Nth newest run."""
    if ref.isdigit():
        n = int(ref)
        runs = sched.runs()
        if 1 <= n <= len(runs):
            return runs[n - 1]["graph_id"], None
        return None, 2
    # Try as graph_id (returns its tasks).
    tasks = sched.tasks(ref)
    if tasks:
        return ref, None
    # Try as run_id.
    for r in sched.runs():
        if r["run_id"] == ref:
            return r["graph_id"], None
    return None, 3


def cmd_schedule(args: argparse.Namespace) -> int:
    """WRITE: Plan -> Task Graph -> Assignments -> Schedule for a goal."""
    raw = getattr(args, "goal", None)
    goal = " ".join(raw) if isinstance(raw, (list, tuple)) else (raw or "")
    if not goal.strip():
        print('error: a goal is required: friday schedule "<goal>"',
              file=sys.stderr)
        return 2
    conn = connect()

    # 1. Derive plan + compile task graph (reuses existing engine).
    graph_eng = TaskGraphEngine(conn)
    g = graph_eng.generate(goal)

    # 2. Resolve every task to a worker (existing resolver).
    resolver = CapabilityResolver(conn)
    resolver.resolve_graph(g.id)

    # 3. Schedule execution ordering (new).
    sched = TaskScheduler(conn)
    try:
        result = sched.schedule_graph(g.id)
    except (CycleDetectedError, MissingAssignmentError, InvalidGraphError) as e:
        print(f"error: schedule rejected: {e}", file=sys.stderr)
        conn.close()
        return 1

    conn.close()

    print(f"Schedule: {g.id}\n")
    print(f"Goal:           {g.goal}")
    print(f"Tasks:          {result.schedule.task_count}")
    print(f"Waves:          {result.schedule.wave_count}")
    print(f"Critical path:  {result.schedule.critical_path_length}")
    print(f"Max parallelism:{result.schedule.max_parallelism}")
    print(f"Blocked:        {len(result.blocked)}")
    print(f"Run:            {result.run_id}\n")

    for w in wave_summary(result.schedule):
        members = ", ".join(
            f"{t}({w['worker_ids'][i] or '-'})"
            for i, t in enumerate(w["task_ids"]))
        print(f"  Wave {w['wave']} [{w['count']}]: {members}")
    if result.blocked:
        print(f"\n  BLOCKED: {', '.join(result.blocked)}")
    return 0


def cmd_scheduler_list(args: argparse.Namespace) -> int:
    """READ: list scheduler runs."""
    conn = connect()
    sched = TaskScheduler(conn)
    runs = sched.runs()
    conn.close()
    if not runs:
        print("No schedules yet.\n")
        print("Run:\n")
        print('  friday schedule "<goal>"\n')
        return 0
    print(f"Scheduler runs ({len(runs)}):\n")
    for r in runs:
        print(f"  {r['run_id']}")
        print(f"      graph:   {r['graph_id']}")
        print(f"      waves:   {r['wave_count']}  tasks: {r['task_count']}")
        print(f"      status:  {r['status']}")
    return 0


def cmd_scheduler_explain(args: argparse.Namespace) -> int:
    """READ: explain one schedule in full."""
    ref = getattr(args, "id", None) or getattr(args, "schedule_id", None)
    if not ref:
        print("error: schedule ID required (use --id <id> or provide as argument)",
              file=sys.stderr)
        return 2
    conn = connect()
    sched = TaskScheduler(conn)
    graph_id, err = _resolve_schedule_ref(ref, sched)
    if err is not None:
        conn.close()
        print(f"error: schedule not found: {ref}", file=sys.stderr)
        return 2

    tasks = sched.tasks(graph_id)
    if not tasks:
        conn.close()
        print(f"error: no scheduled tasks for: {graph_id}", file=sys.stderr)
        return 2

    # Rebuild a lightweight ExecutionSchedule view from DB rows.
    from .scheduler.timeline import build_timeline, wave_summary, \
        critical_path_status
    from .scheduler.models import ScheduledTask, TaskState, ExecutionSchedule

    sts = []
    for row in tasks:
        sts.append(ScheduledTask(
            schedule_id=row["schedule_id"], graph_id=row["graph_id"],
            assignment_id=row["assignment_id"], task_id=row["task_id"],
            worker_id=row["worker_id"], phase=row["phase"],
            wave=row["wave"], status=TaskState(row["status"]),
            priority=row["priority"],
            dependency_count=row["dependency_count"],
            estimated_start=row["estimated_start"],
            estimated_finish=row["estimated_finish"],
            blocked_reason=row["blocked_reason"],
            confidence=row["confidence"],
            selection_strategy=row["selection_strategy"],
            schema_version=row["schema_version"],
            created_at=row["created_at"], updated_at=row["updated_at"],
        ))

    g_row = conn.execute(
        "SELECT goal, critical_path_length FROM task_graphs WHERE id = ?",
        (graph_id,)).fetchone()
    cp = [r["task_id"] for r in conn.execute(
        "SELECT task_id FROM tasks WHERE graph_id = ? ORDER BY sequence",
        (graph_id,)).fetchall()]

    schedule = ExecutionSchedule(
        schedule_id=graph_id, graph_id=graph_id,
        goal=g_row["goal"] if g_row else "",
        wave_count=max((t.wave for t in sts), default=0),
        critical_path=cp,
        tasks=sts,
    )

    print(f"Schedule: {graph_id}\n")
    print(f"Goal:            {schedule.goal}")
    print(f"Waves:           {schedule.wave_count}")
    print(f"Tasks:           {len(sts)}\n")

    print("Timeline:")
    for step in build_timeline(schedule):
        mark = "*" if step["status"] == "ready" else \
               "!" if step["status"] == "blocked" else "."
        print(f"  {step['order']:>2}. [W{step['wave']}] {mark} {step['task_id']}")
        print(f"        worker: {step['worker_id'] or '(none)'}")
        print(f"        status: {step['status']}  priority: {step['priority']}")
        if step["blocked_reason"]:
            print(f"        blocked: {step['blocked_reason']}")

    cps = critical_path_status(schedule)
    print(f"\nCritical path ({cps['critical_path_length']}): "
          f"{' -> '.join(cps['critical_path'])}")
    if cps["blocked_on_critical_path"]:
        print(f"  BLOCKED on critical path: "
              f"{', '.join(cps['blocked_on_critical_path'])}")

    conn.close()
    return 0


def cmd_scheduler_export(args: argparse.Namespace) -> int:
    """READ: export all scheduled tasks as JSON."""
    conn = connect()
    sched = TaskScheduler(conn)
    tasks = sched.tasks()
    runs = sched.runs()
    conn.close()
    data = {
        "schema_version": "1.0",
        "schedule_count": len(tasks),
        "run_count": len(runs),
        "runs": runs,
        "tasks": tasks,
    }
    print(json.dumps(data, indent=2))
    return 0


def cmd_scheduler(args: argparse.Namespace) -> int:
    """Dispatch friday scheduler subcommands."""
    token = getattr(args, "token", None)
    if token == "export":
        return cmd_scheduler_export(args)
    if token == "explain":
        args.id = getattr(args, "schedule_id", None)
        return cmd_scheduler_explain(args)
    if token:
        args.id = token
        return cmd_scheduler_explain(args)
    return cmd_scheduler_list(args)
