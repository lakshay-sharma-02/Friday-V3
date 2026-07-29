"""Pillar B Stage 4 — Skill Formation.

Takes labeled workflow intents (Stage 3) and forms them into deployable,
replayable skills registered in the shared workers registry.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional
from uuid import uuid4

from .db import (
    delete_formed_skill,
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


def form_skills(conn, force: bool = False) -> list[dict]:
    """Run the skill formation pipeline.

    For each high/medium confidence workflow intent that doesn't already
    have a formed skill, creates the skill record and registers a worker.
    When ``force=True``, existing formed skills are deleted and re-created.

    Args:
        conn: Open SQLite connection.
        force: If True, delete and re-form intents that already have skills.

    Returns:
        List of formed skill dicts that were created (empty if none).
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

        # Check if already formed.
        existing = get_formed_skill_by_intent(conn, intent_id)
        if existing:
            if force:
                # Delete existing worker + formed_skill before re-forming.
                ref = f"formed_skill:{existing['id']}"
                w = conn.execute(
                    "SELECT id FROM workers WHERE manifest_ref = ?", (ref,)
                ).fetchone()
                if w is not None:
                    conn.execute("DELETE FROM workers WHERE id = ?", (w["id"],))
                delete_formed_skill(conn, existing["id"])
            else:
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
        on_failure: str = "abort",
    ) -> None:
        self.worker_id = worker_id
        self._task_graph = task_graph or []
        self._exemplars = exemplars or {}
        self._ws = workspace
        self._on_failure = on_failure
        # Auto-downgrade is True by default (when executor is created by
        # resolve_executor without explicit CLI flags). Set to False when
        # the user explicitly passes --on-failure.
        self._auto_downgrade = True

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

        # Extract skill_id from the task for log queries.
        ref = getattr(task, "manifest_ref", None) or getattr(task, "runtime_hint", "")
        skill_id = None
        if ref and "formed_skill:" in ref:
            try:
                skill_id = int(ref.split("formed_skill:")[1].split(":")[0])
            except (ValueError, IndexError):
                pass

        # Resolve per-step strategies: auto-downgrade only when the user
        # hasn't explicitly passed --on-failure.
        step_strategies = {}
        if self._auto_downgrade and skill_id is not None:
            step_strategies = self._resolve_strategies(skill_id)

        results: list[dict] = []
        all_succeeded = True

        for i, (action_type, target) in enumerate(steps):
            strategy = step_strategies.get(str(i), self._on_failure)

            # Determine which executor to use.
            dispatch_action = None
            if action_type in ("workspace_switch", "window_focus", "app_launch",
                               "app_close"):
                worker_id = "worker:hyprctl"
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

            # Build step detail: track which concrete value was used per step
            # for drift-detection seed data.
            pos_key = str(i)
            pos_data = self._exemplars.get(pos_key, {})
            default_val = pos_data.get("default", "") if isinstance(pos_data, dict) else ""
            exemplar_source = "default"

            sub_task = _MiniTask(payload=payload)
            step_result = sub_exec.execute(sub_task)

            if not step_result.success:
                if strategy == "abort":
                    results.append({
                        "step": i, "action": action_type, "target": target,
                        "target_used": target,
                        "exemplar_source": exemplar_source,
                        "success": False,
                        "error": step_result.error or "execution failed",
                        "aborted_remaining": len(steps) - i - 1,
                    })
                    all_succeeded = False
                    break

                elif strategy == "skip":
                    results.append({
                        "step": i, "action": action_type, "target": target,
                        "target_used": target,
                        "exemplar_source": exemplar_source,
                        "success": False,
                        "skipped": True,
                        "error": step_result.error or "execution failed, skipped",
                    })
                    # Move to next step — the failure IS recorded so the
                    # action log has it for drift detection.
                    continue

                elif strategy == "retry_alt":
                    # Try the next most common exemplar value from the distribution.
                    alt_target = self._next_alt(i, target)
                    if alt_target and alt_target != target:
                        alt_payload = self._build_retry_payload(
                            action_type, dispatch_action, alt_target)
                        # dispatch_action is the dispatcher action string
                        # (workspace/focuswindow/exec/closewindow) for
                        # Hyprland actions; None for browser actions.
                        sub_task2 = _MiniTask(payload=alt_payload)
                        retry_result = sub_exec.execute(sub_task2)
                        if retry_result.success:
                            results.append({
                                "step": i, "action": action_type,
                                "target": alt_target,
                                "target_used": target,
                                "exemplar_source": "retry_alt",
                                "success": True,
                            })
                            continue

                    # Both primary and alt failed — abort.
                    results.append({
                        "step": i, "action": action_type, "target": target,
                        "target_used": target,
                        "exemplar_source": exemplar_source,
                        "success": False,
                        "error": (retry_result.error if alt_target else
                                   step_result.error or "execution failed"),
                        "aborted_remaining": len(steps) - i - 1,
                    })
                    all_succeeded = False
                    break

            else:
                results.append({
                    "step": i, "action": action_type, "target": target,
                    "target_used": target,
                    "exemplar_source": exemplar_source,
                    "success": True,
                })

        dur = int((_time.monotonic() - t0) * 1000)

        # Increment invocation count.
        try:
            conn = _db_connect()
            if skill_id is not None:
                from .db import increment_formed_skill_invocation
                increment_formed_skill_invocation(conn, skill_id)
        except Exception:
            pass

        # Log the replay action with per-step concrete values.
        try:
            conn = _db_connect()
            log_action(conn, ActionEvent(
                source="friday",
                action_type="skill_replay",
                target=json.dumps({
                    "step_count": len(steps),
                    "succeeded": all_succeeded,
                    "on_failure": self._on_failure,
                    "skill_id": skill_id,
                    "worker_id": self.worker_id,
                }),
                detail=json.dumps({
                    "results": results,
                    "strategy": self._on_failure,
                }),
                confidence="observed",
                observed_at=_now(),
            ))
        except Exception:
            pass

        # Summarize which steps failed/succeeded for the ExecutionResult.
        failed_steps = [r for r in results if not r.get("success")]
        final_success = all_succeeded
        final_error = ""
        if not final_success:
            failed_summaries = []
            for fs in failed_steps[:3]:
                step_i = fs.get("step", "?")
                err = fs.get("error", "unknown")
                failed_summaries.append(f"step {step_i}: {err}")
            remaining = len(failed_steps) - 3
            if remaining > 0:
                failed_summaries.append(f"... and {remaining} more")
            final_error = "; ".join(failed_summaries)

        return ExecutionResult(
            success=final_success,
            stdout=json.dumps({"steps": len(steps), "results": results}),
            stderr="",
            exit_code=0 if final_success else 1,
            duration_ms=dur,
            error=final_error,
        )

    def _resolve_strategies(self, skill_id: int) -> dict[str, str]:
        """Compute per-step failure strategies from the action log.

        Reads the last 10 ``skill_replay`` entries for this skill. If a step
        has failed in 3+ of those invocations, auto-downgrade that step's
        strategy from ``abort`` to ``skip`` for this invocation.

        Only applies when ``self._on_failure == "abort"`` (the default).
        When the user explicitly passes ``--on-failure``, no downgrade occurs.

        .. note::

           The LIKE query on ``target`` assumes ``skill_id`` appears as a
           JSON key in the target blob. Suffix collisions are unlikely with
           a single-user SQLite database.
        """
        if self._on_failure != "abort":
            return {}

        try:
            conn = _db_connect()
            try:
                # LIKE on target column; skill_id is stored in target JSON
                # by ReplayExecutor.execute().
                rows = conn.execute(
                    """SELECT target, detail FROM actions
                       WHERE action_type = 'skill_replay'
                       AND target LIKE '%skill_id%'
                       ORDER BY observed_at DESC LIMIT 10""",
                ).fetchall()
            finally:
                conn.close()

            # Filter to rows whose target actually references this skill_id.
            # Note: json.dumps produces '"skill_id": 1' (space after colon).
            target_marker = f'"skill_id": {skill_id}'
            matching = []
            for r in rows:
                t = r["target"] if isinstance(r["target"], str) else json.dumps(r["target"])
                if target_marker in t:
                    matching.append(r)

            if len(matching) < 3:
                return {}

            # Count failures per step across the recent invocations.
            from collections import Counter
            step_failures: Counter = Counter()
            # Use string keys (same type as step_failures keys) throughout.
            step_seen: set[str] = set()

            for r in matching:
                try:
                    detail = json.loads(r["detail"]) if isinstance(r["detail"], str) else dict(r["detail"])
                except (json.JSONDecodeError, TypeError):
                    continue
                step_results = detail.get("results", []) if isinstance(detail, dict) else []
                for sr in step_results:
                    step_i = sr.get("step")
                    if not sr.get("success") and step_i is not None:
                        sk = str(step_i)
                        step_failures[sk] += 1
                        step_seen.add(sk)

            strategies: dict[str, str] = {}
            for step_key in step_seen:
                if step_failures.get(step_key, 0) >= 3:
                    strategies[step_key] = "skip"
            return strategies
        except Exception:
            return {}

    def _next_alt(self, step_idx: int, current_target: str) -> str | None:
        """Return the next most common exemplar value for a step,
        excluding the value that just failed."""
        pos_key = str(step_idx)
        pos_data = self._exemplars.get(pos_key, {})
        if not isinstance(pos_data, dict):
            return None
        dist = pos_data.get("distribution", {})
        if not isinstance(dist, dict) or len(dist) < 2:
            return None
        # Sort by count descending, pick the first that isn't the failed one.
        sorted_vals = sorted(dist.items(), key=lambda x: -x[1])
        for val, _count in sorted_vals:
            if val != current_target:
                return val
        return None

    def _build_retry_payload(self, action_type, action_name, target) -> str:
        """Build a new payload for retry with an alternative target.

        ``action_name`` is the dispatcher action string (e.g. "workspace")
        for Hyprland actions, or None for browser actions (which use action_type).
        """
        if action_name:
            return json.dumps({"action": action_name, "target": target})
        # Browser actions use action_type directly.
        return json.dumps({"action": action_type, "target": target})

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


# ---------------------------------------------------------------------------
# ShadowExecutor — simulate formed skill execution with zero side effects
# ---------------------------------------------------------------------------


_SHADOW_PROMOTION_THRESHOLD = 3  # consecutive clean shadow runs -> promote

# Number of successful beta executions before promoting to stable.
# Beta skills execute for real (canary mode). After N consecutive successful
# executions, they promote to stable (full execution, monitored via drift).
_CANARY_PROMOTION_THRESHOLD = 5


class ShadowExecutor:
    """Simulate a formed skill's execution without producing side effects.

    Unlike ReplayExecutor which actually dispatches actions through real
    executors (Hyprland, Browser, etc.), ShadowExecutor:
      1. Reads the skill's task_graph and exemplars
      2. For each step, checks if the exemplar target is reasonable
         (exists in workspace, matches expected patterns)
      3. Compares actual workspace state against exemplar expectations
      4. Logs all comparisons to shadow_runs table
      5. Never executes anything with side effects

    After N consecutive clean shadow runs (N = SHADOW_PROMOTION_THRESHOLD),
    the skill auto-promotes from proposed -> beta status.
    """

    def __init__(
        self,
        conn,
        skill_id: int,
        worker_id: str,
        task_graph: list[list[str]] | None = None,
        exemplars: dict[str, dict] | None = None,
        workspace: str = ".",
    ) -> None:
        self.conn = conn
        self.skill_id = skill_id
        self.worker_id = worker_id
        self._task_graph = task_graph or []
        self._exemplars = exemplars or {}
        self._ws = workspace

    def run(self) -> dict:
        """Execute the shadow run. Returns a result dict with match details.

        Returns:
            Dict with keys:
              - skill_id
              - step_count
              - steps_matched
              - steps_mismatched
              - steps_skipped
              - exemplar_comparison (list of per-step dicts)
              - overall_match_score (0.0 to 1.0)
              - outcome ('matched', 'mismatched', 'error')
              - promoted (bool)
        """
        from datetime import datetime, timezone
        import json

        # Rate-limit: skip if a shadow run was recorded within the last
        # _AUTO_DISPATCH_INTERVAL seconds (avoid flooding on rapid cycles).
        try:
            last_run = self.conn.execute(
                "SELECT run_at FROM shadow_runs WHERE skill_id = ? "
                "ORDER BY id DESC LIMIT 1",
                (self.skill_id,)
            ).fetchone()
            if last_run:
                from datetime import datetime as _dt
                last_dt = _dt.fromisoformat(last_run["run_at"])
                if (datetime.now(timezone.utc) - last_dt).total_seconds() < 3600:
                    return {
                        "skill_id": self.skill_id,
                        "step_count": 0,
                        "steps_matched": 0,
                        "steps_mismatched": 0,
                        "steps_skipped": 0,
                        "exemplar_comparison": [],
                        "overall_match_score": 0.0,
                        "outcome": "skipped",
                        "promoted": False,
                        "skipped_reason": "rate_limited",
                    }
        except Exception:
            pass

        steps = self._task_graph
        if not steps:
            return {
                "skill_id": self.skill_id,
                "step_count": 0,
                "steps_matched": 0,
                "steps_mismatched": 0,
                "steps_skipped": 0,
                "exemplar_comparison": [],
                "overall_match_score": 0.0,
                "outcome": "error",
                "promoted": False,
            }

        # Per-step comparison: check exemplar targets against workspace state.
        comparison: list[dict] = []
        matched = 0
        mismatched = 0
        skipped = 0

        for i, (action_type, target) in enumerate(steps):
            step_key = str(i)
            pos_data = self._exemplars.get(step_key, {})
            if isinstance(pos_data, dict) and pos_data:
                default_val = pos_data.get("default", "") or ""
                distribution = pos_data.get("distribution", {})
                consensus = pos_data.get("consensus", 1.0)
                stable = pos_data.get("stable", True)

                # Determine if the exemplar target is still valid in the
                # current workspace. We check: would the action succeed?
                # For file/workspace actions, check if target path/workspace
                # exists. For browser actions, just verify exemplar has
                # reasonable format.
                target_valid = self._check_target_valid(action_type, target)

                comparison.append({
                    "step": i,
                    "action": action_type,
                    "exemplar_target": default_val or target,
                    "exemplar_consensus": consensus,
                    "exemplar_stable": stable,
                    "target_valid": target_valid,
                    "step_matched": target_valid,
                })
                if target_valid:
                    matched += 1
                else:
                    mismatched += 1
            else:
                # No exemplar data for this step — check if workspace is
                # in a reasonable state for this action type.
                workspace_ok = self._check_workspace_reasonable(action_type)
                comparison.append({
                    "step": i,
                    "action": action_type,
                    "exemplar_target": target,
                    "exemplar_consensus": 0.0,
                    "exemplar_stable": False,
                    "target_valid": workspace_ok,
                    "step_matched": workspace_ok,
                })
                if workspace_ok:
                    matched += 1
                else:
                    skipped += 1

        total = len(steps)
        score = matched / max(total, 1)
        outcome = "matched" if score >= 0.8 else "mismatched"

        result = {
            "skill_id": self.skill_id,
            "step_count": total,
            "steps_matched": matched,
            "steps_mismatched": mismatched,
            "steps_skipped": skipped,
            "exemplar_comparison": comparison,
            "overall_match_score": round(score, 3),
            "outcome": outcome,
            "promoted": False,
        }

        # Persist shadow run to DB.
        self._persist(result)

        # Check promotion: if score meets threshold and we have enough
        # consecutive clean runs, promote to beta.
        if score >= 0.8:
            self._maybe_promote(result)

        return result

    def _check_target_valid(self, action_type: str, target: str) -> bool:
        """Check if an exemplar target is reasonable for the action type.

        For workspace operations: check if the workspace id exists.
        For app launch: check if the binary exists on PATH.
        For browser operations: validate URL or element format.
        """
        from pathlib import Path

        if not target:
            return True  # No target = generic action, always valid

        # Workspace switch: verify the target workspace id exists.
        if action_type == "workspace_switch":
            try:
                from .hyprctl_util import hyprctl
                import json as _json
                raw = hyprctl("workspaces")
                if raw:
                    try:
                        ws_list = _json.loads(raw) if isinstance(raw, str) else raw
                        if isinstance(ws_list, list):
                            ws_ids = {str(w.get("id", "")) for w in ws_list}
                            ws_names = {w.get("name", "") for w in ws_list}
                            return target in ws_ids or target in ws_names
                    except (_json.JSONDecodeError, TypeError, AttributeError):
                        pass
                return False
            except Exception:
                # Can't check Hyprland — trust the exemplar.
                return True

        # Window focus: can't easily verify without listing windows.
        if action_type == "window_focus":
            return True

        # App launch: check if the binary exists on PATH.
        if action_type == "app_launch":
            import shutil
            return shutil.which(target) is not None or Path(target).exists()

        # Browser navigation actions: validate URL format.
        if action_type == "navigate":
            return target.startswith("http://") or target.startswith("https://") or target.startswith("file://")

        # Browser actions that reference a page element: assume valid.
        if action_type in ("click", "type", "wait", "screenshot"):
            return True

        # App close: check if target looks like a window address.
        if action_type == "app_close":
            return bool(target and len(target) > 0)

        # All other actions (read, title, url): trust the exemplar.
        return True

    def _check_workspace_reasonable(self, action_type: str) -> bool:
        """Check if the workspace is in a reasonable state for an action
        that has no exemplar data."""
        from pathlib import Path

        # For any action, check that the workspace directory is valid.
        ws_path = Path(self._ws)
        if not ws_path.is_dir():
            return False

        # Workspace switch needs Hyprland.
        if action_type == "workspace_switch":
            try:
                from .hyprctl_util import hyprctl
                hyprctl("monitors")
                return True
            except Exception:
                return False

        # Window focus needs Hyprland.
        if action_type == "window_focus":
            try:
                from .hyprctl_util import hyprctl
                hyprctl("clients")
                return True
            except Exception:
                return False

        # App launch just needs a working shell.
        if action_type == "app_launch":
            return True

        # Browser operations: always reasonable to try.
        if action_type in ("navigate", "click", "type", "screenshot", "title", "url", "wait"):
            return True

        # App close: possible if any windows exist.
        if action_type == "app_close":
            return True

        return True

    def _persist(self, result: dict) -> None:
        """Persist shadow run to the shadow_runs table."""
        from datetime import datetime, timezone
        import json

        from .db import insert_shadow_run

        try:
            row = {
                "skill_id": self.skill_id,
                "run_at": datetime.now(timezone.utc).isoformat(),
                "step_count": result["step_count"],
                "steps_matched": result["steps_matched"],
                "steps_mismatched": result["steps_mismatched"],
                "exemplar_comparison": json.dumps(result["exemplar_comparison"]),
                "overall_match_score": result["overall_match_score"],
                "outcome": result["outcome"],
                "promoted": 1 if result["promoted"] else 0,
            }
            insert_shadow_run(self.conn, row)
        except Exception:
            pass  # Shadow run logging is best-effort

    def _maybe_promote(self, result: dict) -> None:
        """Promote skill from proposed -> beta if enough clean shadow runs."""
        from datetime import datetime, timezone
        from .db import count_recent_shadow_runs

        try:
            clean_count = count_recent_shadow_runs(
                self.conn, self.skill_id, _SHADOW_PROMOTION_THRESHOLD)

            if clean_count >= _SHADOW_PROMOTION_THRESHOLD:
                # Promote: update worker status from proposed -> beta.
                self.conn.execute(
                    """UPDATE workers SET status = 'beta', updated_at = ?
                       WHERE manifest_ref = ? AND status = 'proposed'""",
                    (datetime.now(timezone.utc).isoformat(),
                     f"formed_skill:{self.skill_id}")
                )
                self.conn.commit()

                # Mark this run as promoted.
                result["promoted"] = True

                _log_dispatch(
                    self.skill_id,
                    f"auto-promoted to beta after {clean_count} "
                    f"clean shadow runs (score={result['overall_match_score']})")

                # Push ambient event for the promotion.
                self._push_promotion_event()

        except Exception as exc:
            _log_dispatch(self.skill_id, f"promotion check failed: {exc}")

    def _push_promotion_event(self) -> None:
        """Push an ambient feed event for the shadow-run promotion."""
        try:
            from .ambient import push_event, AmbientEvent
            from datetime import datetime, timezone

            # Get skill name for the event title.
            worker_row = self.conn.execute(
                "SELECT name FROM workers WHERE manifest_ref = ?",
                (f"formed_skill:{self.skill_id}",)
            ).fetchone()
            skill_name = worker_row["name"] if worker_row else f"skill #{self.skill_id}"

            ev = AmbientEvent(
                source="friday",
                event_type="skill_promotion",
                title=f"Skill '{skill_name}' promoted to beta",
                detail=(
                    f"Skill '{skill_name}' auto-promoted from proposed to beta "
                    f"after {_SHADOW_PROMOTION_THRESHOLD} clean shadow runs."
                ),
                priority=2,
                category="execution",
                project="",
                confidence=1.0,
                payload=f'{{"skill_id": {self.skill_id}, '
                        f'"worker": "{skill_name}", '
                        f'"new_status": "beta"}}',
                salience=0.0,
            )
            push_event(self.conn, ev)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Shadow event helpers — push ambient events for shadow run outcomes
# ---------------------------------------------------------------------------


def _push_shadow_event(conn, skill_id: int, worker_name: str, result: dict) -> None:
    """Push an ambient feed event for a shadow run (not just promotions)."""
    try:
        from .ambient import push_event, AmbientEvent

        outcome = result.get("outcome", "?")
        score = result.get("overall_match_score", 0.0)
        matched = result.get("steps_matched", 0)
        total = result.get("step_count", 0)

        if outcome == "skipped":
            return  # No event for rate-limited skips

        ev = AmbientEvent(
            source="friday",
            event_type="shadow_run",
            title=f"Shadow run: {worker_name}",
            detail=(
                f"Skill '{worker_name}' shadow run completed: "
                f"{matched}/{total} steps matched (score={score:.0%}, "
                f"outcome={outcome})"
            ),
            priority=1,
            category="intelligence",
            project="",
            confidence=0.7,
            payload=f'{{"skill_id": {skill_id}, '
                    f'"worker": "{worker_name}", '
                    f'"score": {score}, '
                    f'"matched": {matched}, '
                    f'"total": {total}}}',
            salience=0.0,
        )
        push_event(conn, ev)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Auto-dispatch — trigger formed skills when their pattern is observed
# ---------------------------------------------------------------------------


_AUTO_DISPATCH_INTERVAL = 3600  # seconds between auto-dispatches per skill


def auto_dispatch_skills(conn) -> list[dict]:
    """Check freshly mined patterns against formed skills and auto-dispatch
    any skill whose task_graph matches an observed pattern.

    Runs in the daemon cycle after Pillar B Stage 4 (skill formation).
    Dispatches are rate-limited per skill (``_AUTO_DISPATCH_INTERVAL``).
    A failure in dispatch never crashes the caller.

    Returns a list of dispatch result dicts (empty if none triggered).
    """
    dispatched: list[dict] = []

    try:
        # 1. Read formed skills with their task graphs.
        skills = conn.execute(
            """SELECT fs.id, fs.task_graph, fs.exemplars,
                      fs.invocation_count, fs.last_invoked_at,
                      w.id AS worker_id, w.name AS worker_name,
                      w.status, w.manifest_ref
               FROM formed_skills fs
               JOIN workers w
                 ON w.manifest_ref = 'formed_skill:' || CAST(fs.id AS TEXT)
               WHERE w.kind = 'formed_skill'
                 AND w.status IN ('beta', 'proposed', 'stable')"""
        ).fetchall()

        if not skills:
            return dispatched

        # 2. Read mined patterns from this cycle.
        patterns = conn.execute(
            """SELECT id, sequence_json, exemplars
               FROM mined_patterns ORDER BY count DESC"""
        ).fetchall()

        if not patterns:
            return dispatched

        # 3. For each skill, check if its task_graph matches a pattern.
        from datetime import datetime, timezone
        now_utc = datetime.now(timezone.utc)

        for skill in skills:
            try:
                skill_id = skill["id"]
                worker_id = skill["worker_id"]
                status = skill["status"]
                last_invoked = skill["last_invoked_at"]

                # Stable skills skip rate-limiting — they run whenever triggered.
                # Beta and proposed skills respect _AUTO_DISPATCH_INTERVAL.
                if status != "stable" and last_invoked:
                    try:
                        last_dt = datetime.fromisoformat(last_invoked)
                        elapsed = (now_utc - last_dt).total_seconds()
                        if elapsed < _AUTO_DISPATCH_INTERVAL:
                            continue
                    except (ValueError, TypeError):
                        pass

                task_graph_str = skill["task_graph"] or "[]"
                skill_graph = json.loads(task_graph_str) if isinstance(task_graph_str, str) else task_graph_str
                if not skill_graph:
                    continue

                # Normalize skill task_graph to action-type sequence for matching.
                skill_actions = [step[0] for step in skill_graph if len(step) >= 1]

                # Check each pattern for a match.
                matched_pattern = None
                for p in patterns:
                    seq_str = p["sequence_json"] or "[]"
                    seq = json.loads(seq_str) if isinstance(seq_str, str) else seq_str
                    pattern_actions = [step[0] for step in seq if len(step) >= 1]
                    if pattern_actions == skill_actions:
                        matched_pattern = p
                        break

                if matched_pattern is None:
                    continue

                # 4. Dispatch the skill.
                # Proposed skills run in shadow mode (simulation, no side effects).
                # Beta skills execute for real (via ReplayExecutor).
                if status == "proposed":
                    result = _shadow_dispatch_one(conn, skill, worker_id)
                else:
                    result = _dispatch_one(conn, skill, worker_id, now_utc)
                if result:
                    dispatched.append(result)

            except Exception as exc:
                _log_dispatch(skill.get("id", "?") if isinstance(skill, dict) else "?",
                              f"dispatch check failed: {exc}")
                continue

    except Exception as exc:
        _log_dispatch("all", f"auto_dispatch_skills failed: {exc}")

    return dispatched


def _shadow_dispatch_one(conn, skill_row, worker_id: str) -> Optional[dict]:
    """Run a formed skill through the ShadowExecutor.

    Proposed skills are simulated instead of executed for real. The shadow
    run compares exemplar targets against current workspace state and logs
    the comparison. No side effects. After N clean shadow runs, the skill
    auto-promotes to beta.

    Returns a dict with shadow dispatch metadata, or None if shadow failed.
    """
    try:
        skill_id = skill_row["id"]
        task_graph_str = skill_row["task_graph"] or "[]"
        skill_graph = json.loads(task_graph_str) if isinstance(task_graph_str, str) else task_graph_str
        exemplars_str = skill_row["exemplars"] or "{}"
        exemplars = json.loads(exemplars_str) if isinstance(exemplars_str, str) else exemplars_str

        worker_name = skill_row["worker_name"] if "worker_name" in skill_row.keys() else ""

        # Build and run the ShadowExecutor.
        shadow = ShadowExecutor(
            conn=conn,
            skill_id=skill_id,
            worker_id=worker_id,
            task_graph=skill_graph,
            exemplars=exemplars,
        )
        result = shadow.run()

        # Log the shadow run to action log.
        from .action_log import ActionEvent, log_action, now_iso as _n
        log_action(conn, ActionEvent(
            source="friday",
            action_type="skill_shadow_run",
            target=json.dumps({
                "skill_id": skill_id,
                "worker_id": worker_id,
                "worker_name": worker_name,
                "outcome": result["outcome"],
                "match_score": result["overall_match_score"],
            }),
            detail=json.dumps({
                "steps": result["step_count"],
                "steps_graph": skill_graph,
                "matched": result["steps_matched"],
                "mismatched": result["steps_mismatched"],
                "promoted": result["promoted"],
            }),
            confidence="derived",
            observed_at=_n(),
        ))

        # Push ambient event for the shadow run (low priority, intelligence category).
        _push_shadow_event(conn, skill_id, worker_name, result)

        dispatch_info = {
            "skill_id": skill_id,
            "worker_id": worker_id,
            "worker_name": worker_name,
            "succeeded": result["outcome"] == "matched",
            "error": "",
            "shadow": True,
            "outcome": result["outcome"],
            "match_score": result["overall_match_score"],
            "promoted": result["promoted"],
        }

        status_str = "promoted" if result["promoted"] else result["outcome"]
        _log_dispatch(skill_id, f"shadow {status_str} for {worker_name}")
        return dispatch_info

    except Exception as exc:
        _log_dispatch(
            skill_row["id"] if "id" in skill_row.keys() else "?",
            f"shadow dispatch failed: {exc}")
        return None


def _dispatch_one(conn, skill_row, worker_id: str, now_utc) -> Optional[dict]:
    """Dispatch one formed skill via resolve_executor + ReplayExecutor.

    Returns a dict with dispatch metadata, or None if dispatch was skipped.
    """
    try:
        skill_id = skill_row["id"]
        task_graph_str = skill_row["task_graph"] or "[]"
        skill_graph = json.loads(task_graph_str) if isinstance(task_graph_str, str) else task_graph_str
        exemplars_str = skill_row["exemplars"] or "{}"
        exemplars = json.loads(exemplars_str) if isinstance(exemplars_str, str) else exemplars_str

        # Resolve the executor for this formed skill.
        from .runtime.executors import resolve_executor
        executor = resolve_executor(worker_id)
        if executor is None:
            _log_dispatch(skill_id, f"no executor for {worker_id}")
            return None

        # Build a minimal task for the ReplayExecutor.
        task = _MiniTask(
            payload="",
            hint="",
            ref=f"formed_skill:{skill_id}:auto",
        )

        result = executor.execute(task)

        # sqlite3.Row does not support .get(), so access via bracket with
        # key-existence guard.
        worker_name = skill_row["worker_name"] if "worker_name" in skill_row.keys() else ""

        # Record the auto-dispatch in the action log.
        from .action_log import (
            ActionEvent, log_action, now_iso as _n)

        log_action(conn, ActionEvent(
            source="friday",
            action_type="skill_auto_dispatch",
            target=json.dumps({
                "skill_id": skill_id,
                "worker_id": worker_id,
                "succeeded": result.success,
                "worker_name": worker_name,
            }),
            detail=json.dumps({
                "error": result.error or "",
                "steps": skill_graph,
            }),
            confidence="observed",
            observed_at=_n(),
        ))

        dispatch_info = {
            "skill_id": skill_id,
            "worker_id": worker_id,
            "worker_name": worker_name,
            "succeeded": result.success,
            "error": result.error or "",
        }

        _log_dispatch(skill_id,
                      f"{'succeeded' if result.success else 'failed'}"
                      f" for {worker_name}")

        # Canary promotion: if this beta skill succeeded, check if it's
        # ready to promote to stable.
        if result.success:
            skill_status = skill_row["status"] if "status" in skill_row.keys() else ""
            if skill_status == "beta":
                _maybe_promote_canary(conn, skill_id, worker_name)

        return dispatch_info

    except Exception as exc:
        _log_dispatch(
            skill_id if "skill_id" in dir() else "?",
            f"dispatch execution failed: {exc}")
        return None


def _log_dispatch(skill_id, message: str) -> None:
    """Log an auto-dispatch message. Best-effort; failures are silent.

    Uses print() so the message reaches the daemon's logfile (stderr is
    captured in daemon mode). The caller (daemon.py) also logs summary
    counts via its own _log() — no risk of circular imports.
    """
    try:
        import sys
        print(f"[auto-dispatch skill #{skill_id}] {message}", file=sys.stderr)
    except Exception:
        pass


def _maybe_promote_canary(conn, skill_id: int, worker_name: str) -> bool:
    """Promote a beta skill to stable after N successful executions.

    Checks:
      1. invocation_count >= _CANARY_PROMOTION_THRESHOLD
      2. Recent 5 skill_auto_dispatch entries have >= 80% success rate

    If both conditions are met, updates the worker status from beta to stable
    and pushes an ambient feed event.

    Returns True if promotion happened, False otherwise.
    """
    from datetime import datetime, timezone

    try:
        # 1. Check invocation count.
        fs = conn.execute(
            "SELECT invocation_count FROM formed_skills WHERE id = ?",
            (skill_id,)
        ).fetchone()
        if not fs or (fs["invocation_count"] or 0) < _CANARY_PROMOTION_THRESHOLD:
            return False

        # 2. Check recent success rate from auto-dispatch log.
        recent = conn.execute(
            """SELECT target FROM actions
               WHERE action_type = 'skill_auto_dispatch'
               AND target LIKE '%skill_id%'
               ORDER BY observed_at DESC LIMIT 5""",
        ).fetchall()

        target_marker = f'"skill_id": {skill_id}'
        matching = []
        for r in recent:
            t = r["target"] if isinstance(r["target"], str) else json.dumps(r["target"])
            if target_marker in t:
                matching.append(r)

        if len(matching) < _CANARY_PROMOTION_THRESHOLD:
            return False

        successes = 0
        for r in matching:
            try:
                td = json.loads(r["target"]) if isinstance(r["target"], str) else dict(r["target"])
                if td.get("succeeded", False):
                    successes += 1
            except (json.JSONDecodeError, TypeError, AttributeError):
                continue

        success_rate = successes / max(len(matching), 1)
        if success_rate < 0.8:
            return False

        # 3. Promote beta -> stable.
        conn.execute(
            """UPDATE workers SET status = 'stable', updated_at = ?
               WHERE manifest_ref = ? AND status = 'beta'""",
            (datetime.now(timezone.utc).isoformat(),
             f"formed_skill:{skill_id}")
        )
        conn.commit()

        _log_dispatch(
            skill_id,
            f"promoted from beta to stable after "
            f"{fs['invocation_count']} invocations "
            f"({successes}/{len(matching)} recent successes)")

        # Push ambient event.
        try:
            from .ambient import push_event, AmbientEvent
            ev = AmbientEvent(
                source="friday",
                event_type="skill_promotion",
                title=f"Skill '{worker_name}' promoted to stable",
                detail=(
                    f"Skill '{worker_name}' auto-promoted from beta to stable "
                    f"after {fs['invocation_count']} successful executions "
                    f"({success_rate:.0%} recent success rate)."
                ),
                priority=2,
                category="execution",
                project="",
                confidence=1.0,
                payload=f'{{"skill_id": {skill_id}, '
                        f'"worker": "{worker_name}", '
                        f'"new_status": "stable"}}',
                salience=0.0,
            )
            push_event(conn, ev)
        except Exception:
            pass

        return True

    except Exception as exc:
        _log_dispatch(skill_id, f"canary promotion check failed: {exc}")
        return False


class _MiniTask:
    """Minimal task-like object for sub-executor dispatch."""

    def __init__(self, payload: str = "", hint: str = "", ref: str = ""):
        self.runtime_payload = payload
        self.runtime_hint = hint
        self.manifest_ref = ref


# ---------------------------------------------------------------------------
# Drift Detection — detect degrading skills by comparing recent invocations
# against formation-time exemplars
# ---------------------------------------------------------------------------


@dataclass
class DriftReport:
    """Drift assessment for one formed skill."""

    skill_id: int
    worker_name: str
    worker_id: str
    overall_health: str  # "healthy", "degrading", "unhealthy"
    invocation_count: int
    recent_invocations: int  # how many invocations were analyzed
    overall_success_rate: float  # 0.0 to 1.0
    step_breakdown: list[dict]  # per-step: step_idx, action, success_rate, failure_count, exemplar_stable
    recommendation: str  # what to do about this skill


def detect_skill_drift(conn) -> list[DriftReport]:
    """Analyze all formed skills for signs of degradation.

    For each formed skill with at least 3 invocations, queries the last 10
    ``skill_replay`` action log entries. Computes per-step success rates
    and compares against formation-time exemplar stability.

    Health levels:
      - ``healthy``: overall success rate ≥ 80% in recent invocations
      - ``degrading``: overall success rate 50-80%, or any step failing in
        more than 2 recent invocations
      - ``unhealthy``: overall success rate < 50%, or any step failing in
        more than 5 recent invocations

    Returns a list of ``DriftReport`` objects (empty if no skills with
    sufficient replay history exist).
    """
    from collections import Counter
    from datetime import datetime, timezone

    reports: list[DriftReport] = []

    try:
        skills = conn.execute(
            """SELECT fs.id, fs.task_graph, fs.exemplars,
                      fs.invocation_count, fs.last_invoked_at,
                      w.id AS worker_id, w.name AS worker_name
               FROM formed_skills fs
               JOIN workers w
                 ON w.manifest_ref = 'formed_skill:' || CAST(fs.id AS TEXT)
               WHERE w.kind = 'formed_skill'"""
        ).fetchall()

        if not skills:
            return reports

        for skill in skills:
            skill_id = skill["id"]
            inv_count = skill["invocation_count"] or 0
            worker_name = skill["worker_name"] or f"skill_{skill_id}"
            worker_id = skill["worker_id"] or ""

            # Need at least 3 invocations for meaningful analysis.
            if inv_count < 3:
                continue

            # Parse formation-time exemplars for baseline stability.
            exemplars_raw = skill["exemplars"] or "{}"
            try:
                exemplars = json.loads(exemplars_raw) if isinstance(exemplars_raw, str) else exemplars_raw
            except (json.JSONDecodeError, TypeError):
                exemplars = {}

            # Build exemplar stability map: step_key -> {'stable': bool, 'consensus': float}
            exemplar_stability: dict[str, dict] = {}
            for pos_key, pos_data in exemplars.items():
                if isinstance(pos_data, dict):
                    exemplar_stability[str(pos_key)] = {
                        "stable": pos_data.get("stable", False),
                        "consensus": pos_data.get("consensus", 1.0),
                        "default": pos_data.get("default", ""),
                    }

            # Query recent skill_replay entries with matching skill_id.
            recent = conn.execute(
                """SELECT target, detail, observed_at
                   FROM actions
                   WHERE action_type = 'skill_replay'
                   AND target LIKE '%skill_id%'
                   ORDER BY observed_at DESC LIMIT 10""",
            ).fetchall()

            # Filter to rows for THIS skill_id.
            target_marker = f'"skill_id": {skill_id}'
            matching = []
            for r in recent:
                t = r["target"] if isinstance(r["target"], str) else json.dumps(r["target"])
                if target_marker in t:
                    matching.append(r)

            if len(matching) < 3:
                # Not enough replay history for drift analysis.
                continue

            # Analyze per-step outcomes across all matching entries.
            step_stats: dict[str, dict] = {}  # step_idx -> {total, failures}
            step_action_map: dict[str, str] = {}  # step_idx -> action_type
            total_invocations = len(matching)
            total_successes = 0

            for r in matching:
                try:
                    detail = json.loads(r["detail"]) if isinstance(r["detail"], str) else dict(r["detail"])
                except (json.JSONDecodeError, TypeError):
                    detail = {}

                # The "succeeded" flag is in the TARGET JSON, not the detail.
                try:
                    target_data = json.loads(r["target"]) if isinstance(r["target"], str) else dict(r["target"])
                    invocation_succeeded = target_data.get("succeeded", False) if isinstance(target_data, dict) else False
                except (json.JSONDecodeError, TypeError):
                    invocation_succeeded = False

                step_results = detail.get("results", []) if isinstance(detail, dict) else []

                if invocation_succeeded:
                    total_successes += 1

                for sr in step_results:
                    step_i = sr.get("step")
                    if step_i is None:
                        continue
                    sk = str(step_i)
                    if sk not in step_stats:
                        step_stats[sk] = {"total": 0, "failures": 0}
                    step_stats[sk]["total"] += 1
                    if not sr.get("success"):
                        step_stats[sk]["failures"] += 1
                    # Capture action type from the first occurrence.
                    if sk not in step_action_map:
                        step_action_map[sk] = sr.get("action", "?")

            if not step_stats:
                continue

            overall_success_rate = total_successes / max(total_invocations, 1)

            # Build per-step breakdown.
            step_breakdown: list[dict] = []
            for sk in sorted(step_stats.keys(), key=int):
                stats = step_stats[sk]
                total = stats["total"]
                failures = stats["failures"]
                success_rate = (total - failures) / max(total, 1)
                exemplar_info = exemplar_stability.get(str(sk), {})
                step_breakdown.append({
                    "step_idx": int(sk),
                    "action": step_action_map.get(sk, "?"),
                    "total": total,
                    "failures": failures,
                    "success_rate": round(success_rate, 3),
                    "exemplar_stable": exemplar_info.get("stable", False),
                    "exemplar_consensus": exemplar_info.get("consensus", 1.0),
                })

            # Determine health level.
            max_step_failures = max((s["failures"] for s in step_breakdown), default=0)

            if overall_success_rate < 0.5 or max_step_failures > 5:
                overall_health = "unhealthy"
            elif overall_success_rate < 0.8 or max_step_failures > 2:
                overall_health = "degrading"
            else:
                overall_health = "healthy"

            # Auto-demotion: if a stable skill is degrading/unhealthy, demote
            # it back to beta so it gets re-evaluated.
            worker_status = conn.execute(
                "SELECT status FROM workers WHERE manifest_ref = ?",
                (f"formed_skill:{skill_id}",)
            ).fetchone()
            if worker_status and worker_status["status"] == "stable" \
               and overall_health in ("degrading", "unhealthy"):
                from datetime import datetime as _dt
                conn.execute(
                    """UPDATE workers SET status = 'beta', updated_at = ?
                       WHERE manifest_ref = ? AND status = 'stable'""",
                    (_dt.now(timezone.utc).isoformat(),
                     f"formed_skill:{skill_id}")
                )
                conn.commit()
                _log_dispatch(
                    skill_id,
                    f"auto-demoted from stable to beta: drift={overall_health}, "
                    f"success_rate={overall_success_rate:.0%}")
                # Update the report and push ambient event.
                _push_stable_demotion_event(conn, skill_id, worker_name,
                                             overall_health,
                                             overall_success_rate,
                                             total_invocations)

            # Generate recommendation.
            if overall_health == "unhealthy":
                recommendation = (
                    f"This skill has a {overall_success_rate:.0%} success rate. "
                    "Consider re-forming it with 'friday patterns form --force' "
                    "to capture current workflows, or delete it if the workflow "
                    "is no longer relevant."
                )
            elif overall_health == "degrading":
                failing_steps = [s["step_idx"] for s in step_breakdown if s["success_rate"] < 0.5]
                if failing_steps:
                    steps_str = ", ".join(str(s) for s in failing_steps)
                    recommendation = (
                        f"Step(s) {steps_str} are failing frequently. "
                        "The ReplayExecutor will auto-skip these steps. "
                        "If failures persist, consider re-forming the skill."
                    )
                else:
                    recommendation = (
                        "Success rate is acceptable but below optimal. "
                        "Monitor for further degradation."
                    )
            else:
                recommendation = "Skill is performing well. No action needed."

            reports.append(DriftReport(
                skill_id=skill_id,
                worker_name=worker_name,
                worker_id=worker_id,
                overall_health=overall_health,
                invocation_count=inv_count,
                recent_invocations=total_invocations,
                overall_success_rate=round(overall_success_rate, 3),
                step_breakdown=step_breakdown,
                recommendation=recommendation,
            ))

    except Exception:
        pass

    return reports


def _push_stable_demotion_event(conn, skill_id: int, worker_name: str,
                                 health: str, success_rate: float,
                                 recent_count: int) -> None:
    """Push an ambient feed event when a stable skill is demoted to beta."""
    try:
        from .ambient import push_event, AmbientEvent
        ev = AmbientEvent(
            source="friday",
            event_type="skill_demotion",
            title=f"Skill '{worker_name}' demoted to beta",
            detail=(
                f"Skill '{worker_name}' demoted from stable to beta "
                f"due to drift: {health} "
                f"({success_rate:.0%} success rate over "
                f"{recent_count} invocations)."
            ),
            priority=3,
            category="quality",
            project="",
            confidence=0.9,
            payload=f'{{"skill_id": {skill_id}, '
                    f'"worker": "{worker_name}", '
                    f'"old_status": "stable", '
                    f'"new_status": "beta", '
                    f'"reason": "{health}"}}',
            salience=0.0,
        )
        push_event(conn, ev)
    except Exception:
        pass


def format_drift_reports(reports: list[DriftReport]) -> str:
    """Render drift reports as a human-readable summary."""
    if not reports:
        return "No skills with sufficient replay history for drift analysis."

    lines = ["Skill Drift Analysis", "=" * 40, ""]

    healthy = [r for r in reports if r.overall_health == "healthy"]
    degrading = [r for r in reports if r.overall_health == "degrading"]
    unhealthy = [r for r in reports if r.overall_health == "unhealthy"]

    if unhealthy:
        lines.append(f"🔴 Unhealthy ({len(unhealthy)}):")
        for r in unhealthy:
            lines.append(f"  {r.worker_name}")
            lines.append(f"    Success rate: {r.overall_success_rate:.0%} over {r.recent_invocations} invocations")
            lines.append(f"    Recommendation: {r.recommendation}")
        lines.append("")

    if degrading:
        lines.append(f"🟡 Degrading ({len(degrading)}):")
        for r in degrading:
            lines.append(f"  {r.worker_name}")
            lines.append(f"    Success rate: {r.overall_success_rate:.0%} over {r.recent_invocations} invocations")
            failing = [s for s in r.step_breakdown if s["success_rate"] < 0.8]
            if failing:
                lines.append(f"    Failing steps: {', '.join(f'#{s["step_idx"]} ({s["action"]})' for s in failing)}")
            lines.append(f"    Recommendation: {r.recommendation}")
        lines.append("")

    if healthy:
        lines.append(f"🟢 Healthy ({len(healthy)}):")
        for r in healthy:
            lines.append(f"  {r.worker_name} — {r.overall_success_rate:.0%} success rate")
        lines.append("")

    summary = f"{len(healthy)} healthy, {len(degrading)} degrading, {len(unhealthy)} unhealthy"
    lines.append("─" * 40)
    lines.append(f"Summary: {summary}")

    return "\n".join(lines)
