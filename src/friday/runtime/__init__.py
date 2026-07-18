"""Execution Runtime (Milestone 9.5).

The ONLY layer that *performs* work. It consumes a frozen `ExecutionSchedule`
(M9.4) and executes it — wave by wave, respecting dependencies and worker
assignments. It NEVER plans, schedules, resolves capabilities, reviews, repairs,
retries, or learns. Execution (and only execution) happens here.

The Runtime depends on a single generic `Worker` interface
(`models.Worker.execute(task) -> ExecutionResult`). Concrete backends
(ClaudeWorker, GeminiWorker, CodexWorker, PythonWorker, ShellWorker, ...) are
adapters that implement that interface; the Runtime core never references them
by name.
"""

from __future__ import annotations

from .dispatcher import WorkerResolver, dispatch
from .engine import RuntimeEngine
from .events import (
    load as load_events,
    session_finished,
    session_started,
    task_failed,
    task_finished,
    task_started,
)
from .executor import execute_schedule
from .history import load as load_history, snapshot
from .models import (
    ExecutionReport,
    ExecutionResult,
    MockWorker,
    PythonWorker,
    RunState,
    RuntimeEvent,
    RuntimeTask,
    SCHEMA_VERSION,
    SessionState,
    ShellWorker,
    Worker,
)
from .state import (
    blocked_descendants,
    can_transition,
    mark_cancelled,
    next_state_for_result,
)

__all__ = [
    "RuntimeEngine",
    "execute_schedule",
    "dispatch",
    "WorkerResolver",
    "Worker",
    "MockWorker",
    "PythonWorker",
    "ShellWorker",
    "ExecutionResult",
    "RuntimeTask",
    "RuntimeEvent",
    "ExecutionReport",
    "RunState",
    "SessionState",
    "SCHEMA_VERSION",
    "blocked_descendants",
    "can_transition",
    "mark_cancelled",
    "next_state_for_result",
    "session_started",
    "session_finished",
    "task_started",
    "task_finished",
    "task_failed",
    "load_events",
    "load_history",
    "snapshot",
]
