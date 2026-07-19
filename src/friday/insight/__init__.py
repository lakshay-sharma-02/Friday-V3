"""Insight Engine package (Milestone 8.5).

Write-only layer above Initiatives. Derives rare, high-value engineering
insights from accumulated understanding (plus initiatives and knowledge). The
Brain consumes insights as evidence; it never computes them. Insights are
ephemeral: a build retires those whose triggering conditions no longer hold.
"""

from .confidence import (
    Contributor,
    aggregate_confidence,
    explain_score,
    status_from_confidence,
)
from .derivation import Candidate, detect
from .engine import InsightBuildResult, InsightEngine
from .models import Insight, InsightConfidence, InsightStatus, InsightType

__all__ = [
    "InsightEngine",
    "InsightBuildResult",
    "Insight",
    "InsightType",
    "InsightStatus",
    "InsightConfidence",
    "Candidate",
    "detect",
    "Contributor",
    "aggregate_confidence",
    "status_from_confidence",
    "explain_score",
]
