"""Repair loop models (Law 16)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from ..db import now_iso


@dataclass
class RepairCandidateEvent:
    """A failed, reviewed task/graph that is a candidate for repair.

    Detected deterministically from Review verdicts on runtime sessions.
    """
    original_graph_id: str
    original_task_id: str
    failure_reason: str           # from the Review finding, not guessed
    capability: str
    repair_depth: int = 0         # 0 = first attempt, increments per re-repair
    detected_at: str = field(default_factory=lambda: now_iso())


@dataclass
class RepairProposal:
    """Drafted repair plan, pending human approval. Never auto-executed."""
    id: str
    candidate: RepairCandidateEvent
    decision: str                 # "auto_eligible" | "escalate_bottleneck" | "escalate_depth_cap"
    evidence_ids: List[str]       # Knowledge/Observation ids the decision was based on
    proposed_goal: str            # the goal text to hand back to Planning
    status: str = "pending"       # pending | approved | rejected
