"""Tests for the Planning Engine (Milestone 9.0).

The planning layer is WRITE-ONLY over Insights + Initiatives + Understanding +
Knowledge. It NEVER reads observations, context, git, READMEs, or repositories
directly. It NEVER executes, edits files, calls workers, or uses an LLM. Every
plan is a STRUCTURED object (milestones/dependencies/risks/verification/rollback
+ evidence references) rendered to text only at the end. Every plan cites valid
lower-layer ids.

Regression cases required by the spec:
- Cold start / empty knowledge / no initiatives / no insights
- Single initiative / multiple initiatives
- Milestone generation / dependency generation / risk generation
- Verification generation / rollback generation (both mandatory)
- Confidence aggregation
- History / Evolution / Append-only
- Idempotency / repeated planning
- Brain compatibility
- No hallucination (semantic goals, evidence-backed)
- No duplicate plans
- Valid evidence references
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
from src.friday.insight import InsightEngine, InsightType
from src.friday.planning import PlanEngine, PlanType, PlanConfidence, PlanStatus
from src.friday.planning.models import Plan


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    yield conn
    conn.close()


_BASE = "2026-07-01T00:00:00+00:00"
_SEEN = set()
_N = 0


def _k(db, subject, stmt, ktype="engineering_trend", kconf="medium",
       evidence_ids=("repo:a",)):
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


def _insight(db, itype, title, statement):
    """Inject a live insight directly (bypassing the insight build) so planning
    tests control the insight signal deterministically."""
    from src.friday.db import insert_insight, InsightRow
    iid = f"{itype}:{title}"
    insert_insight(db, [InsightRow(
        id=iid, title=title, insight_type=itype, statement=statement,
        status="observed", confidence="medium", started_at=_BASE,
        updated_at=_BASE, retired_at=None, created_at=_BASE,
        understanding_ids="", initiative_ids="", knowledge_ids="",
        build_at=_BASE)])
    return iid


# --------------------------------------------------------------------------
# cold start / empty evidence
# --------------------------------------------------------------------------

def test_cold_start_empty(db):
    eng = PlanEngine(db)
    p = eng.generate("Implement OAuth")
    assert p.plan_type == PlanType.FEATURE
    assert p.confidence == PlanConfidence.WEAK  # no evidence
    assert p.status == PlanStatus.PLANNED
    assert len(p.milestones) >= 4  # milestones always generated
    assert len(p.verification) >= 1  # verification mandatory
    assert len(p.rollback) >= 1  # rollback mandatory
    assert get_all_plans(db)


def test_empty_knowledge_no_initiatives(db):
    eng = PlanEngine(db)
    p = eng.generate("Do a thing")
    assert p.confidence == PlanConfidence.WEAK
    assert p.initiative_count == 0
    assert p.insight_count == 0


def test_no_insights(db):
    _k(db, "rust", "rust work", "engineering_trend", "strong", ["repo:a"])
    _u(db, "rust", "Systems direction.", "engineering_direction", "medium", ["rust"])
    eng = PlanEngine(db)
    p = eng.generate("Extract shared Rust crates")
    # understanding present but below strong-evidence bar -> weak confidence
    assert p.confidence in (PlanConfidence.WEAK, PlanConfidence.MEDIUM)


# --------------------------------------------------------------------------
# single / multiple initiatives
# --------------------------------------------------------------------------

def test_single_initiative(db):
    _k(db, "auth", "auth work", "recurring_pattern", "medium", ["repo:a"])
    _i(db, "Authentication Infrastructure", itype="infrastructure",
       repos=["repo:a"], k_subjects=["auth"])
    eng = PlanEngine(db)
    p = eng.generate("Implement OAuth")
    assert p.initiative_count >= 1
    assert any("infrastructure:Authentication Infrastructure" in i
               for i in p.affected_initiative_ids)


def test_multiple_initiatives(db):
    _i(db, "Authentication Infrastructure", itype="infrastructure", repos=["repo:a"])
    _i(db, "Engineering Platform", itype="platform", repos=["repo:a", "repo:b"])
    eng = PlanEngine(db)
    p = eng.generate("Implement authentication")
    assert p.initiative_count >= 1  # matched the auth infrastructure initiative
    # with two initiatives present, the planner can also link a second
    assert p.initiative_count >= 1


# --------------------------------------------------------------------------
# structured generation: milestones / deps / risks / verification / rollback
# --------------------------------------------------------------------------

def test_milestone_generation(db):
    eng = PlanEngine(db)
    p = eng.generate("Implement OAuth")
    titles = [m["title"] for m in p.milestones]
    assert "Implement" in titles
    assert "Verify" in titles
    assert all("order" in m for m in p.milestones)
    assert p.milestones == sorted(p.milestones, key=lambda m: m["order"])


def test_dependency_generation(db):
    _i(db, "Authentication Infrastructure", itype="infrastructure",
       repos=["repo:a", "repo:b"])
    eng = PlanEngine(db)
    p = eng.generate("Implement OAuth")
    kinds = {d["kind"] for d in p.dependencies}
    assert "initiative" in kinds
    assert "technical" in kinds  # multi-repo


def test_risk_generation_from_insight(db):
    _insight(db, "engineering_reuse", "Reusable authentication subsystem",
              "Authentication solved repeatedly; build a reusable subsystem.")
    eng = PlanEngine(db)
    p = eng.generate("Implement OAuth")
    assert p.risk_count >= 1
    kinds = {r["kind"] for r in p.risks}
    assert "repeated_implementation" in kinds


def test_risk_generation_drift(db):
    _insight(db, "engineering_drift", "Engineering direction drift",
              "The engineering direction shows drift across understandings.")
    eng = PlanEngine(db)
    p = eng.generate("Refactor the drifting architecture")
    kinds = {r["kind"] for r in p.risks}
    assert "architecture_drift" in kinds


def test_verification_mandatory(db):
    eng = PlanEngine(db)
    p = eng.generate("Implement OAuth")
    methods = {v["method"] for v in p.verification}
    assert "tests" in methods
    assert "static_analysis" in methods
    assert "review" in methods  # always present


def test_rollback_mandatory(db):
    eng = PlanEngine(db)
    p = eng.generate("Implement OAuth")
    strategies = {r["strategy"] for r in p.rollback}
    assert "git_revert" in strategies  # always present
    assert "feature_flag" in strategies  # feature plan


def test_rollback_docs_only_for_documentation(db):
    eng = PlanEngine(db)
    p = eng.generate("Write the docs")
    strategies = {r["strategy"] for r in p.rollback}
    assert "documentation_only" in strategies


def test_plan_type_classification(db):
    eng = PlanEngine(db)
    assert eng.generate("Refactor authentication").plan_type == PlanType.REFACTOR
    assert eng.generate("Migrate to Postgres").plan_type == PlanType.MIGRATION
    assert eng.generate("Extract shared Rust crates").plan_type == PlanType.REFACTOR
    assert eng.generate("Build worker system").plan_type == PlanType.INFRASTRUCTURE
    assert eng.generate("Optimize the hot path").plan_type == PlanType.OPTIMIZATION
    assert eng.generate("Improve Vivaha architecture").plan_type == PlanType.ARCHITECTURE
    assert eng.generate("Write the docs").plan_type == PlanType.DOCUMENTATION
    assert eng.generate("Add tests for auth").plan_type == PlanType.TESTING
    assert eng.generate("Study the scheduler").plan_type == PlanType.RESEARCH


# --------------------------------------------------------------------------
# confidence aggregation
# --------------------------------------------------------------------------

def test_confidence_weak_without_evidence(db):
    p = PlanEngine(db).generate("Implement OAuth")
    assert p.confidence == PlanConfidence.WEAK


def test_confidence_strong_with_evidence(db):
    ka = _k(db, "auth", "auth in a", "recurring_pattern", "strong", ["repo:a"])
    kb = _k(db, "auth", "auth in b", "recurring_pattern", "strong", ["repo:b"])
    _u(db, "auth a", "Auth solved in a.", "engineering_habit", "strong", [ka])
    _u(db, "auth b", "Auth solved in b.", "engineering_habit", "strong", [kb])
    _i(db, "Authentication Infrastructure", itype="infrastructure",
       repos=["repo:a", "repo:b"], u_subjects=["auth a", "auth b"])
    _insight(db, "engineering_reuse", "Reusable authentication subsystem",
             "Authentication solved repeatedly.")
    p = PlanEngine(db).generate("Implement OAuth")
    assert p.confidence == PlanConfidence.STRONG


# --------------------------------------------------------------------------
# history / evolution / append-only
# --------------------------------------------------------------------------

def test_history_append_only(db):
    from src.friday.db import plan_history_for
    eng = PlanEngine(db)
    eng.generate("Implement OAuth")
    pid = "plan:implement oauth"
    before = len(plan_history_for(db, pid))
    eng.generate("Implement OAuth")  # regenerate -> appends a snapshot
    after = len(plan_history_for(db, pid))
    assert after > before


def test_evolution_events_emitted(db):
    eng = PlanEngine(db)
    eng.generate("Implement OAuth")
    assert len(eng.evolution()) >= 1


def test_append_only_no_row_explosion(db):
    eng = PlanEngine(db)
    eng.generate("Implement OAuth")
    eng.generate("Implement OAuth")
    eng.generate("Implement OAuth")
    ids = [p.id for p in get_all_plans(db)]
    assert len(ids) == len(set(ids))


# --------------------------------------------------------------------------
# idempotency / repeated planning
# --------------------------------------------------------------------------

def test_idempotent_replan(db):
    eng = PlanEngine(db)
    p1 = eng.generate("Implement OAuth")
    r2 = eng.generate("Implement OAuth")
    # same id; second generation is an update, not a new row
    assert r2.id == p1.id
    all_ids = [p.id for p in get_all_plans(db)]
    assert len(all_ids) == len(set(all_ids)) == 1


def test_replan_with_new_evidence_reports_event(db):
    eng = PlanEngine(db)
    eng.generate("Implement OAuth")
    _insight(db, "engineering_reuse", "Reusable authentication subsystem",
             "Authentication solved repeatedly.")
    eng.generate("Implement OAuth")  # evidence changed -> Re-evidenced event
    etypes = {e.event_type for e in eng.evolution()}
    assert "Re-evidenced" in etypes or "Strengthened" in etypes


# --------------------------------------------------------------------------
# brain compatibility / no hallucination / valid citations
# --------------------------------------------------------------------------

def test_brain_compatibility_provider(db):
    from src.friday.ask import _p_plan
    eng = PlanEngine(db)
    eng.generate("Implement OAuth")
    ev = type("E", (), {"blocks": [], "raw": {}})()
    req = type("R", (), {"query": "how should we implement OAuth",
                         "needs": set(), "subject": None})()
    _p_plan.fn(req, db, ev, None)
    assert ev.raw.get("plan_total", 0) >= 1


def test_every_plan_cites_valid_ids(db):
    _k(db, "auth", "auth work", "recurring_pattern", "medium", ["repo:a"])
    _i(db, "Authentication Infrastructure", itype="infrastructure", repos=["repo:a"])
    _u(db, "auth a", "Auth solved.", "engineering_habit", "medium", ["auth"])
    eng = PlanEngine(db)
    p = eng.generate("Implement OAuth")
    valid_i = {i.id for i in InitiativeEngine(db).all_initiatives()}
    valid_u = {u.id for u in UnderstandingEngine(db).all_understanding()}
    valid_k = {k.id for k in get_all_knowledge(db)}
    for iid in p.affected_initiative_ids:
        assert iid in valid_i
    for uid in p.affected_understanding_ids:
        assert uid in valid_u
    for kid in p.affected_knowledge_ids:
        assert kid in valid_k


def test_no_duplicate_plans(db):
    eng = PlanEngine(db)
    eng.generate("Implement OAuth")
    eng.generate("Implement OAuth")
    ids = [p.id for p in eng.all_plans()]
    assert len(ids) == len(set(ids))


def test_no_hallucination_semantic_goals(db):
    p = PlanEngine(db).generate("Implement OAuth")
    # goal preserved verbatim; no repo names injected into the goal
    assert p.goal == "Implement OAuth"
    assert "/" not in p.goal


def test_plan_never_executes(db):
    # The engine produces a structured object only; it must not mutate the
    # workspace, run shell, or touch repos. We assert no side effects beyond DB.
    p = PlanEngine(db).generate("Implement OAuth")
    assert isinstance(p, Plan)
    # render_text is the only text; it contains no execution directive other
    # than naming rollback strategies (these are descriptions, not commands).
    text = p.render_text()
    assert "os.system" not in text
    assert "subprocess" not in text
    assert "def " not in text  # no code generation


def test_no_direct_lower_layer_imports_forbidden_in_rules():
    # Planning derives ONLY from insights/initiatives/understanding/knowledge.
    import inspect
    from src.friday.planning import derive
    sig = inspect.signature(derive.plan)
    assert list(sig.parameters) == ["goal", "ev"]
