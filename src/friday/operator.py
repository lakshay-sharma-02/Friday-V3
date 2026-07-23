"""Operator identity — who you are and what you consistently prefer.

OperatorProfile is DERIVED ON READ from two sources:
  1. Evidence-derived fields — computed from existing persisted DB tables
     (proposed_workers, task_graphs) — never inferred.
  2. Explicit preferences — written only via `friday profile set`, never by
     Friday's own code (no inference writes).

This mirrors `identity.py`'s discipline exactly: nothing invents, every field
is Optional, and missing evidence is stated plainly rather than fabricated.

This is Phase 1: build the model and let you populate it deliberately. No
module reads OperatorProfile for decision-making yet — that's a separate,
reviewed phase.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .db import (
    connect,
    get_all_operator_preferences,
    get_proposed_workers,
)


@dataclass
class OperatorProfile:
    """Standing model of the operator (you), derived from persisted evidence.

    Fields mirror ProjectIdentity's shape:
    - Evidence-derived: computed on read from existing DB tables
    - Explicit: loaded from `operator_preferences` where source='explicit'
    - All fields Optional — None when evidence is insufficient
    """

    # --- Evidence-derived fields ---

    # Capability proposal approval rate: derived from proposed_workers table
    # (Worker Genesis). Computed as (approved count, rejected count, pending count).
    capability_approval_rate: Optional[dict[str, int]] = None

    # Graph review pattern: derived from task_graphs review/approval history.
    # Shows counts of approved vs rejected graph proposals. None if no review
    # history exists (no task_graphs with proposal status).
    graph_review_pattern: Optional[dict[str, int]] = None

    # --- Explicit preferences (set via `friday profile set`) ---
    explicit_preferences: dict[str, str] = field(default_factory=dict)

    @property
    def has_profile(self) -> bool:
        """Whether the profile has any meaningful content."""
        return bool(self.capability_approval_rate
                    or self.graph_review_pattern
                    or self.explicit_preferences)


def _compute_capability_approval_rate(conn) -> Optional[dict[str, int]]:
    """Derive capability approval rate from proposed_workers table.

    Counts approved, rejected, and pending proposals. Returns None when
    there are no proposals at all (no evidence to compute from).
    """
    all_proposals = get_proposed_workers(conn)
    if not all_proposals:
        return None
    approved = sum(1 for p in all_proposals if p.status == "approved")
    rejected = sum(1 for p in all_proposals if p.status == "rejected")
    pending = sum(1 for p in all_proposals if p.status == "pending")
    total = approved + rejected + pending
    rate = round(approved / total, 2) if total > 0 else 0.0
    return {
        "approved": approved,
        "rejected": rejected,
        "pending": pending,
        "total": total,
        "rate": rate,
    }


def _compute_graph_review_pattern(conn) -> Optional[dict[str, int]]:
    """Derive graph review pattern from task_graphs table.

    Counts approved vs rejected graph proposals. Returns None when
    no graphs have been reviewed (no task_graphs with proposal/approved/
    rejected status).
    """
    rows = conn.execute(
        "SELECT status, COUNT(*) AS c FROM task_graphs "
        "WHERE status IN ('proposal', 'approved', 'rejected') "
        "GROUP BY status"
    ).fetchall()
    if not rows:
        return None
    result: dict[str, int] = {}
    for r in rows:
        result[r["status"]] = r["c"]
    return result


def build_operator_profile(conn=None) -> OperatorProfile:
    """Assemble an OperatorProfile from persisted evidence.

    Returns a fully populated profile (never None), with None fields for
    dimensions that lack evidence — same discipline as identity.py.
    """
    own_conn = conn is None
    if own_conn:
        conn = connect()

    try:
        cap_rate = _compute_capability_approval_rate(conn)
        graph_review = _compute_graph_review_pattern(conn)
        explicit = {
            r.key: r.value
            for r in get_all_operator_preferences(conn, source="explicit")
        }

        return OperatorProfile(
            capability_approval_rate=cap_rate,
            graph_review_pattern=graph_review,
            explicit_preferences=explicit,
        )
    finally:
        if own_conn:
            conn.close()
