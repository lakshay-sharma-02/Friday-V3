"""Scheduler engine (Milestone 9.4).

Orchestrates the ONLY allowed computation of execution *ordering*. It:
  - reads the validated Task Graph (read-only),
  - reads the Capability Assignments (read-only),
  - reads the Worker Registry active set (read-only),
  - computes waves / dependency depth / critical path / priority / runnable
    state (scheduler.scheduler + scheduler.state + scheduler.timeline),
  - persists the schedule + append-only history + evolution,
  - is idempotent: re-scheduling records history/evolution but never duplicates.

The engine executes NOTHING. It never calls a worker, never runs a task, never
invokes an LLM, never touches a repository. Execution is a future milestone
(M9.5 Runtime).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import List, Optional

from ..db import (
    atomic,
    get_scheduler_history,
    get_scheduler_runs,
    get_scheduler_tasks,
    get_scheduler_evolution,
    get_tasks_for_graph,
    get_edges_for_graph,
    insert_scheduler_evolution,
    insert_scheduler_history,
    insert_scheduler_run,
    insert_scheduler_task,
    now_iso,
)
from ..planning import TaskGraphEngine
from ..planning.compiler import Task
from ..resolver.engine import CapabilityResolver
from ..worker.engine import WorkerRegistry
from .models import ExecutionSchedule, SCHEMA_VERSION, ScheduledTask, TaskState
from .scheduler import (
    _loads,
    build_schedule,
    compute_critical_path,
    detect_cycle,
)


class CycleDetectedError(Exception):
    """Raised when the graph contains a cycle (schedule rejected)."""


class MissingAssignmentError(Exception):
    """Raised when a task has no capability assignment (schedule rejected)."""


class InvalidGraphError(Exception):
    """Raised when the graph is invalid (e.g. dangling edge)."""


@dataclass
class ScheduleResult:
    """Outcome of scheduling one graph."""
    schedule: ExecutionSchedule
    run_id: str
    scheduled_at: str
    blocked: List[str] = field(default_factory=list)   # task ids BLOCKED
    rejected: bool = False
    rejection_reason: str = ""

    def to_dict(self) -> dict:
        d = self.schedule.to_dict()
        d.update({
            "run_id": self.run_id,
            "scheduled_at": self.scheduled_at,
            "blocked": list(self.blocked),
            "rejected": self.rejected,
            "rejection_reason": self.rejection_reason,
        })
        return d


class TaskScheduler:
    """Reads Task Graph + Assignments, writes a deterministic ExecutionSchedule."""

    def __init__(self, conn) -> None:
        self.conn = conn
        self._graph_eng = TaskGraphEngine(conn)
        self._resolver = CapabilityResolver(conn)
        self._registry = WorkerRegistry(conn)

    # --- READ ---------------------------------------------------------------

    def tasks(self, graph_id: Optional[str] = None) -> List[dict]:
        return get_scheduler_tasks(self.conn, graph_id)

    def task(self, schedule_id: str) -> Optional[dict]:
        return get_scheduler_task(self.conn, schedule_id)

    def runs(self, graph_id: Optional[str] = None) -> List[dict]:
        return get_scheduler_runs(self.conn, graph_id)

    def history(self, graph_id: Optional[str] = None) -> List[dict]:
        return get_scheduler_history(self.conn, graph_id)

    def evolution(self, graph_id: Optional[str] = None) -> List[dict]:
        return get_scheduler_evolution(self.conn, graph_id)

    # --- WRITE (scheduling) -------------------------------------------------

    def schedule_graph(self, graph_id: str) -> ScheduleResult:
        """Compute a deterministic execution schedule for a graph.

        Validates the graph (cycle / dangling edges), requires capability
        assignments for every task, then builds waves + state and persists.
        Raises CycleDetectedError / MissingAssignmentError / InvalidGraphError
        when the schedule must be rejected.

        The graph is loaded RAW (tasks + edges read directly), NOT via the
        frozen TaskGraphEngine, because `graph_by_id` re-runs graph validation
        and would reject an intentionally-invalid graph (cycle / dangling edge)
        before the Scheduler's own rejection rules can report it. The Scheduler
        owns scheduling validation; it does not re-validate the frozen contract.
        """
        # 1. Raw load of tasks + edges (no frozen-contract validation).
        g_row = self.conn.execute(
            "SELECT goal FROM task_graphs WHERE id = ?", (graph_id,)
        ).fetchone()
        if g_row is None:
            raise InvalidGraphError(f"unknown graph_id: {graph_id}")
        goal = g_row["goal"]
        rows = get_tasks_for_graph(self.conn, graph_id)

        tasks = [Task(
            id=r.id, graph_id=r.graph_id, plan_id=r.plan_id,
            milestone_order=r.milestone_order, title=r.title,
            description=r.description, task_type=r.task_type,
            required_capabilities=[
                c for c in r.required_capabilities.split(",") if c],
            complexity=r.complexity, priority=r.priority,
            estimated_effort=r.estimated_effort,
            dependencies=[],  # derived from edges below
            inputs=_loads(r.inputs), outputs=_loads(r.outputs),
            acceptance_criteria=_loads(r.acceptance_criteria),
            verification=_loads(r.verification),
            rollback=_loads(r.rollback), evidence=_loads(r.evidence),
            status=r.status, confidence=r.confidence, sequence=r.sequence,
        ) for r in rows]
        edge_rows = get_edges_for_graph(self.conn, graph_id)
        edges = [{"from": e.from_task, "to": e.to_task, "kind": e.kind}
                 for e in edge_rows]
        task_ids = [t.id for t in tasks]

        # 2. Cycle detection (reject) — runs BEFORE frozen validation.
        cycle = detect_cycle(task_ids, edges)
        if cycle is not None:
            raise CycleDetectedError(
                "cycle detected: " + " -> ".join(cycle))

        # 3. Dangling edges (reject).
        id_set = set(task_ids)
        for e in edges:
            if e.get("from") not in id_set or e.get("to") not in id_set:
                raise InvalidGraphError(
                    f"dangling edge: {e.get('from')} -> {e.get('to')}")

        # 4. Capability assignments required for every task (reject).
        assignments = {
            a.task_id: {
                "assignment_id": a.assignment_id,
                "worker_id": a.worker_id,
                "status": a.status.value,
                "confidence": a.confidence,
                "selection_strategy": a.selection_strategy.value,
            }
            for a in self._resolver.assignments(graph_id)
        }
        missing = [t for t in task_ids if t not in assignments]
        if missing:
            raise MissingAssignmentError(
                "tasks without capability assignment: " + ", ".join(missing))

        # 5. Active worker set (for BLOCKED determination).
        active = {w.id for w in self._registry.active_workers()}

        # Critical path is computed HERE (scheduler authority), not borrowed
        # from the frozen graph — a single independent node must not be marked
        # critical, which would corrupt priority ordering.
        critical_path = compute_critical_path(task_ids, edges)

        scheduled, wave_count, worker_util = build_schedule(
            graph_id=graph_id,
            goal=goal,
            tasks=tasks,
            edges=edges,
            levels={},
            critical_path=critical_path,
            assignments=assignments,
            active_workers=active,
        )

        blocked = [s.task_id for s in scheduled if s.status == TaskState.BLOCKED]

        schedule = ExecutionSchedule(
            schedule_id=graph_id,
            graph_id=graph_id,
            goal=goal,
            task_count=len(scheduled),
            wave_count=wave_count,
            critical_path=critical_path,
            critical_path_length=len(critical_path),
            max_parallelism=max_parallelism_from(wave_count, scheduled),
            worker_utilization=worker_util,
            tasks=scheduled,
            status="scheduled",
        )

        run_id = f"run:{graph_id}:{now_iso()}"
        scheduled_at = now_iso()

        with atomic(self.conn):
            insert_scheduler_run(self.conn, {
                "run_id": run_id,
                "graph_id": graph_id,
                "goal": goal,
                "wave_count": wave_count,
                "task_count": len(scheduled),
                "critical_path_length": len(critical_path),
                "max_parallelism": schedule.max_parallelism,
                "status": "scheduled",
                "created_at": scheduled_at,
                "updated_at": scheduled_at,
            })
            for s in scheduled:
                now = now_iso()
                s.created_at = now
                s.updated_at = now
                insert_scheduler_task(self.conn, s.to_row())
                # Append-only history snapshot.
                insert_scheduler_history(self.conn, {
                    "scheduled_at": scheduled_at,
                    "schedule_id": s.schedule_id,
                    "graph_id": graph_id,
                    "task_id": s.task_id,
                    "worker_id": s.worker_id,
                    "wave": s.wave,
                    "status": s.status.value,
                    "priority": s.priority,
                    "assignment_id": s.assignment_id,
                })

        return ScheduleResult(
            schedule=schedule,
            run_id=run_id,
            scheduled_at=scheduled_at,
            blocked=blocked,
        )


def max_parallelism_from(wave_count: int,
                         scheduled: List[ScheduledTask]) -> int:
    if wave_count == 0:
        return 0
    return max(
        (sum(1 for s in scheduled if s.wave == w)
         for w in range(1, wave_count + 1)),
        default=0,
    )
