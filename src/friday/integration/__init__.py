"""Integration Engine — cross-project integration via the Planning→Runtime pipeline.

Extension layer (not frozen). Takes synthesis results and routes them through
the existing Planning → Task Graph → Capability Resolver → Scheduler → Runtime
pipeline, with the same approval gates (graph review) as every other path.
"""

from __future__ import annotations

from .engine import IntegrationEngine, IntegrateResult, _MAX_REPOS

__all__ = [
    "IntegrationEngine",
    "IntegrateResult",
    "_MAX_REPOS",
]
