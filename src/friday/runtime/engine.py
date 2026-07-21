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
    get_runtime_results,
    get_runtime_session,
    get_runtime_tasks,
    insert_runtime_evolution,
    insert_runtime_result,
    insert_runtime_session,
    insert_runtime_task,
    now_iso,
    update_runtime_session,
)


def _loads(s: str):
    """Parse a JSON-list column (tasks.outputs etc.); tolerant of empty/garbage."""
    try:
        return json.loads(s) if s else []
    except (ValueError, TypeError):
        return []


def _loads_dict(s: str):
    """Parse a JSON-object column (tasks.symbolic)."""
    if not s:
        return {}
    try:
        out = json.loads(s)
        return out if isinstance(out, dict) else {}
    except (ValueError, TypeError):
        return {}
from .state import blocked_descendants, mark_cancelled
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
from .verification import verify_creation_task
from .symbolic import verify_symbolic, build_payload
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
                 max_workers: int = 8,
                 workspace: str = ".",
                 fallback: Optional[bool] = None) -> None:
        self.conn = conn
        self._max_workers = max_workers
        self._workspace = workspace
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
        # Phase 4: where/why a blocking failure stopped the mission (truthful
        # journal "stopped at" block). None until a blocking failure occurs.
        self._stopped_at: Optional[str] = None
        self._stop_reason: Optional[str] = None
        # Phase 4: a fresh database must be executable without manual seeding.
        # Runtime initialization auto-bootstraps the Worker Registry (built-in
        # + external AI adapter profiles) idempotently. Imported lazily to avoid
        # a circular import at module load time; failures never break init.
        try:
            from ..worker.engine import ensure_runtime_bootstrapped
            ensure_runtime_bootstrapped(self.conn)
        except Exception:
            pass
        if worker_resolver is not None:
            self._resolve_worker = worker_resolver
        elif workers is not None:
            self._resolve_worker = _default_worker_resolver(workers)
        else:
            # Single source of truth for execution: resolve_executor maps any
            # registry worker_id -> its adapter. Wrapped in execute_with_fallback
            # so an external executor that hangs/fails degrades to the next
            # candidate (other AI executors, then deterministic built-ins)
            # instead of terminating the mission. Never a dead end.
            base = resolve_executor

            def _resolve_with_fallback(worker_id: str) -> Optional[Worker]:
                return base(worker_id)

            self._resolve_worker = _resolve_with_fallback

        # Fallback-aware dispatcher: when the assigned worker adapter is missing
        # or the assigned worker is a non-deterministic AI executor, attempt the
        # full fallback chain. Enabled by default for the standard resolver path
        # (so execution never aborts on one external failure); can be forced on
        # or off explicitly.
        if fallback is None:
            fallback = worker_resolver is None and workers is None
        self._fallback_enabled = fallback

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
                max_workers=self._max_workers, blocked_ids=blocked_ids,
                workspace=self._workspace, fallback=self._fallback_enabled)

            # Phase 3: truthful verification. An executor exiting 0 is not
            # mission success. For any task that claimed success, verify the
            # expected artifact actually exists; if not, flip to FAILED and
            # cascade-cancel descendants. Never report success with no file.
            verification_time_ms = [0]
            states = self._reconcile_verification(
                tasks, states, list(blocked_ids), verification_time_ms)

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

        return self._report(
            session_id, schedule, states, started, finished, dur,
            verification_time_ms=verification_time_ms[0] if verification_time_ms
            else 0)

    # --- internals ---------------------------------------------------------

    def _build_tasks(self, schedule: ExecutionSchedule) -> List[RuntimeTask]:
        out: List[RuntimeTask] = []
        # Cheap read-only lookup: planning task metadata (type/title) so
        # workers (e.g. CLIWorker) can persist file artifacts correctly.
        meta = {}
        goal = ""
        try:
            for row in self.conn.execute(
                "SELECT id, task_type, title, outputs, acceptance_criteria, "
                "verification, symbolic FROM tasks"):
                meta[row["id"]] = (
                    row["task_type"] or "", row["title"] or "",
                    _loads(row["outputs"]), _loads(row["acceptance_criteria"]),
                    _loads(row["verification"]), _loads_dict(row["symbolic"]),
                )
            g = self.conn.execute(
                "SELECT goal FROM task_graphs WHERE id=?",
                (schedule.schedule_id,)).fetchone()
            if g:
                goal = g["goal"] or ""
        except Exception:
            pass
        for st in schedule.tasks:
            ttype, title, outputs, acs, ver, sym = meta.get(
                st.task_id, ("", "", [], [], [], {}))
            payload = getattr(st, "runtime_payload", "") or ""
            rt = RuntimeTask(
                execution_id=f"{schedule.schedule_id}:{st.task_id}",
                session_id="",  # filled per session at run time
                schedule_id=schedule.schedule_id,
                task_id=st.task_id,
                worker_id=st.worker_id or "",
                wave=st.wave,
                dependencies=list(st.dependencies),
                runtime_payload=payload,
                task_type=ttype,
                title=title,
                goal=goal,
                outputs=outputs,                 # explicit artifact contract
                acceptance_criteria=acs,         # success conditions
                verification=ver,                # verification steps
                symbolic=sym,
            )
            # Phase 4: translate symbolic intent into a concrete executor
            # payload using the workspace (read-only grep). Non-symbolic tasks
            # keep their existing payload. This is the Executor half of the
            # Planner= intent / Resolver= repo / Executor= work split.
            if sym:
                rt.runtime_payload = build_payload(rt, self._workspace)
            out.append(rt)
        return out

    def _blocked_origin(self, schedule: ExecutionSchedule) -> Dict[str, bool]:
        return {st.task_id: (st.status == TaskState.BLOCKED
                             or not st.worker_id)
                for st in schedule.tasks}

    def _reconcile_verification(
        self, tasks: List[RuntimeTask], states: Dict[str, RunState],
        blocked_ids: List[str], verification_time_ms: Optional[list] = None,
    ) -> Dict[str, RunState]:
        """Phase 3 truthful verification. Re-derive task states from EVIDENCE.

        For every task the executor left in SUCCESS, verify the expected
        artifact actually exists. If verification fails, flip the task to FAILED
        (with a reason naming the missing artifact) and cancel all of its
        transitive descendants. Returns the corrected state map.

        Tasks already FAILED/CANCELLED/PENDING are untouched. This is the single
        guard that prevents "Mission Complete" with no file on disk.
        """
        t0 = time.monotonic()
        by_id = {t.task_id: t for t in tasks}
        results = {r["task_id"]: r for r in
                   get_runtime_results(self.conn, self._current_session_id or "")}
        # dependents: task_id -> tasks that depend on it (reverse of deps).
        dependents: Dict[str, List[str]] = {t.task_id: [] for t in tasks}
        for t in tasks:
            for d in t.dependencies:
                dependents.setdefault(d, []).append(t.task_id)

        verified_failures: List[str] = []
        for t in tasks:
            st = states.get(t.task_id)
            r = results.get(t.task_id)
            artifacts = json.loads(r["artifacts"]) if r and r.get("artifacts") else []
            result = ExecutionResult(
                success=(st == RunState.SUCCESS),
                artifacts=artifacts or [],
                stdout=r.get("stdout", "") if r else "",
                stderr=r.get("stderr", "") if r else "",
                exit_code=r.get("exit_code") if r else None,
                duration_ms=r.get("duration_ms", 0) if r else 0,
                error=r.get("error", "") if r else "",
                verification_passed=(r.get("verification_passed")
                                     if r and r.get("verification_passed") is not None
                                     else None))
            vres = verify_symbolic(t, result, self._workspace)
            if vres is None:
                vres = verify_creation_task(t, result, self._workspace)
            result.verification_passed = vres.passed
            result.metadata = {**result.metadata,
                               "verify_evidence": vres.evidence or {},
                               "verify_reason": vres.reason}
            # Attach the structured evidence to the EXISTING result row (a
            # targeted UPDATE — never a new append-only row, never clobbers
            # started_at). Done for every task so the journal can prove the
            # verdict (test summary, git diff, symbol counts...), including
            # already-failed tasks whose proof is e.g. their test summary.
            self._attach_verification(
                t.task_id, vres.passed, vres.evidence or {},
                vres.reason if not vres.passed else "")
            if st == RunState.SUCCESS and not vres.passed:
                verified_failures.append(t.task_id)
                states[t.task_id] = RunState.FAILED
                # Flip terminal state (this path pre-dates Phase 4; the
                # execute_schedule row already exists, so we upsert it).
                self._persist(
                    t.execution_id, t.task_id, RunState.FAILED, result,
                    None, now_iso(), worker_id=t.worker_id, wave=t.wave,
                    attempt=1, error=vres.reason, reason=vres.reason)
                self._cancel_descendants(t.task_id, dependents, states, tasks)
                # Record where/why the mission stopped (blocking failure).
                self._stopped_at = t.task_id
                self._stop_reason = vres.reason

        if verification_time_ms is not None:
            verification_time_ms.append(int((time.monotonic() - t0) * 1000))
        return states

    def _attach_verification(self, task_id: str, passed: bool,
                             evidence: dict, reason: str) -> None:
        """Stamp verification verdict + evidence onto the existing result row.

        A targeted UPDATE — does NOT insert a new append-only row and does NOT
        touch started_at (so wave-ordering / row-count invariants hold).
        """
        with self._write_lock:
            self.conn.execute(
                "UPDATE runtime_results SET verification_passed=?, "
                "verification_evidence=? WHERE session_id=? AND task_id=?",
                (1 if passed else 0, json.dumps(evidence or {}),
                 self._current_session_id or "", task_id))

    def _cancel_descendants(
        self, task_id: str, dependents: Dict[str, List[str]],
        states: Dict[str, RunState], tasks: List[RuntimeTask],
    ) -> None:
        """Cancel every transitive descendant of a verified-failed task."""
        by_id = {t.task_id: t for t in tasks}
        # Use the same rule the executor uses: PENDING descendants are cancelled.
        for desc in blocked_descendants(task_id, dependents):
            cur = states.get(desc)
            if cur == RunState.PENDING:
                states[desc] = mark_cancelled(cur)
                dt = by_id.get(desc)
                if dt is not None:
                    self._persist(
                        dt.execution_id, desc, RunState.CANCELLED, None, None,
                        None, worker_id=dt.worker_id, wave=dt.wave, attempt=1,
                        reason="ancestor failed verification")

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
                # Structured verification evidence (test summary, git diff,
                # symbol counts...) rides along on the result so the journal can
                # prove the verdict. Kept off the generic stdout/stderr fields.
                verify_evidence = getattr(result, "metadata", {}).get(
                    "verify_evidence", {})
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
                    "verification_passed": result.verification_passed,
                    "verification_evidence": json.dumps(
                        verify_evidence or {}),
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

    def _report(self, session_id, schedule, states, started, finished, dur,
                verification_time_ms: int = 0) -> ExecutionReport:
        succeeded = sum(1 for s in states.values() if s == RunState.SUCCESS)
        failed = sum(1 for s in states.values() if s == RunState.FAILED)
        cancelled = sum(1 for s in states.values() if s == RunState.CANCELLED)
        rows = get_runtime_tasks(self.conn, session_id)
        # Phase 1.5: surface the executor's contract check. It lives on the
        # runtime_results row (not runtime_tasks), so join by task_id. SQLite
        # stores it as 0/1; coerce to a real bool (None stays None = unchecked).
        vp_by_task = {}
        for r in get_runtime_results(self.conn, session_id):
            raw = r.get("verification_passed")
            vp_by_task[r["task_id"]] = None if raw is None else bool(raw)
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
                "verification_passed": vp_by_task.get(r["task_id"]),
            })
        return ExecutionReport(
            session_id=session_id,
            schedule_id=schedule.schedule_id,
            state=SessionState.FINISHED.value,
            started_at=started,
            finished_at=finished,
            wave_count=schedule.wave_count,
            duration_ms=dur,
            verification_time_ms=verification_time_ms,
            stopped_at=self._stopped_at,
            stop_reason=self._stop_reason,
            executed=len(states),
            succeeded=succeeded,
            failed=failed,
            cancelled=cancelled,
            tasks=task_dicts,
            workers_used=workers_used,
            artifacts=artifacts,
        )
