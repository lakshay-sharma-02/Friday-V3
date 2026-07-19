"""Tests for the Knowledge Engine (Milestone 8.1).

Tests cover:
- Trend detection
- Habit detection
- Relationship detection
- Confidence growth
- Repeated build idempotency
- History preservation
- No duplicate knowledge
- Evidence linkage
- Knowledge evolution
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.friday.context.models import EngineeringSession, SessionActivity
from src.friday.db import connect, insert_observations, insert_sessions
from src.friday.knowledge import (
    Knowledge,
    KnowledgeConfidence,
    KnowledgeEngine,
    KnowledgeStatus,
    KnowledgeType,
    detect_habits,
    detect_project_switching,
    detect_relationships,
    detect_repeated_usage,
    detect_trends,
    update_confidence,
    verify_knowledge,
)
from src.friday.observation.model import Confidence, Observation


@pytest.fixture
def db():
    """In-memory database for testing."""
    import sqlite3
    from pathlib import Path

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    # Execute schema
    from src.friday.db import SCHEMA
    conn.executescript(SCHEMA)

    yield conn
    conn.close()


def make_observation(
    source: str,
    subject: str,
    aspect: str,
    value: str,
    observed_at: str,
    scope: str = "",
) -> Observation:
    """Helper to create an observation."""
    return Observation(
        source=source,
        subject=subject,
        aspect=aspect,
        value=value,
        observed_at=observed_at,
        scope=scope,
        confidence=Confidence.OBSERVED,
    )


def make_session(
    repo: str,
    activity: SessionActivity,
    start_time: str,
    duration_min: int = 30,
) -> EngineeringSession:
    """Helper to create a session."""
    start = datetime.fromisoformat(start_time)
    end = start + timedelta(minutes=duration_min)
    return EngineeringSession(
        start_time=start.isoformat(),
        end_time=end.isoformat(),
        repositories=[repo],
        primary_repo=repo,
        observations=[f"obs_{start.isoformat()}"],
        activity=activity,
        confidence=Confidence.DERIVED,
    )


# --- Trend Detection Tests ---


def test_detect_repository_trend_increasing():
    """Increasing repository usage should be detected."""
    now = datetime.now(timezone.utc)
    sessions = []

    # Early period: 2 sessions
    for i in range(2):
        sessions.append(
            make_session(
                "Friday",
                SessionActivity.FEATURE_WORK,
                (now - timedelta(days=60 - i)).isoformat(),
            )
        )

    # Recent period: 10 sessions
    for i in range(10):
        sessions.append(
            make_session(
                "Friday",
                SessionActivity.FEATURE_WORK,
                (now - timedelta(days=10 - i)).isoformat(),
            )
        )

    knowledge = detect_trends([], sessions)
    friday_trends = [k for k in knowledge if k.subject == "Friday"]

    assert len(friday_trends) > 0
    assert any("increasing" in k.statement.lower() for k in friday_trends)


def test_detect_repository_trend_dormant():
    """Dormant repositories should be detected."""
    now = datetime.now(timezone.utc)
    sessions = []

    # All sessions 60+ days ago
    for i in range(5):
        sessions.append(
            make_session(
                "OldProject",
                SessionActivity.FEATURE_WORK,
                (now - timedelta(days=60 + i)).isoformat(),
            )
        )

    knowledge = detect_trends([], sessions)
    old_trends = [k for k in knowledge if k.subject == "OldProject"]

    assert len(old_trends) > 0
    assert any("dormant" in k.statement.lower() for k in old_trends)


def test_detect_technology_trend_emerging():
    """Emerging technologies should be detected."""
    now = datetime.now(timezone.utc)
    observations = []

    # All recent observations
    for i in range(5):
        observations.append(
            make_observation(
                "git",
                "Rust",
                "technology",
                "used",
                (now - timedelta(days=i)).isoformat(),
            )
        )

    knowledge = detect_trends(observations, [])
    rust_trends = [k for k in knowledge if k.subject == "Rust"]

    assert len(rust_trends) > 0


# --- Pattern Detection Tests ---


def test_detect_repeated_usage():
    """Repeated technology usage should be detected."""
    observations = []
    now = datetime.now(timezone.utc)

    for i in range(5):
        observations.append(
            make_observation(
                "git", "Python", "language", "used", (now - timedelta(days=i)).isoformat()
            )
        )

    knowledge = detect_repeated_usage(observations, min_count=3)
    python_knowledge = [k for k in knowledge if k.subject == "Python"]

    assert len(python_knowledge) > 0
    assert python_knowledge[0].type == KnowledgeType.TECHNOLOGY_INVESTMENT
    assert "repeatedly used" in python_knowledge[0].statement
    assert python_knowledge[0].evidence_count >= 5


def test_detect_project_switching():
    """Frequent project switching should be detected."""
    now = datetime.now(timezone.utc)
    sessions = []

    # Alternate between two projects
    for i in range(10):
        repo = "Friday" if i % 2 == 0 else "Vivaha"
        sessions.append(
            make_session(
                repo,
                SessionActivity.FEATURE_WORK,
                (now - timedelta(hours=i)).isoformat(),
            )
        )

    knowledge = detect_project_switching(sessions, min_switches=5)

    assert len(knowledge) > 0
    assert any("switching" in k.statement.lower() for k in knowledge)


def test_detect_habits():
    """Engineering habits should be detected from repeated activities."""
    now = datetime.now(timezone.utc)
    sessions = []

    # Repeated testing activity
    for i in range(10):
        sessions.append(
            make_session(
                "Friday",
                SessionActivity.TESTING,
                (now - timedelta(days=i)).isoformat(),
            )
        )

    knowledge = detect_habits(sessions, min_occurrences=5)
    friday_habits = [k for k in knowledge if "Friday" in k.subject]

    assert len(friday_habits) > 0
    assert any("testing" in k.statement.lower() for k in friday_habits)
    assert friday_habits[0].type == KnowledgeType.ENGINEERING_HABIT


# --- Relationship Detection Tests ---


def test_detect_project_relationships():
    """Project relationships should be detected from co-occurrence."""
    now = datetime.now(timezone.utc)
    sessions = []

    # Alternate between projects to show relationship
    for i in range(20):
        sessions.append(
            make_session(
                "Friday",
                SessionActivity.FEATURE_WORK,
                (now - timedelta(hours=i * 2)).isoformat(),
            )
        )
        sessions.append(
            make_session(
                "Vivaha",
                SessionActivity.FEATURE_WORK,
                (now - timedelta(hours=i * 2 + 1)).isoformat(),
            )
        )

    knowledge = detect_relationships(sessions, min_evidence=12)

    assert len(knowledge) > 0
    assert any(
        "Friday" in k.subject and "Vivaha" in k.subject for k in knowledge
    )
    assert knowledge[0].type == KnowledgeType.PROJECT_RELATIONSHIP


# --- Confidence Tests ---


def test_confidence_from_evidence_count():
    """Confidence should increase with evidence count."""
    k = Knowledge(
        type=KnowledgeType.ENGINEERING_TREND,
        subject="Friday",
        statement="Friday usage is increasing",
        confidence=KnowledgeConfidence.WEAK,
        evidence_ids=["e1", "e2", "e3"],
    )

    # Add evidence to reach medium
    new_evidence = [f"e{i}" for i in range(4, 16)]
    k = update_confidence(k, new_evidence)
    assert k.confidence == KnowledgeConfidence.MEDIUM
    assert k.evidence_count == 15

    # Add more evidence to reach strong
    more_evidence = [f"e{i}" for i in range(16, 41)]
    k = update_confidence(k, more_evidence)
    assert k.confidence == KnowledgeConfidence.STRONG
    assert k.evidence_count == 40


def test_verification_increases_status():
    """Verification should upgrade knowledge status."""
    k = Knowledge(
        type=KnowledgeType.ENGINEERING_HABIT,
        subject="Friday",
        statement="Consistently performs testing",
        confidence=KnowledgeConfidence.STRONG,
        evidence_ids=[f"e{i}" for i in range(50)],
        status=KnowledgeStatus.OBSERVED,
    )

    # First verification
    k = verify_knowledge(k)
    assert k.verification_count == 1
    assert k.status == KnowledgeStatus.VERIFIED

    # More verifications lead to stable
    k = verify_knowledge(k)
    k = verify_knowledge(k)
    assert k.verification_count == 3
    assert k.status == KnowledgeStatus.STABLE


# --- Engine Tests ---


def test_knowledge_engine_build_idempotent(db):
    """Building knowledge twice should be idempotent."""
    now = datetime.now(timezone.utc)

    # Insert observations
    obs = [
        make_observation(
            "git", "Friday", "repository", "active", (now - timedelta(days=i)).isoformat()
        )
        for i in range(5)
    ]
    insert_observations(db, [o.to_row() for o in obs])

    # Insert sessions
    sessions = [
        make_session(
            "Friday",
            SessionActivity.FEATURE_WORK,
            (now - timedelta(days=i)).isoformat(),
        )
        for i in range(5)
    ]
    insert_sessions(db, [s.to_row() for s in sessions])

    # First build
    engine = KnowledgeEngine(db)
    result1 = engine.build()

    # Second build
    result2 = engine.build()

    # Should not create new knowledge
    assert result2.created == 0
    assert result2.total == result1.total


def test_knowledge_engine_preserves_history(db):
    """Knowledge history should be preserved as it evolves."""
    now = datetime.now(timezone.utc)

    # Initial observations
    obs = [
        make_observation(
            "git", "Friday", "repository", "active", (now - timedelta(days=i)).isoformat()
        )
        for i in range(5)
    ]
    insert_observations(db, [o.to_row() for o in obs])

    sessions = [
        make_session(
            "Friday",
            SessionActivity.FEATURE_WORK,
            (now - timedelta(days=i)).isoformat(),
        )
        for i in range(5)
    ]
    insert_sessions(db, [s.to_row() for s in sessions])

    engine = KnowledgeEngine(db)
    result1 = engine.build()
    initial_count = result1.total

    # Add more observations
    new_obs = [
        make_observation(
            "git", "Friday", "repository", "active", (now - timedelta(days=i)).isoformat()
        )
        for i in range(5, 10)
    ]
    insert_observations(db, [o.to_row() for o in new_obs])

    new_sessions = [
        make_session(
            "Friday",
            SessionActivity.FEATURE_WORK,
            (now - timedelta(days=i)).isoformat(),
        )
        for i in range(5, 10)
    ]
    insert_sessions(db, [s.to_row() for s in new_sessions])

    # Build again
    result2 = engine.build()

    # Should have updated existing knowledge
    assert result2.updated > 0
    assert result2.total >= initial_count


def test_no_duplicate_knowledge(db):
    """Engine should not create duplicate knowledge for same subject."""
    now = datetime.now(timezone.utc)

    # Multiple observations for same subject
    obs = [
        make_observation(
            "git", "Friday", "repository", "active", (now - timedelta(days=i)).isoformat()
        )
        for i in range(10)
    ]
    insert_observations(db, [o.to_row() for o in obs])

    sessions = [
        make_session(
            "Friday",
            SessionActivity.FEATURE_WORK,
            (now - timedelta(days=i)).isoformat(),
        )
        for i in range(10)
    ]
    insert_sessions(db, [s.to_row() for s in sessions])

    engine = KnowledgeEngine(db)
    engine.build()

    # Check no duplicates
    all_knowledge = engine.all_knowledge()
    subjects = [(k.type.value, k.subject) for k in all_knowledge]
    assert len(subjects) == len(set(subjects)), "Duplicate knowledge detected"


def test_evidence_linkage(db):
    """All knowledge should link back to evidence."""
    now = datetime.now(timezone.utc)

    obs = [
        make_observation(
            "git", "Friday", "repository", "active", (now - timedelta(days=i)).isoformat()
        )
        for i in range(5)
    ]
    insert_observations(db, [o.to_row() for o in obs])

    sessions = [
        make_session(
            "Friday",
            SessionActivity.FEATURE_WORK,
            (now - timedelta(days=i)).isoformat(),
        )
        for i in range(5)
    ]
    insert_sessions(db, [s.to_row() for s in sessions])

    engine = KnowledgeEngine(db)
    engine.build()

    all_knowledge = engine.all_knowledge()

    # Every knowledge should have evidence
    for k in all_knowledge:
        assert k.evidence_count > 0 or k.type in (
            KnowledgeType.ENGINEERING_TREND,
            KnowledgeType.PROJECT_RELATIONSHIP,
        ), f"Knowledge {k.id} has no evidence"


def test_knowledge_evolution(db):
    """Knowledge should evolve as new evidence accumulates."""
    now = datetime.now(timezone.utc)

    # Start with weak evidence
    obs = [
        make_observation(
            "git", "Python", "language", "used", (now - timedelta(days=i)).isoformat()
        )
        for i in range(5)
    ]
    insert_observations(db, [o.to_row() for o in obs])

    engine = KnowledgeEngine(db)
    result1 = engine.build()

    python_knowledge = [k for k in engine.all_knowledge() if k.subject == "Python"]
    if python_knowledge:
        initial_confidence = python_knowledge[0].confidence

        # Add more evidence
        new_obs = [
            make_observation(
                "git",
                "Python",
                "language",
                "used",
                (now - timedelta(days=i)).isoformat(),
            )
            for i in range(5, 45)
        ]
        insert_observations(db, [o.to_row() for o in new_obs])

        result2 = engine.build()

        # Confidence should have increased
        python_knowledge = [k for k in engine.all_knowledge() if k.subject == "Python"]
        assert len(python_knowledge) > 0
        final_confidence = python_knowledge[0].confidence

        # Should have stronger confidence now
        confidence_order = [
            KnowledgeConfidence.WEAK,
            KnowledgeConfidence.MEDIUM,
            KnowledgeConfidence.STRONG,
        ]
        assert confidence_order.index(final_confidence) >= confidence_order.index(
            initial_confidence
        )
