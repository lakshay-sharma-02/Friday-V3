"""Confidence aggregation for the Understanding Engine (Milestone 8.3).

Understanding confidence is DERIVED, never guessed. It reflects how strongly the
contributing *knowledge* reinforces a stable engineering meaning, weighted by:

  1. knowledge agreement  — how many distinct knowledge items back it, and
     whether they agree in direction (no contradiction among contributors).
  2. confidence aggregation — the confidence band of the contributors
     (strong knowledge counts more than weak).
  3. cross-source reinforcement — knowledge drawn from multiple knowledge TYPES
     (e.g. a trend + an investment + a relationship) is worth more than many
     sightings of one type.

Algorithm (documented, deterministic):

  score = ( sum of contributor confidence weight )
          * cross_source_multiplier
          * agreement_factor

  contributor weight: WEAK=1, MEDIUM=2, STRONG=4
  cross_source_multiplier: 1.0 + 0.15 * (distinct_contributor_types - 1),
      capped at 1.6. Two types = 1.15, three = 1.30, four = 1.45, >=5 = 1.6.
  agreement_factor: 1.0 if all contributors share the same direction sign
      (their `direction` metadata agree), else 0.6 (a real but contested signal).

  band: score >= 16 -> STRONG ; >= 6 -> MEDIUM ; else WEAK.

Mirror: knowledge thresholds were weak/medium/strong at count 15/40. Here the
weighted score reaches MEDIUM with ~3 medium contributors (2*3=6) and STRONG
with ~4 strong contributors (4*4=16) or several cross-typed mediums.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple

from .models import (
    UnderstandingConfidence,
    UnderstandingStatus,
)


_CONF_WEIGHT = {
    UnderstandingConfidence.WEAK: 1,
    UnderstandingConfidence.MEDIUM: 2,
    UnderstandingConfidence.STRONG: 4,
}

STRONG_THRESHOLD = 16.0
MEDIUM_THRESHOLD = 6.0


@dataclass
class Contributor:
    """One knowledge entry that backs an understanding.

    `weight` is the contributor's confidence band weight.
    `source_type` is the knowledge TYPE (used for cross-source reinforcement).
    `agrees` is True when the contributor's direction is consistent with the
    understanding's thesis (default True; contradiction detectors set False).
    """

    knowledge_id: str
    source_type: str
    weight: int
    agrees: bool = True


def aggregate_confidence(contributors: List[Contributor]) -> UnderstandingConfidence:
    """Derive an understanding's confidence from its backing knowledge.

    Every understanding must have >=1 contributor (cites knowledge). With none,
    the caller must not create the understanding at all.
    """
    if not contributors:
        return UnderstandingConfidence.WEAK

    total = sum(c.weight for c in contributors)

    distinct_types = {c.source_type for c in contributors}
    cross = min(1.6, 1.0 + 0.15 * (len(distinct_types) - 1))

    agreeing = [c for c in contributors if c.agrees]
    agreement = 1.0 if len(agreeing) == len(contributors) else 0.6

    score = total * cross * agreement

    if score >= STRONG_THRESHOLD:
        return UnderstandingConfidence.STRONG
    if score >= MEDIUM_THRESHOLD:
        return UnderstandingConfidence.MEDIUM
    return UnderstandingConfidence.WEAK


def status_from_confidence(
    conf: UnderstandingConfidence, contributor_count: int
) -> UnderstandingStatus:
    """Map derived confidence + reinforcement breadth to a lifecycle status.

    Mirrors knowledge: strong + broad reinforcement -> Verified/Stable; thin ->
    Candidate/Observed. Reinforcement breadth substitutes for verification_count.
    """
    if conf == UnderstandingConfidence.STRONG and contributor_count >= 4:
        return UnderstandingStatus.STABLE
    if conf == UnderstandingConfidence.STRONG and contributor_count >= 2:
        return UnderstandingStatus.VERIFIED
    if conf == UnderstandingConfidence.MEDIUM and contributor_count >= 2:
        return UnderstandingStatus.OBSERVED
    return UnderstandingStatus.CANDIDATE


def confidence_band(conf: UnderstandingConfidence) -> int:
    return _CONF_WEIGHT[conf]


def agreement_factor(contributors: List[Contributor]) -> float:
    if not contributors:
        return 1.0
    agreeing = sum(1 for c in contributors if c.agrees)
    return 1.0 if agreeing == len(contributors) else 0.6


def cross_source_multiplier(contributors: List[Contributor]) -> float:
    if not contributors:
        return 1.0
    distinct_types = {c.source_type for c in contributors}
    return min(1.6, 1.0 + 0.15 * (len(distinct_types) - 1))


def explain_score(contributors: List[Contributor]) -> Tuple[float, Dict[str, float]]:
    """Return (score, breakdown) so the CLI can show how confidence was derived."""
    total = sum(c.weight for c in contributors)
    cross = cross_source_multiplier(contributors)
    agree = agreement_factor(contributors)
    score = total * cross * agree
    return score, {
        "total_contributor_weight": float(total),
        "cross_source_multiplier": round(cross, 2),
        "agreement_factor": agree,
    }
