"""Wave executor (Milestone 9.5).

Executes one `ExecutionSchedule` wave-by-wave. Within a wave, tasks run in
parallel (thread pool). The executor waits for the whole wave to finish before
proceeding to the next. If a task fails, its transitive descendants are
CANCELLED (never executed) — the dependency chain is not continued.

The executor does NOT:
  - plan, schedule, or resolve capabilities (upstream owns those),
  - retry DETERMINISTIC failures (a linter exit 1 on bad code is final),
  - review or accept work,
  - learn from outcomes.

It DOES retry transient failures (timeouts, rate limits, dropped connections)
a bounded number of times (MAX_ATTEMPTS), and distinguishes BLOCKING failures
(mission stops, dependents cancelled) from NON-blocking failures (e.g. a
formatter/linter) that let the mission continue.

It only: runs READY tasks in wave order, records state, and stops the invalid
portion of the graph on failure.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, List, Optional

from ..db import now_iso
from .dispatcher import dispatch
from .models import RunState, RuntimeTask, Worker
from .executors import execute_with_fallback, _is_ai_executor_id
from .state import (
    blocked_descendants,
    can_transition,
    mark_cancelled,
    next_state_for_result,
)


# A callback the engine supplies to persist a task-state change.
# signature: (execution_id, task_id, state: RunState, result: Optional[ExecutionResult], started_at, finished_at)
PersistFn = Callable[..., None]

# Bounded retry for a single task. Phases 1-3 deliberately ran each task once;
# Phase 4 requires deterministic recovery: a transient failure (timeout, rate
# limit, dropped connection) is retried a few times, a deterministic logic
# failure (linter exit 1 on bad code) is NOT retried pointlessly.
MAX_ATTEMPTS = 3

# Task types whose failure is NON-BLOCKING: a formatter/linter/style step (or
# an advisory AI review) that fails after retries must not abort the whole
# mission. The engineering change itself (rename/test/extract) is what defines
# success; an unavailable/refusing AI reviewer is recorded truthfully in the
# journal but does not cancel the dependency chain. Everything else is BLOCKING
# — its failure cancels the dependency chain (mission stops there).
_NON_BLOCKING_TYPES = frozenset({"configuration", "cleanup", "review"})

# Transient failure signatures worth retrying. Deterministic failures (exit 1
# from a linter over genuinely bad code) are intentionally NOT here.
_TRANSIENT = (
    "timed out", "timeout", "rate limit", "rate-limit", "429", "503",
    "504", "connection reset", "connection refused", "network", "try again",
    "temporarily unavailable", "econnreset", "etimedout",
)


def _is_recoverable(result) -> bool:
    """True if the failure is transient and worth retrying."""
    if result is None or result.success:
        return False
    blob = " ".join(str(x) for x in (
        result.error or "", result.stderr or "", getattr(result, "metadata", {})
        .get("verify_reason", ""))).lower()
    return any(tok in blob for tok in _TRANSIENT)


def _is_non_blocking(task_type: str) -> bool:
    """A non-blocking failure lets the mission continue past it."""
    return (task_type or "").lower() in _NON_BLOCKING_TYPES


def _dependents_by_task(tasks: List[RuntimeTask]) -> Dict[str, List[str]]:
    deps: Dict[str, List[str]] = {t.task_id: [] for t in tasks}
    for t in tasks:
        for d in t.dependencies:
            deps.setdefault(d, []).append(t.task_id)
    return deps


def execute_schedule(
    tasks: List[RuntimeTask],
    worker_resolver: Callable[[str], Optional[Worker]],
    persist: PersistFn,
    max_workers: int = 8,
    blocked_ids: Optional[set] = None,
    workspace: str = ".",
    fallback: bool = False,
) -> Dict[str, RunState]:
    """Run all tasks respecting waves + dependencies. Returns task_id -> state.

    `persist` is called on every state transition so the engine owns storage.
    `blocked_ids` are tasks the Scheduler already BLOCKED (no worker); they start
    CANCELLED and are never executed — the Runtime never re-assigns them.
    """
    blocked_ids = blocked_ids or set()
    states: Dict[str, RunState] = {
        t.task_id: (RunState.CANCELLED if t.task_id in blocked_ids
                    else RunState.PENDING)
        for t in tasks
    }
    by_id: Dict[str, RuntimeTask] = {t.task_id: t for t in tasks}
    dependents = _dependents_by_task(tasks)

    # Group by wave (1-based). Tasks without a wave go in wave 1.
    waves: Dict[int, List[RuntimeTask]] = {}
    for t in tasks:
        waves.setdefault(t.wave, []).append(t)

    wave_nums = sorted(waves.keys())
    for wave in wave_nums:
        members = waves[wave]
        # Only execute tasks still PENDING (CANCELLED ones were blocked by an
        # earlier failure in a prior wave).
        runnable = [t for t in members if states[t.task_id] == RunState.PENDING]
        if not runnable:
            continue

        # Mark RUNNING (main thread) before dispatching, so the timeline shows
        # work started even for parallel tasks.
        for t in runnable:
            persist(t.execution_id, t.task_id, RunState.RUNNING, None,
                    now_iso(), None, worker_id=t.worker_id, wave=t.wave,
                    attempt=1)
            states[t.task_id] = RunState.RUNNING

        # Execute workers in parallel. The worker execution is the concurrent
        # part; DB writes happen on the MAIN thread (below) so a single shared
        # connection never crosses threads.
        def _execute(task: RuntimeTask):
            worker = worker_resolver(task.worker_id)
            started = now_iso()
            # Robust fallback (Phase 3): if the resolver found no adapter for
            # the assigned worker, or the assigned worker is a non-deterministic
            # AI executor, run the full fallback chain (other AI executors ->
            # deterministic built-ins) so one external failure never aborts the
            # mission. Deterministic built-ins run directly via dispatch.
            run_fallback = fallback and (worker is None
                                          or _is_ai_executor_id(task.worker_id))
            last_result = None
            final_state = RunState.FAILED
            last_attempt = 1
            for attempt in range(1, MAX_ATTEMPTS + 1):
                if run_fallback:
                    result = execute_with_fallback(
                        task, task.worker_id, workspace,
                        worker_resolver=worker_resolver)
                else:
                    result = dispatch(task, worker)
                last_result = result
                last_attempt = attempt
                if result.success:
                    final_state = next_state_for_result(
                        RunState.RUNNING, result.success)
                    break
                # Failure. Retry only transient/recoverable failures (bounded by
                # MAX_ATTEMPTS). Deterministic logic failures stop here.
                if not _is_recoverable(result):
                    final_state = RunState.FAILED
                    break
            return task, started, last_result, final_state, last_attempt

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            outcomes = list(pool.map(_execute, runnable))

        # Persist final outcomes on the main thread (single DB connection).
        for task, started, result, state, last_attempt in outcomes:
            states[task.task_id] = state
            finished = now_iso()
            persist(task.execution_id, task.task_id, state, result,
                    started, finished, worker_id=task.worker_id, wave=task.wave,
                    attempt=last_attempt, exit_code=result.exit_code,
                    error=result.error, stdout=result.stdout, stderr=result.stderr,
                    artifacts=result.artifacts, duration_ms=result.duration_ms)

        # Propagate BLOCKING failures: cancel all transitive descendants.
        # NON-blocking failures (e.g. a formatter/linter) let the mission
        # continue — they are recorded as failed but do not cancel dependents.
        for t in runnable:
            if states[t.task_id] == RunState.FAILED and not _is_non_blocking(t.task_type):
                for desc in blocked_descendants(t.task_id, dependents):
                    if states[desc] == RunState.PENDING:
                        states[desc] = mark_cancelled(states[desc])
                        dt = by_id[desc]
                        persist(dt.execution_id, desc, RunState.CANCELLED,
                                None, None, None, worker_id=dt.worker_id,
                                wave=dt.wave, attempt=1,
                                reason="ancestor failed")

    return states
