"""Knowledge domain objects → view models."""
from __future__ import annotations
from ..models import SummaryView


def knowledge_result_to_summary(
    total: int, created: int, updated: int, verified: int,
    candidates: int, stable: int,
) -> SummaryView:
    """Build a SummaryView from a KnowledgeBuildResult."""
    return SummaryView(
        files_modified=total,
        tests_passed=stable,
        warnings=candidates,
    )
