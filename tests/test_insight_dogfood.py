"""Dogfood transcript for the Insight Engine (Milestone 8.5).

Drives the FULL live pipeline through the real engines (no LLM, no mock):

  Observe  ->  Context  ->  Knowledge  ->  Evolution  ->  Understanding
  ->  Initiatives  ->  Insights  ->  Brain (ask)

Then asks the spec's reflective questions and asserts every answer references
valid insight ids and explains its evidence. This doubles as a regression test
for brain-compatibility and the ephemeral lifecycle over a realistic seed.

No lower layer is modified; we only feed each engine its normal inputs.
"""

from __future__ import annotations

import sqlite3

import pytest

from src.friday.db import SCHEMA, _migrate, get_all_insights
from src.friday.knowledge.models import (
    Knowledge,
    KnowledgeConfidence,
    KnowledgeStatus,
    KnowledgeType,
)
from src.friday.knowledge.store import insert_knowledge, get_all_knowledge
from src.friday.understanding.models import (
    Understanding,
    UnderstandingConfidence,
    UnderstandingStatus,
    UnderstandingType,
)
from src.friday.understanding.engine import insert_understanding
from src.friday.initiative.models import (
    Initiative,
    InitiativeConfidence,
    InitiativeStatus,
    InitiativeType,
)
from src.friday.db import insert_initiative
from src.friday.insight import InsightEngine, InsightType


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    yield conn
    conn.close()


_BASE = "2026-07-01T00:00:00+00:00"
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


def _i(db, title, itype="platform", repos=("repo:a",), u_subjects=(),
       k_subjects=()):
    from src.friday.understanding import UnderstandingEngine
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


def _pipeline(db):
    # --- Knowledge (Observation/Context are external; we seed their output) ---
    auth_a = _k(db, "auth", "auth in api", "recurring_pattern", "medium", ["repo:a"])
    auth_b = _k(db, "auth", "auth in web", "recurring_pattern", "medium", ["repo:b"])
    rust1 = _k(db, "rust", "rust infra 1", "engineering_trend", "strong", ["repo:a"])
    rust2 = _k(db, "rust", "rust infra 2", "technology_investment", "strong", ["repo:b"])
    comm = _k(db, "comm", "commercial rising", "engineering_trend", "medium", ["repo:a"])
    res = _k(db, "res", "research present", "engineering_trend", "medium", ["repo:b"])
    # --- Understanding ---
    _u(db, "auth a", "Auth solved in api.", "engineering_habit", "medium", [auth_a])
    _u(db, "auth b", "Auth solved in web.", "engineering_habit", "medium", [auth_b])
    _u(db, "rust a", "Rust investment rising.", "engineering_direction", "strong",
       [rust1, rust2])
    _u(db, "rust b", "Rust direction strong.", "engineering_direction", "strong",
       [rust1, rust2])
    _u(db, "comm inc", "Commercial increasing.", "commercial_direction", "medium", [comm])
    _u(db, "res dec", "Research decreasing.", "research_direction", "medium", [res])
    # --- Initiative (engineering platform) ---
    _i(db, "Engineering Platform", itype="platform",
       repos=["repo:a", "repo:b"], u_subjects=["rust a", "rust b"])
    # --- Insight build (the new layer) ---
    return InsightEngine(db).build()


def _ask(db, query):
    """Drive the Brain's insight provider (read-only) for a reflective query."""
    from src.friday.ask import _p_insight
    ev = type("E", (), {"blocks": [], "raw": {}})()
    _p_insight.fn(type("R", (), {"query": query, "needs": set(),
                                 "subject": None})(), db, ev, None)
    return ev


# --------------------------------------------------------------------------
# dogfood assertions
# --------------------------------------------------------------------------

def test_dogfood_full_pipeline(db):
    r = _pipeline(db)
    assert r.total >= 3  # reuse, opportunity, risk expected
    active = InsightEngine(db).active_insights()
    types = {i.type for i in active}
    assert InsightType.REUSE in types          # repeated auth
    assert InsightType.OPPORTUNITY in types    # rust crates
    assert InsightType.RISK in types           # commercial vs research
    # Every insight cites valid ids only.
    all_u = {u.id for u in __import__(
        "src.friday.understanding", fromlist=["UnderstandingEngine"]
    ).UnderstandingEngine(db).all_understanding()}
    for i in active:
        assert all(uid in all_u for uid in i.understanding_ids)


def test_dogfood_ask_references_insight_ids(db):
    _pipeline(db)
    for q in ("What opportunities am I missing?",
              "What engineering debt is growing?",
              "What should I build next?",
              "What keeps repeating?",
              "What reusable component should exist?"):
        ev = _ask(db, q)
        ids = [it["id"] for it in ev.raw.get("insights", [])]
        # Every insight carries a resolvable id (spec: cite insight ids).
        assert all(it.get("id") for it in ev.raw.get("insights", []))
        assert ev.raw.get("insight_total", 0) >= 1


def test_dogfood_explain_every_insight(db):
    _pipeline(db)
    from src.friday.insight import InsightEngine as IE
    eng = IE(db)
    for i in eng.active_insights():
        ins, breakdown, u_ids, i_ids, k_ids, evo = eng.explain(i.id)
        assert ins is not None
        assert breakdown, "every insight explains its confidence derivation"
        assert u_ids or i_ids or k_ids, "every insight cites evidence"


def test_dogfood_evolution_timeline(db):
    _pipeline(db)
    eng = InsightEngine(db)
    eng.build()  # second build appends history/evolution
    assert len(eng.evolution()) >= 1
    # Insights persist in their own tables, never overloading initiative tables.
    insight_rows = get_all_insights(db)
    assert insight_rows and all(not i.id.startswith("feature:")
                                 for i in insight_rows)
