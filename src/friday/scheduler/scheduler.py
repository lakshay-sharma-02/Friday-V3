"""Deterministic scheduling computation (Milestone 9.4).

Pure functions: no I/O, no LLM, no randomness, no time dependence. Given a
validated Task Graph (tasks + edges + levels + critical path), the Capability
Assignments, and the active-worker set, produce a deterministic execution
ordering (waves, dependency depth, priority, runnable state).

The Scheduler answers ONE question: WHEN does each task become runnable, and in
what order. It does not execute, schedule workers' internal timing, or assign
workers (the Resolver owns assignment).

Worker conflicts (same worker on multiple runnable tasks) are serialized
deterministically by task id within a wave — the future Runtime obeys this.
"""

from __future__ import annotations

import json
from typing import Dict, List, Optional, Tuple

from .models import ScheduledTask, TaskState
from .state import compute_initial_state


def _loads(s: str) -> object:
    try:
        return json.loads(s) if s else []
    except (ValueError, TypeError):
        return []


# Priority tunables (deterministic, documented).
_P_CRITICAL_PATH = 1000     # on the critical path
_P_DEPENDENCY_DEPTH = 100   # per level of dependency depth
_P_EXPLICIT_PRIORITY = {    # plan-stated task priority
    "critical": 40,
    "high": 30,
    "medium": 20,
    "low": 10,
}
_P_EXPLICIT_DEFAULT = 20


def detect_cycle(task_ids: List[str], edges: List[dict]) -> Optional[List[str]]:
    """Return a cycle (list of task ids) if one exists, else None.

    Deterministic DFS with a fixed (sorted) adjacency order so the reported
    cycle is stable across runs.
    """
    adj: Dict[str, List[str]] = {t: [] for t in task_ids}
    for e in edges:
        f, to = e.get("from"), e.get("to")
        if f in adj and to in adj:
            adj[f].append(to)
    for k in adj:
        adj[k].sort()

    WHITE, GRAY, BLACK = 0, 1, 2
    color = {t: WHITE for t in task_ids}
    stack: List[str] = []

    def dfs(node: str) -> Optional[List[str]]:
        color[node] = GRAY
        stack.append(node)
        for nxt in adj[node]:
            if color[nxt] == GRAY:
                # Found a back-edge: extract the cycle slice.
                idx = stack.index(nxt)
                return stack[idx:] + [nxt]
            if color[nxt] == WHITE:
                res = dfs(nxt)
                if res:
                    return res
        stack.pop()
        color[node] = BLACK
        return None

    for t in sorted(task_ids):
        if color[t] == WHITE:
            res = dfs(t)
            if res:
                return res
    return None


def compute_waves(task_ids: List[str], edges: List[dict],
                  root_depths: Dict[str, int]) -> Dict[str, int]:
    """Assign each task to a 1-based parallel wave from its root depth.

    Wave = distance-from-root + 1. Independent roots are wave 1; each dependency
    hop pushes successors to a later wave. `root_depths` is the forward
    distance from a root (see `_root_depths`).
    """
    return {t: root_depths.get(t, 0) + 1 for t in task_ids}


def _root_depths(task_ids: List[str], edges: List[dict]) -> Dict[str, int]:
    """Forward longest-path distance from a root (root = 0) for each task.

    Edge A->B has kind 'depends_on', meaning A depends on B, so B is a
    predecessor of A (B runs first). Hence B is the predecessor stored under A.
    Deterministic.
    """
    preds: Dict[str, List[str]] = {t: [] for t in task_ids}
    for e in edges:
        f, to = e.get("from"), e.get("to")
        if f in preds and to in preds:
            preds[f].append(to)

    depth: Dict[str, int] = {}

    def fwd(u: str, stack: tuple = ()) -> int:
        if u in depth:
            return depth[u]
        if u in stack:
            return 0
        if not preds.get(u):
            depth[u] = 0
            return 0
        d = 1 + max(fwd(p, stack + (u,)) for p in preds[u])
        depth[u] = d
        return d

    for t in task_ids:
        fwd(t)
    return depth


def compute_dependency_count(task_ids: List[str],
                             edges: List[dict]) -> Dict[str, int]:
    """Transitive ancestor count per task (number of predecessors).

    A 3-chain A->B->C gives C a count of 2 (A and B), not 1. Deterministic.
    """
    preds: Dict[str, List[str]] = {t: [] for t in task_ids}
    for e in edges:
        f, to = e.get("from"), e.get("to")
        if f in preds and to in preds:
            preds[f].append(to)

    seen: Dict[str, int] = {}

    def count(u: str, stack: tuple = ()) -> int:
        if u in seen:
            return seen[u]
        if u in stack:  # cycle guard (already rejected by caller)
            return 0
        if not preds.get(u):
            seen[u] = 0
            return 0
        n = len(preds[u]) + sum(count(p, stack + (u,)) for p in preds[u])
        seen[u] = n
        return n

    for t in task_ids:
        count(t)
    return seen


def compute_critical_path(task_ids: List[str],
                          edges: List[dict]) -> List[str]:
    """Longest path by node count (the critical path). Deterministic.

    Unlike the compiler's version (which can mark a lone independent node as a
    critical path), this is empty for graphs with no edges, so a single task is
    NOT on the critical path. Tie-break: lowest task id.
    """
    if not edges or len(task_ids) < 2:
        return []

    succ: Dict[str, List[str]] = {t: [] for t in task_ids}
    preds: Dict[str, List[str]] = {t: [] for t in task_ids}
    for e in edges:
        f, to = e.get("from"), e.get("to")
        if f in succ and to in succ:
            succ[to].append(f)
            preds[f].append(to)

    best: Dict[str, int] = {}
    nxt: Dict[str, Optional[str]] = {}

    def longest(u: str, stack: tuple = ()) -> int:
        if u in best:
            return best[u]
        if u in stack:
            best[u] = 1
            return 1
        outs = succ.get(u, [])
        if not outs:
            best[u] = 1
            nxt[u] = None
            return 1
        cand = []
        for v in outs:
            cand.append((longest(v, stack + (u,)), v))
        cand.sort(key=lambda x: (-x[0], x[1]))
        bl, bv = cand[0]
        best[u] = bl + 1
        nxt[u] = bv
        return best[u]

    for t in task_ids:
        longest(t)

    roots = [t for t in task_ids if not preds.get(t)]
    if not roots:
        return []
    start = max(roots, key=lambda r: (best.get(r, 1), -ord(r[0]) if r else 0,
                                      r))
    path: List[str] = []
    cur: Optional[str] = start
    guard = 0
    while cur is not None and guard <= len(task_ids) + 1:
        path.append(cur)
        cur = nxt.get(cur)
        guard += 1
    return path


def compute_priority(task_id: str,
                     root_depths: Dict[str, int],
                     critical_path: List[str],
                     explicit_priority: str) -> int:
    """Compute a deterministic scheduler priority (higher = sooner).

    priority = critical_path_bonus + dependency_depth_bonus + explicit_priority
    `root_depths` is the forward distance from a root (root = 0). Tie-break
    (task id) is applied by the caller's sort, not here.
    """
    p = 0
    if task_id in critical_path:
        p += _P_CRITICAL_PATH
    p += _P_DEPENDENCY_DEPTH * root_depths.get(task_id, 0)
    p += _P_EXPLICIT_PRIORITY.get(
        (explicit_priority or "").lower(), _P_EXPLICIT_DEFAULT)
    return p


def serialize_worker_conflicts(wave_tasks: List[ScheduledTask]) -> None:
    """Deterministically serialize same-worker tasks within a wave.

    Tasks sharing a worker are ordered by task id; the wave's `estimated_start`
    carries a sub-order so the Runtime runs them sequentially, never in parallel.
    Mutates the `estimated_start`/`estimated_finish` of the passed tasks in place.
    """
    by_worker: Dict[str, List[ScheduledTask]] = {}
    for t in wave_tasks:
        if t.worker_id:
            by_worker.setdefault(t.worker_id, []).append(t)

    for wid, group in by_worker.items():
        # Deterministic order: task id ascending.
        group.sort(key=lambda x: x.task_id)
        for i, t in enumerate(group):
            t.estimated_start = i
            t.estimated_finish = i + 1


def build_schedule(
    graph_id: str,
    goal: str,
    tasks: List,
    edges: List[dict],
    levels: Dict[str, int],
    critical_path: List[str],
    assignments: Dict[str, dict],     # task_id -> assignment dict
    active_workers: set,
) -> Tuple[List[ScheduledTask], int, Dict[str, int]]:
    """Build the list of ScheduledTask nodes for a graph.

    Returns (scheduled_tasks, wave_count, worker_utilization).

    Rejects nothing here (cycle/assignment validation is the caller's job);
    instead each task receives a runnable state: READY / NOT_READY / BLOCKED.

    `assignments` maps task_id -> {
        "assignment_id", "worker_id" (or None), "status", "confidence",
        "selection_strategy"}.
    """
    task_ids = [t.id for t in tasks]
    task_by_id = {t.id: t for t in tasks}

    # Forward root depths (root = 0) used for both waves and priority.
    root_depths = _root_depths(task_ids, edges)
    waves = compute_waves(task_ids, edges, root_depths)
    wave_count = max(waves.values()) if waves else 0

    # Direct predecessors per task (in-edges), derived from edges so the
    # Scheduler's notion of dependencies does not depend on how the Task Graph
    # persisted the `dependencies` column. Edge from->to (kind depends_on)
    # means `from` depends on `to`, so `to` is a predecessor of `from`.
    deps: Dict[str, List[str]] = {t: [] for t in task_ids}
    for e in edges:
        f, to = e.get("from"), e.get("to")
        if f in deps and to in deps and to not in deps[f]:
            deps[f].append(to)

    scheduled: List[ScheduledTask] = []
    worker_util: Dict[str, int] = {}

    # Transitive ancestor count per task (drives dependency_depth priority).
    dep_count = compute_dependency_count(task_ids, edges)

    for t in tasks:
        aid = assignments.get(t.id, {}).get("assignment_id", f"{graph_id}:{t.id}")
        worker_id = assignments.get(t.id, {}).get("worker_id")
        status_val = assignments.get(t.id, {}).get("status")
        conf = assignments.get(t.id, {}).get("confidence", "low")
        strat = assignments.get(t.id, {}).get("selection_strategy", "single")

        # Attach derived dependencies so compute_initial_state (NOT_READY when
        # predecessors exist) is correct regardless of the stored column.
        task_deps = sorted(deps.get(t.id, []))
        t.dependencies = task_deps

        # Initial runnable state from dependencies + assignment + worker.
        state, reason = compute_initial_state(
            task=t,
            worker_id=worker_id,
            assignment_status=status_val,
            active_workers=active_workers,
        )

        wave = waves.get(t.id, 1)
        priority = compute_priority(
            t.id, root_depths, critical_path, t.priority)

        st = ScheduledTask(
            schedule_id=f"{graph_id}:{t.id}",
            graph_id=graph_id,
            assignment_id=aid,
            task_id=t.id,
            worker_id=worker_id if state != TaskState.BLOCKED else None,
            phase=f"wave-{wave}",
            wave=wave,
            status=state,
            priority=priority,
            dependency_count=dep_count[t.id],
            dependencies=task_deps,
            blocked_reason=reason,
            confidence=conf,
            selection_strategy=strat,
        )
        scheduled.append(st)
        if worker_id and state != TaskState.BLOCKED:
            worker_util[worker_id] = worker_util.get(worker_id, 0) + 1

    # Serialize same-worker conflicts within each wave (deterministic).
    for w in range(1, wave_count + 1):
        wave_tasks = [s for s in scheduled if s.wave == w]
        serialize_worker_conflicts(wave_tasks)

    return scheduled, wave_count, worker_util
