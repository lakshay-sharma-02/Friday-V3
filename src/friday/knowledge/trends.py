"""Trend detection for the Knowledge Engine (Milestone 8.1).

Detects trends in engineering activity over time:
- Increasing usage
- Stable usage
- Decreasing usage
- Dormant projects
- Emerging interests

All based entirely on timestamps and observation counts.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Dict, List

from ..context.models import EngineeringSession
from ..observation.model import Observation
from .models import (
    Knowledge,
    KnowledgeConfidence,
    KnowledgeType,
    Trend,
    TrendDirection,
)


def detect_trends(
    observations: List[Observation], sessions: List[EngineeringSession]
) -> List[Knowledge]:
    """Detect trends in engineering activity."""
    knowledge = []

    # Repository trends from sessions
    repo_trends = _analyze_repository_trends(sessions)
    for trend in repo_trends:
        if trend.evidence_count < 3:
            continue

        statement = _trend_to_statement(trend)
        confidence = _confidence_from_evidence(trend.evidence_count)

        knowledge.append(
            Knowledge(
                type=KnowledgeType.ENGINEERING_TREND,
                subject=trend.subject,
                statement=statement,
                confidence=confidence,
                evidence_ids=[],  # Trends are derived from aggregates
            )
        )

    # Technology trends from observations
    tech_trends = _analyze_technology_trends(observations)
    for trend in tech_trends:
        if trend.evidence_count < 3:
            continue

        statement = _trend_to_statement(trend)
        confidence = _confidence_from_evidence(trend.evidence_count)

        knowledge.append(
            Knowledge(
                type=KnowledgeType.ENGINEERING_INTEREST,
                subject=trend.subject,
                statement=statement,
                confidence=confidence,
                evidence_ids=[],
            )
        )

    return knowledge


def _spans_window(timestamps: List[datetime], min_span_days: int = 1) -> bool:
    """Require the evidence to be spread over time, not all from one instant.

    A 'trend'/'emerging interest' needs at least two points separated by a real
    gap; a single observation run (all stamped the same second) is not a trend.
    """
    if len(timestamps) < 2:
        return False
    span = (max(timestamps) - min(timestamps)).days
    return span >= min_span_days


def _analyze_repository_trends(sessions: List[EngineeringSession]) -> List[Trend]:
    """Analyze repository usage trends over time."""
    if not sessions:
        return []

    trends = []

    # Group sessions by repository and time bucket
    repo_timeline: Dict[str, List[datetime]] = defaultdict(list)
    for session in sessions:
        if not session.primary_repo:
            continue
        try:
            dt = datetime.fromisoformat(session.start_time)
        except (ValueError, TypeError):
            continue
        repo_timeline[session.primary_repo].append(dt)

    for repo, timestamps in repo_timeline.items():
        if len(timestamps) < 3:
            continue
        if not _spans_window(timestamps):
            continue

        direction = _compute_trend_direction(timestamps)
        trends.append(
            Trend(
                subject=repo,
                direction=direction,
                evidence_count=len(timestamps),
                first_seen=min(timestamps).isoformat(),
                last_seen=max(timestamps).isoformat(),
            )
        )

    return trends


def _analyze_technology_trends(observations: List[Observation]) -> List[Trend]:
    """Analyze technology usage trends over time."""
    trends = []

    # Group observations by subject (technology/language)
    subject_timeline: Dict[str, List[datetime]] = defaultdict(list)
    for obs in observations:
        if obs.aspect not in ("technology", "language", "tool"):
            continue
        try:
            dt = datetime.fromisoformat(obs.observed_at)
        except (ValueError, TypeError):
            continue
        subject_timeline[obs.subject].append(dt)

    for subject, timestamps in subject_timeline.items():
        if len(timestamps) < 3:
            continue
        if not _spans_window(timestamps):
            continue

        direction = _compute_trend_direction(timestamps)
        trends.append(
            Trend(
                subject=subject,
                direction=direction,
                evidence_count=len(timestamps),
                first_seen=min(timestamps).isoformat(),
                last_seen=max(timestamps).isoformat(),
            )
        )

    return trends


def _compute_trend_direction(timestamps: List[datetime]) -> TrendDirection:
    """Compute trend direction from timestamps."""
    if len(timestamps) < 3:
        return TrendDirection.STABLE

    sorted_times = sorted(timestamps)
    now = datetime.now(timezone.utc)

    # Check if dormant (no activity in last 30 days)
    if (now - sorted_times[-1]).days > 30:
        return TrendDirection.DORMANT

    # Check if emerging (first seen in last 30 days)
    if (now - sorted_times[0]).days <= 30:
        return TrendDirection.EMERGING

    # Split timeline into two halves
    mid = len(sorted_times) // 2
    first_half = sorted_times[:mid]
    second_half = sorted_times[mid:]

    first_span = (first_half[-1] - first_half[0]).days or 1
    second_span = (second_half[-1] - second_half[0]).days or 1

    first_density = len(first_half) / first_span
    second_density = len(second_half) / second_span

    # Compare density
    if second_density > first_density * 1.5:
        return TrendDirection.INCREASING
    elif first_density > second_density * 1.5:
        return TrendDirection.DECREASING
    else:
        return TrendDirection.STABLE


def _trend_to_statement(trend: Trend) -> str:
    """Convert a trend to a human-readable statement."""
    if trend.direction == TrendDirection.INCREASING:
        return f"{trend.subject} usage is increasing"
    elif trend.direction == TrendDirection.DECREASING:
        return f"{trend.subject} usage is decreasing"
    elif trend.direction == TrendDirection.DORMANT:
        return f"{trend.subject} has become dormant"
    elif trend.direction == TrendDirection.EMERGING:
        return f"{trend.subject} is an emerging interest"
    else:
        return f"{trend.subject} usage is stable"


def _confidence_from_evidence(count: int) -> KnowledgeConfidence:
    """Map evidence count to confidence level."""
    if count >= 40:
        return KnowledgeConfidence.STRONG
    elif count >= 15:
        return KnowledgeConfidence.MEDIUM
    else:
        return KnowledgeConfidence.WEAK
