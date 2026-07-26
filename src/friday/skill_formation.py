"""Pillar B Stage 4 — Skill Formation.

Takes labeled workflow intents (Stage 3) and forms them into deployable,
replayable skills registered in the shared workers registry.
"""

from __future__ import annotations

import json
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
                 AND w.status IN ('beta', 'proposed')"""
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

                # Rate-limit: skip if dispatched within the interval.
                if last_invoked:
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


class _MiniTask:
    """Minimal task-like object for sub-executor dispatch."""

    def __init__(self, payload: str = "", hint: str = "", ref: str = ""):
        self.runtime_payload = payload
        self.runtime_hint = hint
        self.manifest_ref = ref
