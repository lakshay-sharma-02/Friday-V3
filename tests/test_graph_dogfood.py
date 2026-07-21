"""Dogfood transcript for the Task Graph Compiler (Milestone 9.1).

Drives the REAL engines (no LLM, no mock) through the full live chain:

  Knowledge -> Understanding -> Initiatives -> Insights -> Plans -> Task Graphs

Then compiles task graphs for the spec's five goals and asserts every section
(tasks, edges, critical path, parallel tasks, acceptance criteria, verification,
rollback, capability inference) is present and deterministic. The Planning
Engine is FROZEN and untouched — we only feed it as normal and compile its
output. Workers will later consume ONLY the compiled graph.

Goals:
  "Implement OAuth"
  "Refactor authentication"
  "Extract shared Rust crates"
  "Build worker system"
  "Improve Vivaha architecture"
"""

from __future__ import annotations

import sqlite3

import pytest

from src.friday.db import (SCHEMA, _migrate, get_all_task_graphs,
                           get_tasks_for_graph, get_edges_for_graph)
from src.friday.knowledge.models import (
    Knowledge, KnowledgeConfidence, KnowledgeStatus, KnowledgeType)
from src.friday.knowledge.store import insert_knowledge, get_all_knowledge
from src.friday.understanding import UnderstandingEngine
from src.friday.understanding.models import (
    Understanding, UnderstandingConfidence, UnderstandingStatus, UnderstandingType)
from src.friday.understanding.engine import insert_understanding
from src.friday.initiative.models import (
    Initiative, InitiativeConfidence, InitiativeStatus, InitiativeType)
from src.friday.db import insert_initiative
from src.friday.initiative import InitiativeEngine
from src.friday.insight import InsightEngine
from src.friday.planning import PlanEngine, PlanType, TaskGraphEngine
from src.friday.planning.compiler import TaskType


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    yield conn
    conn.close()


_BASE = "2026-07-15T00:00:00+00:00"
_SEEN = set()
_N = 0


def _k(db, subject, stmt, ktype, kconf="medium", evidence_ids=("repo:a",)):
    global _N
    real = subject
    while real in _SEEN:
        _N += 1
        real = f"{subject}_{_N}"
    _SEEN.add(real)
    _N += 1
    insert_knowledge(db, [Knowledge(
        type=KnowledgeType.from_str(ktype), subject=real, statement=stmt,
        confidence=KnowledgeConfidence.from_str(kconf), evidence_ids=list(evidence_ids),
        status=KnowledgeStatus.VERIFIED, created_at=_BASE, updated_at=_BASE, id=None)])
    return real


def _u(db, subject, stmt, utype, uconf="medium", knowledge_subjects=()):
    kmap = {k.subject: k.id for k in get_all_knowledge(db)}
    kids = [kmap[x] for x in knowledge_subjects if x in kmap]
    insert_understanding(db, [Understanding(
        type=UnderstandingType.from_str(utype), subject=subject, statement=stmt,
        confidence=UnderstandingConfidence.from_str(uconf),
        status=UnderstandingStatus.OBSERVED, knowledge_ids=kids,
        build_at=_BASE, created_at=_BASE, updated_at=_BASE, id=None).to_row()])


def _i(db, title, itype="platform", repos=("repo:a",), u_subjects=(), k_subjects=()):
    u_by = {u.subject: u.id for u in UnderstandingEngine(db).all_understanding()}
    k_by = {k.subject: k.id for k in get_all_knowledge(db)}
    uids = [u_by[s] for s in u_subjects if s in u_by]
    kids = [k_by[s] for s in k_subjects if s in k_by]
    init = Initiative(
        type=InitiativeType.from_str(itype), title=title,
        status=InitiativeStatus.ACTIVE,
        confidence=InitiativeConfidence.from_str("medium"),
        participating_repositories=list(repos), understanding_ids=uids,
        knowledge_ids=kids, build_at=_BASE, started_at=_BASE, statement="",
        created_at=_BASE, updated_at=_BASE, id=None)
    insert_initiative(db, [init.to_row()])


def _seed(db):
    # Auth (repeated, multi-repo) -> reuse insight
    auth_a = _k(db, "auth", "auth solved in api", "recurring_pattern", "medium", ["repo:a"])
    auth_b = _k(db, "auth", "auth solved in web", "recurring_pattern", "medium", ["repo:b"])
    # Rust (heavy investment)
    rust1 = _k(db, "rust", "rust infra 1", "engineering_trend", "strong", ["repo:a"])
    rust2 = _k(db, "rust", "rust infra 2", "technology_investment", "strong", ["repo:b"])
    # Vivaha architecture knowledge
    viv = _k(db, "vivaha", "vivaha architecture", "project_architecture", "strong", ["repo:c"])
    # Worker / scheduling knowledge
    wk = _k(db, "worker", "worker scheduling", "engineering_trend", "medium", ["repo:a"])

    _u(db, "auth a", "Auth solved in api.", "engineering_habit", "medium", [auth_a])
    _u(db, "auth b", "Auth solved in web.", "engineering_habit", "medium", [auth_b])
    _u(db, "rust a", "Rust investment rising.", "engineering_direction", "strong",
       [rust1, rust2])
    _u(db, "rust b", "Rust direction strong.", "engineering_direction", "strong",
       [rust1, rust2])
    _u(db, "viv", "Vivaha structure.", "engineering_direction", "strong", [viv])
    _u(db, "wk", "Worker scheduling emerging.", "engineering_direction", "medium", [wk])

    _i(db, "Authentication Infrastructure", itype="infrastructure", repos=["repo:a", "repo:b"],
       u_subjects=["auth a", "auth b"])
    _i(db, "Engineering Platform", itype="platform", repos=["repo:a", "repo:b"],
       u_subjects=["rust a", "rust b"])
    _i(db, "Vivaha Platform", itype="platform", repos=["repo:c"], u_subjects=["viv"])
    _i(db, "Worker System", itype="infrastructure", repos=["repo:a"], u_subjects=["wk"])

    UnderstandingEngine(db).build()
    InitiativeEngine(db).build()
    InsightEngine(db).build()


GOALS = [
    "Implement OAuth",
    "Refactor authentication",
    "Extract shared Rust crates",
    "Build worker system",
    "Improve Vivaha architecture",
]


def test_dogfood_compile_all_goals(db):
    _seed(db)
    eng = TaskGraphEngine(db)
    graphs = [(g, eng.generate(g)) for g in GOALS]
    assert len(get_all_task_graphs(db)) >= 5
    # Every graph compiles into a non-trivial DAG.
    for goal, g in graphs:
        assert len(g.tasks) >= 3, f"{goal}: too few tasks"
        assert len(g.edges) >= 3, f"{goal}: too few edges"
        # acyclic
        ids = [t.id for t in g.tasks]
        from src.friday.planning.compiler import _detect_cycle
        assert _detect_cycle(g.edges, ids) is False, f"{goal}: cycle"


def test_dogfood_every_section_present(db):
    _seed(db)
    eng = TaskGraphEngine(db)
    for g in GOALS:
        graph = eng.generate(g)
        assert graph.tasks, f"{g}: no tasks"
        assert graph.edges or len(graph.tasks) == 1, f"{g}: edges"
        assert graph.critical_path, f"{g}: critical path"
        for t in graph.tasks:
            assert t.task_type in TaskType.all(), f"{g}: bad task type"
            assert t.required_capabilities, f"{g}: no capabilities for {t.title}"
            assert t.acceptance_criteria, f"{g}: no acceptance for {t.title}"
            assert t.verification, f"{g}: no verification for {t.title}"
            assert t.rollback, f"{g}: no rollback for {t.title}"
            # outputs optional; everything else mandatory present
            assert t.status == "pending"
            assert t.confidence in ("weak", "medium", "strong")


def test_dogfood_critical_path_and_parallel(db):
    _seed(db)
    eng = TaskGraphEngine(db)
    oauth = eng.generate("Implement OAuth")
    # Feature plan: Backend + Frontend run in parallel.
    assert oauth.parallel_groups >= 1
    assert len(oauth.parallel_tasks) >= 2
    # Critical path chains through the dependency DAG.
    by_id = {t.id: t for t in oauth.tasks}
    for a, b in zip(oauth.critical_path, oauth.critical_path[1:]):
        assert b in by_id[a].dependencies or a in by_id[b].dependencies, \
            "critical path must be a dependency chain"
    # every critical-path task is a real task
    assert all(c in by_id for c in oauth.critical_path)


def test_dogfood_capability_inference(db):
    _seed(db)
    eng = TaskGraphEngine(db)
    # Rust goal -> rust capability surfaces somewhere.
    rust = eng.generate("Extract shared Rust crates")
    all_caps = set()
    for t in rust.tasks:
        all_caps.update(t.required_capabilities)
    assert "rust" in all_caps
    # Worker system -> infrastructure capability.
    wk = eng.generate("Build worker system")
    wk_caps = set()
    for t in wk.tasks:
        wk_caps.update(t.required_capabilities)
    assert "infrastructure" in wk_caps


def test_dogfood_json_export_worker_ready(db):
    _seed(db)
    eng = TaskGraphEngine(db)
    g = eng.generate("Implement OAuth")
    j = g.to_json()
    import json
    payload = json.dumps(j)
    json.loads(payload)  # round-trip
    assert j["task_count"] == len(g.tasks)
    assert j["metadata"]["acyclic"] is True
    # Worker-facing fields present on every task.
    for t in j["tasks"]:
        for f in ("id", "task_type", "required_capabilities", "priority",
                  "dependencies", "acceptance_criteria", "verification",
                  "rollback", "status", "confidence"):
            assert f in t, f"missing {f} in exported task"


def test_dogfood_idempotency(db):
    _seed(db)
    eng = TaskGraphEngine(db)
    g1 = eng.generate("Implement OAuth")
    g2 = eng.generate("Implement OAuth")
    assert g1.id == g2.id
    assert len(g1.tasks) == len(g2.tasks)
    assert len(get_all_task_graphs(db)) == 1  # replaced, not duplicated


def test_dogfood_plan_layer_unchanged(db):
    _seed(db)
    eng = TaskGraphEngine(db)
    eng.generate("Implement OAuth")
    # Planning tables still intact; graph tables are ADDITIONAL.
    plans = PlanEngine(db).all_plans()
    assert plans, "plans must still exist (planning untouched)"
    assert all(not p.id.startswith("taskgraph:") for p in plans)
    graphs = get_all_task_graphs(db)
    assert all(g.plan_id.startswith("plan:") for g in graphs)
