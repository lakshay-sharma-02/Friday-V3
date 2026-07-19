"""Relationship detection for the Knowledge Engine (Milestone 8.1).

Detects long-term relationships between projects based on session evidence.
No graph database — just deterministic relationships.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Dict, List, Set, Tuple

from ..context.models import EngineeringSession
from .models import Knowledge, KnowledgeConfidence, KnowledgeType, Relationship


def detect_relationships(
    sessions: List[EngineeringSession], min_evidence: int = 12
) -> List[Knowledge]:
    """Detect project relationships from session patterns."""
    knowledge = []

    # Count co-occurrence and transitions between repositories
    repo_pairs: Dict[Tuple[str, str], List[str]] = defaultdict(list)

    for i in range(len(sessions)):
        curr_session = sessions[i]
        if not curr_session.primary_repo:
            continue

        # Look at next few sessions for related work
        for j in range(i + 1, min(i + 5, len(sessions))):
            next_session = sessions[j]
            if not next_session.primary_repo:
                continue

            if curr_session.primary_repo == next_session.primary_repo:
                continue

            # Order consistently (alphabetically)
            repos = tuple(sorted([curr_session.primary_repo, next_session.primary_repo]))
            repo_pairs[repos].append(curr_session.id)

    for (repo_a, repo_b), evidence_ids in repo_pairs.items():
        if len(evidence_ids) < min_evidence:
            continue

        confidence = _confidence_from_count(len(evidence_ids))
        relationship_kind = _infer_relationship_kind(repo_a, repo_b, len(evidence_ids))

        statement = f"{repo_a} {relationship_kind} {repo_b}"

        knowledge.append(
            Knowledge(
                type=KnowledgeType.PROJECT_RELATIONSHIP,
                subject=f"{repo_a},{repo_b}",
                statement=statement,
                confidence=confidence,
                evidence_ids=evidence_ids[:30],  # Cap evidence
            )
        )

    return knowledge


def detect_project_evolution(
    sessions: List[EngineeringSession], min_sessions: int = 20
) -> List[Knowledge]:
    """Detect how projects have evolved over time."""
    knowledge = []

    # Group sessions by repository
    repo_sessions: Dict[str, List[EngineeringSession]] = defaultdict(list)
    for session in sessions:
        if session.primary_repo:
            repo_sessions[session.primary_repo].append(session)

    for repo, sess_list in repo_sessions.items():
        if len(sess_list) < min_sessions:
            continue

        # Sort by time
        sess_list.sort(key=lambda s: s.start_time)

        # Analyze activity distribution over time
        early = sess_list[:len(sess_list) // 3]
        late = sess_list[len(sess_list) * 2 // 3:]

        early_activities = {s.activity.value for s in early}
        late_activities = {s.activity.value for s in late}

        # Detect shift in activities
        new_activities = late_activities - early_activities
        dropped_activities = early_activities - late_activities

        if new_activities or dropped_activities:
            confidence = _confidence_from_count(len(sess_list))

            if new_activities:
                statement = f"{repo} has evolved to include {', '.join(new_activities)}"
            else:
                statement = f"{repo} work patterns have shifted over time"

            knowledge.append(
                Knowledge(
                    type=KnowledgeType.PROJECT_EVOLUTION,
                    subject=repo,
                    statement=statement,
                    confidence=confidence,
                    evidence_ids=[s.id for s in sess_list[:30]],
                )
            )

    return knowledge


def _infer_relationship_kind(repo_a: str, repo_b: str, evidence_count: int) -> str:
    """Infer relationship type from evidence strength."""
    if evidence_count >= 30:
        return "frequently improves"
    elif evidence_count >= 20:
        return "often benefits from work in"
    else:
        return "has a relationship with"


def _confidence_from_count(count: int) -> KnowledgeConfidence:
    """Map evidence count to confidence level."""
    if count >= 40:
        return KnowledgeConfidence.STRONG
    elif count >= 15:
        return KnowledgeConfidence.MEDIUM
    else:
        return KnowledgeConfidence.WEAK
