"""Confidence aggregation for the Initiative Engine (Milestone 8.4).

Initiative confidence is DERIVED, never guessed. It reflects how strongly the
contributing *understanding* (and knowledge) reinforces a stable engineering
objective, weighted by:

  1. understanding agreement — how many distinct understandings back it, and
     whether they agree in direction.
  2. knowledge agreement — the confidence band of the underlying knowledge.
  3. breadth — total contributing evidence across understanding + knowledge.
  4. cross-project reinforcement — evidence drawn from multiple repositories is
     worth more than many sightings in one repo.

Algorithm (documented, deterministic):

  score = ( sum of contributor confidence weight )
          * cross_project_multiplier
          * agreement_factor

  contributor weight: WEAK=1, MEDIUM=2, STRONG=4
  cross_project_multiplier: 1.0 + 0.20 * (distinct_repos - 1), capped at 1.8.
      Two repos = 1.2, three = 1.4, four = 1.6, >=5 = 1.8.
  agreement_factor: 1.0 if all contributors share direction sign, else 0.6.

  band: score >= 16 -> STRONG ; >= 6 -> MEDIUM ; else WEAK.

Mirror: understanding thresholds were weak/medium/strong at scores 6/16. Here a
single strong understanding (weight 4) with one repo scores 4 -> WEAK; two
cross-repo strong understandings score ~9.6 -> MEDIUM; four cross-repo strong
understandings reach STRONG.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple

from .models import InitiativeConfidence


_CONF_WEIGHT = {
    InitiativeConfidence.WEAK: 1,
    InitiativeConfidence.MEDIUM: 2,
    InitiativeConfidence.STRONG: 4,
}

STRONG_THRESHOLD = 16.0
MEDIUM_THRESHOLD = 6.0

# A contributor may be an understanding (cross-repo reinforced) or knowledge.
@dataclass
class Contributor:
    """One understanding/knowledge entry that backs an initiative.

    `weight` is the contributor's confidence band weight. `source_type` is
    "understanding" or "knowledge". `repo` is the originating repository (used
    for cross-project reinforcement). `agrees` is True when the contributor's
    direction is consistent with the initiative's thesis.
    """

    evidence_id: str
    source_type: str
    weight: int
    repo: str = ""
    agrees: bool = True


def aggregate_confidence(
    contributors: List[Contributor], repos: List[str]
) -> InitiativeConfidence:
    """Derive an initiative's confidence from its backing evidence.

    Every initiative must have >=1 contributor (cites understanding/knowledge).
    With none, the caller must not create the initiative at all.
    """
    if not contributors:
        return InitiativeConfidence.WEAK

    total = sum(c.weight for c in contributors)

    distinct_repos = {c.repo for c in contributors if c.repo} | set(repos)
    cross = min(1.8, 1.0 + 0.20 * (max(1, len(distinct_repos)) - 1))

    agreeing = [c for c in contributors if c.agrees]
    agreement = 1.0 if len(agreeing) == len(contributors) else 0.6

    score = total * cross * agreement

    if score >= STRONG_THRESHOLD:
        return InitiativeConfidence.STRONG
    if score >= MEDIUM_THRESHOLD:
        return InitiativeConfidence.MEDIUM
    return InitiativeConfidence.WEAK


def status_from_confidence(
    conf: InitiativeConfidence, contributor_count: int
) -> "InitiativeStatus":
    """Map derived confidence + evidence breadth to a lifecycle status.

    Mirrors lower layers: strong + broad reinforcement -> Active/Review; thin ->
    Candidate/Started. Breadth substitutes for verification_count.
    """
    from .models import InitiativeStatus

    if conf == InitiativeConfidence.STRONG and contributor_count >= 4:
        return InitiativeStatus.ACTIVE
    if conf == InitiativeConfidence.STRONG and contributor_count >= 2:
        return InitiativeStatus.REVIEW
    if conf == InitiativeConfidence.MEDIUM and contributor_count >= 2:
        return InitiativeStatus.STARTED
    return InitiativeStatus.CANDIDATE


def confidence_band(conf: InitiativeConfidence) -> int:
    return _CONF_WEIGHT[conf]


def agreement_factor(contributors: List[Contributor]) -> float:
    if not contributors:
        return 1.0
    agreeing = sum(1 for c in contributors if c.agrees)
    return 1.0 if agreeing == len(contributors) else 0.6


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
    agree = agreement_factor(contributors)
    score = total * cross * agree
    return score, {
        "total_contributor_weight": float(total),
        "cross_project_multiplier": round(cross, 2),
        "agreement_factor": agree,
    }
