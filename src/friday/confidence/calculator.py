"""Shared confidence scoring infrastructure.

Extracted from duplicated logic in understanding/initiative/insight confidence
modules. Domain-specific weights, thresholds, Contributor types, multiplier
functions, and status mappings stay in their owning modules.
"""

from __future__ import annotations

from typing import Dict, Tuple


def agreement_factor(contributors: list) -> float:
    """1.0 if all contributors agree, else 0.6.

    Identical across understanding/initiative/insight. Extracted to eliminate
    the three-way copy-paste.
    """
    if not contributors:
        return 1.0
    agreeing = sum(1 for c in contributors if getattr(c, "agrees", True))
    return 1.0 if agreeing == len(contributors) else 0.6


def confidence_band(conf, weight_dict: dict) -> int:
    """Map a confidence enum to its numeric weight via the domain's weight dict."""
    return weight_dict[conf]


def make_explain_score(
    total_weight: float,
    multiplier: float,
    agreement: float,
    multiplier_label: str = "cross_source_multiplier",
) -> Tuple[float, Dict[str, float]]:
    """Build the score + breakdown dict shared by all domain ``explain_score``
    implementations.  The caller provides the domain-specific multiplier and
    its label (e.g. ``"cross_project_multiplier"``)."""
    score = total_weight * multiplier * agreement
    return score, {
        "total_contributor_weight": float(total_weight),
        multiplier_label: round(multiplier, 2),
        "agreement_factor": agreement,
    }
