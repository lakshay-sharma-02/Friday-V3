"""Mission journal + execution metrics (Phase 4, execution layer).

After a mission runs, the runtime has all the evidence in the database
(session, tasks, results, history). This module assembles it into a single
structured journal: mission goal, generated graph, executor assignments,
per-task retries / evidence / verification / failures, and a completion summary.
No analysis, no LLM — a faithful read-out of what actually happened.

Metrics are derived from the same persisted rows so they always match the
journal (no separate counters that can drift).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from .models import ExecutionReport, RunState, SessionState


def build_journal(session_id: str, conn, report: ExecutionReport,
                  goal: str = "", graph_id: str = "",
                  planner_time_ms: int = 0, verification_time_ms: int = 0,
                  graph: Optional[dict] = None,
                  executor_assignments: Optional[list] = None,
                  stopped_at: Optional[str] = None,
                  stop_reason: Optional[str] = None) -> dict:
    """Assemble a structured mission journal from persisted execution rows."""
    tasks = conn.execute(
        "SELECT task_id, worker_id, status, wave, attempt, duration_ms, "
        "exit_code, error FROM runtime_tasks WHERE session_id=? "
        "ORDER BY wave, task_id",
        (session_id,)).fetchall()
    results = conn.execute(
        "SELECT task_id, success, stdout, stderr, artifacts, "
        "verification_passed, verification_evidence FROM runtime_results "
        "WHERE session_id=?",
        (session_id,)).fetchall()

    res_by_task: Dict[str, dict] = {}
    for r in results:
        res_by_task[r["task_id"]] = {
            "success": bool(r["success"]),
            "verification_passed": (None if r["verification_passed"] is None
                                    else bool(r["verification_passed"])),
            "verification_evidence": (json.loads(r["verification_evidence"])
                                      if r["verification_evidence"] else {}),
            "artifacts": json.loads(r["artifacts"]) if r["artifacts"] else [],
            "stdout": (r["stdout"] or "")[:2000],
            "stderr": (r["stderr"] or "")[:2000],
        }

    task_entries = []
    for t in tasks:
        tid = t["task_id"]
        res = res_by_task.get(tid, {})
        task_entries.append({
            "task_id": tid,
            "worker_id": t["worker_id"],
            "wave": t["wave"],
            "status": t["status"],
            "attempts": t["attempt"] or 1,
            "duration_ms": t["duration_ms"] or 0,
            "exit_code": t["exit_code"],
            "error": t["error"] or "",
            "verification_passed": res.get("verification_passed"),
            "artifacts": res.get("artifacts", []),
            "evidence": res.get("verification_evidence") or _evidence_for(t, res),
        })

    failed = [e for e in task_entries if e["status"] == RunState.FAILED.value]
    retried = [e for e in task_entries if e["attempts"] and e["attempts"] > 1]
    verified_fail = [e for e in task_entries
                     if e["verification_passed"] is False]

    completed = (len(failed) == 0 and report.state
                 == SessionState.FINISHED.value and report.failed == 0)

    journal = {
        "schema_version": "1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "session_id": session_id,
        "graph_id": graph_id,
        "mission": goal,
        "planner_time_ms": planner_time_ms,
        "execution_time_ms": report.duration_ms,
        "verification_time_ms": verification_time_ms,
        "summary": {
            "completed": completed,
            "tasks_total": len(task_entries),
            "succeeded": report.succeeded,
            "failed": report.failed,
            "cancelled": report.cancelled,
            "retried": len(retried),
            "verification_failures": len(verified_fail),
            "workers_used": report.workers_used,
            "stopped_at": stopped_at or report.stopped_at,
            "stop_reason": stop_reason or report.stop_reason,
        },
        "executor_assignments": executor_assignments
        or [{"task_id": e["task_id"], "worker_id": e["worker_id"]}
            for e in task_entries],
        "graph": graph or {"nodes": [e["task_id"] for e in task_entries],
                           "edges": []},
        "tasks": task_entries,
        "failures": [
            {"task_id": e["task_id"], "worker_id": e["worker_id"],
             "error": e["error"], "evidence": e["evidence"]}
            for e in failed
        ],
    }
    return journal


def _evidence_for(task_row, res: dict) -> dict:
    """Extract the evidence a task produced (or why it failed)."""
    if res.get("artifacts"):
        return {"artifacts": res["artifacts"]}
    if res.get("verification_passed") is False:
        return {"verification": "failed", "stderr": res.get("stderr", "")}
    if not (task_row["status"] == RunState.SUCCESS.value):
        return {"error": task_row["error"] or "no evidence",
                "stderr": res.get("stderr", "")}
    return {"exit_code": res.get("exit_code", 0)}


def write_journal(journal: dict, path: str) -> str:
    """Write the journal as JSON; return the path written."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(journal, indent=2), encoding="utf-8")
    return str(p)


def collect_metrics(journal: dict) -> dict:
    """Derive execution-quality metrics from the journal (single source)."""
    s = journal["summary"]
    return {
        "planner_time_ms": journal.get("planner_time_ms", 0),
        "execution_time_ms": journal.get("execution_time_ms", 0),
        "verification_time_ms": journal.get("verification_time_ms", 0),
        "retry_count": s["retried"],
        "executor_failures": s["failed"],
        "verification_failures": s["verification_failures"],
        "missions_completed": 1 if s["completed"] else 0,
        "missions_failed": 0 if s["completed"] else 1,
        "tasks_total": s["tasks_total"],
        "tasks_succeeded": s["succeeded"],
        "tasks_cancelled": s["cancelled"],
    }


def format_metrics(metrics: dict) -> str:
    """Human-readable one-block metrics summary."""
    return (
        "Mission metrics:\n"
        f"  planner_time:      {metrics['planner_time_ms']} ms\n"
        f"  execution_time:    {metrics['execution_time_ms']} ms\n"
        f"  verification_time: {metrics['verification_time_ms']} ms\n"
        f"  retry_count:       {metrics['retry_count']}\n"
        f"  executor_failures: {metrics['executor_failures']}\n"
        f"  verification_fail: {metrics['verification_failures']}\n"
        f"  missions_completed:{metrics['missions_completed']}\n"
        f"  missions_failed:   {metrics['missions_failed']}\n"
        f"  tasks:             {metrics['tasks_succeeded']}/{metrics['tasks_total']}"
        f" succeeded"
        + (f", {metrics['tasks_cancelled']} cancelled"
           if metrics['tasks_cancelled'] else "")
    )
