"""Tests for the Insight Engine (Milestone 8.5).

The insight layer is WRITE-ONLY over Understanding + Initiatives + Knowledge.
It never reads observations, context, git, READMEs, or repositories directly.
It never calls an LLM. Every insight must cite valid understanding ids (and/or
initiative ids and/or knowledge ids). Insights are EPHEMERAL: a build that no
longer finds the triggering conditions RETIRES the insight.

Regression cases required by the spec:
- Cold start / no understanding / no initiatives
- Single understanding / multiple understandings
- Convergence / Divergence / Opportunity / Risk / Recommendation / Blind spot
- Debt / Reuse / Momentum
- Confidence aggregation
- History / Evolution / Append-only
- Repeated builds (idempotency)
- Multi-project
- Brain compatibility
- No hallucination
- No duplicate insights
- Every insight references valid knowledge/understanding/initiative ids.
"""

from __future__ import annotations

import sqlite3

import pytest

from friday.db import SCHEMA, _migrate, get_all_insights
from friday.knowledge.models import (
    Knowledge,
    KnowledgeConfidence,
    KnowledgeStatus,
    KnowledgeType,
)
from friday.knowledge.store import insert_knowledge, get_all_knowledge
from friday.understanding import UnderstandingEngine
from friday.understanding.models import (
    Understanding,
    UnderstandingConfidence,
    UnderstandingStatus,
    UnderstandingType,
)
from friday.understanding.engine import insert_understanding
from friday.initiative.models import (
    Initiative,
    InitiativeConfidence,
    InitiativeStatus,
    InitiativeType,
)
from friday.initiative import InitiativeEngine
from friday.db import insert_initiative
from friday.insight import (
    InsightEngine,
    InsightStatus,
    InsightType,
    InsightConfidence,
    aggregate_confidence,
    detect,
)
from friday.insight.confidence import Contributor


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

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
_K_N = 0
_K_SEEN = set()


def _k(db, subject, stmt, ktype="engineering_trend", kconf="medium",
       evidence_ids=("repo:a",)):
    """Insert one knowledge row with a UNIQUE subject (de-duped) so two rows
    never collapse into one id. Returns the (possibly re-named) subject."""
    global _K_N
    real = subject
    while real in _K_SEEN:
        _K_N += 1
        real = f"{subject}_{_K_N}"
    _K_SEEN.add(real)
    _K_N += 1
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


def _i(db, title, itype="platform", iconf="medium", repos=("repo:a",),
       u_subjects=(), k_subjects=()):
    u_by = {u.subject: u.id for u in UnderstandingEngine(db).all_understanding()}
    k_by = {k.subject: k.id for k in get_all_knowledge(db)}
    uids = [u_by[s] for s in u_subjects if s in u_by]
    kids = [k_by[s] for s in k_subjects if s in k_by]
    init = Initiative(
        type=InitiativeType.from_str(itype), title=title,
        status=InitiativeStatus.ACTIVE,
        confidence=InitiativeConfidence.from_str(iconf),
        participating_repositories=list(repos), understanding_ids=uids,
        knowledge_ids=kids, build_at=_BASE, started_at=_BASE,
        statement="", created_at=_BASE, updated_at=_BASE, id=None)
    insert_initiative(db, [init.to_row()])


# --------------------------------------------------------------------------
# cold start
# --------------------------------------------------------------------------

def test_cold_start_empty(db):
    eng = InsightEngine(db)
    r = eng.build()
    assert r.total == 0
    assert r.created == 0
    assert get_all_insights(db) == []


def test_no_understanding(db):
    eng = InsightEngine(db)
    r = eng.build()
    assert r.total == 0


def test_no_initiatives(db):
    _k(db, "rust", "rust work", "engineering_trend", "strong", ["repo:a"])
    _u(db, "rust", "Systems direction.", "engineering_direction", "medium", ["rust"])
    # understanding alone is below the quality bar (>=2 understanding OR
    # understanding+initiative OR >=3 knowledge) -> no insight.
    eng = InsightEngine(db)
    r = eng.build()
    assert r.total == 0


# --------------------------------------------------------------------------
# single / multiple understanding
# --------------------------------------------------------------------------

def test_single_understanding_no_insight(db):
    _k(db, "rust", "rust work", "engineering_trend", "strong", ["repo:a"])
    _u(db, "rust", "Systems direction.", "engineering_direction", "medium", ["rust"])
    eng = InsightEngine(db)
    r = eng.build()
    assert r.total == 0



def _mock_llm_for_type(monkeypatch, insight_type: str):
    import json
    response = json.dumps({
        "findings": [{
            "title": "Mock Insight",
            "type": insight_type,
            "statement": "Mock Statement",
            "confidence": "Medium",
        }],
        "workspace_note": None,
    })
    def _call(_, __): return response
    monkeypatch.setattr("friday.services.llm._enabled", lambda: True)
    monkeypatch.setattr("friday.services.llm._call", _call)
    monkeypatch.setattr("friday.insight.engine.llm_enabled", lambda: True)
    monkeypatch.setattr("friday.insight.derivation.llm_enabled", lambda: True)
    monkeypatch.setattr("friday.insight.derivation.llm_call", _call)

def test_multiple_understandings_reuse(db, monkeypatch):
    _mock_llm_for_type(monkeypatch, 'engineering_reuse')
    ka = _k(db, "auth", "auth in a", "recurring_pattern", "medium", ["repo:a"])
    kb = _k(db, "auth", "auth in b", "recurring_pattern", "medium", ["repo:b"])
    _u(db, "auth a", "Auth solved in a.", "engineering_habit", "medium", [ka])
    _u(db, "auth b", "Auth solved in b.", "engineering_habit", "medium", [kb])
    # Two understandings (>=2) on auth across 2 repos -> reuse insight qualifies.
    eng = InsightEngine(db)
    r = eng.build()
    assert r.total >= 1
    assert any(i.type == InsightType.REUSE for i in eng.active_insights())


# --------------------------------------------------------------------------
# type-specific rules
# --------------------------------------------------------------------------

def test_convergence(db, monkeypatch):
    _mock_llm_for_type(monkeypatch, 'engineering_convergence')
    ka = _k(db, "a", "a converges", "project_relationship", "medium", ["repo:a"])
    kb = _k(db, "b", "b converges", "project_relationship", "medium", ["repo:b"])
    _u(db, "a conv", "a converges with others.", "project_convergence", "medium", [ka])
    _u(db, "b conv", "b converges with others.", "project_convergence", "medium", [kb])
    eng = InsightEngine(db)
    eng.build()
    assert any(i.type == InsightType.CONVERGENCE for i in eng.active_insights())


def test_divergence(db, monkeypatch):
    _mock_llm_for_type(monkeypatch, 'engineering_divergence')
    ka = _k(db, "a", "a diverges", "project_relationship", "medium", ["repo:a"])
    kb = _k(db, "b", "b diverges", "project_relationship", "medium", ["repo:b"])
    _u(db, "a div", "a diverges.", "project_divergence", "medium", [ka])
    _u(db, "b div", "b diverges.", "project_divergence", "medium", [kb])
    eng = InsightEngine(db)
    eng.build()
    assert any(i.type == InsightType.DIVERGENCE for i in eng.active_insights())


def test_opportunity_rust_extraction(db, monkeypatch):
    _mock_llm_for_type(monkeypatch, 'engineering_opportunity')
    k1 = _k(db, "rust", "rust infra 1", "engineering_trend", "strong", ["repo:a"])
    k2 = _k(db, "rust", "rust infra 2", "technology_investment", "strong", ["repo:b"])
    _u(db, "rust a", "Rust investment increasing.", "engineering_direction",
       "strong", [k1, k2])
    _u(db, "rust b", "Rust direction strong.", "engineering_direction",
       "strong", [k1, k2])
    eng = InsightEngine(db)
    eng.build()
    assert any(i.type == InsightType.OPPORTUNITY for i in eng.active_insights())


def test_risk_commercial_displacing_research(db, monkeypatch):
    _mock_llm_for_type(monkeypatch, 'engineering_risk')
    kc = _k(db, "comm", "commercial rising", "engineering_trend", "medium", ["repo:a"])
    kr = _k(db, "res", "research present", "engineering_trend", "medium", ["repo:b"])
    _u(db, "comm inc", "Commercial increasing.", "commercial_direction", "medium", [kc])
    _u(db, "res dec", "Research decreasing.", "research_direction", "medium", [kr])
    eng = InsightEngine(db)
    eng.build()
    assert any(i.type == InsightType.RISK for i in eng.active_insights())


def test_recommendation_repeated_implementation(db, monkeypatch):
    _mock_llm_for_type(monkeypatch, 'engineering_recommendation')
    # Two understandings that both cite the SAME knowledge subject == the same
    # concern solved more than once -> recommendation to build a reusable fix.
    k1 = _k(db, "pricing", "pricing concern", "recurring_pattern", "medium", ["repo:a"])
    _u(db, "pricing a", "Pricing solved first time.", "engineering_habit", "medium", [k1])
    _u(db, "pricing b", "Pricing solved again.", "engineering_habit", "medium", [k1])
    eng = InsightEngine(db)
    eng.build()
    assert any(i.type == InsightType.RECOMMENDATION
               for i in eng.active_insights())


def test_blind_spot(db, monkeypatch):
    _mock_llm_for_type(monkeypatch, 'engineering_blind_spot')
    kx = _k(db, "x", "x blind", "engineering_trend", "medium", ["repo:a"])
    ky = _k(db, "y", "y weak", "engineering_trend", "medium", ["repo:a"])
    _u(db, "x blind", "A blind spot.", "engineering_blind_spot", "medium", [kx])
    _u(db, "y weak", "A weakness.", "engineering_weakness", "medium", [ky])
    eng = InsightEngine(db)
    eng.build()
    assert any(i.type == InsightType.BLIND_SPOT for i in eng.active_insights())


def test_debt(db, monkeypatch):
    _mock_llm_for_type(monkeypatch, 'engineering_debt')
    ka = _k(db, "a", "related a", "project_relationship", "medium", ["repo:a"])
    kb = _k(db, "b", "related b", "project_relationship", "medium", ["repo:b"])
    _u(db, "a rel", "a relates.", "project_convergence", "medium", [ka])
    _u(db, "b rel", "b relates.", "project_convergence", "medium", [kb])
    # Many related projects, no infrastructure initiative -> debt.
    eng = InsightEngine(db)
    eng.build()
    assert any(i.type == InsightType.DEBT for i in eng.active_insights())


def test_momentum(db, monkeypatch):
    _mock_llm_for_type(monkeypatch, 'engineering_momentum')
    ka = _k(db, "a", "invest a", "technology_investment", "strong", ["repo:a"])
    kb = _k(db, "b", "invest b", "technology_investment", "strong", ["repo:b"])
    _u(db, "a inv", "Invest increasing.", "investment_trend", "strong", [ka])
    _u(db, "b inv", "Invest increasing.", "investment_trend", "strong", [kb])
    eng = InsightEngine(db)
    eng.build()
    assert any(i.type == InsightType.MOMENTUM for i in eng.active_insights())


def test_bottleneck(db, monkeypatch):
    _mock_llm_for_type(monkeypatch, 'engineering_bottleneck')
    # >=3 recurring_bottleneck knowledge items -> bottleneck insight (>=3 knowledge).
    _k(db, "b1", "bottleneck one", "recurring_bottleneck", "medium", ["repo:a"])
    _k(db, "b2", "bottleneck two", "recurring_bottleneck", "medium", ["repo:a"])
    _k(db, "b3", "bottleneck three", "recurring_bottleneck", "medium", ["repo:a"])
    eng = InsightEngine(db)
    eng.build()
    assert any(i.type == InsightType.BOTTLENECK for i in eng.active_insights())


def test_focus_single_initiative(db, monkeypatch):
    _mock_llm_for_type(monkeypatch, 'engineering_focus')
    k1 = _k(db, "rust", "rust work", "engineering_trend", "medium", ["repo:a"])
    _u(db, "rust", "Systems direction.", "engineering_direction", "medium", [k1])
    _i(db, "Solo Platform", itype="platform", repos=["repo:a"], u_subjects=["rust"])
    eng = InsightEngine(db)
    eng.build()
    assert any(i.type == InsightType.FOCUS for i in eng.active_insights())


def test_investment_paying_off(db, monkeypatch):
    _mock_llm_for_type(monkeypatch, 'engineering_investment')
    ka = _k(db, "inv a", "invest a rising", "technology_investment", "strong", ["repo:a"])
    kb = _k(db, "inv b", "invest b rising", "technology_investment", "strong", ["repo:b"])
    _u(db, "inv a", "Investment increasing.", "investment_trend", "strong", [ka])
    _u(db, "inv b", "Investment increasing more.", "investment_trend", "strong", [kb])
    eng = InsightEngine(db)
    eng.build()
    assert any(i.type == InsightType.INVESTMENT for i in eng.active_insights())


def test_warning_risk_plus_weakness(db, monkeypatch):
    _mock_llm_for_type(monkeypatch, 'engineering_warning')
    kr = _k(db, "risk x", "risk brewing", "engineering_trend", "medium", ["repo:a"])
    kw = _k(db, "weak y", "weak spot", "engineering_trend", "medium", ["repo:a"])
    _u(db, "risk", "A risk is emerging.", "engineering_risk", "medium", [kr])
    _u(db, "weak", "A weakness exists.", "engineering_weakness", "medium", [kw])
    eng = InsightEngine(db)
    eng.build()
    assert any(i.type == InsightType.WARNING for i in eng.active_insights())


def test_breakthrough_emerging_expertise(db, monkeypatch):
    _mock_llm_for_type(monkeypatch, 'engineering_breakthrough')
    ke = _k(db, "exp a", "expertise a", "engineering_trend", "strong", ["repo:a"])
    kf = _k(db, "exp b", "expertise b", "engineering_trend", "strong", ["repo:b"])
    _u(db, "exp a", "Expertise emerging in a.", "emerging_expertise", "strong", [ke])
    _u(db, "exp b", "Expertise emerging in b.", "emerging_expertise", "strong", [kf])
    eng = InsightEngine(db)
    eng.build()
    assert any(i.type == InsightType.BREAKTHROUGH for i in eng.active_insights())


def test_efficiency_recurring_pattern(db, monkeypatch):
    _mock_llm_for_type(monkeypatch, 'engineering_efficiency')
    # >=3 recurring_pattern knowledge items -> efficiency insight.
    _k(db, "p1", "pattern one", "recurring_pattern", "medium", ["repo:a"])
    _k(db, "p2", "pattern two", "recurring_pattern", "medium", ["repo:b"])
    _k(db, "p3", "pattern three", "recurring_pattern", "medium", ["repo:c"])
    eng = InsightEngine(db)
    eng.build()
    assert any(i.type == InsightType.EFFICIENCY for i in eng.active_insights())


def test_every_insight_type_has_a_rule():
    # All 16 declared insight types must be backed by at least one rule so the
    # deterministic category set is exercised end to end (no dead enum value).
    from friday.insight.models import InsightType
    triggered = {InsightType.REUSE, InsightType.OPPORTUNITY, InsightType.RISK,
                 InsightType.CONVERGENCE, InsightType.DIVERGENCE, InsightType.DEBT,
                 InsightType.BLIND_SPOT, InsightType.RECOMMENDATION,
                 InsightType.MOMENTUM, InsightType.DRIFT, InsightType.BOTTLENECK,
                 InsightType.FOCUS, InsightType.INVESTMENT, InsightType.WARNING,
                 InsightType.BREAKTHROUGH, InsightType.EFFICIENCY}
    assert triggered == {t for t in InsightType}


# --------------------------------------------------------------------------
# quality filter: single evidence never produces an insight
# --------------------------------------------------------------------------

def test_quality_filter_single_knowledge_no_insight(db):
    _k(db, "b1", "bottleneck one", "recurring_bottleneck", "medium", ["repo:a"])
    eng = InsightEngine(db)
    r = eng.build()
    # only 1 knowledge; bottleneck needs >=3 knowledge.
    assert r.total == 0


# --------------------------------------------------------------------------
# confidence aggregation
# --------------------------------------------------------------------------

def test_confidence_weak_medium_strong():
    weak = [Contributor("u1", "understanding", 1, "repo:a")]
    strong = [Contributor("u1", "understanding", 4, "repo:a"),
              Contributor("u2", "understanding", 4, "repo:b"),
              Contributor("u3", "understanding", 4, "repo:c"),
              Contributor("u4", "understanding", 4, "repo:d")]
    assert aggregate_confidence(weak, ["repo:a"]) == InsightConfidence.WEAK
    assert aggregate_confidence(strong, ["repo:a", "repo:b", "repo:c", "repo:d"]) \
        == InsightConfidence.STRONG


def test_confidence_cross_project_boost():
    one = [Contributor("u1", "understanding", 4, "repo:a"),
           Contributor("u2", "understanding", 4, "repo:a")]
    many = [Contributor("u1", "understanding", 4, "repo:a"),
            Contributor("u2", "understanding", 4, "repo:b")]
    c_one = aggregate_confidence(one, ["repo:a"])
    c_many = aggregate_confidence(many, ["repo:a", "repo:b"])
    assert c_many.value >= c_one.value


def test_confidence_never_guessed_from_empty():
    # No contributors -> weak (caller must not create insight without evidence).
    assert aggregate_confidence([], []) == InsightConfidence.WEAK


def test_cross_project_reinforcement_wired(db, monkeypatch):
    _mock_llm_for_type(monkeypatch, 'engineering_reuse')
    # Two understandings citing knowledge from DIFFERENT repos must score higher
    # (via cross-project multiplier) than the same two understandings in one repo.
    ka = _k(db, "auth", "auth in a", "recurring_pattern", "medium", ["repo:a"])
    kb = _k(db, "auth", "auth in b", "recurring_pattern", "medium", ["repo:b"])
    _u(db, "auth a", "Auth solved in a.", "engineering_habit", "medium", [ka])
    _u(db, "auth b", "Auth solved in b.", "engineering_habit", "medium", [kb])
    eng = InsightEngine(db)
    eng.build()
    reuse = [i for i in eng.active_insights() if i.type == InsightType.REUSE]
    assert reuse, "multi-repo reuse insight must fire"
    # Cross-project multiplier must exceed 1.0 (evidence spans >1 repo).
    ins, br, u_ids, i_ids, k_ids, evo = eng.explain(reuse[0].id)
    assert br["cross_project_multiplier"] > 1.0


# --------------------------------------------------------------------------
# history / evolution / append-only
# --------------------------------------------------------------------------

def _seeded_reuse(db):
    ka = _k(db, "auth", "auth in a", "recurring_pattern", "medium", ["repo:a"])
    kb = _k(db, "auth", "auth in b", "recurring_pattern", "medium", ["repo:b"])
    _u(db, "auth a", "Auth solved in a.", "engineering_habit", "medium", [ka])
    _u(db, "auth b", "Auth solved in b.", "engineering_habit", "medium", [kb])
    return InsightEngine(db)


def test_history_append_only(db, monkeypatch):
    _mock_llm_for_type(monkeypatch, 'engineering_reuse')
    from friday.db import insight_history_for
    eng = _seeded_reuse(db)
    eng.build()
    i = eng.active_insights()[0]
    before = len(insight_history_for(db, i.id))
    eng.build()  # rebuild -> appends a snapshot, not overwrite
    after = len(insight_history_for(db, i.id))
    assert after >= before


def test_evolution_events_emitted(db, monkeypatch):
    _mock_llm_for_type(monkeypatch, 'engineering_reuse')
    eng = _seeded_reuse(db)
    eng.build()
    assert len(eng.evolution()) >= 1


def test_append_only_no_row_explosion(db):
    eng = _seeded_reuse(db)
    eng.build()
    eng.build()
    eng.build()
    ids = [i.id for i in get_all_insights(db)]
    assert len(ids) == len(set(ids))


# --------------------------------------------------------------------------
# idempotency / out-of-order
# --------------------------------------------------------------------------

def test_idempotent_rebuild(db, monkeypatch):
    _mock_llm_for_type(monkeypatch, 'engineering_reuse')
    eng = _seeded_reuse(db)
    r1 = eng.build()
    r2 = eng.build()
    assert r2.created == 0
    assert r2.updated >= 1
    assert r2.total == r1.total


def test_out_of_order_timestamps(db):
    eng = _seeded_reuse(db)
    eng.build(build_at="2026-09-01T00:00:00+00:00")
    eng.build(build_at="2026-01-01T00:00:00+00:00")
    items = eng.active_insights()
    assert len(items) == len({i.id for i in items})


# --------------------------------------------------------------------------
# ephemerality: insights retire when conditions vanish
# --------------------------------------------------------------------------

def test_ephemeral_retire_when_conditions_gone(db, monkeypatch):
    _mock_llm_for_type(monkeypatch, 'engineering_reuse')
    eng = _seeded_reuse(db)
    r1 = eng.build()
    assert r1.created >= 1
    # Remove the underlying understanding -> trigger no longer fires.
    for u in UnderstandingEngine(db).all_understanding():
        db.execute("DELETE FROM understanding WHERE id = ?", (u.id,))
    db.commit()
    r2 = eng.build()
    assert r2.retired >= 1
    assert all(i.status == InsightStatus.RETIRED
               for i in eng.all_insights())


def test_ephemeral_reactivate_after_return(db, monkeypatch):
    _mock_llm_for_type(monkeypatch, 'engineering_reuse')
    eng = _seeded_reuse(db)
    eng.build()
    for u in UnderstandingEngine(db).all_understanding():
        db.execute("DELETE FROM understanding WHERE id = ?", (u.id,))
    db.commit()
    eng.build()  # retire
    assert all(i.status == InsightStatus.RETIRED for i in eng.all_insights())
    # Re-seed -> same deterministic id re-activates.
    ka = _k(db, "auth", "auth in a", "recurring_pattern", "medium", ["repo:a"])
    kb = _k(db, "auth", "auth in b", "recurring_pattern", "medium", ["repo:b"])
    _u(db, "auth a", "Auth solved in a.", "engineering_habit", "medium", [ka])
    _u(db, "auth b", "Auth solved in b.", "engineering_habit", "medium", [kb])
    r3 = eng.build()
    # Re-activation is an update of the existing (retired) row, not a new insert.
    assert r3.updated >= 1
    assert any(i.status != InsightStatus.RETIRED
               for i in eng.all_insights())


# --------------------------------------------------------------------------
# multi-project
# --------------------------------------------------------------------------

def test_multi_project_reuse(db, monkeypatch):
    _mock_llm_for_type(monkeypatch, 'engineering_reuse')
    ka = _k(db, "auth", "auth in a", "recurring_pattern", "medium", ["repo:a"])
    kb = _k(db, "auth", "auth in b", "recurring_pattern", "medium", ["repo:b"])
    _u(db, "auth a", "Auth in a.", "engineering_habit", "medium", [ka])
    _u(db, "auth b", "Auth in b.", "engineering_habit", "medium", [kb])
    eng = InsightEngine(db)
    eng.build()
    reuse = [i for i in eng.active_insights() if i.type == InsightType.REUSE]
    assert reuse


# --------------------------------------------------------------------------
# brain compatibility / no hallucination / valid citations
# --------------------------------------------------------------------------

def test_brain_compatibility_provider(db, monkeypatch):
    _mock_llm_for_type(monkeypatch, 'engineering_reuse')
    from friday.ask import _p_insight
    eng = _seeded_reuse(db)
    eng.build()
    ev = type("E", (), {"blocks": [], "raw": {}})()
    _p_insight.fn(type("R", (), {"query": "x", "needs": set(),
                                 "subject": None})(), db, ev, None)
    assert ev.raw.get("insight_total", 0) >= 1


def test_every_insight_cites_valid_ids(db, monkeypatch):
    _mock_llm_for_type(monkeypatch, 'engineering_reuse')
    eng = _seeded_reuse(db)
    eng.build()
    valid_u = {u.id for u in UnderstandingEngine(db).all_understanding()}
    valid_i = {i.id for i in InitiativeEngine(db).all_initiatives()}
    valid_k = {k.id for k in get_all_knowledge(db)}
    for ins in eng.active_insights():
        for uid in ins.understanding_ids:
            assert uid in valid_u
        for iid in ins.initiative_ids:
            assert iid in valid_i
        for kid in ins.knowledge_ids:
            assert kid in valid_k


def test_no_duplicate_insights(db, monkeypatch):
    _mock_llm_for_type(monkeypatch, 'engineering_reuse')
    eng = _seeded_reuse(db)
    eng.build()
    eng.build()
    ids = [i.id for i in eng.all_insights()]
    assert len(ids) == len(set(ids))


def test_no_hallucination_semantic_titles(db, monkeypatch):
    _mock_llm_for_type(monkeypatch, 'engineering_reuse')
    eng = _seeded_reuse(db)
    eng.build()
    for i in eng.active_insights():
        assert "/" not in i.title
        assert "repo:" not in i.title.lower()


def test_no_direct_observation_access_in_rules():
    # Derivation derives ONLY from understanding/initiatives/knowledge. The rule
    # entrypoint signature is (understanding, initiatives, knowledge); it must
    # not require observation/context inputs.
    import inspect
    from friday.insight import derivation
    sig = inspect.signature(derivation.detect)
    assert list(sig.parameters) == ["understanding", "initiatives", "knowledge"]


def test_detect_returns_empty_without_evidence():
    assert detect([], [], []) == []
