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

# Determinism preference: a *deterministic* built-in executor (local,
# reproducible) is preferred over an AI executor, which is only used when a
# deterministic executor cannot satisfy the task. This is the inverse of the
# old "_PREFERRED_WORKERS = (worker:claude,)" behaviour that routed everything
# to Claude.
_W_DETERMINISTIC = 50    # bonus for a deterministic (non-AI) executor
_W_AI_PENALTY = 40       # penalty for an AI executor (LLM/service/agent)

# Phase 1.5: explicit artifact contract -> capability. When the planner declares
# an expected artifact (e.g. "calculator.py"), an executor whose capability
# covers that artifact kind is transparently boosted. This keeps selection
# CAPABILITY-DRIVEN (no hardcoded worker:claude routing) — the boost reuses the
# worker's own declared capabilities, so a deterministic worker that genuinely
# covers the artifact wins on evidence.
_ARTIFACT_CAP: dict = {
    "py": "python", "pyi": "python", "ipynb": "python",
    "md": "documentation", "rst": "documentation", "txt": "documentation",
    "json": "file editing", "yaml": "file editing", "yml": "file editing",
    "toml": "file editing", "cfg": "file editing", "ini": "file editing",
    "sh": "shell commands", "bash": "shell commands",
    "sql": "git operations",   # schema/migration artifacts usually git-tracked
    "rs": "rust", "go": "go", "ts": "typescript", "tsx": "typescript",
    "js": "typescript", "jsx": "typescript", "java": "java",
    "c": "c", "h": "c", "cpp": "cpp", "hpp": "cpp",
}
_W_ARTIFACT_MATCH = 8      # transparent bonus when a worker covers the contract


_P_MISSING_CAP = 20      # per missing mandatory capability (penalized, not fatal)
_P_DISABLED = 20         # disabled worker
_P_UNSUPPORTED_LANG = 5  # task needs a language the worker lacks
_P_UNSUPPORTED_TASK = 5  # worker lacks the task_type
_P_UNSUPPORTED_PLAN = 3  # worker lacks the plan_type

# Intent-based executor routing has been removed. The resolver now scores
# workers purely on capability match, task-type support, determinism, and
# availability — all queried from the real Worker Registry. No hardcoded
# worker IDs. See rank_workers() and select_assignment().

from ..worker.models import WorkerKind as _WorkerKind  # noqa: E402

# Executor ids backed by external AI CLIs / APIs. These are non-deterministic
# and must only be used when no deterministic built-in covers the task.
_AI_EXECUTOR_IDS = frozenset({
    "worker:claude", "worker:codex", "worker:gemini", "worker:opencode",
    "worker:aider", "worker:deepseek",
})

# Deterministic, local, reproducible built-in executors.
_DETERMINISTIC_EXECUTOR_IDS = frozenset({
    "worker:shell", "worker:git", "worker:filesystem", "worker:python",
    "worker:testing", "worker:documentation",
})


def is_ai_executor(worker) -> bool:
    """True for AI/LLM/service/agent workers or external AI CLI/API adapters."""
    if worker.id in _AI_EXECUTOR_IDS:
        return True
    return worker.kind.value in (_WorkerKind.LLM.value, _WorkerKind.SERVICE.value,
                                 _WorkerKind.AGENT.value)


def is_deterministic(worker) -> bool:
    """True for local, reproducible built-in executors."""
    if worker.id in _DETERMINISTIC_EXECUTOR_IDS:
        return True
    return worker.kind.value in (_WorkerKind.FUNCTION.value,
                                 _WorkerKind.TOOL.value, _WorkerKind.CLI.value) \
        and worker.id not in _AI_EXECUTOR_IDS


def _intent_for(task_required: List[str], task_type: str) -> str:
    """Derive the planner intent for a task (task_type first, else a cap)."""
    _ = None  # worker IDs no longer referenced
    return (task_type or "").lower()


def _langs_in_task(required: List[str]) -> List[str]:
    """Language tokens present in a task's required_capabilities.

    The capability vocabulary overlaps the language vocabulary (Rust, Python,
    SQL, TypeScript, ...), so a task that requires 'Rust' is also expressing a
    language need. Exact, deterministic — no inference."""
    return [c for c in required if is_valid_language(c)]


def _artifact_capabilities(expected_artifacts: List[str]) -> List[str]:
    """Map declared expected-artifact paths to the capabilities that produce them."""
    caps: List[str] = []
    for a in expected_artifacts or []:
        a = (a or "").strip()
        if "." in a and not a.endswith(".") and not a.startswith("."):
            ext = a.rsplit(".", 1)[1].lower()
            cap = _ARTIFACT_CAP.get(ext)
            if cap and cap not in caps:
                caps.append(cap)
    return caps


def score_worker(
    task_required: List[str],
    task_type: str,
    plan_type: str,
    worker: Worker,
    intent: str = "",
    expected_artifacts: Optional[List[str]] = None,
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

    # Phase 1.5: explicit artifact-contract signal. If the planner declared an
    # expected artifact whose producing capability this worker actually has,
    # boost it. Capability-driven (reuses worker.capabilities), not a hardcoded
    # route to any specific executor.
    for ac in _artifact_capabilities(expected_artifacts or []):
        if ac in have and ac not in matched:
            sb.capability += _W_ARTIFACT_MATCH

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

    # Check runtime availability, not just registry status.
    # An active worker whose binary is missing or unavailable gets penalized
    # at resolution time so the resolver picks a viable alternative or marks
    # the task UNRESOLVED with a clear reason — never hands runtime a worker
    # that will 404 at execution time.
    if worker.availability != "available":
        sb.penalty += _P_DISABLED

    sb.confidence = _W_CONFIDENCE.get(worker.confidence, 0)

    # Separately record determinism preference (not a penalty on the base
    # score, so it does not distort the capability explanation).
    if missing:
        sb.penalty += _P_MISSING_CAP * len(missing)
    _tag_executor_kind(sb, worker)

    return sb, matched, missing


# Determinism / AI classification is recorded on the ScoreBreakdown so the
# ranking can prefer deterministic executors without losing transparency.
def _tag_executor_kind(sb, worker, intent: str = "") -> None:
    _ = intent  # intent-based AI routing removed; all tasks score with
                # deterministic-first preference.
    if is_ai_executor(worker):
        sb.executor_pref += -_W_AI_PENALTY
    elif is_deterministic(worker):
        sb.executor_pref += _W_DETERMINISTIC


def _confidence_for(
    matched: List[str],
    missing: List[str],
    task_type: str,
    plan_type: str,
    worker: Worker,
    successful_history: int,
) -> str:
    return derive_confidence(ConfidenceInputs(
        capability_coverage=(len(matched) / (len(matched) + len(missing)))
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
    expected_artifacts: Optional[List[str]] = None,
) -> List[Tuple[Worker, ScoreBreakdown, List[str], List[str], str]]:
    """Score + rank workers for a task, deterministically.

    RANKS rather than rejecting: a worker missing a capability is heavily
    penalized but still considered, so selection degrades gracefully instead of
    an all-or-nothing UNRESOLVED. Disabled workers are excluded (never runnable).

    Ranking (best-first):
      1) total score (capability + determinism bonus - penalties),
      2) confidence,
      3) estimated speed,
      4) estimated cost,
      5) alphabetical worker id.

    Returns a list of (worker, score, matched, missing, confidence) sorted
    best-first. Empty only when there are no active workers at all.
    """
    intent = _intent_for(task_required, task_type)
    hist = successful_history or {}
    scored = []
    for w in workers:
        sb, matched, missing = score_worker(
            task_required, task_type, plan_type, w, intent=intent,
            expected_artifacts=expected_artifacts)
        # Disabled workers can never run — exclude. Missing caps are NOT fatal;
        # the penalty already pushes them below capable workers.
        if w.status != "active":
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
            -sb.total,               # 1) net score (higher better)
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
    expected_artifacts: Optional[List[str]] = None,
) -> Tuple[Optional[Worker], List[Worker], str, List[str], List[str], str, List[dict]]:
    """Pick the assignment for a task.

    Returns:
      (chosen_worker_or_None, candidate_list, confidence, matched, missing,
       reason, alternatives)

    `chosen_worker` is None and `missing` is populated when no worker satisfied
    mandatory capabilities (UNRESOLVED). Workers are NEVER invented.
    """
    ranked = rank_workers(
        task_required, task_type, plan_type, workers, successful_history,
        expected_artifacts=expected_artifacts)

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
    missing = ranked[0][3]
    conf = ranked[0][4]

    # Transparent, spec-format diagnostic: why this executor was selected.
    deterministic = is_deterministic(chosen)
    why = [f"✓ supports {', '.join(matched)}" if matched
           else "no direct capability match"]
    why.append("deterministic" if deterministic else "ai executor")
    why.append("highest score")
    if deterministic:
        reason = f"{chosen.id} selected — " + "; ".join(why) + \
                 (f" (missing: {', '.join(missing)})" if missing else "")
    else:
        reason = (
            f"{chosen.id} selected (AI fallback) — " + "; ".join(why) +
            (f" (missing: {', '.join(missing)})" if missing else
             "; no deterministic executor covered this intent")
        )
    return (chosen, candidates, conf, matched, missing, reason, alternatives)
