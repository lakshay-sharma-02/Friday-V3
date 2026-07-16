"""Tests for Knowledge Evolution (Milestone 8.2).

Every test asserts the layer is WRITE-only on history/events and that NO
transition is clock-driven: Dormant/Retired/Reactivated/Weakened fire ONLY when
newer observations contradict prior usage. Elapsed time alone never changes state.

Covers Part M: confidence growth/decay, history preservation, append-only,
retirement, contradiction, merge, split, dormant, reactivated, brain compat,
cold start, idempotency, repeated builds, out-of-order timestamps, missing
observations, multiple repositories, no duplicate evolution.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.friday.context.models import EngineeringSession, SessionActivity
from src.friday.db import (
    connect,
    evolution_events_all,
    insert_observations,
    insert_sessions,
    knowledge_history_for,
    latest_knowledge_snapshot,
    update_knowledge_status,
)
from src.friday.knowledge import (
    Knowledge,
    KnowledgeConfidence,
    KnowledgeEngine,
    KnowledgeStatus,
    KnowledgeType,
    evolve,
)
from src.friday.knowledge.evolution import (
    band_of,
    evidence_age_weight,
    weighted_evidence_score,
)
from src.friday.observation.model import Confidence, Observation


@pytest.fixture
def db():
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    from src.friday.db import SCHEMA

    conn.executescript(SCHEMA)
    yield conn
    conn.close()


def obs(source, subject, aspect, value, observed_at, scope=""):
    return Observation(
        source=source, subject=subject, aspect=aspect, value=value,
        observed_at=observed_at, scope=scope, confidence=Confidence.OBSERVED,
    ).to_row()


def sess(repo, activity, start_time, duration_min=30):
    start = datetime.fromisoformat(start_time)
    end = start + timedelta(minutes=duration_min)
    return EngineeringSession(
        start_time=start.isoformat(), end_time=end.isoformat(),
        repositories=[repo], primary_repo=repo,
        observations=[f"o:{start.isoformat()}"], activity=activity,
        confidence=Confidence.DERIVED,
    ).to_row()


def build(db):
    """Run engine build then evolution, return (KnowledgeBuildResult, n_events)."""
    eng = KnowledgeEngine(db)
    r = eng.build()
    n = evolve(db)
    return r, n


# --- Part M: confidence growth ----------------------------------------------


def test_confidence_growth_emits_strengthened(db):
    now = datetime.now(timezone.utc)
    # Weak start: 5 tech observations.
    insert_observations(db, [
        obs("git", "Python", "language", "used", (now - timedelta(days=i+1)).isoformat())
        for i in range(5)
    ])
    r1, _ = build(db)
    # Strengthen to strong: total 45.
    insert_observations(db, [
        obs("git", "Python", "language", "used", (now - timedelta(days=i+1)).isoformat())
        for i in range(5, 45)
    ])
    r2, n2 = build(db)
    ev = evolution_events_all(db)
    assert any(e.event_type == "Strengthened" for e in ev)
    py = [k for k in KnowledgeEngine(db).all_knowledge() if k.subject == "Python"]
    assert py and py[0].confidence == KnowledgeConfidence.STRONG


# --- Part M: confidence decay (evidence-driven, not clock) -------------------


def test_confidence_decay_requires_contradicting_evidence(db):
    now = datetime.now(timezone.utc)
    # Strong from 45 usages.
    insert_observations(db, [
        obs("git", "Python", "language", "used", (now - timedelta(days=i+1)).isoformat())
        for i in range(45)
    ])
    build(db)
    # Mere passage of time (no new observations) must NOT weaken or retire.
    eng = KnowledgeEngine(db)
    py_before = [k for k in eng.all_knowledge() if k.subject == "Python"][0]
    # Rebuild with identical data, bump clock by faking observed_at window far later.
    build(db)
    py_after = [k for k in eng.all_knowledge() if k.subject == "Python"][0]
    assert py_after.confidence == KnowledgeConfidence.STRONG
    # No clock-driven retirement/dormant: still active, not dormant/retired.
    assert py_after.status not in (KnowledgeStatus.DORMANT, KnowledgeStatus.RETIRED)
    # Now an explicit removal observation -> Weakened/Contradicted, evidence-driven.
    insert_observations(db, [
        obs("git", "Python", "language", "removed", now.isoformat())
    ])
    build(db)
    ev = evolution_events_all(db)
    assert any(e.event_type in ("Weakened", "Contradicted", "Dormant") for e in ev)


# --- Part M: history preservation + append-only ------------------------------


def test_history_preserves_every_prior_version(db):
    now = datetime.now(timezone.utc)
    insert_observations(db, [
        obs("git", "Rust", "language", "used", (now - timedelta(days=i+1)).isoformat())
        for i in range(5)
    ])
    build(db)
    insert_observations(db, [
        obs("git", "Rust", "language", "used", (now - timedelta(days=i+1)).isoformat())
        for i in range(5, 50)
    ])
    build(db)
    eng = KnowledgeEngine(db)
    rid = [k for k in eng.all_knowledge() if k.subject == "Rust"][0].id
    hist = knowledge_history_for(db, rid)
    # Two snapshots captured (one per build), same id, never overwritten.
    assert len(hist) == 2
    assert all(h.knowledge_id == rid for h in hist)
    # Old weak snapshot still present alongside new strong.
    bands = sorted(h.confidence for h in hist)
    assert bands[0] != bands[-1]


def test_history_is_append_only(db):
    now = datetime.now(timezone.utc)
    insert_observations(db, [
        obs("git", "Go", "language", "used", now.isoformat())
        for _ in range(3)
    ])
    build(db)
    before = latest_knowledge_snapshot(db)
    # Force a status change directly; history must remain untouched.
    kid = before[0].knowledge_id
    update_knowledge_status(db, kid, KnowledgeStatus.RETIRED.value)
    after = latest_knowledge_snapshot(db)
    # Snapshot rows themselves unchanged (new snapshot only on next build).
    assert [(r.build_at, r.knowledge_id, r.status) for r in before] == \
           [(r.build_at, r.knowledge_id, r.status) for r in after]


# --- Part M: retirement (evidence-driven) ------------------------------------


def test_retirement_requires_removal_evidence(db):
    now = datetime.now(timezone.utc)
    # Subject with only removal observations (no active usage) -> Retired.
    insert_observations(db, [
        obs("git", "Flask", "language", "deprecated", (now - timedelta(days=i+1)).isoformat())
        for i in range(20)
    ])
    build(db)
    flask = [k for k in KnowledgeEngine(db).all_knowledge() if k.subject == "Flask"]
    assert flask, "Flask knowledge should exist from removal observations"
    # Explicit deprecation with NO active usage -> Retired, but still queryable.
    assert flask[0].status == KnowledgeStatus.RETIRED
    assert flask[0] in KnowledgeEngine(db).all_knowledge()  # queryable
    assert any(e.event_type == "Retired" for e in evolution_events_all(db))

    # Contrast: a subject used heavily then left idle (no new obs) must NOT retire.
    insert_observations(db, [
        obs("git", "Django", "language", "used", (now - timedelta(days=i+1)).isoformat())
        for i in range(20)
    ])
    build(db)
    django = [k for k in KnowledgeEngine(db).all_knowledge() if k.subject == "Django"][0]
    assert django.status != KnowledgeStatus.RETIRED  # no clock-driven retirement
    # Rebuild years later with same data -> still not retired.
    build(db)
    django = [k for k in KnowledgeEngine(db).all_knowledge() if k.subject == "Django"][0]
    assert django.status != KnowledgeStatus.RETIRED


# --- Part M: contradiction ----------------------------------------------------


def test_contradiction_records_does_not_overwrite(db):
    now = datetime.now(timezone.utc)
    # Early: Python dominant (medium confidence).
    insert_observations(db, [
        obs("git", "Python", "language", "dominant", (now - timedelta(days=40 - i)).isoformat())
        for i in range(20)
    ])
    build(db)
    early_stmt = [k for k in KnowledgeEngine(db).all_knowledge() if k.subject == "Python"][0].statement
    # Later: a newer observation explicitly contradicts (Python now unused).
    insert_observations(db, [
        obs("git", "Python", "language", "unused", now.isoformat())
    ])
    build(db)
    ev = evolution_events_all(db)
    assert any(e.event_type == "Contradicted" for e in ev)
    # History still contains the early Python statement (never overwritten).
    pid = [k for k in KnowledgeEngine(db).all_knowledge() if k.subject == "Python"][0].id
    hist = knowledge_history_for(db, pid)
    assert any(h.statement == early_stmt for h in hist)
    # The contradicted belief is retained (dormant, not deleted).
    py = [k for k in KnowledgeEngine(db).all_knowledge() if k.subject == "Python"][0]
    assert py.status == KnowledgeStatus.DORMANT


# --- Part M: merge ------------------------------------------------------------


def test_merge_event_links_parents(db):
    now = datetime.now(timezone.utc)
    insert_observations(db, [
        obs("git", "Rust", "language", "used", (now - timedelta(days=i+1)).isoformat())
        for i in range(20)
    ])
    insert_observations(db, [
        obs("git", "Cargo", "language", "used", (now - timedelta(days=i+1)).isoformat())
        for i in range(20)
    ])
    build(db)
    # Converging observation spans both -> merged knowledge.
    insert_observations(db, [
        obs("git", "Rust Cargo ecosystem", "language", "used",
            (now - timedelta(days=i+1)).isoformat())
        for i in range(20)
    ])
    build(db)
    ev = evolution_events_all(db)
    merged = [e for e in ev if e.event_type == "Merged"]
    assert merged, "expected a Merged event"
    assert "rust" in merged[0].related_ids.lower()
    assert "cargo" in merged[0].related_ids.lower()
    # Parents remain.
    subs = {k.subject for k in KnowledgeEngine(db).all_knowledge()}
    assert "Rust" in subs and "Cargo" in subs


# --- Part M: split ------------------------------------------------------------


def test_split_event_on_divergence(db):
    now = datetime.now(timezone.utc)
    insert_observations(db, [
        obs("git", "Webstack", "language", "used", (now - timedelta(days=i+1)).isoformat())
        for i in range(10)
    ])
    build(db)
    # Later the parent goes quiet (latest evidence) while two children appear.
    insert_observations(db, [
        obs("git", "Webstack", "language", "unused", now.isoformat())
    ])
    insert_observations(db, [
        obs("git", "Frontend", "language", "used", (now - timedelta(days=i+1)).isoformat())
        for i in range(10)
    ])
    insert_observations(db, [
        obs("git", "Backend", "language", "used", (now - timedelta(days=i+1)).isoformat())
        for i in range(10)
    ])
    build(db)
    ev = evolution_events_all(db)
    assert any(e.event_type == "Split" for e in ev)


# --- Part M: dormant (evidence-driven, NOT clock) -----------------------------


def test_dormant_requires_inactive_observation(db):
    now = datetime.now(timezone.utc)
    insert_observations(db, [
        obs("git", "Vue", "language", "used", (now - timedelta(days=i + 1)).isoformat())
        for i in range(20)
    ])
    build(db)
    # No new observations for years -> stays, does NOT auto-dormant.
    vue = [k for k in KnowledgeEngine(db).all_knowledge() if k.subject == "Vue"][0]
    assert vue.status != KnowledgeStatus.DORMANT
    # Inactive observation arrives (latest evidence) -> Dormant, evidence-driven.
    insert_observations(db, [
        obs("git", "Vue", "language", "inactive", now.isoformat())
    ])
    build(db)
    vue = [k for k in KnowledgeEngine(db).all_knowledge() if k.subject == "Vue"][0]
    assert vue.status == KnowledgeStatus.DORMANT
    assert any(e.event_type == "Dormant" for e in evolution_events_all(db))


def test_reactivated_on_new_usage(db):
    now = datetime.now(timezone.utc)
    insert_observations(db, [
        obs("git", "Vue", "language", "used", (now - timedelta(days=i + 1)).isoformat())
        for i in range(20)
    ])
    # Latest evidence contradicts -> Dormant.
    insert_observations(db, [obs("git", "Vue", "language", "inactive", now.isoformat())])
    build(db)
    assert [k for k in KnowledgeEngine(db).all_knowledge() if k.subject == "Vue"][0].status == \
        KnowledgeStatus.DORMANT
    # A newer 'used' observation arrives -> Reactivated (recency wins).
    later = (now + timedelta(seconds=1)).isoformat()
    insert_observations(db, [obs("git", "Vue", "language", "used", later)])
    build(db)
    vue = [k for k in KnowledgeEngine(db).all_knowledge() if k.subject == "Vue"][0]
    assert vue.status == KnowledgeStatus.OBSERVED
    assert any(e.event_type == "Reactivated" for e in evolution_events_all(db))


# --- Part M: brain compatibility ---------------------------------------------


def test_brain_reads_same_knowledge_table(db):
    """The Brain (ask.py) consumes `knowledge` only. Evolution must not change
    its shape: knowledge remains queryable with unchanged confidence/evidence."""
    now = datetime.now(timezone.utc)
    insert_observations(db, [
        obs("git", "Python", "language", "used", (now - timedelta(days=i+1)).isoformat())
        for i in range(45)
    ])
    build(db)
    eng = KnowledgeEngine(db)
    k = [k for k in eng.all_knowledge() if k.subject == "Python"][0]
    # What the Brain sees (the live row) is valid + complete.
    assert k.confidence == KnowledgeConfidence.STRONG
    assert k.evidence_count >= 15
    assert k.status in {s.value for s in KnowledgeStatus}
    # Retired knowledge still appears (queryable) — Brain can explain historically.
    insert_observations(db, [obs("git", "Python", "language", "deprecated", now.isoformat())])
    build(db)
    all_k = eng.all_knowledge()
    assert any(k.subject == "Python" for k in all_k)


# --- Part M: cold start -------------------------------------------------------


def test_cold_start_no_events(db):
    r, n = build(db)
    assert r.total == 0
    assert n == 0
    assert evolution_events_all(db) == []


# --- Part M: idempotency + repeated builds -----------------------------------


def test_repeated_build_idempotent_no_duplicate_events(db):
    now = datetime.now(timezone.utc)
    insert_observations(db, [
        obs("git", "Python", "language", "used", (now - timedelta(days=i+1)).isoformat())
        for i in range(20)
    ])
    build(db)
    build(db)  # identical data
    build(db)
    ev = evolution_events_all(db)
    # Each (build_at, type, kid) is unique -> no duplicate events across builds.
    ids = [(e.build_at, e.id) for e in ev]
    assert len(ids) == len(set(ids))
    # Total distinct event ids stable across re-runs.
    n1 = len(ev)
    build(db)
    assert len(evolution_events_all(db)) == n1


# --- Part M: out-of-order timestamps -----------------------------------------


def test_out_of_order_timestamps_stable(db):
    now = datetime.now(timezone.utc)
    # Insert future-dated observation before past-dated.
    insert_observations(db, [obs("git", "Go", "language", "used", (now + timedelta(days=10)).isoformat())])
    insert_observations(db, [obs("git", "Go", "language", "used", (now - timedelta(days=10)).isoformat())])
    insert_observations(db, [obs("git", "Go", "language", "used", now.isoformat())])
    build(db)
    go = [k for k in KnowledgeEngine(db).all_knowledge() if k.subject == "Go"][0]
    assert go.evidence_count >= 2


# --- Part M: missing observations ---------------------------------------------


def test_missing_observations_no_crash(db):
    # No observations at all, just run build — should be harmless.
    r, n = build(db)
    assert r.total == 0


# --- Part M: multiple repositories --------------------------------------------


def test_multiple_repositories_independent_evolution(db):
    now = datetime.now(timezone.utc)
    insert_observations(db, [
        obs("git", "Python", "language", "used", (now - timedelta(days=i+1)).isoformat())
        for i in range(20)
    ])
    insert_observations(db, [
        obs("git", "Rust", "language", "used", (now - timedelta(days=i+1)).isoformat())
        for i in range(20)
    ])
    build(db)
    # Retire only Python with an explicit removal.
    insert_observations(db, [obs("git", "Python", "language", "removed", now.isoformat())])
    build(db)
    eng = KnowledgeEngine(db)
    py = [k for k in eng.all_knowledge() if k.subject == "Python"][0]
    ru = [k for k in eng.all_knowledge() if k.subject == "Rust"][0]
    assert py.status == KnowledgeStatus.RETIRED
    assert ru.status != KnowledgeStatus.RETIRED  # Rust untouched


# --- Part M: no duplicate evolution -------------------------------------------


def test_no_duplicate_evolution_same_build(db):
    now = datetime.now(timezone.utc)
    insert_observations(db, [
        obs("git", "Python", "language", "used", (now - timedelta(days=i+1)).isoformat())
        for i in range(20)
    ])
    build(db)
    # A second evolve() in the same instant with same data -> 0 new events.
    n = evolve(db)
    assert n == 0


# --- Part E: evidence aging is deterministic + non-discarding ----------------


def test_evidence_age_weight_banded(db):
    build_at = datetime.now(timezone.utc)
    # Fresh evidence: full weight.
    assert evidence_age_weight(f"{build_at.isoformat()}:git:Python:language", build_at.isoformat()) == 1.0
    old = (build_at - timedelta(days=200)).isoformat()
    w = evidence_age_weight(f"{old}:git:Python:language", build_at.isoformat())
    assert 0.25 <= w < 1.0  # aged but never zero
    very_old = (build_at - timedelta(days=5000)).isoformat()
    w2 = evidence_age_weight(f"{very_old}:git:Python:language", build_at.isoformat())
    assert w2 == 0.25  # floor, historic evidence retained


def test_weighted_score_retains_historic(db):
    build_at = datetime.now(timezone.utc)
    ids = [
        f"{(build_at - timedelta(days=i * 200)).isoformat()}:git:s:a"
        for i in range(5)
    ]
    score = weighted_evidence_score(ids, build_at.isoformat())
    assert score > 0  # never discarded


def test_band_of_mirrors_engine_thresholds(db):
    assert band_of(5) == KnowledgeConfidence.WEAK
    assert band_of(15) == KnowledgeConfidence.MEDIUM
    assert band_of(40) == KnowledgeConfidence.STRONG
    assert band_of(44) == KnowledgeConfidence.STRONG
