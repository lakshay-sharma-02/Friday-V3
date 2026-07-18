"""Capability matching + scoring (Milestone 9.3).

Pure, deterministic functions. No I/O, no LLM, no randomness, no time
dependence. Given a Task (from the Task Graph) and the active Worker pool (from
the Worker Registry), produce a ranked, transparent scoring for each worker.

The Resolver answers ONE question: WHO can perform WHICH task, and WHY. It does
not execute, schedule, or persist — that is the engine's job (engine.py) and
future milestones (Scheduler, Runtime).

Capability matching is EXACT (case-insensitive canonical form, per the frozen
Worker Registry vocabulary). No fuzzy logic, no embeddings, no synonyms.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from ..worker.models import Worker, is_valid_language, validate_capabilities
from .confidence import ConfidenceInputs, derive_confidence
from .models import (
    ResolutionStatus,
    ScoreBreakdown,
    SelectionStrategy,
)

# --- deterministic score weights (integers; fully reproducible) --------------
_W_CAPABILITY = 10       # per matched mandatory capability
_W_LANGUAGE = 5          # task language supported by worker
_W_TASK_TYPE = 5         # worker supports the task's task_type
_W_PLAN_TYPE = 3         # worker supports the plan's plan_type
_W_AVAILABLE = 5         # worker is active
_W_CONFIDENCE = {"high": 5, "medium": 2, "low": 0}

_P_MISSING_CAP = 20      # per missing mandatory capability (also rejects)
_P_DISABLED = 20         # disabled worker
_P_UNSUPPORTED_LANG = 5  # task needs a language the worker lacks
_P_UNSUPPORTED_TASK = 5  # worker lacks the task_type
_P_UNSUPPORTED_PLAN = 3  # worker lacks the plan_type


def _langs_in_task(required: List[str]) -> List[str]:
    """Language tokens present in a task's required_capabilities.

    The capability vocabulary overlaps the language vocabulary (Rust, Python,
    SQL, TypeScript, ...), so a task that requires 'Rust' is also expressing a
    language need. Exact, deterministic — no inference."""
    return [c for c in required if is_valid_language(c)]


def score_worker(
    task_required: List[str],
    task_type: str,
    plan_type: str,
    worker: Worker,
) -> Tuple[ScoreBreakdown, List[str], List[str]]:
    """Score one (task, worker) pair.

    Returns (breakdown, matched_caps, missing_caps). Missing mandatory
    capabilities are reported but the penalty is applied by the caller's
    rejection rule; the score still reflects the gap for explainability.

    Capability matching is EXACT and case-insensitive: the task's required
    capabilities are canonicalized via the Worker Registry vocabulary (which
    uses Capitalized canonical forms) before comparison with the worker's
    capabilities. No fuzzy logic.
    """
    # Canonicalize required caps to the registry's Capitalized forms so the
    # Task Graph's lowercase tokens match worker capabilities exactly.
    # Keep originals separate: unknown caps are reported as missing, not
    # silently dropped.
    original_required = list(task_required)
    required = validate_capabilities(list(task_required))
    req_set = set(required)
    have = set(worker.capabilities)

    matched = sorted(req_set & have)
    # Unknown caps (not in vocabulary) are always missing — no worker has them.
    known_lower = {c.lower() for c in required}
    unknown = sorted({c for c in original_required if c.lower() not in known_lower})
    missing = sorted((req_set - have) | set(unknown))

    sb = ScoreBreakdown()
    sb.capability = _W_CAPABILITY * len(matched)

    task_langs = _langs_in_task(required)
    if task_langs and set(task_langs) & set(worker.supported_languages):
        sb.language = _W_LANGUAGE
    elif task_langs:
        sb.penalty += _P_UNSUPPORTED_LANG

    if task_type in worker.supported_task_types:
        sb.task_type = _W_TASK_TYPE
    else:
        sb.penalty += _P_UNSUPPORTED_TASK

    if plan_type in worker.supported_plan_types:
        sb.plan_type = _W_PLAN_TYPE
    else:
        sb.penalty += _P_UNSUPPORTED_PLAN

    if worker.status == "active":
        sb.availability = _W_AVAILABLE
    else:
        sb.penalty += _P_DISABLED

    sb.confidence = _W_CONFIDENCE.get(worker.confidence, 0)

    if missing:
        sb.penalty += _P_MISSING_CAP * len(missing)

    return sb, matched, missing


def _confidence_for(
    matched: List[str],
    missing: List[str],
    task_type: str,
    plan_type: str,
    worker: Worker,
    successful_history: int,
) -> str:
    return derive_confidence(ConfidenceInputs(
        capability_coverage=(len(matched) / len(matched) + len(missing))
        if (matched or missing) else 1.0,
        task_supported=task_type in worker.supported_task_types,
        plan_supported=plan_type in worker.supported_plan_types,
        worker_confidence=worker.confidence,
        successful_history=successful_history,
        required_count=len(matched) + len(missing),
    ))


def rank_workers(
    task_required: List[str],
    task_type: str,
    plan_type: str,
    workers: List[Worker],
    successful_history: Optional[dict] = None,
) -> List[Tuple[Worker, ScoreBreakdown, List[str], List[str], str]]:
    """Score + rank workers for a task, deterministically.

    Rejects (excludes) any worker missing a MANDATORY capability. The remaining
    workers are ranked by:
      tie-break 1) capability score, 2) confidence, 3) estimated speed,
      4) estimated cost, 5) alphabetical worker id.

    Returns a list of (worker, score, matched, missing, confidence) sorted
    best-first. Empty if no worker satisfied mandatory capabilities.
    """
    hist = successful_history or {}
    scored = []
    for w in workers:
        sb, matched, missing = score_worker(
            task_required, task_type, plan_type, w)
        # Reject disabled workers and workers missing mandatory capabilities.
        if w.status != "active" or missing:
            continue
        conf = _confidence_for(
            matched, missing, task_type, plan_type, w,
            hist.get(w.id, 0))
        scored.append((w, sb, matched, missing, conf))

    def sort_key(item):
        w, sb, _, _, conf = item
        speed_rank = {"fast": 0, "medium": 1, "slow": 2, "unknown": 3}.get(
            w.estimated_speed, 3)
        cost_rank = {"low": 0, "medium": 1, "high": 2, "unknown": 3}.get(
            w.estimated_cost, 3)
        return (
            -sb.capability,          # 1) capability score (higher better)
            _conf_rank(conf),        # 2) confidence (high=0 best, low=2 worst)
            speed_rank,              # 3) estimated speed (faster better)
            cost_rank,               # 4) estimated cost (cheaper better)
            w.id,                    # 5) alphabetical worker id
        )

    return sorted(scored, key=sort_key)


def _conf_rank(band: str) -> int:
    return {"high": 0, "medium": 1, "low": 2}.get(band, 2)


def select_assignment(
    task_required: List[str],
    task_type: str,
    plan_type: str,
    workers: List[Worker],
    strategy: SelectionStrategy = SelectionStrategy.SINGLE,
    successful_history: Optional[dict] = None,
) -> Tuple[Optional[Worker], List[Worker], str, List[str], List[str], str, List[dict]]:
    """Pick the assignment for a task.

    Returns:
      (chosen_worker_or_None, candidate_list, confidence, matched, missing,
       reason, alternatives)

    `chosen_worker` is None and `missing` is populated when no worker satisfied
    mandatory capabilities (UNRESOLVED). Workers are NEVER invented.
    """
    ranked = rank_workers(
        task_required, task_type, plan_type, workers, successful_history)

    if not ranked:
        return (None, [], "low", [], list(task_required),
                "No eligible worker satisfied the mandatory capabilities.",
                [])

    # Build the candidate/alternative split by strategy.
    if strategy == SelectionStrategy.SINGLE:
        chosen_idx = 1
    elif strategy == SelectionStrategy.PARALLEL:
        chosen_idx = len(ranked)  # all eligible run in parallel
    else:  # SEQUENTIAL
        chosen_idx = len(ranked)  # all eligible run in sequence

    chosen = ranked[0][0]
    candidates = [w.id for w, _, _, _, _ in ranked[:chosen_idx]]
    alternatives = [
        {
            "worker_id": w.id,
            "worker_name": w.name,
            "confidence": conf,
            "score": sb.to_dict(),
            "matched_capabilities": matched,
        }
        for w, sb, matched, _, conf in ranked[chosen_idx:]
    ]

    matched = ranked[0][2]
    conf = ranked[0][4]
    reason = (
        f"Best capability match ({len(matched)}/{len(task_required)} "
        f"required) among {len(ranked)} eligible worker(s); "
        f"confidence={conf}."
    )
    return (chosen, candidates, conf, matched, [], reason, alternatives)
