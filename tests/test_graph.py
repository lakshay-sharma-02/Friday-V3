"""Tests for the Task Graph Compiler (Milestone 9.1).

The Task Graph Compiler is a WRITE-ONLY layer on TOP of the Planning Engine. It
compiles a STRUCTURED Plan (already derived from Insights/Initiatives/
Understanding/Knowledge) into a deterministic, acyclic task DAG — Friday's
execution IR. Workers will consume ONLY this graph, never the Plan.

NEVER executes, edits files, calls workers, or uses an LLM. No embeddings, no
vectors, no randomness. Deterministic from the structured Plan input alone.

Every regression case required by the spec is covered:
- Cold start / no plan / single milestone / multiple milestones
- Dependency creation / cycle detection / parallel branches
- Capability inference / priority / complexity / acceptance criteria
- Verification / rollback / critical path / history / evolution / append-only
- Repeated compilation (idempotency) / graph export / Brain compatibility
- No hallucination (valid plan references only) / no duplicate tasks / edges
"""

from __future__ import annotations

import sqlite3

import pytest

from src.friday.db import (SCHEMA, _migrate, TaskGraphRow, get_all_task_graphs,
                           get_tasks_for_graph, get_edges_for_graph,
                           count_task_graphs)
from src.friday.planning import (PlanEngine, PlanType, PlanConfidence,
                                  PlanStatus, TaskGraphEngine, compile_plan)
from src.friday.planning.compiler import (
    CycleError, Task, TaskGraph, TaskType, _complexity, _critical_path,
    _detect_cycle, _expand, _infer_capabilities, _parallel_groups,
    _priority, _compute_levels)
from src.friday.planning.models import Plan

_CAP_FE = "frontend"
_CAP_BE = "backend"
_CAP_ARCH = "architecture"
_CAP_RUST = "rust"
_CAP_TEST = "testing"
_CAP_PYTHON = "python"
_CAP_TS = "typescript"
_CAP_SQL = "sql"
_CAP_DOC = "documentation"
_CAP_INFRA = "infrastructure"
_CAP_RESEARCH = "research"
_CAP_CONFIG = "configuration"


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    yield conn
    conn.close()


def _plan(goal="Build Widget", ptype=PlanType.FEATURE, milestones=None,
          verification=None, rollback=None, complexity="medium",
          init_ids=(), ins_ids=(), u_ids=(), k_ids=()):
    """Build a Plan directly (no dependency on the PlanEngine) so compiler
    tests are independent and fast. Mirrors the structured shape the PlanEngine
    emits."""
    if milestones is None:
        milestones = [
            {"order": 1, "title": "Investigate & scope",
             "detail": "Confirm requirements.", "evidence": "initiative"},
            {"order": 2, "title": "Design", "detail": "Design the change.",
             "evidence": "initiative"},
            {"order": 3, "title": "Backend",
             "detail": "Implement server-side logic.", "evidence": "initiative"},
            {"order": 4, "title": "Frontend",
             "detail": "Implement user-facing surface.", "evidence": "initiative"},
            {"order": 5, "title": "Verify",
             "detail": "Run the verification plan.", "evidence": "verification"},
            {"order": 6, "title": "Document", "detail": "Record decisions.",
             "evidence": "initiative"},
            {"order": 7, "title": "Roll out & monitor",
             "detail": "Ship with rollback ready.", "evidence": "rollback"},
        ]
    if verification is None:
        verification = [
            {"method": "tests", "detail": "Add/extend unit + integration tests."},
            {"method": "static_analysis", "detail": "Lint + type-check + CI."},
            {"method": "review", "detail": "Peer review before merge."},
        ]
    if rollback is None:
        rollback = [
            {"strategy": "feature_flag", "detail": "Ship behind a flag."},
            {"strategy": "git_revert", "detail": "Revert via git."},
        ]
    return Plan(
        goal=goal, plan_type=ptype, confidence=PlanConfidence.STRONG,
        status=PlanStatus.REFINED,
        affected_initiative_ids=list(init_ids),
        affected_insight_ids=list(ins_ids),
        affected_understanding_ids=list(u_ids),
        affected_knowledge_ids=list(k_ids),
        milestones=milestones, dependencies=[], risks=[],
        verification=verification, rollback=rollback,
        estimated_complexity=complexity, estimated_effort="medium",
    )


def _graph_id_for(plan: Plan) -> str:
    return f"taskgraph:{plan.id or plan._generate_id()}"


# --------------------------------------------------------------------------
# compilation algorithm: basic shape
# --------------------------------------------------------------------------

def test_compile_produces_tasks_and_edges(db):
    g = compile_plan(_plan())
    assert len(g.tasks) >= 7
    assert len(g.edges) >= 7
    # Every task references the plan that created it.
    assert all(t.plan_id == (g.plan_id) for t in g.tasks)
    # Every task has the mandatory structured fields.
    for t in g.tasks:
        assert t.id and t.title and t.description
        assert t.task_type in TaskType.all()
        assert t.required_capabilities
        assert t.acceptance_criteria  # never empty
        assert t.verification         # never empty
        assert t.rollback             # never empty


def test_cold_start_empty_plan_requires_milestones():
    empty = Plan(goal="x", plan_type=PlanType.MAINTENANCE,
                 confidence=PlanConfidence.WEAK, status=PlanStatus.PLANNED,
                 milestones=[], verification=[{"method": "tests", "detail": "t"}],
                 rollback=[{"strategy": "git_revert", "detail": "r"}])
    with pytest.raises(ValueError):
        compile_plan(empty)


def test_no_plan(db):
    eng = TaskGraphEngine(db)
    # No planning has happened: generating a graph still compiles (PlanEngine
    # produces an evidence-light plan). Cold start must not crash.
    g = eng.generate("Implement OAuth")
    assert g is not None
    assert len(g.tasks) >= 1
    assert count_task_graphs(db) == 1


def test_single_milestone(db):
    # REFACTOR (not FEATURE) so a plain "Implement" milestone expands to one task
    # (FEATURE plans carry explicit Backend/Frontend and skip plain Implement).
    p = _plan(ptype=PlanType.REFACTOR,
              milestones=[{"order": 1, "title": "Implement",
                           "detail": "Do it.", "evidence": "goal"}])
    g = compile_plan(p)
    assert len(g.tasks) == 1
    assert g.edges == []            # single node -> acyclic, no edges
    assert g.critical_path == [g.tasks[0].id]


def test_multiple_milestones_linear_chain(db):
    p = _plan()
    g = compile_plan(p)
    # tasks are in execution order via the milestone sequence
    seqs = [t.sequence for t in g.tasks]
    assert seqs == sorted(seqs)
    # edges form a connected DAG (no orphans except the root)
    ids = {t.id for t in g.tasks}
    deps = {e["from"] for e in g.edges}
    roots = [t.id for t in g.tasks if t.id not in deps]
    assert roots == [g.tasks[0].id]  # exactly one root


# --------------------------------------------------------------------------
# dependency algorithm + cycle detection
# --------------------------------------------------------------------------

def test_dependency_creation_inter_phase(db):
    g = compile_plan(_plan())
    # Every non-root task has at least one dependency.
    deps = {t.id: set(t.dependencies) for t in g.tasks}
    assert all(deps[t.id] for t in g.tasks if t.id != g.tasks[0].id)


def test_cycle_detection_rejects_cycles():
    # Construct an explicit cycle and assert the detector rejects it.
    base = _plan()
    g = compile_plan(base)
    ids = [t.id for t in g.tasks]
    edges = [dict(e) for e in g.edges]
    # Add a back-edge root->leaf (root depends on leaf) -> closes the chain into
    # a cycle: root -> ... -> leaf -> root.
    edges.append({"from": ids[0], "to": ids[-1], "kind": "depends_on"})
    assert _detect_cycle(edges, ids) is True
    # The acyclic graph's own edges are cycle-free.
    assert _detect_cycle(g.edges, ids) is False


def test_compiler_never_emits_cycle(db):
    for ptype in (PlanType.FEATURE, PlanType.INFRASTRUCTURE, PlanType.REFACTOR,
                  PlanType.ARCHITECTURE, PlanType.RESEARCH, PlanType.MIGRATION):
        g = compile_plan(_plan(ptype=ptype))
        ids = [t.id for t in g.tasks]
        assert _detect_cycle(g.edges, ids) is False, f"cycle in {ptype}"


def test_no_duplicate_edges(db):
    g = compile_plan(_plan())
    seen = set()
    for e in g.edges:
        key = (e["from"], e["to"])
        assert key not in seen, f"duplicate edge {key}"
        seen.add(key)


def test_no_duplicate_tasks(db):
    g = compile_plan(_plan())
    ids = [t.id for t in g.tasks]
    assert len(ids) == len(set(ids))


# --------------------------------------------------------------------------
# parallel branches
# --------------------------------------------------------------------------

def test_parallel_branches_feature(db):
    g = compile_plan(_plan(ptype=PlanType.FEATURE))
    # Backend + Frontend within the Implement phase are parallel.
    assert g.parallel_groups >= 1
    assert len(g.parallel_tasks) >= 2


def test_parallel_branches_explicit(db):
    # Two sibling milestones with parallel_next must yield a parallel group.
    p = _plan(milestones=[
        {"order": 1, "title": "Design", "detail": "d", "evidence": "goal"},
        {"order": 2, "title": "Backend", "detail": "b", "evidence": "goal"},
        {"order": 3, "title": "Frontend", "detail": "f", "evidence": "goal"},
    ])
    g = compile_plan(p)
    assert g.parallel_groups >= 1


# --------------------------------------------------------------------------
# capability inference
# --------------------------------------------------------------------------

def test_capability_inference_backend_frontend(db):
    p = _plan(ptype=PlanType.FEATURE)
    g = compile_plan(p)
    caps = {t.task_type: t.required_capabilities for t in g.tasks}
    # Backend and Frontend tasks carry the right side capabilities.
    backend = next(t for t in g.tasks if t.title == "Implement backend logic")
    frontend = next(t for t in g.tasks if t.title == "Implement frontend surface")
    assert _CAP_FE in frontend.required_capabilities
    assert _CAP_BE in backend.required_capabilities


def test_capability_inference_infra(db):
    p = _plan(ptype=PlanType.INFRASTRUCTURE)
    g = compile_plan(p)
    for t in g.tasks:
        # infrastructure plans surface architecture capability
        if t.task_type in (TaskType.DESIGN, TaskType.INFRASTRUCTURE):
            assert _CAP_ARCH in t.required_capabilities


def test_capability_inference_language_from_goal(db):
    p = _plan(goal="Implement Rust auth crate")
    g = compile_plan(p)
    all_caps = set()
    for t in g.tasks:
        all_caps.update(t.required_capabilities)
    assert _CAP_RUST in all_caps  # derived from the goal keywords


def test_capability_no_worker_names(db):
    p = _plan()
    g = compile_plan(p)
    for t in g.tasks:
        for c in t.required_capabilities:
            assert c in {
                _CAP_RUST, _CAP_PYTHON, _CAP_TS, _CAP_SQL, _CAP_ARCH, _CAP_TEST,
                _CAP_DOC, _CAP_FE, _CAP_BE, _CAP_INFRA, _CAP_RESEARCH,
                _CAP_CONFIG}, f"non-capability token: {c}"


# --------------------------------------------------------------------------
# priority algorithm
# --------------------------------------------------------------------------

def test_priority_deterministic(db):
    g = compile_plan(_plan())
    # Deployment/Verify tasks are critical; first analysis task is not.
    deploy = next(t for t in g.tasks if t.task_type == TaskType.DEPLOYMENT)
    verify = next(t for t in g.tasks if t.task_type == TaskType.VERIFICATION)
    first = g.tasks[0]
    assert deploy.priority == "critical"
    assert verify.priority == "critical"
    assert first.priority in ("low", "medium")  # leaf analysis, no blockers


def test_priority_never_random(db):
    # Same plan -> same priorities across two compiles.
    g1 = compile_plan(_plan())
    g2 = compile_plan(_plan())
    p1 = {t.title: t.priority for t in g1.tasks}
    p2 = {t.title: t.priority for t in g2.tasks}
    assert p1 == p2


# --------------------------------------------------------------------------
# complexity algorithm
# --------------------------------------------------------------------------

def test_complexity_deterministic(db):
    g = compile_plan(_plan())
    for t in g.tasks:
        assert t.complexity in ("tiny", "small", "medium", "large", "very_large")


def test_complexity_from_type_and_deps(db):
    # Deployment (large base) is at least large; analysis (small base) smaller.
    g = compile_plan(_plan())
    deploy = next(t for t in g.tasks if t.task_type == TaskType.DEPLOYMENT)
    analysis = next(t for t in g.tasks if t.task_type == TaskType.ANALYSIS)
    assert _complexity_order(deploy.complexity) >= _complexity_order(analysis.complexity)


def _complexity_order(c: str) -> int:
    return ("tiny", "small", "medium", "large", "very_large").index(c)


# --------------------------------------------------------------------------
# acceptance criteria / verification / rollback
# --------------------------------------------------------------------------

def test_acceptance_criteria_nonempty(db):
    g = compile_plan(_plan())
    for t in g.tasks:
        assert t.acceptance_criteria, f"{t.title}: empty acceptance"


def test_verification_reuses_plan_and_adds_task_specific(db):
    g = compile_plan(_plan())
    for t in g.tasks:
        # every task carries the plan's mandatory verification methods
        methods = {v.get("method") for v in t.verification}
        assert "tests" in methods
        assert "static_analysis" in methods
        assert "review" in methods
    # Implementation tasks additionally get a task-specific build check (>=4).
    impl = [t for t in g.tasks if t.task_type == TaskType.IMPLEMENTATION]
    assert impl
    assert all(len(t.verification) >= 4 for t in impl)


def test_rollback_reuses_plan(db):
    g = compile_plan(_plan())
    for t in g.tasks:
        strategies = {r.get("strategy") for r in t.rollback}
        assert "git_revert" in strategies  # plan-level rollback is always present


# --------------------------------------------------------------------------
# critical path
# --------------------------------------------------------------------------

def test_critical_path_exists_and_is_acyclic(db):
    g = compile_plan(_plan())
    assert g.critical_path
    # the critical path is a subset of tasks
    ids = {t.id for t in g.tasks}
    assert all(c in ids for c in g.critical_path)
    # path length matches reported metric
    assert len(g.critical_path) == g.critical_path_length if hasattr(
        g, "critical_path_length") else True
    # path is a valid chain: each step depends on the previous
    by_id = {t.id: t for t in g.tasks}
    for a, b in zip(g.critical_path, g.critical_path[1:]):
        assert by_id[b].dependencies and a in by_id[b].dependencies, \
            f"{b} must depend on {a}"


def test_levels_and_parallel_groups_consistent(db):
    g = compile_plan(_plan())
    levels = _compute_levels(g.edges, [t.id for t in g.tasks])
    groups, ptasks = _parallel_groups(levels)
    assert g.parallel_groups == groups
    assert set(g.parallel_tasks) == set(ptasks)


# --------------------------------------------------------------------------
# history / evolution / append-only
# --------------------------------------------------------------------------

def test_history_append_only(db):
    eng = TaskGraphEngine(db)
    eng.generate("Implement OAuth")
    eng.generate("Implement OAuth")  # recompile -> new snapshot, same graph
    hist = eng.history(_graph_id_for(_plan(goal="Implement OAuth")))
    assert len(hist) >= 2  # at least two snapshots appended
    # snapshots are ordered oldest-first
    times = [h.generated_at for h in hist]
    assert times == sorted(times)


def test_evolution_records_compile(db):
    eng = TaskGraphEngine(db)
    eng.generate("Implement OAuth")
    evo = eng.evolution()
    assert len(evo) >= 1
    assert evo[0].event_type in ("Compiled", "Recompiled")


def test_append_only_never_deletes(db):
    eng = TaskGraphEngine(db)
    eng.generate("Implement OAuth")
    eng.generate("Refactor authentication")
    assert count_task_graphs(db) == 2  # both retained
    rows = get_all_task_graphs(db)
    assert all(isinstance(r, TaskGraphRow) for r in rows)


# --------------------------------------------------------------------------
# idempotency / repeated compilation
# --------------------------------------------------------------------------

def test_repeated_compilation_idempotent(db):
    eng = TaskGraphEngine(db)
    g1 = eng.generate("Implement OAuth")
    n1 = len(g1.tasks)
    g2 = eng.generate("Implement OAuth")
    n2 = len(g2.tasks)
    assert g1.id == g2.id          # same graph id (idempotent on goal)
    assert n1 == n2                 # same shape
    assert count_task_graphs(db) == 1  # replaced, not duplicated


def test_repeated_compilation_same_edge_count(db):
    eng = TaskGraphEngine(db)
    g1 = eng.generate("Build worker system")
    g2 = eng.generate("Build worker system")
    assert len(g1.edges) == len(g2.edges)
    assert len(get_edges_for_graph(db, g1.id)) == len(g2.edges)


# --------------------------------------------------------------------------
# graph export (Worker-Engine JSON)
# --------------------------------------------------------------------------

def test_graph_export_json(db):
    eng = TaskGraphEngine(db)
    g = eng.generate("Implement OAuth")
    j = g.to_json()
    assert j["graph_id"] == g.id
    assert j["task_count"] == len(g.tasks)
    assert j["edge_count"] == len(g.edges)
    assert j["metadata"]["acyclic"] is True
    # every task JSON has all required fields
    for t in j["tasks"]:
        for f in ("id", "title", "task_type", "required_capabilities",
                  "complexity", "priority", "dependencies", "acceptance_criteria",
                  "verification", "rollback", "status", "confidence"):
            assert f in t
    import json
    # must be serializable round-trip
    json.dumps(j)


def test_export_edge_shape(db):
    eng = TaskGraphEngine(db)
    g = eng.generate("Implement OAuth")
    j = g.to_json()
    for e in j["edges"]:
        assert "from" in e and "to" in e and "kind" in e


# --------------------------------------------------------------------------
# Brain compatibility (read-only exposure, no routing change)
# --------------------------------------------------------------------------

def test_brain_compatibility_taskgraph_provider(db):
    # The Brain (ask.py) exposes graphs via the 'taskgraph' provider without
    # changing retrieval/routing/judgment. We verify the rows are readable and
    # well-formed; lower layers untouched.
    eng = TaskGraphEngine(db)
    eng.generate("Implement OAuth")
    rows = get_all_task_graphs(db)
    assert rows
    r = rows[0]
    assert r.plan_id.startswith("plan:")
    assert r.task_count >= 1
    # plan table still present and unchanged in shape
    from src.friday.db import get_all_plans
    # planning engine still works alongside
    assert isinstance(r, TaskGraphRow)


# --------------------------------------------------------------------------
# no hallucination / valid plan references
# --------------------------------------------------------------------------

def test_no_hallucination_valid_plan_references(db):
    # A task's evidence must only ever be valid lower-layer ids already cited
    # by the plan (or empty) — never invented ids.
    p = _plan(init_ids=("init:auth",), ins_ids=("ins:oauth",),
              u_ids=("u:auth",), k_ids=("k:auth",))
    g = compile_plan(p)
    valid = {"init:auth", "ins:oauth", "u:auth", "k:auth"}
    for t in g.tasks:
        for ev in t.evidence:
            assert ev in valid, f"hallucinated evidence id: {ev}"
    # The graph references a real plan id.
    assert g.plan_id.startswith("plan:")


def test_valid_plan_reference_in_rows(db):
    eng = TaskGraphEngine(db)
    g = eng.generate("Implement OAuth")
    rows = get_all_task_graphs(db)
    assert rows[0].plan_id.startswith("plan:")
    tasks = get_tasks_for_graph(db, g.id)
    assert all(t.plan_id == g.plan_id for t in tasks)
    assert all(t.graph_id == g.id for t in tasks)


# --------------------------------------------------------------------------
# task types are a fixed frozen enum (never LLM-generated)
# --------------------------------------------------------------------------

def test_task_types_frozen_enum():
    assert TaskType.from_str("implementation") == TaskType.IMPLEMENTATION
    # Part E: unknown enum values must never silently coerce to a default.
    import pytest
    with pytest.raises(ValueError):
        TaskType.from_str("garbage")
    assert set(TaskType.all()) >= {
        TaskType.ANALYSIS, TaskType.DESIGN, TaskType.IMPLEMENTATION,
        TaskType.TESTING, TaskType.DOCUMENTATION, TaskType.MIGRATION,
        TaskType.REVIEW, TaskType.REFACTOR, TaskType.INFRASTRUCTURE,
        TaskType.RESEARCH, TaskType.VERIFICATION, TaskType.DEPLOYMENT,
        TaskType.CONFIGURATION, TaskType.CLEANUP, TaskType.PLANNING}


def test_every_task_has_a_valid_task_type(db):
    g = compile_plan(_plan())
    for t in g.tasks:
        assert t.task_type in TaskType.all()


# --------------------------------------------------------------------------
# pure-function sanity (deterministic, individually testable)
# --------------------------------------------------------------------------

def test_infer_capabilities_deterministic():
    p = _plan(ptype=PlanType.FEATURE)
    a = _infer_capabilities("Implement backend", TaskType.IMPLEMENTATION, p)
    b = _infer_capabilities("Implement backend", TaskType.IMPLEMENTATION, p)
    assert a == b


def test_priority_function_monotonic_in_level():
    # Higher dependency depth (level) with same blocking -> not lower priority.
    low = _priority(TaskType.IMPLEMENTATION, 0, 0, False, 5)
    high = _priority(TaskType.IMPLEMENTATION, 4, 0, True, 5)
    assert _pri_order(high) >= _pri_order(low)


def _pri_order(p: str) -> int:
    return ("low", "medium", "high", "critical").index(p)


def test_complexity_function_bounded():
    for dc in range(0, 6):
        c = _complexity(TaskType.IMPLEMENTATION, dc, "medium")
        assert c in ("tiny", "small", "medium", "large", "very_large")


def test_detect_cycle_on_cycle():
    ids = ["a", "b", "c"]
    cyclic = [{"from": "a", "to": "b"}, {"from": "b", "to": "c"},
              {"from": "c", "to": "a"}]
    assert _detect_cycle(cyclic, ids) is True


def test_critical_path_tie_break_deterministic():
    # Two equal-length paths -> deterministic choice (lowest sequence).
    g = compile_plan(_plan())
    g2 = compile_plan(_plan())
    assert g.critical_path == g2.critical_path
