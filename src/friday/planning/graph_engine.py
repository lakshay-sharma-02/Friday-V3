"""Task Graph Engine (Milestone 9.1).

Orchestration + persistence for the Task Graph Compiler. WRITE entrypoint:
`generate()` derives the Plan (via the FROZEN PlanEngine) and compiles it into a
TaskGraph, then persists both the graph header and its tasks/edges to the new
dedicated tables. READ entrypoints: list / explain / export / history / evolution.

NEVER executes, edits files, calls workers, or uses an LLM. NEVER reads
observations/context/git/repositories directly. The Planning Engine is FROZEN;
this layer only invokes it and consumes the structured Plan it returns. Idle on
recompilation: recompiling a goal REPLACES the same graph row (idempotent on
goal->graph id) and records the prior version in task_history (append-only).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import List, Optional, Tuple

from ..db import (
    TaskEdgeRow,
    TaskEvolutionRow,
    TaskGraphRow,
    TaskHistoryRow,
    TaskRow,
    get_all_task_graphs,
    get_edges_for_graph,
    get_task_graph_by_id,
    get_tasks_for_graph,
    insert_task_evolution,
    insert_task_graph,
    insert_task_history,
    task_evolution_for,
    task_history_for,
)
from .compiler import Task, TaskGraph, TaskType, _graph_id, compile_plan
from .graph_schema import validate_task_graph
from .engine import PlanEngine
from .models import Plan, now_iso

@dataclass
class GraphBuildResult:
    total: int
    created: int
    updated: int
    events: int = 0

    def to_text(self) -> str:
        lines = [
            "Task Graph Compiler",
            "",
            f"Total graphs: {self.total}",
            f"Created: {self.created}",
            f"Updated: {self.updated}",
            f"Evolution events: {self.events}",
            "",
            "Done.",
        ]
        return "\n".join(lines) + "\n"


class TaskGraphEngine:
    """Derives and stores task graphs. WRITE entrypoint: generate()."""

    def __init__(self, conn) -> None:
        self.conn = conn
        self._plan_eng = PlanEngine(conn)

    # --- READ (never mutate) --------------------------------------------------

    def all_graphs(self) -> List[TaskGraphRow]:
        return get_all_task_graphs(self.conn)

    def graph_by_id(self, gid: str) -> Optional[TaskGraph]:
        row = get_task_graph_by_id(self.conn, gid)
        if row is None:
            return None
        return self._rebuild(row)

    def _rebuild(self, row: TaskGraphRow) -> TaskGraph:
        tasks = [
            self._task_from_row(r) for r in get_tasks_for_graph(self.conn, row.id)]
        edges = [
            {"from": e.from_task, "to": e.to_task, "kind": e.kind}
            for e in get_edges_for_graph(self.conn, row.id)]
        g = TaskGraph(
            id=row.id, goal=row.goal, plan_id=row.plan_id,
            plan_type=row.plan_type, tasks=tasks, edges=edges,
            status=row.status, created_at=row.created_at,
            updated_at=row.updated_at,
        )
        # Recompute derived metrics deterministically from the persisted graph.
        self._recompute(g)
        # Loading from storage must also satisfy the frozen contract.
        validate_task_graph(g.to_json())
        return g

    @staticmethod
    def _task_from_row(r: TaskRow):
        return Task(
            id=r.id, graph_id=r.graph_id, plan_id=r.plan_id,
            milestone_order=r.milestone_order, title=r.title,
            description=r.description,
            task_type=TaskType.from_str(r.task_type),
            required_capabilities=[
                c for c in r.required_capabilities.split(",") if c],
            complexity=r.complexity, priority=r.priority,
            estimated_effort=r.estimated_effort,
            dependencies=[d for d in r.dependencies.split(",") if d],
            inputs=_loads(r.inputs), outputs=_loads(r.outputs),
            acceptance_criteria=_loads(r.acceptance_criteria),
            verification=_loads(r.verification),
            rollback=_loads(r.rollback), evidence=_loads(r.evidence),
            symbolic=_loads_dict(r.symbolic), status=r.status, confidence=r.confidence,
            sequence=r.sequence,
        )

    @staticmethod
    def _recompute(g: TaskGraph) -> None:
        from .compiler import (
            _compute_levels, _critical_path, _parallel_groups)
        ids = [t.id for t in g.tasks]
        g.levels = _compute_levels(g.edges, ids)
        g.critical_path = _critical_path(g.edges, g.tasks)
        groups, ptasks = _parallel_groups(g.levels)
        g.parallel_groups = groups
        g.parallel_tasks = ptasks

    def history(self, gid: str) -> List[TaskHistoryRow]:
        return task_history_for(self.conn, gid)

    def evolution(self, gid: Optional[str] = None) -> List[TaskEvolutionRow]:
        if gid is None:
            from ..db import task_evolution_all
            return task_evolution_all(self.conn)
        return task_evolution_for(self.conn, gid)

    # --- WRITE ----------------------------------------------------------------

    def generate(self, goal: str, generated_at: Optional[str] = None) -> TaskGraph:
        """Derive the Plan (frozen PlanEngine) and compile + persist the graph.

        Idempotent on goal: recompiling REPLACES the same graph row and appends
        a snapshot to task_history.
        """
        if generated_at is None:
            generated_at = now_iso()

        plan = self._plan_eng.generate(goal, generated_at=generated_at)
        graph = compile_plan(plan, generated_at=generated_at)

        # Enforce the frozen Task Graph contract before persisting. A malformed
        # graph must fail loudly, not silently enter the execution pipeline.
        validate_task_graph(graph.to_json())

        gid = graph.id
        prev_row = get_task_graph_by_id(self.conn, gid)
        prev = self._rebuild(prev_row) if prev_row else None

        created = 0
        updated = 0
        if prev is None:
            created = 1
        else:
            updated = 1

        self._persist(graph, prev_created=prev_row.created_at if prev_row else generated_at)
        self._record_history(generated_at, graph, prev)
        self._record_evolution(generated_at, graph, prev)
        return graph

    def _persist(self, g: TaskGraph, prev_created: str) -> None:
        graph_row = TaskGraphRow(
            id=g.id, goal=g.goal, plan_id=g.plan_id, plan_type=g.plan_type,
            task_count=len(g.tasks), edge_count=len(g.edges),
            critical_path_length=len(g.critical_path),
            parallel_groups=g.parallel_groups, status=g.status,
            created_at=prev_created, updated_at=g.updated_at,
        )
        task_rows: List[TaskRow] = []
        for t in g.tasks:
            task_rows.append(TaskRow(
                id=t.id, graph_id=t.graph_id, plan_id=t.plan_id,
                milestone_order=t.milestone_order, title=t.title,
                description=t.description, task_type=t.task_type,
                required_capabilities=",".join(t.required_capabilities),
                complexity=t.complexity, priority=t.priority,
                estimated_effort=t.estimated_effort,
                dependencies=",".join(t.dependencies),
                inputs=_dumps(t.inputs), outputs=_dumps(t.outputs),
                acceptance_criteria=_dumps(t.acceptance_criteria),
                verification=_dumps(t.verification),
                rollback=_dumps(t.rollback), evidence=_dumps(t.evidence),
                symbolic=_dumps(t.symbolic), status=t.status,
                confidence=t.confidence, sequence=t.sequence,
            ))
        edge_rows: List[TaskEdgeRow] = []
        for i, e in enumerate(g.edges):
            edge_rows.append(TaskEdgeRow(
                id=f"{g.id}#e{i}", graph_id=g.id, from_task=e["from"],
                to_task=e["to"], kind=e.get("kind", "depends_on")))
        insert_task_graph(self.conn, [graph_row], task_rows, edge_rows)

    def _record_history(self, generated_at: str, g: TaskGraph,
                        prev: Optional[TaskGraph]) -> int:
        insert_task_history(self.conn, [TaskHistoryRow(
            generated_at=generated_at, graph_id=g.id, goal=g.goal,
            task_count=len(g.tasks), edge_count=len(g.edges),
            critical_path_length=len(g.critical_path),
            parallel_groups=g.parallel_groups,
            tasks_json=_dumps([t.to_dict() for t in g.tasks]),
            edges_json=_dumps(g.edges),
        )])
        return 1

    def _record_evolution(self, generated_at: str, g: TaskGraph,
                          prev: Optional[TaskGraph]) -> int:
        events: List[TaskEvolutionRow] = []
        gid = g.id
        if prev is None:
            events.append(self._event(
                generated_at, "Compiled", gid, None, g.status, None,
                len(g.tasks), None, len(g.edges),
                f"Task graph compiled for plan {g.plan_id}."))
            insert_task_evolution(self.conn, events)
            return len(events)
        if len(g.tasks) != len(prev.tasks) or len(g.edges) != len(prev.edges):
            events.append(self._event(
                generated_at, "Recompiled", gid, prev.status, g.status,
                len(prev.tasks), len(g.tasks), len(prev.edges), len(g.edges),
                "Graph shape changed on recompilation (plan changed)."))
            insert_task_evolution(self.conn, events)
            return len(events)
        return 0

    @staticmethod
    def _event(gen_at, etype, gid, prev_status, new_status, prev_tasks,
               new_tasks, prev_edges, new_edges, reason):
        return TaskEvolutionRow(
            id=f"{gen_at}:{etype}:{gid}", generated_at=gen_at,
            event_type=etype, graph_id=gid, previous_status=prev_status,
            new_status=new_status, reason=reason, task_count=new_tasks or 0,
            edge_count=new_edges or 0, timestamp=gen_at)


def _dumps(xs: list) -> str:
    try:
        return json.dumps(xs, separators=(",", ":"))
    except (TypeError, ValueError):
        return "[]"


def _loads(s: str) -> list:
    if not s:
        return []
    try:
        out = json.loads(s)
        return out if isinstance(out, list) else []
    except (TypeError, ValueError):
        return []


def _loads_dict(s: str) -> dict:
    """Parse a JSON object column (e.g. a task's symbolic intent)."""
    if not s:
        return {}
    try:
        out = json.loads(s)
        return out if isinstance(out, dict) else {}
    except (TypeError, ValueError):
        return {}
