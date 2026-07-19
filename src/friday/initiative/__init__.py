"""Initiative Engine package (Milestone 8.4).

Write-only layer above Understanding. Derives durable long-running engineering
initiatives from accumulated understanding (plus knowledge-evolution and
knowledge). The Brain consumes initiatives as evidence; it never computes them.
"""

from .confidence import (
    Contributor,
    aggregate_confidence,
    explain_score,
    status_from_confidence,
)
from .derivation import Candidate, detect
from .engine import InitiativeBuildResult, InitiativeEngine
from .models import (
    Initiative,
    InitiativeConfidence,
    InitiativeStatus,
    InitiativeType,
)

__all__ = [
    "InitiativeEngine",
    "InitiativeBuildResult",
    "Initiative",
    "InitiativeType",
    "InitiativeStatus",
    "InitiativeConfidence",
    "Candidate",
    "detect",
    "Contributor",
    "aggregate_confidence",
    "status_from_confidence",
    "explain_score",
]
