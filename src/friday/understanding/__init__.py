"""Understanding Engine package (Milestone 8.3).

Write-only layer above Knowledge. Derives durable engineering understanding from
accumulated knowledge. The Brain consumes it as evidence; it never computes it.
"""

from .confidence import (
    Contributor,
    aggregate_confidence,
    explain_score,
    status_from_confidence,
)
from .derivation import Candidate, detect
from .engine import UnderstandingBuildResult, UnderstandingEngine
from .models import (
    Understanding,
    UnderstandingConfidence,
    UnderstandingStatus,
    UnderstandingType,
)

__all__ = [
    "UnderstandingEngine",
    "UnderstandingBuildResult",
    "Understanding",
    "UnderstandingType",
    "UnderstandingStatus",
    "UnderstandingConfidence",
    "Candidate",
    "detect",
    "Contributor",
    "aggregate_confidence",
    "status_from_confidence",
    "explain_score",
]
