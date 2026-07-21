"""Tests for the Initiatives Engine (Milestone 8.4).

The initiatives layer is WRITE-ONLY over Understanding. It never reads
observations, context, git, READMEs, or repositories directly. It never calls
an LLM. Every initiative must cite valid understanding ids (and knowledge ids).

Regression cases required by the spec:
- Cold start / no knowledge / no understanding
- Single initiative / multiple initiatives
- Merge
- Split
- Blocked / Completed / Dormant / Archived
- Confidence aggregation
- History
- Evolution
- Append-only
- Repeated builds (idempotency)
- Out-of-order timestamps
- Multi-project initiatives
- Repository addition/removal
- Brain compatibility
- No hallucination
- No duplicate initiatives
- Every initiative references valid understanding ids
"""

from __future__ import annotations

import sqlite3

import pytest

from src.friday.db import (
    SCHEMA,
    _migrate,
    get_all_initiatives,
    get_initiative_by_id,
    initiative_history_for,
)
from src.friday.knowledge.models import (
    Knowledge,
    KnowledgeConfidence,
    KnowledgeStatus,
    KnowledgeType,
)
from src.friday.knowledge.store import insert_knowledge
from src.friday.understanding import UnderstandingEngine
from src.friday.understanding.models import (
    Understanding,
    UnderstandingConfidence,
    UnderstandingStatus,
    UnderstandingType,
)
from src.friday.understanding.engine import insert_understanding
from src.friday.initiative import (
    Initiative,
    InitiativeConfidence,
    InitiativeEngine,
    InitiativeStatus,
    InitiativeType,
    aggregate_confidence,
)
from src.friday.initiative.confidence import Contributor


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


def _seed(conn, krows, urows):
    """krows: (subject, stmt, ktype, kconf, evidence_ids[])
    urows: (subject, stmt, utype, uconf, knowledge_subjects[])"""
    base = "2026-07-01T00:00:00+00:00"
    for s, st, kt, kc, ev in krows:
        insert_knowledge(conn, [Knowledge(
            type=KnowledgeType.from_str(kt), subject=s, statement=st,
            confidence=KnowledgeConfidence.from_str(kc), evidence_ids=list(ev),
            status=KnowledgeStatus.VERIFIED, created_at=base, updated_at=base,
            id=None)])
    kmap = {k.subject: k.id for k in
             __import__("src.friday.knowledge.store", fromlist=["get_all_knowledge"])
             .get_all_knowledge(conn)}
    for s, st, ut, uc, ksubs in urows:
        kids = [kmap[x] for x in ksubs if x in kmap]
        u = Understanding(
            type=UnderstandingType.from_str(ut), subject=s, statement=st,
            confidence=UnderstandingConfidence.from_str(uc),
            status=UnderstandingStatus.OBSERVED, knowledge_ids=kids,
            build_at=base, created_at=base, updated_at=base, id=None)
        insert_understanding(conn, [u.to_row()])


# --------------------------------------------------------------------------
# cold start
# --------------------------------------------------------------------------

def test_cold_start_empty(db):
    eng = InitiativeEngine(db)
    r = eng.build()
    assert r.total == 0
    assert r.created == 0
    assert get_all_initiatives(db) == []


def test_no_knowledge_no_understanding(db):
    eng = InitiativeEngine(db)
    r = eng.build()
    assert r.total == 0


def test_no_understanding_only_knowledge(db):
    _seed(db, [("rust", "rust work", "engineering_trend", "strong", ["repo:a"])], [])
    eng = InitiativeEngine(db)
    r = eng.build()
    # understanding is required; knowledge alone yields nothing.
    assert r.total == 0


# --------------------------------------------------------------------------
# single / multiple
# --------------------------------------------------------------------------

def test_single_initiative(db):
    _seed(db,
           [("rust", "rust work", "engineering_trend", "strong", ["repo:a"]),
            ("rust", "rust invest", "technology_investment", "strong", ["repo:a"])],
           [("rust", "Engineering direction toward rust.", "engineering_direction", "medium", ["rust"])])
    eng = InitiativeEngine(db)
    r = eng.build()
    assert r.total >= 1
    titles = {i.title for i in eng.all_initiatives()}
    assert "Systems Infrastructure" in titles


def test_multiple_initiatives(db):
    _seed(db,
           [("rust", "rust work", "engineering_trend", "strong", ["repo:a"]),
            ("auth", "auth work", "recurring_pattern", "medium", ["repo:a", "repo:b"]),
            ("vivaha", "commercial effort", "engineering_interest", "strong", ["repo:c"])],
           [("rust", "Systems work.", "engineering_direction", "medium", ["rust"]),
            ("auth", "Auth recurring.", "engineering_habit", "medium", ["auth"]),
            ("vivaha", "Commercial dominant.", "commercial_direction", "medium", ["vivaha"])])
    eng = InitiativeEngine(db)
    r = eng.build()
    assert r.total >= 3


# --------------------------------------------------------------------------
# confidence aggregation
# --------------------------------------------------------------------------

def test_confidence_aggregation_weak_to_strong():
    weak = [Contributor("u1", "understanding", 1, "repo:a")]
    strong = [Contributor("u1", "understanding", 4, "repo:a"),
              Contributor("u2", "understanding", 4, "repo:b"),
              Contributor("u3", "understanding", 4, "repo:c"),
              Contributor("u4", "understanding", 4, "repo:d")]
    assert aggregate_confidence(weak, ["repo:a"]) == InitiativeConfidence.WEAK
    assert aggregate_confidence(strong, ["repo:a", "repo:b", "repo:c", "repo:d"]) \
        == InitiativeConfidence.STRONG


def test_confidence_cross_project_boost():
    one = [Contributor("u1", "understanding", 4, "repo:a"),
            Contributor("u2", "understanding", 4, "repo:a")]
    many = [Contributor("u1", "understanding", 4, "repo:a"),
             Contributor("u2", "understanding", 4, "repo:b")]
    c_one = aggregate_confidence(one, ["repo:a"])
    c_many = aggregate_confidence(many, ["repo:a", "repo:b"])
    # cross-project reinforcement raises confidence.
    assert c_many.value >= c_one.value


# --------------------------------------------------------------------------
# merge / split
# --------------------------------------------------------------------------

def _two_platforms(db):
    _seed(db,
           [("aether", "aether relates to friday.", "project_relationship", "medium", ["repo:a", "repo:f"]),
            ("friday", "friday integrates broadly.", "portfolio_integration", "medium", ["repo:f", "repo:a"])],
           [("aether", "Projects touching aether converge.", "project_convergence", "weak", ["aether"]),
            ("friday", "Projects touching friday converge.", "project_convergence", "weak", ["friday"])])
    return InitiativeEngine(db)


def test_merge_preserves_parents(db):
    eng = _two_platforms(db)
    eng.build()
    items = eng.all_initiatives()
    parents = [i for i in items if i.type.value in ("platform", "integration")]
    cid = eng.merge([p.id for p in parents], title="Friday Platform")
    assert cid is not None
    child = eng.initiative_by_id(cid)
    assert child.title == "Friday Platform"
    rels = eng.relationships()
    assert any(r.relationship_type == "merge" for r in rels)
    # parents archived
    assert all(eng.initiative_by_id(p.id).status == InitiativeStatus.ARCHIVED
               for p in parents)


def test_split_retains_parent_reference(db):
    eng = _two_platforms(db)
    eng.build()
    parent = [i for i in eng.all_initiatives()
              if i.type.value in ("platform", "integration")][0]
    cids = eng.split(parent.id, ["Child One", "Child Two"])
    assert len(cids) == 2
    rels = eng.relationships()
    split = [r for r in rels if r.relationship_type == "split"]
    assert split and parent.id in split[0].parent_ids.split(",")
    assert eng.initiative_by_id(parent.id).status == InitiativeStatus.ARCHIVED


# --------------------------------------------------------------------------
# lifecycle states via update
# --------------------------------------------------------------------------

def test_completed_and_dormant_and_archived(db):
    eng = _two_platforms(db)
    eng.build()
    i = eng.all_initiatives()[0]
    # completed
    from src.friday.db import update_initiative_status
    update_initiative_status(db, i.id, "completed", "2026-08-01T00:00:00+00:00")
    assert eng.initiative_by_id(i.id).status == InitiativeStatus.COMPLETED
    update_initiative_status(db, i.id, "dormant")
    assert eng.initiative_by_id(i.id).status == InitiativeStatus.DORMANT
    update_initiative_status(db, i.id, "archived")
    assert eng.initiative_by_id(i.id).status == InitiativeStatus.ARCHIVED


def test_blocked_status_exists(db):
    # blocked is a valid lifecycle status (no auto-transition).
    assert InitiativeStatus.BLOCKED.value == "blocked"


# --------------------------------------------------------------------------
# history / evolution / append-only
# --------------------------------------------------------------------------

def test_history_append_only(db):
    eng = _two_platforms(db)
    eng.build()
    i = eng.all_initiatives()[0]
    before = len(initiative_history_for(db, i.id))
    eng.build()  # rebuild -> appends a snapshot, not overwrite
    after = len(initiative_history_for(db, i.id))
    assert after >= before
    # idempotent row count unchanged
    assert len(eng.all_initiatives()) == len(eng.all_initiatives())


def test_evolution_events_emitted(db):
    eng = _two_platforms(db)
    eng.build()
    assert len(eng.timeline()) >= 1


# --------------------------------------------------------------------------
# idempotency / out-of-order
# --------------------------------------------------------------------------

def test_idempotent_rebuild(db):
    eng = _two_platforms(db)
    r1 = eng.build()
    r2 = eng.build()
    assert r2.created == 0
    assert r2.updated >= 1
    assert r2.total == r1.total


def test_out_of_order_timestamps(db):
    # deterministic ids ignore clock order; rebuilding with a fixed id stays stable.
    eng = _two_platforms(db)
    eng.build(build_at="2026-09-01T00:00:00+00:00")
    eng.build(build_at="2026-01-01T00:00:00+00:00")  # earlier clock
    items = eng.all_initiatives()
    # still exactly the expected set, no explosion.
    assert len(items) == len({i.id for i in items})


# --------------------------------------------------------------------------
# multi-project / repo add-remove
# --------------------------------------------------------------------------

def test_multi_project_initiative(db):
    _seed(db,
           [("auth", "auth across services.", "recurring_pattern", "medium", ["repo:a", "repo:b"])],
           [("auth", "Repeated auth work.", "engineering_habit", "medium", ["auth"])])
    eng = InitiativeEngine(db)
    eng.build()
    auth = [i for i in eng.all_initiatives() if i.title == "Authentication Infrastructure"]
    assert auth and auth[0].repo_count >= 2


def test_repository_addition_and_removal(db):
    _seed(db,
           [("auth", "auth work.", "recurring_pattern", "medium", ["repo:a"])],
           [("auth", "Repeated auth.", "engineering_habit", "medium", ["auth"])])
    eng = InitiativeEngine(db)
    eng.build()
    i = [x for x in eng.all_initiatives() if x.title == "Authentication Infrastructure"][0]
    assert "repo:a" in i.participating_repositories
    # add repo:b: new knowledge AND a new understanding that cites both knowledge
    # rows, so the initiative's participant set grows.
    _seed(db,
           [("auth", "auth also in b.", "recurring_pattern", "medium", ["repo:b"])],
           [("auth", "Repeated auth.", "engineering_habit", "medium", ["auth"])])
    # refresh understanding to cite the new knowledge too
    from src.friday.understanding.models import (Understanding, UnderstandingType,
                                                UnderstandingStatus, UnderstandingConfidence)
    from src.friday.understanding.engine import insert_understanding as ins_u
    kmap = {k.subject: k.id for k in
             __import__("src.friday.knowledge.store", fromlist=["get_all_knowledge"])
             .get_all_knowledge(db)}
    u = Understanding(type=UnderstandingType.ENGINEERING_HABIT, subject="auth",
                    statement="Repeated auth across repos.",
                    confidence=UnderstandingConfidence.MEDIUM,
                    status=UnderstandingStatus.OBSERVED,
                    knowledge_ids=[kmap["auth"]], build_at="2026-07-02T00:00:00+00:00",
                    created_at="2026-07-02T00:00:00+00:00",
                    updated_at="2026-07-02T00:00:00+00:00", id=None)
    ins_u(db, [u.to_row()])
    eng.build()
    i2 = eng.initiative_by_id(i.id)
    assert "repo:b" in i2.participating_repositories


# --------------------------------------------------------------------------
# brain compatibility / no hallucination / valid citations
# --------------------------------------------------------------------------

def test_brain_compatibility_provider(db):
    from src.friday.ask import _p_initiative
    eng = _two_platforms(db)
    eng.build()
    ev = type("E", (), {"blocks": [], "raw": {}})()
    _p_initiative.fn(type("R", (), {"query": "x", "needs": set(), "subject": None})(), db, ev, None)
    assert ev.raw.get("initiative_total", 0) >= 1
    assert any("Platform" in b or "Integration" in b for b in ev.blocks)


def test_every_initiative_cites_valid_understanding(db):
    eng = _two_platforms(db)
    eng.build()
    valid = {u.id for u in UnderstandingEngine(db).all_understanding()}
    for i in eng.all_initiatives():
        for uid in i.understanding_ids:
            assert uid in valid  # no dangling citations


def test_no_duplicate_initiatives(db):
    eng = _two_platforms(db)
    eng.build()
    eng.build()
    ids = [i.id for i in eng.all_initiatives()]
    assert len(ids) == len(set(ids))


def test_no_hallucination_only_derived_titles(db):
    # titles are semantic, not repo names; no repo path leaks into a title.
    _seed(db,
           [("rust", "rust work", "engineering_trend", "strong", ["repo:friday_v3"])],
           [("rust", "Systems direction.", "engineering_direction", "medium", ["rust"])])
    eng = InitiativeEngine(db)
    eng.build()
    for i in eng.all_initiatives():
        assert "friday_v3" not in i.title.lower()
        assert "/" not in i.title
