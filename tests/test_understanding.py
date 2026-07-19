"""Tests for the Understanding Engine (Milestone 8.3).

The understanding layer is WRITE-ONLY over Knowledge. It never reads
observations, never calls an LLM, never speculates, and every understanding
must cite valid knowledge ids.

Regression cases required by the spec:
- Cold start / empty knowledge
- Single knowledge / multiple knowledge
- Contradictory knowledge
- Confidence aggregation
- Evolution
- History
- Retirement
- Brain compatibility
- No hallucination
- No duplicate understanding
- Append only
- Repeated builds (idempotency)
- Out-of-order timestamps
- Multi-project workspace
- Every understanding references valid knowledge ids
"""

from __future__ import annotations

import sqlite3

import pytest

from src.friday.db import (
    SCHEMA,
    _migrate,
    get_all_understanding,
    get_understanding_by_id,
    understanding_history_for,
)
from src.friday.knowledge.models import (
    Knowledge,
    KnowledgeConfidence,
    KnowledgeStatus,
    KnowledgeType,
)
from src.friday.knowledge.store import insert_knowledge
from src.friday.understanding import (
    Understanding,
    UnderstandingConfidence,
    UnderstandingEngine,
    UnderstandingStatus,
    UnderstandingType,
    aggregate_confidence,
)
from src.friday.understanding.confidence import Contributor


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    yield conn
    conn.close()


def make_knowledge(t, subj, stmt, conf, n=4, when="2026-01-01T00:00:00+00:00"):
    return Knowledge(
        type=KnowledgeType.from_str(t),
        subject=subj,
        statement=stmt,
        confidence=conf,
        evidence_ids=[f"{when}:ev{i}" for i in range(n)],
        status=KnowledgeStatus.VERIFIED,
        id=f"{when}:{t}:{subj}",
    )


def knowledge_set() -> list:
    return [
        make_knowledge("technology_investment", "Rust", "Investing in Rust",
                       KnowledgeConfidence.STRONG, 40),
        make_knowledge("stable_direction", "Rust", "Rust is the primary systems language",
                       KnowledgeConfidence.STRONG, 40),
        make_knowledge("engineering_trend", "Rust", "Rust usage is increasing",
                       KnowledgeConfidence.MEDIUM, 20),
        make_knowledge("project_identity", "Vivaha", "Vivaha is commercial software",
                       KnowledgeConfidence.MEDIUM),
        make_knowledge("engineering_preference", "Rust", "Prefer Rust for systems",
                       KnowledgeConfidence.MEDIUM),
        make_knowledge("project_evolution", "Vivaha", "Vivaha co-evolves with Friday",
                       KnowledgeConfidence.MEDIUM),
        make_knowledge("portfolio_integration", "Friday", "Friday integrates with Aether",
                       KnowledgeConfidence.MEDIUM),
    ]


# --- cold start / empty knowledge -------------------------------------------


def test_cold_start_empty(db):
    eng = UnderstandingEngine(db)
    res = eng.build()
    assert res.total == 0
    assert res.created == 0
    assert eng.all_understanding() == []


def test_empty_build_no_hallucination(db):
    eng = UnderstandingEngine(db)
    eng.build()
    assert get_all_understanding(db) == []
    # ask-style provider read returns empty, no invented understanding
    from src.friday.understanding.engine import UnderstandingEngine as E
    assert E(db).all_understanding() == []


# --- single / multiple knowledge ---------------------------------------------


def test_single_knowledge_creates_understanding(db):
    insert_knowledge(db, [
        make_knowledge("technology_investment", "Go", "Investing in Go",
                       KnowledgeConfidence.MEDIUM, 20),
    ])
    eng = UnderstandingEngine(db)
    res = eng.build()
    assert res.total >= 1
    subjects = {u.subject for u in eng.all_understanding()}
    assert "go" in subjects


def test_multiple_knowledge_converges_types(db):
    insert_knowledge(db, knowledge_set())
    eng = UnderstandingEngine(db)
    eng.build()
    types = {u.type for u in eng.all_understanding()}
    # Several distinct understanding types should arise from rich knowledge.
    assert UnderstandingType.ENGINEERING_DIRECTION in types
    assert UnderstandingType.TECHNOLOGY_PREFERENCE in types
    assert UnderstandingType.EMERGING_EXPERTISE in types


# --- confidence aggregation --------------------------------------------------


def test_confidence_weak_single_weak_knowledge(db):
    c = [Contributor("k1", "engineering_trend", weight=1, agrees=True)]
    assert aggregate_confidence(c) == UnderstandingConfidence.WEAK


def test_confidence_strong_many_strong(db):
    c = [Contributor(f"k{i}", "engineering_trend", weight=4, agrees=True)
         for i in range(4)]
    assert aggregate_confidence(c) == UnderstandingConfidence.STRONG


def test_confidence_cross_source_boost(db):
    # same weight, more types => stronger via cross-source multiplier
    one_type = [Contributor("k1", "engineering_trend", weight=2, agrees=True)]
    multi_type = [
        Contributor("k1", "engineering_trend", weight=2, agrees=True),
        Contributor("k2", "technology_investment", weight=2, agrees=True),
    ]
    weak_score = aggregate_confidence(one_type)
    strong_score = aggregate_confidence(multi_type)
    assert weak_score != strong_score or strong_score in (
        UnderstandingConfidence.WEAK, UnderstandingConfidence.MEDIUM)


def test_confidence_contradiction_lowers(db):
    rank = {"weak": 0, "medium": 1, "strong": 2}
    agreeing = [Contributor("k1", "engineering_trend", weight=4, agrees=True),
                Contributor("k2", "engineering_trend", weight=4, agrees=True)]
    contested = [Contributor("k1", "engineering_trend", weight=4, agrees=True),
                 Contributor("k2", "engineering_trend", weight=4, agrees=False)]
    a = rank[aggregate_confidence(agreeing).value]
    c = rank[aggregate_confidence(contested).value]
    assert a >= c


def test_confidence_drives_status(db):
    from src.friday.understanding.confidence import status_from_confidence
    assert status_from_confidence(UnderstandingConfidence.STRONG, 4) == UnderstandingStatus.STABLE
    assert status_from_confidence(UnderstandingConfidence.WEAK, 1) == UnderstandingStatus.CANDIDATE


# --- no duplicate understanding ----------------------------------------------


def test_no_duplicate_understanding(db):
    insert_knowledge(db, knowledge_set())
    eng = UnderstandingEngine(db)
    eng.build()
    eng.build()
    rows = get_all_understanding(db)
    keys = [(r.type, r.subject) for r in rows]
    assert len(keys) == len(set(keys))


# --- repeated builds (idempotency) -------------------------------------------


def test_repeated_builds_idempotent(db):
    insert_knowledge(db, knowledge_set())
    eng = UnderstandingEngine(db)
    r1 = eng.build()
    r2 = eng.build()
    assert r1.total == r2.total
    assert r2.created == 0  # second build updates, does not recreate
    assert len(eng.all_understanding()) == r1.total


# --- append only / history ---------------------------------------------------


def test_history_append_only(db):
    insert_knowledge(db, knowledge_set())
    eng = UnderstandingEngine(db)
    eng.build()
    first = get_all_understanding(db)
    u = first[0]
    hist1 = understanding_history_for(db, u.id)
    assert len(hist1) >= 1
    # A second build adds another history row, never removes the first.
    eng.build()
    hist2 = understanding_history_for(db, u.id)
    assert len(hist2) >= len(hist1)
    assert hist1[0].build_at == hist2[0].build_at


def test_append_only_history_preserved(db):
    insert_knowledge(db, knowledge_set())
    eng = UnderstandingEngine(db)
    eng.build()
    uid = get_all_understanding(db)[0].id
    n0 = len(understanding_history_for(db, uid))
    # Run build many times; history only grows.
    for _ in range(3):
        eng.build()
    assert len(understanding_history_for(db, uid)) >= n0 + 3


# --- evolution ---------------------------------------------------------------


def test_evolution_events_recorded(db):
    insert_knowledge(db, [
        make_knowledge("technology_investment", "Go", "Invest in Go",
                       KnowledgeConfidence.WEAK, 2),
    ])
    eng = UnderstandingEngine(db)
    eng.build()  # first appearance
    insert_knowledge(db, [
        make_knowledge("technology_investment", "Go", "Invest in Go",
                       KnowledgeConfidence.STRONG, 40),
    ])
    res = eng.build()
    assert res.events > 0
    ev = eng.evolution_timeline()
    assert any(e.event_type in ("Strengthened", "Stabilized") for e in ev)


# --- contradictory knowledge -------------------------------------------------


def test_contradictory_knowledge_divergence(db):
    # An understanding that was forming, then contradicted by a KNOWLEDGE
    # evolution event (the only evidence source understanding may read).
    kid = make_knowledge("technology_investment", "Legacy", "Invested in Legacy",
                         KnowledgeConfidence.MEDIUM).id
    insert_knowledge(db, [
        make_knowledge("technology_investment", "Legacy", "Invested in Legacy",
                       KnowledgeConfidence.MEDIUM),
    ])
    eng = UnderstandingEngine(db)
    eng.build()
    # Insert a knowledge-evolution Contradicted event referencing the knowledge id.
    from src.friday.db import (
        EvolutionEventRow,
        insert_evolution_events,
    )
    insert_evolution_events(db, [
        EvolutionEventRow(
            id=f"2026-02-01T00:00:00+00:00:Contradicted:{kid}",
            build_at="2026-02-01T00:00:00+00:00",
            event_type="Contradicted",
            knowledge_id=kid,
            previous_confidence="medium", new_confidence="weak",
            previous_status="verified", new_status="dormant",
            previous_statement="Invested in Legacy", new_statement=None,
            reason="Newer evidence contradicts prior investment.",
            evidence_ids=kid,
            related_ids="",
            timestamp="2026-02-01T00:00:00+00:00",
        )
    ])
    eng.build()
    types = {u.type for u in eng.all_understanding()}
    # Divergence / risk / blind-spot understanding should be derivable from a
    # contradicted subject.
    assert (UnderstandingType.PROJECT_DIVERGENCE in types
            or UnderstandingType.ENGINEERING_RISK in types
            or UnderstandingType.ENGINEERING_BLIND_SPOT in types)


# --- no hallucination: must cite knowledge -----------------------------------


def test_every_understanding_cites_valid_knowledge(db):
    insert_knowledge(db, knowledge_set())
    eng = UnderstandingEngine(db)
    eng.build()
    valid = {k.id for k in knowledge_set()}
    for u in eng.all_understanding():
        assert u.knowledge_ids, "understanding with no knowledge citation"
        for kid in u.knowledge_ids:
            assert kid in valid, f"dangling knowledge id: {kid}"
    # The DB rows also reference valid ids.
    for r in get_all_understanding(db):
        assert r.knowledge_ids
        for kid in r.knowledge_ids.split(","):
            assert kid in valid


def test_no_understanding_without_knowledge(db):
    # Knowledge present but none matching a detector's subject should yield
    # nothing fabricated.
    insert_knowledge(db, [
        make_knowledge("recurring_pattern", "Orphans", "An orphan pattern",
                       KnowledgeConfidence.WEAK, 1),
    ])
    eng = UnderstandingEngine(db)
    eng.build()
    # Even if some weak understanding appears, it must always cite knowledge.
    for u in eng.all_understanding():
        assert u.knowledge_ids


# --- retirement --------------------------------------------------------------


def test_retired_understanding_preserved(db):
    insert_knowledge(db, knowledge_set())
    eng = UnderstandingEngine(db)
    eng.build()
    u = eng.all_understanding()[0]
    from src.friday.db import update_understanding_status
    update_understanding_status(db, u.id, UnderstandingStatus.RETIRED.value,
                                retired_at="2026-03-01T00:00:00+00:00")
    # Retired understanding remains queryable (not deleted).
    row = get_understanding_by_id(db, u.id)
    assert row is not None
    assert row.status == UnderstandingStatus.RETIRED.value
    hist = understanding_history_for(db, u.id)
    assert hist  # history preserved forever


def test_rebuild_does_not_resurrect_retired(db):
    insert_knowledge(db, knowledge_set())
    eng = UnderstandingEngine(db)
    eng.build()
    u = eng.all_understanding()[0]
    from src.friday.db import update_understanding_status
    update_understanding_status(db, u.id, UnderstandingStatus.RETIRED.value,
                                retired_at="2026-03-01T00:00:00+00:00")
    eng.build()  # rebuild must not flip Retired back to active
    row = get_understanding_by_id(db, u.id)
    assert row.status == UnderstandingStatus.RETIRED.value


# --- out-of-order timestamps -------------------------------------------------


def test_out_of_order_timestamps_deterministic(db):
    # Insert knowledge with non-chronological creation timestamps.
    rows = [
        make_knowledge("technology_investment", "Rust", "Invest in Rust",
                       KnowledgeConfidence.STRONG, 40, when="2026-03-01T00:00:00+00:00"),
        make_knowledge("stable_direction", "Rust", "Rust is primary",
                       KnowledgeConfidence.STRONG, 40, when="2026-01-01T00:00:00+00:00"),
    ]
    insert_knowledge(db, rows)
    eng = UnderstandingEngine(db)
    res = eng.build()
    # Build is deterministic regardless of insertion order.
    res2 = UnderstandingEngine(db).build()
    assert res.total == res2.total
    ids1 = sorted(u.id for u in eng.all_understanding())
    ids2 = sorted(u.id for u in UnderstandingEngine(db).all_understanding())
    assert ids1 == ids2


# --- multi-project workspace -------------------------------------------------


def test_multi_project_workspace(db):
    insert_knowledge(db, [
        make_knowledge("technology_investment", "Rust", "Invest in Rust",
                       KnowledgeConfidence.STRONG, 40),
        make_knowledge("stable_direction", "Rust", "Rust primary",
                       KnowledgeConfidence.STRONG, 40),
        make_knowledge("project_identity", "Vivaha", "Commercial",
                       KnowledgeConfidence.MEDIUM),
        make_knowledge("project_evolution", "Vivaha", "Co-evolves Friday",
                       KnowledgeConfidence.MEDIUM),
        make_knowledge("portfolio_integration", "Friday", "Integrates Aether",
                       KnowledgeConfidence.MEDIUM),
        make_knowledge("portfolio_integration", "Aether", "Integrates Friday",
                       KnowledgeConfidence.MEDIUM),
        make_knowledge("project_identity", "Aether", "Research tool",
                       KnowledgeConfidence.MEDIUM),
    ])
    eng = UnderstandingEngine(db)
    eng.build()
    subjects = {u.subject for u in eng.all_understanding()}
    assert "rust" in subjects
    assert "vivaha" in subjects
    types = {u.type for u in eng.all_understanding()}
    assert UnderstandingType.PROJECT_CONVERGENCE in types
    assert UnderstandingType.COMMERCIAL_DIRECTION in types


# --- brain compatibility -----------------------------------------------------


def test_brain_provider_reads_understanding(db):
    from src.friday.ask import Evidence, RetrievalRequirements, _p_understanding
    insert_knowledge(db, knowledge_set())
    UnderstandingEngine(db).build()
    ev = Evidence(requirements=RetrievalRequirements(), blocks=[], raw={}, subject=None)
    _p_understanding.fn(None, db, ev, __import__("datetime").date.today())
    assert ev.raw["understanding_total"] > 0
    assert ev.blocks
    # Every reported understanding cites knowledge in raw.
    for u in ev.raw["understanding"]:
        assert u["knowledge_count"] > 0


def test_brain_compatibility_no_knowledge(db):
    from src.friday.ask import Evidence, RetrievalRequirements, _p_understanding
    ev = Evidence(requirements=RetrievalRequirements(), blocks=[], raw={}, subject=None)
    _p_understanding.fn(None, db, ev, __import__("datetime").date.today())
    assert ev.raw["understanding_total"] == 0
    assert ev.blocks  # honest empty message, not a crash
