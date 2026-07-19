"""Confidence management for the Knowledge Engine (Milestone 8.1).

Confidence increases through repeated evidence, never through LLM belief.
"""

from __future__ import annotations

from typing import List

from .models import Knowledge, KnowledgeConfidence, KnowledgeStatus


def update_confidence(knowledge: Knowledge, new_evidence_ids: List[str]) -> Knowledge:
    """Update knowledge confidence based on new evidence."""
    # Add new evidence
    all_evidence = set(knowledge.evidence_ids)
    all_evidence.update(new_evidence_ids)

    knowledge.evidence_ids = list(all_evidence)

    # Update confidence based on total evidence count
    count = len(knowledge.evidence_ids)
    knowledge.confidence = _confidence_from_count(count)

    # Update status based on verification and confidence
    if knowledge.confidence == KnowledgeConfidence.STRONG:
        if knowledge.verification_count >= 3:
            knowledge.status = KnowledgeStatus.STABLE
        elif knowledge.verification_count >= 1:
            knowledge.status = KnowledgeStatus.VERIFIED
        else:
            knowledge.status = KnowledgeStatus.OBSERVED
    elif knowledge.confidence == KnowledgeConfidence.MEDIUM:
        if knowledge.verification_count >= 2:
            knowledge.status = KnowledgeStatus.VERIFIED
        else:
            knowledge.status = KnowledgeStatus.OBSERVED
    else:
        knowledge.status = KnowledgeStatus.CANDIDATE

    return knowledge


def verify_knowledge(knowledge: Knowledge) -> Knowledge:
    """Mark knowledge as verified (increases verification count)."""
    knowledge.verification_count += 1

    # Update status based on confidence and verification count
    if knowledge.confidence == KnowledgeConfidence.STRONG and knowledge.verification_count >= 3:
        knowledge.status = KnowledgeStatus.STABLE
    elif knowledge.verification_count >= 1:
        knowledge.status = KnowledgeStatus.VERIFIED

    return knowledge


def should_retire(knowledge: Knowledge, days_since_evidence: int) -> bool:
    """Determine if knowledge should be retired based on evidence age."""
    # Strong knowledge requires longer inactivity to retire
    if knowledge.confidence == KnowledgeConfidence.STRONG:
        return days_since_evidence > 180
    elif knowledge.confidence == KnowledgeConfidence.MEDIUM:
        return days_since_evidence > 120
    else:
        return days_since_evidence > 60


def merge_duplicate_knowledge(k1: Knowledge, k2: Knowledge) -> Knowledge:
    """Merge two knowledge entries about the same subject."""
    # Combine evidence
    all_evidence = set(k1.evidence_ids)
    all_evidence.update(k2.evidence_ids)

    # Use the older creation time
    created_at = min(k1.created_at, k2.created_at)

    # Sum verification counts
    verification_count = k1.verification_count + k2.verification_count

    # Create merged knowledge
    merged = Knowledge(
        type=k1.type,
        subject=k1.subject,
        statement=k1.statement,  # Keep first statement
        confidence=_confidence_from_count(len(all_evidence)),
        evidence_ids=list(all_evidence),
        created_at=created_at,
        verification_count=verification_count,
    )

    # Update status based on merged evidence
    if merged.confidence == KnowledgeConfidence.STRONG and merged.verification_count >= 3:
        merged.status = KnowledgeStatus.STABLE
    elif merged.verification_count >= 1:
        merged.status = KnowledgeStatus.VERIFIED
    else:
        merged.status = KnowledgeStatus.OBSERVED

    return merged


def _confidence_from_count(count: int) -> KnowledgeConfidence:
    """Map evidence count to confidence level."""
    if count >= 40:
        return KnowledgeConfidence.STRONG
    elif count >= 15:
        return KnowledgeConfidence.MEDIUM
    else:
        return KnowledgeConfidence.WEAK
