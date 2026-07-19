"""Pattern detection for the Knowledge Engine (Milestone 8.1).

Detects repeated patterns in observations and sessions:
- Repeated technology usage
- Repeated activity sequences
- Repeated project switching
- Repeated work patterns

Everything is evidence-backed and deterministic.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Dict, List, Tuple

from ..context.models import EngineeringSession, SessionActivity
from ..observation.model import Observation
from .models import Knowledge, KnowledgeConfidence, KnowledgeType, now_iso


def detect_repeated_usage(
    observations: List[Observation], min_count: int = 3
) -> List[Knowledge]:
    """Detect repeated technology or project usage patterns."""
    knowledge = []

    # Count occurrences by subject (repository or technology)
    subjects: Dict[str, List[str]] = defaultdict(list)
    for obs in observations:
        if obs.aspect in ("repository", "technology", "language"):
            subjects[obs.subject].append(obs.id)

    for subject, evidence_ids in subjects.items():
        count = len(evidence_ids)
        if count < min_count:
            continue

        confidence = _confidence_from_count(count)
        statement = f"{subject} is repeatedly used in engineering work"

        knowledge.append(
            Knowledge(
                type=KnowledgeType.TECHNOLOGY_INVESTMENT,
                subject=subject,
                statement=statement,
                confidence=confidence,
                evidence_ids=evidence_ids,
            )
        )

    return knowledge


def detect_repeated_sequences(
    sessions: List[EngineeringSession], min_count: int = 2
) -> List[Knowledge]:
    """Detect repeated sequences of activities."""
    knowledge = []

    # Group sessions by primary repository
    repo_sessions: Dict[str, List[EngineeringSession]] = defaultdict(list)
    for session in sessions:
        if session.primary_repo:
            repo_sessions[session.primary_repo].append(session)

    for repo, sess_list in repo_sessions.items():
        if len(sess_list) < min_count:
            continue

        # Count activity sequences (skip self-transitions: "committing followed
        # by committing" is not a pattern — it is just repeated work).
        sequences = Counter()
        for i in range(len(sess_list) - 1):
            act1 = sess_list[i].activity.value
            act2 = sess_list[i + 1].activity.value
            if act1 == act2:
                continue
            sequences[(act1, act2)] += 1

        for (act1, act2), count in sequences.items():
            if count < min_count:
                continue

            confidence = _confidence_from_count(count)
            statement = f"In {repo}, {act1} is frequently followed by {act2}"

            evidence_ids = [s.id for s in sess_list]

            knowledge.append(
                Knowledge(
                    type=KnowledgeType.RECURRING_PATTERN,
                    subject=repo,
                    statement=statement,
                    confidence=confidence,
                    evidence_ids=evidence_ids[:20],  # Cap evidence
                )
            )

    return knowledge


def detect_project_switching(
    sessions: List[EngineeringSession], min_switches: int = 5
) -> List[Knowledge]:
    """Detect frequent switching between projects."""
    if len(sessions) < 2:
        return []

    knowledge = []

    # Count transitions between repositories
    transitions: Dict[Tuple[str, str], List[str]] = defaultdict(list)
    for i in range(len(sessions) - 1):
        curr = sessions[i].primary_repo
        next_repo = sessions[i + 1].primary_repo
        if curr and next_repo and curr != next_repo:
            transitions[(curr, next_repo)].append(sessions[i].id)

    for (repo_a, repo_b), evidence_ids in transitions.items():
        if len(evidence_ids) < min_switches:
            continue

        confidence = _confidence_from_count(len(evidence_ids))
        statement = f"Frequent switching between {repo_a} and {repo_b}"

        knowledge.append(
            Knowledge(
                type=KnowledgeType.RECURRING_PATTERN,
                subject=f"{repo_a},{repo_b}",
                statement=statement,
                confidence=confidence,
                evidence_ids=evidence_ids[:20],
            )
        )

    return knowledge


def detect_habits(
    sessions: List[EngineeringSession], min_occurrences: int = 5
) -> List[Knowledge]:
    """Detect engineering habits from session patterns."""
    knowledge = []

    # Activity frequency by repository
    repo_activities: Dict[str, Dict[str, List[str]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for session in sessions:
        if session.primary_repo and session.activity != SessionActivity.UNKNOWN:
            repo_activities[session.primary_repo][session.activity.value].append(
                session.id
            )

    for repo, activities in repo_activities.items():
        for activity, evidence_ids in activities.items():
            if len(evidence_ids) < min_occurrences:
                continue

            confidence = _confidence_from_count(len(evidence_ids))
            statement = f"Consistently performs {activity} in {repo}"

            knowledge.append(
                Knowledge(
                    type=KnowledgeType.ENGINEERING_HABIT,
                    subject=repo,
                    statement=statement,
                    confidence=confidence,
                    evidence_ids=evidence_ids[:20],
                )
            )

    return knowledge


def _confidence_from_count(count: int) -> KnowledgeConfidence:
    """Map evidence count to confidence level."""
    if count >= 40:
        return KnowledgeConfidence.STRONG
    elif count >= 15:
        return KnowledgeConfidence.MEDIUM
    else:
        return KnowledgeConfidence.WEAK
