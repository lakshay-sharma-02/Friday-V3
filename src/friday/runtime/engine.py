"""Runtime engine (Milestone 9.5).

The execution orchestrator. It:
  - takes a frozen `ExecutionSchedule` (from M9.4),
  - builds `RuntimeTask`s (one per scheduled task),
  - opens a session, records events, runs waves via the executor,
  - persists every state transition (runtime_tasks / runtime_results /
    runtime_history / runtime_evolution — all append-only where required),
  - returns an `ExecutionReport` (outcomes only, no analysis).

The engine NEVER plans, schedules, resolves capabilities, reviews, repairs, or
learns. It executes the schedule it is given, in wave order, and stops the
invalid dependency chain when a task fails.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from typing import Callable, Dict, List, Optional

from ..db import (
    atomic,
    get_runtime_session,
    get_runtime_tasks,
    insert_runtime_evolution,
    insert_runtime_result,
    insert_runtime_session,
    insert_runtime_task,
    now_iso,
    update_runtime_session,
)
from ..scheduler.models import ExecutionSchedule, ScheduledTask, TaskState
from .dispatcher import WorkerResolver, _default_worker_resolver
from .events import (
    session_finished,
    session_started,
    task_failed,
    task_finished,
    task_started,
)
from .executor import execute_schedule
from .history import snapshot
from .executors import resolve_executor
from .models import (
    ExecutionReport,
    ExecutionResult,
    RunState,
    RuntimeTask,
    SCHEMA_VERSION as RT_SCHEMA_VERSION,
    SessionState,
    Worker,
)


def _session_id() -> str:
    return f"sess:{uuid.uuid4().hex}"


class RuntimeEngine:
    """Executes one ExecutionSchedule into a session. Stateless across calls."""

    def __init__(self, conn,
                 worker_resolver: Optional[WorkerResolver] = None,
                 workers: Optional[Dict[str, Worker]] = None,
                 max_workers: int = 8) -> None:
        self.conn = conn
        self._max_workers = max_workers
        self._current_session_id: Optional[str] = None
        # Parallel waves run tasks in worker threads. The real work (worker
        # execution) happens concurrently; only the DB bookkeeping is serialized
        # via this lock. We relax sqlite's thread check on the shared connection
        # so threads may write (serially, under the lock).
        try:
            self.conn.check_same_thread = False
        except Exception:
            pass
        self._write_lock = threading.Lock()
        if worker_resolver is not None:
            self._resolve_worker = worker_resolver
        elif workers is not None:
            self._resolve_worker = _default_worker_resolver(workers)
        else:
            # Single source of truth for execution: resolve_executor maps any
            # registry worker_id -> its adapter. Never a dead end.
            self._resolve_worker = resolve_executor

    # --- public ------------------------------------------------------------

    def run(self, schedule: ExecutionSchedule) -> ExecutionReport:
        """Execute the schedule. Returns an ExecutionReport (no analysis)."""
        tasks = self._build_tasks(schedule)
        session_id = _session_id()
        self._current_session_id = session_id
        started = now_iso()
        t0 = time.monotonic()

        with atomic(self.conn):
            insert_runtime_session(self.conn, {
                "session_id": session_id,
                "schedule_id": schedule.schedule_id,
                "state": SessionState.RUNNING.value,
                "started_at": started,
                "finished_at": None,
                "schema_version": RT_SCHEMA_VERSION,
                "created_at": started,
                "updated_at": started,
            })
            session_started(self.conn, session_id, started)

            # Seed PENDING rows for every task.
            blocked = self._blocked_origin(schedule)
            blocked_ids = {tid for tid in blocked if blocked[tid]}
            for rt in tasks:
                init = RunState.CANCELLED if rt.task_id in blocked_ids \
                    else RunState.PENDING
                self._persist(rt.execution_id, rt.task_id, init,
                              None, None, None, worker_id=rt.worker_id,
                              wave=rt.wave, attempt=1,
                              reason=("blocked at schedule time"
                                      if rt.task_id in blocked_ids else ""))

            states = execute_schedule(
                tasks, self._resolve_worker, self._persist,
                max_workers=self._max_workers, blocked_ids=blocked_ids)

        finished = now_iso()
        dur = int((time.monotonic() - t0) * 1000)
        with atomic(self.conn):
            update_runtime_session(self.conn, {
                "session_id": session_id,
                "state": SessionState.FINISHED.value,
                "finished_at": finished,
                "updated_at": finished,
            })
            session_finished(self.conn, session_id, finished)

        return self._report(session_id, schedule, states, started, finished, dur)

    # --- internals ---------------------------------------------------------

    def _build_tasks(self, schedule: ExecutionSchedule) -> List[RuntimeTask]:
        out: List[RuntimeTask] = []
        # Cheap read-only lookup: planning task metadata (type/title) so
        # workers (e.g. CLIWorker) can persist file artifacts correctly.
        meta = {}
        goal = ""
        try:
            for row in self.conn.execute(
                "SELECT id, task_type, title FROM tasks"):
                meta[row["id"]] = (row["task_type"] or "", row["title"] or "")
            g = self.conn.execute(
                "SELECT goal FROM task_graphs WHERE id=?",
                (schedule.schedule_id,)).fetchone()
            if g:
                goal = g["goal"] or ""
        except Exception:
            pass
        for st in schedule.tasks:
            ttype, title = meta.get(st.task_id, ("", ""))
            out.append(RuntimeTask(
                execution_id=f"{schedule.schedule_id}:{st.task_id}",
                session_id="",  # filled per session at run time
                schedule_id=schedule.schedule_id,
                task_id=st.task_id,
                worker_id=st.worker_id or "",
                wave=st.wave,
                dependencies=list(st.dependencies),
                runtime_payload=getattr(st, "runtime_payload", "") or "",
                task_type=ttype,
                title=title,
                goal=goal,
            ))
        return out

    def _blocked_origin(self, schedule: ExecutionSchedule) -> Dict[str, bool]:
        return {st.task_id: (st.status == TaskState.BLOCKED
                             or not st.worker_id)
                for st in schedule.tasks}

    def _persist(self, execution_id: str, task_id: str, state: RunState,
                 result: Optional[ExecutionResult], started_at, finished_at,
                 *, worker_id: str = "", wave: int = 1, attempt: int = 1,
                 exit_code=None, error: str = "", stdout: str = "",
                 stderr: str = "", artifacts=None, duration_ms: int = 0,
                 reason: str = "") -> None:
        """Single persistence entrypoint used by the executor.

        Writes the latest runtime_tasks row, an append-only runtime_results row
        (when a result exists), and a runtime_history snapshot. Records
        evolution only when the state actually changes.

        Uses a shared connection with write serialization so parallel wave
        execution is safe; the concurrent part is the worker execution itself.
        """
        c = self.conn
        now = now_iso()
        # Derive schedule_id + session_id from execution_id ("<graph>:<task>").
        schedule_id, _, tid = execution_id.partition(":")
        session_id = self._current_session_id or ""

        with self._write_lock:
            # Append-only result row when we have an outcome.
            if result is not None:
                insert_runtime_result(c, {
                    "execution_id": execution_id,
                    "session_id": session_id,
                    "task_id": task_id,
                    "worker_id": worker_id or None,
                    "success": result.success,
                    "stdout": stdout or result.stdout,
                    "stderr": stderr or result.stderr,
                    "artifacts": json.dumps(
                        list(artifacts or result.artifacts or [])),
                    "exit_code": exit_code
                    if exit_code is not None else result.exit_code,
                    "duration_ms": duration_ms or result.duration_ms,
                    "error": error or result.error,
                    "recorded_at": now,
                })

            insert_runtime_task(c, {
                "execution_id": execution_id,
                "session_id": session_id,
                "schedule_id": schedule_id,
                "task_id": task_id,
                "worker_id": worker_id or None,
                "wave": wave,
                "attempt": attempt,
                "status": state.value,
                "started_at": started_at,
                "finished_at": finished_at,
                "duration_ms": duration_ms,
                "exit_code": exit_code,
                "error": error,
                "output_reference": "",
                "schema_version": RT_SCHEMA_VERSION,
                "created_at": now,
                "updated_at": now,
            })
            snapshot(c, session_id=session_id, schedule_id=schedule_id,
                     task_id=task_id, worker_id=worker_id or None,
                     status=state.value, attempt=attempt, at=now)

            # Event + evolution for meaningful transitions.
            if state == RunState.RUNNING:
                task_started(c, session_id, task_id, worker_id, now)
            elif state == RunState.SUCCESS:
                task_finished(c, session_id, task_id, worker_id, now)
            elif state == RunState.FAILED:
                task_failed(c, session_id, task_id, worker_id, now)
            insert_runtime_evolution(c, {
                "evolved_at": now,
                "session_id": session_id,
                "task_id": task_id,
                "from_state": None,
                "to_state": state.value,
                "change_type": "state",
                "reason": reason or "",
            })

    def _report(self, session_id, schedule, states, started, finished, dur
                ) -> ExecutionReport:
        succeeded = sum(1 for s in states.values() if s == RunState.SUCCESS)
        failed = sum(1 for s in states.values() if s == RunState.FAILED)
        cancelled = sum(1 for s in states.values() if s == RunState.CANCELLED)
        rows = get_runtime_tasks(self.conn, session_id)
        workers_used = sorted({r["worker_id"] for r in rows if r["worker_id"]})
        artifacts: List[str] = []
        task_dicts = []
        for r in rows:
            task_dicts.append({
                "task_id": r["task_id"],
                "worker_id": r["worker_id"],
                "status": r["status"],
                "wave": r["wave"],
                "duration_ms": r["duration_ms"],
                "exit_code": r["exit_code"],
                "error": r["error"],
            })
        return ExecutionReport(
            session_id=session_id,
            schedule_id=schedule.schedule_id,
            state=SessionState.FINISHED.value,
            started_at=started,
            finished_at=finished,
            wave_count=schedule.wave_count,
            duration_ms=dur,
            executed=len(states),
            succeeded=succeeded,
            failed=failed,
            cancelled=cancelled,
            tasks=task_dicts,
            workers_used=workers_used,
            artifacts=artifacts,
        )
