"""Wave executor (Milestone 9.5).

Executes one `ExecutionSchedule` wave-by-wave. Within a wave, tasks run in
parallel (thread pool). The executor waits for the whole wave to finish before
proceeding to the next. If a task fails, its transitive descendants are
CANCELLED (never executed) — the dependency chain is not continued.

The executor does NOT:
  - plan, schedule, or resolve capabilities (upstream owns those),
  - retry or repair failures,
  - review or accept work,
  - learn from outcomes.

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
from .state import (
    blocked_descendants,
    can_transition,
    mark_cancelled,
    next_state_for_result,
)


# A callback the engine supplies to persist a task-state change.
# signature: (execution_id, task_id, state: RunState, result: Optional[ExecutionResult], started_at, finished_at)
PersistFn = Callable[..., None]


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
        # part; no DB access happens here (kept single-threaded/main-thread).
        def _execute(task: RuntimeTask):
            worker = worker_resolver(task.worker_id)
            started = now_iso()
            result = dispatch(task, worker)
            return task, started, result

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            outcomes = list(pool.map(_execute, runnable))

        # Persist results from the main thread (single DB connection).
        for task, started, result in outcomes:
            finished = now_iso()
            new_state = next_state_for_result(RunState.RUNNING, result.success)
            states[task.task_id] = new_state
            persist(task.execution_id, task.task_id, new_state, result,
                    started, finished, worker_id=task.worker_id, wave=task.wave,
                    attempt=1, exit_code=result.exit_code, error=result.error,
                    stdout=result.stdout, stderr=result.stderr,
                    artifacts=result.artifacts, duration_ms=result.duration_ms)

        # Propagate failures: cancel all transitive descendants.
        for t in runnable:
            if states[t.task_id] == RunState.FAILED:
                for desc in blocked_descendants(t.task_id, dependents):
                    if states[desc] == RunState.PENDING:
                        states[desc] = mark_cancelled(states[desc])
                        dt = by_id[desc]
                        persist(dt.execution_id, desc, RunState.CANCELLED,
                                None, None, None, worker_id=dt.worker_id,
                                wave=dt.wave, attempt=1,
                                reason="ancestor failed")

    return states
