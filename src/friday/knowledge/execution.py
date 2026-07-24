"""Execution knowledge detection (Law 17 — Learning Loop).

Detects knowledge from runtime execution observations:
- Capability reliability: how often a capability succeeds vs. total attempts
- Repair bottlenecks: recurring repair_required=true signals for a task type

Follows the same pattern as static.py / patterns.py / trends.py. Every emitted
Knowledge row has `evidence_ids` set to the actual runtime Observation ids the
detector derived its conclusion from (Law 4 — knowledge without evidence does
not exist).
"""

from __future__ import annotations

from collections import Counter
from typing import List

from ..db import get_task_graph_by_id
from ..observation.model import Observation
from .models import (
    Knowledge,
    KnowledgeConfidence,
    KnowledgeStatus,
    KnowledgeType,
    now_iso,
)

# Minimum evidence thresholds, matching convention in patterns.py:
#   detect_repeated_usage     uses min_count=3
#   detect_repeated_sequences uses min_count=2
#   detect_habits             uses min_occurrences=5
_MIN_CAPABILITY_EVIDENCE = 3
_MIN_BOTTLENECK_EVIDENCE = 2


def detect_capability_reliability(observations: List[Observation]) -> List[Knowledge]:
    """One Knowledge entry per capability with enough execution history.

    Processes Observations where source='runtime' and aspect='capability_reliability'.
    Groups by subject (capability name), creates a statement summarising the
    aggregate ratio. Evidence = the observation ids that back the ratio.
    Skips capabilities with fewer than _MIN_CAPABILITY_EVIDENCE observations.

    Example output for a capability 'rust' observed 3/4 success:
      Knowledge(type=CAPABILITY_RELIABILITY, subject='rust',
                statement='rust capability: 3/4 successful executions (75%).',
                evidence_ids=[...3 obs ids...])
    """
    knowledge: List[Knowledge] = []

    # Collect capability reliability observations
    cap_obs: dict[str, list[Observation]] = {}
    for obs in observations:
        if obs.source != "runtime" or obs.aspect != "capability_reliability":
            continue
        cap_obs.setdefault(obs.subject, []).append(obs)

    for cap_name, obs_list in cap_obs.items():
        if len(obs_list) < _MIN_CAPABILITY_EVIDENCE:
            continue

        # Aggregate ratios from observation values (format: "X/Y success")
        total_success = 0
        total_attempts = 0
        for o in obs_list:
            try:
                parts = o.value.split("/")
                successes = int(parts[0])
                attempts = int(parts[1].split()[0])
                total_success += successes
                total_attempts += attempts
            except (ValueError, IndexError):
                continue

        if total_attempts == 0:
            continue

        pct = round(100.0 * total_success / total_attempts)
        statement = (
            f"{cap_name} capability: {total_success}/{total_attempts} "
            f"successful executions ({pct}%)."
        )
        confidence = (
            KnowledgeConfidence.STRONG
            if total_attempts >= 10
            else KnowledgeConfidence.MEDIUM
            if total_attempts >= 5
            else KnowledgeConfidence.WEAK
        )

        knowledge.append(
            Knowledge(
                type=KnowledgeType.CAPABILITY_RELIABILITY,
                subject=cap_name,
                statement=statement,
                confidence=confidence,
                evidence_ids=[o.id for o in obs_list],
                status=KnowledgeStatus.OBSERVED,
            )
        )

    return knowledge


def detect_repair_bottlenecks(observations: List[Observation]) -> List[Knowledge]:
    """Surfaces a graph/task-type where repair_required=true is recurring.

    Reads Observations where source='runtime', aspect='repair_required',
    value='true'. Groups by subject (graph_id). Requires at least
    _MIN_BOTTLENECK_EVIDENCE occurrences before promoting to Knowledge.
    Evidence = the observation ids of the repair_required=true facts.

    Example:
      Knowledge(type=EXECUTION_BOTTLENECK, subject='graph-abc',
                statement='Graph "Improve the README" required repair in 2/3 runs.',
                evidence_ids=[...obs ids...])
    """
    knowledge: List[Knowledge] = []

    repair_obs: dict[str, list[Observation]] = {}
    # Track total observations per graph (repair_required=false + true)
    total_obs: Counter[str] = Counter()
    for obs in observations:
        if obs.source != "runtime" or obs.aspect != "repair_required":
            continue
        total_obs[obs.subject] += 1
        if obs.value == "true":
            repair_obs.setdefault(obs.subject, []).append(obs)

    for graph_id, obs_list in repair_obs.items():
        if len(obs_list) < _MIN_BOTTLENECK_EVIDENCE:
            continue

        total = total_obs.get(graph_id, len(obs_list))
        pct = round(100.0 * len(obs_list) / total) if total > 0 else 100
        # Try to get the graph's goal for a readable statement
        goal_str = graph_id
        statement = (
            f"Graph {goal_str} required repair in {len(obs_list)}/{total} "
            f"executions ({pct}%)."
        )

        confidence = (
            KnowledgeConfidence.MEDIUM
            if len(obs_list) >= 5
            else KnowledgeConfidence.WEAK
        )

        knowledge.append(
            Knowledge(
                type=KnowledgeType.EXECUTION_BOTTLENECK,
                subject=graph_id,
                statement=statement,
                confidence=confidence,
                evidence_ids=[o.id for o in obs_list],
                status=KnowledgeStatus.OBSERVED,
            )
        )

    return knowledge
