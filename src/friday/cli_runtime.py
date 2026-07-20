"""CLI commands for the Execution Runtime (Milestone 9.5).

`friday runtime "<goal>"`  -> Plan -> Graph -> Resolve -> Schedule -> Runtime.
`friday runtime session`   -> list execution sessions.
`friday runtime show <id>` -> session timeline, task states, workers, duration.
`friday runtime export`    -> JSON export of all sessions/results.

This module ONLY ADDS the Runtime layer. Upstream (Plan/Graph/Resolve/Schedule)
is unchanged. The Runtime executes the schedule it is handed; it never plans,
schedules, resolves, reviews, or repairs.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Optional

from .db import connect
from .planning import TaskGraphEngine
from .resolver import CapabilityResolver
from .scheduler import TaskScheduler, CycleDetectedError, \
    MissingAssignmentError, InvalidGraphError
from .runtime import RuntimeEngine, resolve_executor, MockWorker
from .runtime.executors import BuiltinShellWorker
from .runtime.models import SCHEMA_VERSION


def cmd_runtime(args: argparse.Namespace) -> int:
    """WRITE+EXECUTE: Plan -> Graph -> Resolve -> Schedule -> Runtime(<goal>)."""
    raw = getattr(args, "goal", None)
    goal = " ".join(raw) if isinstance(raw, (list, tuple)) else (raw or "")
    if not goal.strip():
        print('error: a goal is required: friday runtime "<goal>"',
              file=sys.stderr)
        return 2
    conn = connect()

    # 1-3. Upstream pipeline (unchanged).
    graph_eng = TaskGraphEngine(conn)
    g = graph_eng.generate(goal)
    CapabilityResolver(conn).resolve_graph(g.id)
    sched = TaskScheduler(conn)
    try:
        result = sched.schedule_graph(g.id)
    except (CycleDetectedError, MissingAssignmentError, InvalidGraphError) as e:
        print(f"error: schedule rejected: {e}", file=sys.stderr)
        conn.close()
        return 1

    # 4. Execute the frozen schedule. The Runtime is generic: it receives a
    #    `Worker` per assigned task and calls execute(). For real execution we
    #    resolve each assigned worker_id to its built-in execution adapter
    #    (shell/git/file/python/testing/documentation). LLM-only ids (no
    #    adapter) resolve to None and the runtime records a clean failure
    #    rather than fabricating success. The working directory defaults to
    #    the process cwd, overridable via FRIDAY_WORKSPACE.
    import os
    workspace = os.environ.get("FRIDAY_WORKSPACE") or os.getcwd()

    def _resolve_any(wid):
        # Real execution adapter for the 6 built-in executors. LLM-only provider
        # ids (no adapter) are delegated to the shell tool, which performs real,
        # verifiable repo evidence-gathering — the actual behavior of analysis/
        # design/coordination tasks in this system. No fabricated success.
        w = resolve_executor(wid or "worker:mock", workspace=workspace)
        if w is None:
            w = BuiltinShellWorker(workspace=workspace)
        return w

    engine = RuntimeEngine(conn, worker_resolver=_resolve_any)
    report = engine.run(result.schedule)
    conn.close()

    print(f"Runtime session: {report.session_id}")
    print(f"Schedule:        {report.schedule_id}")
    print(f"Tasks executed:  {report.executed}")
    print(f"Succeeded:       {report.succeeded}")
    print(f"Failed:          {report.failed}")
    print(f"Cancelled:       {report.cancelled}")
    print(f"Duration (ms):   {report.duration_ms}")
    print(f"Workers used:    {', '.join(report.workers_used) or '(none)'}")
    return 0


def cmd_runtime_session(args: argparse.Namespace) -> int:
    """READ: list execution sessions."""
    conn = connect()
    from .db import get_runtime_sessions
    rows = get_runtime_sessions(conn)
    conn.close()
    if not rows:
        print("No runtime sessions yet.\n")
        print("Run:\n")
        print('  friday runtime "<goal>"\n')
        return 0
    print(f"Runtime sessions ({len(rows)}):\n")
    for r in rows:
        print(f"  {r['session_id']}")
        print(f"      schedule: {r['schedule_id']}")
        print(f"      state:    {r['state']}")
        print(f"      started:  {r['started_at']}")
    return 0


def cmd_runtime_show(args: argparse.Namespace) -> int:
    """READ: show one session in full."""
    ref = getattr(args, "session_id", None) or getattr(args, "id", None)
    if not ref:
        print("error: session ID required (friday runtime show <id>)",
              file=sys.stderr)
        return 2
    conn = connect()
    from .db import get_runtime_session, get_runtime_tasks, get_runtime_events
    sess = get_runtime_session(conn, ref)
    if sess is None:
        conn.close()
        print(f"error: session not found: {ref}", file=sys.stderr)
        return 2
    tasks = get_runtime_tasks(conn, ref)
    events = get_runtime_events(conn, ref)
    conn.close()

    print(f"Session: {sess['session_id']}")
    print(f"Schedule: {sess['schedule_id']}")
    print(f"State:    {sess['state']}")
    print(f"Started:  {sess['started_at']}")
    print(f"Finished: {sess['finished_at'] or '(running)'}\n")

    print("Timeline:")
    for i, t in enumerate(sorted(tasks, key=lambda r: (r["wave"], r["task_id"])), 1):
        mark = {"success": "*", "failed": "!", "cancelled": "x",
                "running": ">", "pending": "."}.get(t["status"], "?")
        print(f"  {i:>2}. [{t['wave']}] {mark} {t['task_id']}")
        print(f"        worker: {t['worker_id'] or '(none)'}")
        print(f"        status: {t['status']}  duration: {t['duration_ms']}ms")
        if t["error"]:
            print(f"        error:  {t['error']}")

    print(f"\nEvents ({len(events)}):")
    for e in events:
        extra = f" {e['task_id']}" if e["task_id"] else ""
        print(f"  {e['at']}  {e['kind']}{extra}")
    return 0


def cmd_runtime_export(args: argparse.Namespace) -> int:
    """READ: export all runtime sessions + results as JSON."""
    conn = connect()
    from .db import (get_runtime_sessions, get_runtime_tasks,
                    get_runtime_results, get_runtime_events)
    sessions = get_runtime_sessions(conn)
    data = {
        "schema_version": SCHEMA_VERSION,
        "session_count": len(sessions),
        "sessions": [],
    }
    for s in sessions:
        sid = s["session_id"]
        data["sessions"].append({
            "session": s,
            "tasks": get_runtime_tasks(conn, sid),
            "results": get_runtime_results(conn, sid),
            "events": get_runtime_events(conn, sid),
        })
    conn.close()
    print(json.dumps(data, indent=2))
    return 0


def cmd_runtime_dispatch(args: argparse.Namespace) -> int:
    """Subcommand router for `friday runtime <subcommand>`."""
    token = getattr(args, "token", None)
    if token == "session":
        return cmd_runtime_session(args)
    if token == "export":
        return cmd_runtime_export(args)
    if token in ("show", "explain"):
        args.id = getattr(args, "session_id", None)
        return cmd_runtime_show(args)
    if token:  # a session id passed positionally
        args.session_id = token
        return cmd_runtime_show(args)
    return cmd_runtime_session(args)
