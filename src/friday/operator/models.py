"""OperatorProfile model — mirrors ProjectIdentity shape and discipline.

Every field is Optional when evidence is insufficient. Nothing invents.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class OperatorProfile:
    """Standing model of the operator (you), derived from persisted evidence.

    Three categories of fields:
    1. Evidence-derived — computed from existing DB tables (read-only)
    2. Explicit — loaded from operator_preferences (set via CLI)
    3. All Optional — None when evidence is insufficient
    """

    # --- Evidence-derived fields ---

    # Capability proposal approval rate: from proposed_workers table.
    capability_approval_rate: Optional[dict[str, int]] = None

    # Graph/task review pattern: from task_graphs / repair_proposals.
    graph_review_pattern: Optional[dict[str, int]] = None

    # Initiative review pattern: from pending_initiatives.
    initiative_review_pattern: Optional[dict[str, int]] = None

    # Repair preference: from repair_proposals (approved/rejected counts).
    repair_approval_rate: Optional[dict[str, int]] = None

    # Active repos: repos with most recent activity (sessions, commits).
    active_repos: Optional[list[dict[str, object]]] = None

    # Watch cycle statistics: from watch_history.
    watch_stats: Optional[dict[str, int | float]] = None

    # Preferred initiative types: derived from which types you approve most.
    preferred_initiative_types: Optional[list[str]] = None

    # --- Explicit preferences (set via `friday profile set`) ---
    explicit_preferences: dict[str, str] = field(default_factory=dict)

    @property
    def has_profile(self) -> bool:
        """Whether the profile has any meaningful content."""
        return bool(
            self.capability_approval_rate
            or self.graph_review_pattern
            or self.initiative_review_pattern
            or self.repair_approval_rate
            or self.active_repos
            or self.watch_stats
            or self.explicit_preferences
        )
