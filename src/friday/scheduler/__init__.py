"""Task Scheduler (Milestone 9.4).

The ONLY layer permitted to compute execution *ordering* (waves, dependency
depth, critical path, priority, runnable state) from a validated Task Graph +
Capability Assignments. Deterministic scheduling only — no execution, no LLM,
no repository access, no worker invocation. Execution (Runtime) is M9.5.
"""

from __future__ import annotations

from .engine import (
    CycleDetectedError,
    InvalidGraphError,
    MissingAssignmentError,
    ScheduleResult,
    TaskScheduler,
)
from .models import (
    ExecutionSchedule,
    SCHEMA_VERSION,
    ScheduledTask,
    TaskState,
)
from .scheduler import (
    build_schedule,
    compute_priority,
    compute_waves,
    detect_cycle,
    serialize_worker_conflicts,
)
from .state import compute_initial_state
from .timeline import (
    build_timeline,
    critical_path_status,
    max_parallelism,
    order_tasks,
    wave_summary,
)

__all__ = [
    "TaskScheduler",
    "ScheduleResult",
    "ExecutionSchedule",
    "ScheduledTask",
    "TaskState",
    "SCHEMA_VERSION",
    "CycleDetectedError",
    "MissingAssignmentError",
    "InvalidGraphError",
    "build_schedule",
    "compute_priority",
    "compute_waves",
    "detect_cycle",
    "serialize_worker_conflicts",
    "compute_initial_state",
    "build_timeline",
    "critical_path_status",
    "max_parallelism",
    "order_tasks",
    "wave_summary",
]
