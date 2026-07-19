"""Worker dispatcher (Milestone 9.5).

The dispatcher is deliberately dumb. It receives a `RuntimeTask` and a `Worker`,
calls `worker.execute(task)`, and returns the `ExecutionResult`. It contains:

  - NO planning
  - NO scheduling
  - NO capability resolution
  - NO retries
  - NO repair
  - NO worker-specific branching (never `if provider == ...`)

If the worker raises, the dispatcher converts the exception into a failed
`ExecutionResult` — but it does NOT retry or recover. The engine decides the
state transition from `success`.
"""

from __future__ import annotations

import time
from typing import Callable, Dict, Optional

from .models import ExecutionResult, RuntimeTask, Worker
from ..worker.models import VerificationResult


# Maps a registry worker_id (from the schedule) to a Worker adapter instance.
WorkerResolver = Callable[[str], Optional[Worker]]


def _default_worker_resolver(workers: Dict[str, Worker]) -> WorkerResolver:
    """Resolve a worker_id to a registered adapter. Returns None if unknown."""
    def _resolve(worker_id: str) -> Optional[Worker]:
        return workers.get(worker_id)
    return _resolve


def dispatch(task: RuntimeTask, worker: Optional[Worker]) -> ExecutionResult:
    """Execute ONE task on its assigned worker. No retry, no repair.

    A missing worker (should not happen — the scheduler already BLOCKED such
    tasks) yields a failed result rather than raising.
    """
    if worker is None:
        return ExecutionResult(
            success=False, stdout="", stderr="no worker resolved",
            exit_code=None, duration_ms=0,
            error=f"no worker for task {task.task_id}")
    t0 = time.monotonic()
    try:
        result = worker.execute(task)
    except Exception as e:  # worker blew up — record, do not retry
        dur = int((time.monotonic() - t0) * 1000)
        return ExecutionResult(
            success=False, stdout="", stderr=str(e),
            exit_code=None, duration_ms=dur, error=f"{type(e).__name__}: {e}")
    result.duration_ms = int((time.monotonic() - t0) * 1000)
    # Verification: objective correctness, worker-owned. Always runs before
    # any review. Record outcome in metadata (provenance). The engine owns
    # persistence and any review step.
    try:
        vres = worker.verify(task, result)
    except Exception as e:  # verify must never break execution reporting
        vres = VerificationResult(passed=False,
                                  reason=f"verify raised: {type(e).__name__}: {e}")
    result.metadata = {**result.metadata,
                       "verified": vres.passed,
                       "verify_reason": vres.reason}
    return result
