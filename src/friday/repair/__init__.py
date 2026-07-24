"""Repair Loop (Law 16).

Repair is not a new execution path. It is a new Planning cycle, triggered by
a failed Review verdict, that goes through the exact same pipeline every other
goal goes through: Planning → Task Graph → Capability Resolver → Scheduler →
Runtime → Review.

Public surface:
  - RepairCandidateEvent / RepairProposal    (models)
  - detect_repair_candidates / evaluate_repair / propose_repair / approve_repair
  - get_pending_proposals / get_all_candidates
"""

from .engine import (
    MAX_REPAIR_DEPTH,
    approve_repair,
    detect_repair_candidates,
    evaluate_repair,
    get_all_candidates,
    get_pending_proposals,
    propose_repair,
)
from .models import RepairCandidateEvent, RepairProposal

__all__ = [
    "RepairCandidateEvent",
    "RepairProposal",
    "MAX_REPAIR_DEPTH",
    "detect_repair_candidates",
    "evaluate_repair",
    "propose_repair",
    "approve_repair",
    "get_pending_proposals",
    "get_all_candidates",
]
