"""Capability Resolver engine (Milestone 9.3).

Orchestrates the ONLY allowed Task -> Worker mapping. It:
  - reads the Task Graph (tasks + required capabilities) — read-only,
  - reads the Worker Registry (capability profiles) — read-only,
  - scores/ranks each task's candidate workers (resolver.resolver),
  - persists Assignments + append-only history + evolution,
  - is idempotent: re-resolution records history/evolution but never duplicates.

The engine executes NOTHING. It never calls a worker, never runs a task, never
invokes an LLM, never touches a repository. Execution is a future milestone.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import List, Optional

from ..db import (
    atomic,
    get_resolver_assignments,
    get_resolver_assignment,
    get_resolver_assignment_by_task,
    get_resolver_evolution,
    get_resolver_history,
    insert_resolver_assignment,
    insert_resolver_evolution,
    insert_resolver_history,
    now_iso,
)
from ..worker.models import validate_capabilities
from ..planning import TaskGraphEngine
from ..worker.engine import WorkerRegistry
from .models import (
    Assignment,
    ResolutionResult,
    ResolutionStatus,
    SCHEMA_VERSION,
    SelectionStrategy,
)
from .resolver import select_assignment

# Deterministic per-task strategy inference (eligibility only; Scheduler picks
# timing later). A task is PARALLEL/SEQUENTIAL only when the graph marks it so;
# we fall back to SINGLE otherwise. This is read from the graph, never invented.
_STRATEGY_FROM_GRAPH = {
    "parallel": SelectionStrategy.PARALLEL,
    "sequential": SelectionStrategy.SEQUENTIAL,
}


@dataclass
class ResolveResult:
    """Outcome of resolving an entire graph."""
    graph_id: str
    assignments: List[Assignment] = field(default_factory=list)
    results: List[ResolutionResult] = field(default_factory=list)
    resolved_at: str = ""
    strategy: SelectionStrategy = SelectionStrategy.SINGLE

    @property
    def assigned(self) -> int:
        return sum(1 for a in self.assignments if a.status == ResolutionStatus.ASSIGNED)

    @property
    def unresolved(self) -> int:
        return sum(1 for a in self.assignments if a.status == ResolutionStatus.UNRESOLVED)

    def to_dict(self) -> dict:
        return {
            "graph_id": self.graph_id,
            "resolved_at": self.resolved_at,
            "strategy": self.strategy.value,
            "assigned": self.assigned,
            "unresolved": self.unresolved,
            "assignments": [a.to_dict() for a in self.assignments],
            "results": [r.to_dict() for r in self.results],
        }


def _strategy_for_task(task, graph) -> SelectionStrategy:
    """Infer a task's selection strategy from the graph (eligibility only)."""
    # The graph exposes parallel_tasks; a task in that set is eligible for
    # parallel execution. Everything else is single. Sequential is reserved for
    # explicit dependency chains (future Scheduler decides order).
    parallel = getattr(graph, "parallel_tasks", None) or []
    if getattr(task, "id", None) in parallel:
        return SelectionStrategy.PARALLEL
    return SelectionStrategy.SINGLE


class CapabilityResolver:
    """Reads Task Graph + Worker Registry, writes Assignments only."""

    def __init__(self, conn) -> None:
        self.conn = conn
        self._graph_eng = TaskGraphEngine(conn)
        self._registry = WorkerRegistry(conn)

    # --- READ ---------------------------------------------------------------

    def assignment_for_task(self, task_id: str) -> Optional[Assignment]:
        row = get_resolver_assignment_by_task(self.conn, task_id)
        if row is None:
            return None
        return self._row_to_assignment(row)

    def assignments(self, graph_id: Optional[str] = None) -> List[Assignment]:
        return [self._row_to_assignment(r)
                for r in get_resolver_assignments(self.conn, graph_id)]

    def history(self, assignment_id: Optional[str] = None) -> list:
        return get_resolver_history(self.conn, assignment_id)

    def evolution(self, graph_id: Optional[str] = None) -> list:
        return get_resolver_evolution(self.conn, graph_id)

    # --- WRITE (resolution) -------------------------------------------------

    def resolve_graph(self, graph_id: str,
                      strategy: Optional[SelectionStrategy] = None
                      ) -> ResolveResult:
        """Resolve every task in a graph to a worker. Atomic, idempotent."""
        g = self._graph_eng.graph_by_id(graph_id)
        if g is None:
            raise ValueError(f"unknown graph_id: {graph_id}")

        workers = self._registry.active_workers()
        # Exclude reasoning-service profiles that have no execution adapter.
        # These are the ... llm and service-kind workers seeded by the registry
        # (worker:claude llm, worker:gpt llm, worker:search llm, etc.).
        # Custom user-registered workers without an adapter are still eligible
        # (they may have an adapter registered later or available at runtime).
        from ..runtime import resolve_executor
        workers = [w for w in workers
                   if resolve_executor(w.id) is not None
                   or not (w.id.endswith(" llm") or w.kind.value == "service")]
        hist_counts = self._successful_history()

        results: List[ResolutionResult] = []
        assignments: List[Assignment] = []
        resolved_at = now_iso()

        with atomic(self.conn):
            for task in g.tasks:
                task_strategy = strategy or _strategy_for_task(task, g)
                chosen, candidates, conf, matched, missing, reason, alts = \
                    select_assignment(
                        list(task.required_capabilities),
                        task.task_type,
                        g.plan_type,
                        workers,
                        strategy=task_strategy,
                        successful_history=hist_counts,
                    )
                res = self._build_result(
                    task, chosen, candidates, conf, matched, missing,
                    reason, alts, task_strategy)
                results.append(res)
                asg = self._persist_assignment(
                    g.id, task, res, resolved_at)
                assignments.append(asg)

        return ResolveResult(
            graph_id=g.id, assignments=assignments, results=results,
            resolved_at=resolved_at,
            strategy=strategy or SelectionStrategy.SINGLE)

    # --- internals ----------------------------------------------------------

    def _successful_history(self) -> dict:
        """Count prior ASSIGNED resolutions per worker (evidence of compat)."""
        out: dict = {}
        for h in get_resolver_history(self.conn):
            if h.get("status") == ResolutionStatus.ASSIGNED.value and h.get("worker_id"):
                out[h["worker_id"]] = out.get(h["worker_id"], 0) + 1
        return out

    def _build_result(self, task, chosen, candidates, conf, matched, missing,
                      reason, alts, strategy) -> ResolutionResult:
        status = (ResolutionStatus.ASSIGNED if chosen is not None
                  else ResolutionStatus.UNRESOLVED)
        return ResolutionResult(
            task_id=task.id, task_title=task.title,
            required_capabilities=validate_capabilities(list(task.required_capabilities)),
            status=status,
            worker_id=chosen.id if chosen else None,
            worker_name=chosen.name if chosen else None,
            confidence=conf, reason=reason,
            matched_capabilities=matched, missing_capabilities=missing,
            selection_strategy=strategy,
            candidates=candidates, alternatives=alts)

    def _persist_assignment(self, graph_id, task, res: ResolutionResult,
                            resolved_at: str) -> Assignment:
        aid = f"{graph_id}:{task.id}"
        prior = get_resolver_assignment(self.conn, aid)
        now = now_iso()
        asg = Assignment(
            assignment_id=aid, graph_id=graph_id, task_id=task.id,
            worker_id=res.worker_id, status=res.status,
            confidence=res.confidence, reason=res.reason,
            matched_capabilities=res.matched_capabilities,
            missing_capabilities=res.missing_capabilities,
            selection_strategy=res.selection_strategy,
            schema_version=SCHEMA_VERSION,
            created_at=prior.created_at if prior else now,
            updated_at=now)

        insert_resolver_assignment(self.conn, asg.to_row())

        # Append-only history snapshot.
        insert_resolver_history(self.conn, {
            "resolved_at": resolved_at,
            "assignment_id": aid,
            "graph_id": graph_id,
            "task_id": task.id,
            "worker_id": res.worker_id,
            "status": res.status.value,
            "confidence": res.confidence,
            "score_total": 0,
            "matched_capabilities": json.dumps(res.matched_capabilities),
            "missing_capabilities": json.dumps(res.missing_capabilities),
            "selection_strategy": res.selection_strategy.value,
        })

        # Evolution: record a change from the prior worker (if any).
        if prior is not None and prior.worker_id != res.worker_id:
            change = ("reassigned" if res.worker_id is not None
                      else "unresolved")
            insert_resolver_evolution(self.conn, {
                "evolved_at": resolved_at,
                "graph_id": graph_id,
                "task_id": task.id,
                "from_worker_id": prior.worker_id,
                "to_worker_id": res.worker_id,
                "change_type": change,
                "reason": res.reason,
            })
        return asg

    @staticmethod
    def _row_to_assignment(row) -> Assignment:
        return Assignment(
            assignment_id=row.assignment_id, graph_id=row.graph_id,
            task_id=row.task_id, worker_id=row.worker_id,
            status=ResolutionStatus(row.status),
            confidence=row.confidence, reason=row.reason,
            matched_capabilities=json.loads(row.matched_capabilities or "[]"),
            missing_capabilities=json.loads(row.missing_capabilities or "[]"),
            selection_strategy=SelectionStrategy(row.selection_strategy),
            schema_version=row.schema_version,
            created_at=row.created_at, updated_at=row.updated_at)
