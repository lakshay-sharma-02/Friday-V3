"""Confidence aggregation for the Insight Engine (Milestone 8.5).

Insight confidence is DERIVED, never guessed. It reflects how strongly the
contributing understanding / initiative / knowledge agrees that something
deserves attention, weighted by:

  1. understanding agreement — how many distinct understandings back it.
  2. initiative agreement    — reinforcing initiatives.
  3. knowledge agreement      — the confidence band of underlying knowledge.
  4. cross-project reinforcement — evidence drawn from multiple repositories is
     worth more than many sightings in one repo.

Algorithm (deterministic):

  score = ( sum of contributor confidence weight )
          * cross_project_multiplier
          * agreement_factor

  contributor weight: WEAK=1, MEDIUM=2, STRONG=4
  cross_project_multiplier: 1.0 + 0.20 * (distinct_repos - 1), capped at 1.8.
  agreement_factor: 1.0 if all contributors share direction sign, else 0.6.

  band: score >= 16 -> STRONG ; >= 6 -> MEDIUM ; else WEAK.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple

from ..confidence.calculator import (
    agreement_factor as _shared_agreement_factor,
    make_explain_score as _shared_make_explain_score,
)
from .models import InsightConfidence


_CONF_WEIGHT = {
    InsightConfidence.WEAK: 1,
    InsightConfidence.MEDIUM: 2,
    InsightConfidence.STRONG: 4,
}

STRONG_THRESHOLD = 16.0
MEDIUM_THRESHOLD = 6.0


@dataclass
class Contributor:
    """One understanding/initiative/knowledge entry that backs an insight.

    `weight` is the contributor's confidence band weight. `source_type` is
    "understanding" / "initiative" / "knowledge". `repo` is the originating
    repository (for cross-project reinforcement). `agrees` is True when the
    contributor's direction is consistent with the insight's thesis.
    """

    evidence_id: str
    source_type: str
    weight: int
    repo: str = ""
    agrees: bool = True


def aggregate_confidence(
    contributors: List[Contributor], repos: List[str]
) -> InsightConfidence:
    """Derive an insight's confidence from its backing evidence.

    Every insight must have >=1 contributor (cites evidence). With none, the
    caller must not create the insight at all.
    """
    if not contributors:
        return InsightConfidence.WEAK

    total = sum(c.weight for c in contributors)

    distinct_repos = {c.repo for c in contributors if c.repo} | set(repos)
    cross = min(1.8, 1.0 + 0.20 * (max(1, len(distinct_repos)) - 1))

    agreeing = [c for c in contributors if c.agrees]
    agreement = 1.0 if len(agreeing) == len(contributors) else 0.6

    score = total * cross * agreement

    if score >= STRONG_THRESHOLD:
        return InsightConfidence.STRONG
    if score >= MEDIUM_THRESHOLD:
        return InsightConfidence.MEDIUM
    return InsightConfidence.WEAK


def status_from_confidence(
    conf: InsightConfidence, contributor_count: int
) -> "InsightStatus":
    """Map derived confidence + evidence breadth to a lifecycle status.

    Mirrors lower layers: strong + broad reinforcement -> Stable/Verified; thin
    -> Candidate/Observed. Breadth substitutes for verification_count.
    """
    from .models import InsightStatus

    if conf == InsightConfidence.STRONG and contributor_count >= 4:
        return InsightStatus.STABLE
    if conf == InsightConfidence.STRONG and contributor_count >= 2:
        return InsightStatus.VERIFIED
    if conf == InsightConfidence.MEDIUM and contributor_count >= 2:
        return InsightStatus.OBSERVED
    return InsightStatus.CANDIDATE


def confidence_band(conf: InsightConfidence) -> int:
    return _CONF_WEIGHT[conf]


def agreement_factor(contributors: List[Contributor]) -> float:
    return _shared_agreement_factor(contributors)


def cross_project_multiplier(contributors: List[Contributor], repos: List[str]) -> float:
    if not contributors and not repos:
        return 1.0
    distinct = {c.repo for c in contributors if c.repo} | set(repos)
    return min(1.8, 1.0 + 0.20 * (max(1, len(distinct)) - 1))


def explain_score(
    contributors: List[Contributor], repos: List[str]
) -> Tuple[float, Dict[str, float]]:
    """Return (score, breakdown) so the CLI can show how confidence was derived."""
    total = sum(c.weight for c in contributors)
    cross = cross_project_multiplier(contributors, repos)
    agree = _shared_agreement_factor(contributors)
    return _shared_make_explain_score(
        total, cross, agree, multiplier_label="cross_project_multiplier",
    )
