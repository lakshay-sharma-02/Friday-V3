"""Pillar B Stage 4 — Skill Formation.

Takes labeled workflow intents (Stage 3) and forms them into deployable,
replayable skills registered in the shared workers registry.
"""

from __future__ import annotations

import json
from typing import Optional
from uuid import uuid4

from .db import (
    get_mined_patterns,
    get_workflow_intents,
    get_formed_skill_by_intent,
    insert_formed_skill,
    insert_worker,
    insert_worker_history,
    insert_worker_version,
    now_iso,
)
from .db import WorkerRow, WorkerHistoryRow, WorkerVersionRow


_CONSENSUS_THRESHOLD = 0.8


def form_skills(conn) -> list[dict]:
    """Run the skill formation pipeline.

    For each high/medium confidence workflow intent that doesn't already
    have a formed skill, creates the skill record and registers a worker.

    Returns list of formed skill dicts that were created (empty if none).
    """
    intents = get_workflow_intents(conn)
    if not intents:
        return []

    created: list[dict] = []

    for intent in intents:
        confidence = (intent.get("confidence") or "low").lower()
        if confidence not in ("high", "medium"):
            continue

        intent_id = intent["id"]

        # Skip if already formed.
        existing = get_formed_skill_by_intent(conn, intent_id)
        if existing:
            continue

        skill = _form_one(conn, intent)
        if skill:
            created.append(skill)

    return created


def _form_one(conn, intent: dict) -> Optional[dict]:
    """Form one skill from a workflow intent."""
    now = now_iso()
    intent_id = intent["id"]

    # Build task_graph from pattern_summary.
    pattern_summary = intent.get("pattern_summary", "")
    try:
        task_graph = json.loads(pattern_summary) if pattern_summary else []
    except (json.JSONDecodeError, TypeError):
        task_graph = []

    if not task_graph:
        return None

    # Get the source mined_pattern for exemplars.
    pattern_id = intent.get("pattern_id")
    exemplar_data: dict = {}
    if pattern_id:
        patterns = get_mined_patterns(conn, limit=9999)
        for p in patterns:
            if p["id"] == pattern_id:
                try:
                    raw = p.get("exemplars", "{}")
                    exemplar_data = json.loads(raw) if raw else {}
                except (json.JSONDecodeError, TypeError):
                    exemplar_data = {}
                break

    # Resolve exemplar values with consensus check.
    resolved_exemplars: dict[str, dict] = {}
    has_low_consensus = False
    for pos_key, dist in exemplar_data.items():
        if not isinstance(dist, dict):
            continue
        total = sum(dist.values())
        if total == 0:
            continue
        best_val = max(dist, key=dist.get)
        best_count = dist[best_val]
        consensus = best_count / total
        is_stable = consensus >= _CONSENSUS_THRESHOLD
        if not is_stable:
            has_low_consensus = True
        resolved_exemplars[pos_key] = {
            "default": best_val,
            "distribution": dist,
            "consensus": round(consensus, 3),
            "stable": is_stable,
        }

    # Determine worker status based on confidence + consensus cap.
    intent_conf = (intent.get("confidence") or "low").lower()

    # Apply distribution cap: low-consensus step caps at "medium".
    if has_low_consensus and intent_conf == "high":
        effective_conf = "medium"
    else:
        effective_conf = intent_conf

    # Map to worker status: high -> beta, medium -> proposed.
    status_map = {"high": "beta", "medium": "proposed"}
    worker_status = status_map.get(effective_conf, "proposed")

    # Derive worker name from intent label.
    label = intent.get("intent_label", "unnamed_workflow")
    worker_name = _sanitize_name(label)

    # Insert formed_skills row.
    fs_data = {
        "workflow_intent_id": intent_id,
        "task_graph": json.dumps(task_graph),
        "exemplars": json.dumps(resolved_exemplars),
        "invocation_count": 0,
        "last_invoked_at": None,
        "created_at": now,
        "updated_at": now,
    }
    skill_id = insert_formed_skill(conn, fs_data)

    # Register worker in workers table with kind='formed_skill'.
    impl_ref = f"formed_skill:{skill_id}"
    wid = f"worker:{worker_name}:{uuid4().hex[:8]}"

    description = intent.get("intent_description", label) or label

    w = WorkerRow(
        id=wid,
        name=worker_name,
        kind="formed_skill",
        description=description[:500],
        capabilities="Workflow Replay",
        confidence=effective_conf,
        version="0.1.0",
        status=worker_status,
        schema_version="1.0",
        created_at=now,
        updated_at=now,
        availability="available",
        manifest_ref=impl_ref,
        worker_kind="formed_skill",
    )
    insert_worker(conn, w)

    # Record history.
    insert_worker_history(conn, [
        WorkerHistoryRow(
            registered_at=now,
            worker_id=wid,
            name=worker_name,
            kind="formed_skill",
            version="0.1.0",
            status=worker_status,
            capabilities="Workflow Replay",
            limitations="auto-formed from observed workflow; verify before use",
            event_type="skill_formation",
            note=f"Formed from workflow intent #{intent_id}: {label[:200]}",
        )
    ])
    insert_worker_version(conn, [
        WorkerVersionRow(
            worker_id=wid,
            version="0.1.0",
            registered_at=now,
            changelog=f"Initial skill formed from workflow intent #{intent_id}: {label[:200]}",
        )
    ])

    return {
        "skill_id": skill_id,
        "worker_id": wid,
        "worker_name": worker_name,
        "status": worker_status,
        "confidence": effective_conf,
        "step_count": len(task_graph),
    }


def _sanitize_name(label: str) -> str:
    """Derive a clean kebab-case name from an intent label."""
    import re
    name = label.lower().strip()
    name = re.sub(r"[^a-z0-9_ ]", "", name)
    parts = name.strip().split()[:4]
    return "_".join(parts) if parts else "formed_workflow"


def format_formed_skills(skills: list[dict]) -> str:
    """Render formed skills as human-readable report."""
    if not skills:
        return "No formed skills yet."
    lines = ["Formed Skills", "=" * 40, ""]
    for i, s in enumerate(skills, 1):
        lines.append(f"{i}. {s.get('worker_name', '?')}")
        lines.append(f"   Worker ID: {s.get('worker_id', '?')}")
        lines.append(f"   Status: {s.get('status', '?')}")
        lines.append(f"   Steps: {s.get('step_count', 0)}")
        context_parts = []
        if s.get("skill_id"):
            context_parts.append(f"skill_id={s['skill_id']}")
        if s.get("confidence"):
            context_parts.append(f"confidence={s['confidence']}")
        if context_parts:
            lines.append(f"   [{', '.join(context_parts)}]")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# ReplayExecutor — dispatches formed skills through existing action executors
# ---------------------------------------------------------------------------

import time as _time

from .runtime.models import ExecutionResult, Executor, VerificationResult
from .action_log import ActionEvent, log_action, now_iso as _now
from .db import connect as _db_connect


class ReplayExecutor(Executor):
    """Execute a formed skill by replaying each step through the appropriate
    action executor (HyprlandExecutor, BrowserExecutor).

    Each step is gated by the confirm gate and verified by the executor's own
    verify-by-diff logic. No new execution path.
    """

    def __init__(
        self,
        worker_id: str = "worker:replay",
        task_graph: list[list[str]] | None = None,
        exemplars: dict[str, dict] | None = None,
        workspace: str = ".",
    ) -> None:
        self.worker_id = worker_id
        self._task_graph = task_graph or []
        self._exemplars = exemplars or {}
        self._ws = workspace

    def execute(self, task) -> ExecutionResult:
        from .runtime.executors import resolve_executor

        t0 = _time.monotonic()
        steps = self.build_steps()
        if not steps:
            return ExecutionResult(
                success=False, stdout="", stderr="",
                exit_code=None, duration_ms=0,
                error="replay executor: no steps in task graph",
            )

        results: list[dict] = []
        all_succeeded = True

        for i, (action_type, target) in enumerate(steps):
            # Determine which executor to use.
            if action_type in ("workspace_switch", "window_focus", "app_launch",
                               "app_close"):
                worker_id = "worker:hyprctl"
                # Map abstract action types to hyprctl dispatcher action names.
                dispatch_action = {
                    "workspace_switch": "workspace",
                    "window_focus": "focuswindow",
                    "app_launch": "exec",
                    "app_close": "closewindow",
                }.get(action_type, action_type)
                payload = json.dumps({"action": dispatch_action, "target": target})
            elif action_type in ("navigate", "click", "type", "read",
                                 "screenshot", "title", "url", "wait"):
                worker_id = "worker:browser"
                payload = json.dumps({"action": action_type, "target": target})
            else:
                results.append({
                    "step": i, "action": action_type, "target": target,
                    "skipped": True,
                    "reason": f"Unknown action type: {action_type}",
                })
                continue

            sub_exec = resolve_executor(worker_id, workspace=self._ws)
            if sub_exec is None:
                results.append({
                    "step": i, "action": action_type, "target": target,
                    "skipped": True,
                    "reason": f"Executor not available: {worker_id}",
                })
                continue

            sub_task = _MiniTask(payload=payload)
            step_result = sub_exec.execute(sub_task)
            results.append({
                "step": i, "action": action_type, "target": target,
                "success": step_result.success,
                "error": step_result.error,
            })
            if not step_result.success:
                all_succeeded = False

        dur = int((_time.monotonic() - t0) * 1000)

        # Increment invocation count.
        try:
            conn = _db_connect()
            ref = getattr(task, "manifest_ref", None) or getattr(task, "runtime_hint", "")
            if ref and "formed_skill:" in ref:
                skill_id = int(ref.split("formed_skill:")[1].split(":")[0])
                from .db import increment_formed_skill_invocation
                increment_formed_skill_invocation(conn, skill_id)
        except Exception:
            pass

        # Log the replay action.
        try:
            conn = _db_connect()
            log_action(conn, ActionEvent(
                source="friday",
                action_type="skill_replay",
                target=json.dumps({"step_count": len(steps), "succeeded": all_succeeded}),
                detail=json.dumps({"results": results}),
                confidence="observed",
                observed_at=_now(),
            ))
        except Exception:
            pass

        return ExecutionResult(
            success=all_succeeded,
            stdout=json.dumps({"steps": len(steps), "results": results}),
            stderr="",
            exit_code=0 if all_succeeded else 1,
            duration_ms=dur,
            error="" if all_succeeded else "One or more steps failed",
        )

    def build_steps(self) -> list[tuple[str, str]]:
        """Resolve exemplar values for each step in the task graph."""
        steps: list[tuple[str, str]] = []
        for i, (action_type, norm_target) in enumerate(self._task_graph):
            pos_key = str(i)
            pos_data = self._exemplars.get(pos_key, {})
            default_val = pos_data.get("default", "") if isinstance(pos_data, dict) else ""
            target = default_val if default_val else ""
            steps.append((action_type, target))
        return steps

    def verify(self, task, result: ExecutionResult) -> VerificationResult:
        return VerificationResult(
            passed=result.success,
            reason="replay executor completed" if result.success
            else result.error or "replay executor failed",
        )


class _MiniTask:
    """Minimal task-like object for sub-executor dispatch."""

    def __init__(self, payload: str = "", hint: str = "", ref: str = ""):
        self.runtime_payload = payload
        self.runtime_hint = hint
        self.manifest_ref = ref
