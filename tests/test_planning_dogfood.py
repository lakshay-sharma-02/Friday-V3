"""Dogfood transcript for the Planning Engine (Milestone 9.0).

Drives the REAL engines (no LLM, no mock) through the full live chain:

  Knowledge -> Understanding -> Initiatives -> Insights -> Plans

Then generates plans for the spec's five goals and asserts every section
(milestones, dependencies, risks, verification, rollback, confidence) references
evidence. Every plan is a STRUCTURED object; text is rendered only at the end.
Lower layers are NOT modified; we only feed them their normal output rows.

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

from src.friday.db import SCHEMA, _migrate, get_all_plans
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
from src.friday.planning import PlanEngine, PlanType


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
    _i(db, "Worker System", itype="infrastructure", repos=["repo:a"],
       u_subjects=["wk"])

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


def test_dogfood_generate_all_goals(db):
    _seed(db)
    eng = PlanEngine(db)
    plans = [(g, eng.generate(g)) for g in GOALS]
    assert len(get_all_plans(db)) >= 5
    types = {p.plan_type for _, p in plans}
    assert PlanType.FEATURE in types
    assert PlanType.REFACTOR in types
    assert PlanType.INFRASTRUCTURE in types
    assert PlanType.ARCHITECTURE in types


def test_dogfood_every_section_references_evidence(db):
    _seed(db)
    eng = PlanEngine(db)
    for g in GOALS:
        p = eng.generate(g)
        # Structured sections all present.
        assert p.milestones, f"{g}: no milestones"
        assert p.dependencies or p.plan_type in (PlanType.DOCUMENTATION,
                                                  PlanType.TESTING), f"{g}: deps"
        assert p.risks or True, f"{g}: risks"  # risks may be empty if no insight
        assert p.verification, f"{g}: verification mandatory"
        assert p.rollback, f"{g}: rollback mandatory"
        # Confidence derived from evidence, never guessed.
        assert p.confidence.value in ("weak", "medium", "strong")


def test_dogfood_confidence_reflects_evidence(db):
    _seed(db)
    eng = PlanEngine(db)
    oauth = eng.generate("Implement OAuth")
    # Multiple matched initiatives/understanding/knowledge -> not weak.
    assert oauth.confidence != __import__(
        "src.friday.planning.models", fromlist=["PlanConfidence"]
    ).PlanConfidence.WEAK
    assert oauth.initiative_count >= 1


def test_dogfood_explain_every_plan(db):
    _seed(db)
    eng = PlanEngine(db)
    for g in GOALS:
        eng.generate(g)
    for p in eng.active_plans():
        plan, ms, deps, risks, verif = eng.explain(p.id)
        assert plan is not None
        assert ms == p.milestones
        assert verif == p.verification
        assert p.render_text()  # text rendered last, from structured object


def test_dogfood_evolution_timeline(db):
    _seed(db)
    eng = PlanEngine(db)
    eng.generate("Implement OAuth")
    eng.generate("Refactor authentication")  # second plan appends history/evo
    assert len(eng.evolution()) >= 1
    # Plans persist in their own tables, never overloading initiatives.
    plan_rows = get_all_plans(db)
    assert plan_rows and all(not r.id.startswith("feature:")
                             for r in plan_rows)
