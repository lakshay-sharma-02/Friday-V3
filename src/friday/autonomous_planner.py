"""Autonomous Action Planner — turns analysis findings into gated execution plans.

Bridge between "Friday knows" and "Friday acts". After each daemon cycle, the
planner reviews what was found (drifted skills, capability gaps, cross-project
suggestions, new patterns) and forms `ActionPlan` records for each actionable
finding. Each plan passes through the autonomy gate system (auto/notify/
confirm/double) before execution is dispatched via RuntimeEngine.

Design:
  - Planning is deterministic: same findings → same plans (no LLM).
  - Each plan targets one action type classified in confirm_gate's two-axis
    matrix. The autonomy system resolves the effective permission level.
  - Plans are persisted in `autonomous_actions` table as pending until
    approved (auto-level) or confirmed by the operator.
  - The daemon cycle calls `plan_and_dispatch()` post-analysis.
  - The CLI (`friday act`) lets the operator inspect and approve/reject.

Skill Repair:
  - When a skill is drifted (unhealthy/degrading), the planner creates a plan
    with action_type="skill_execute". dispatch_plan() detects this and calls
    the _repair_skill() pipeline instead of generic executor dispatch.
  - _repair_skill() reads the drift report from DB, determines the repair
    strategy (auto-skip failing steps, re-form from current pattern, delete
    if irrecoverable, or escalate to operator), and executes the repair.
  - Repair actions are recorded in the autonomous_actions result_json so the
    operator can see what was done.
"""

from __future__ import annotations

import json
import time
from collections import namedtuple
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from .db import connect, now_iso


# ──────────────────────────────────────────────────────────────────────
# Data model
# ──────────────────────────────────────────────────────────────────────


# Mini-task wrapper for dispatching plans through the executor pipeline.
_MiniTask = namedtuple("_MiniTask", [
    "task_id", "worker_id", "runtime_payload", "execution_id",
    "title", "task_type", "timeout", "dependencies", "outputs",
    "acceptance_criteria", "verification", "symbolic",
    "dependency_summaries", "goal", "session_id", "schedule_id", "wave",
])


@dataclass
class ActionPlan:
    """One unit of autonomous work, ready for gated execution.

    Attributes:
        plan_id:        Unique ID (uuid hex).
        created_at:     ISO timestamp of creation.
        source:         What triggered this plan:
                        "drift" | "gap" | "suggestion" | "pattern" |
                        "initiative" | "correlation" | "manual"
        source_id:      ID of the triggering entity (e.g. skill_id, gap_id).
        source_summary: Human-readable one-liner about what was found.
        action_type:    Matches confirm_gate action type (e.g. "shell_exec",
                        "git_ops", "filesystem", "skill_execute").
        target:         What the action operates on (e.g. skill name, file path).
        worker_id:      Executor worker to dispatch (e.g. "worker:shell").
        payload:        JSON string; the runtime_payload for the executor.
        motivation:     Why this action is worth taking (shown during confirm).
        status:         "pending" | "approved" | "rejected" | "executing" |
                        "succeeded" | "failed"
        auto_level:     The effective autonomy level at plan time
                        ("auto" | "notify" | "confirm" | "double").
        requires_confirm: True if the operator must approve before execution.
    """

    plan_id: str
    created_at: str
    source: str
    source_id: str
    source_summary: str
    action_type: str
    target: str
    worker_id: str
    payload: str
    motivation: str
    status: str = "pending"
    auto_level: str = "auto"
    requires_confirm: bool = False
    session_id: Optional[str] = None
    result: Optional[dict] = None


# ──────────────────────────────────────────────────────────────────────
# DB helpers
# ──────────────────────────────────────────────────────────────────────


_AUTO_TABLE = """
    CREATE TABLE IF NOT EXISTS autonomous_actions (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        plan_id         TEXT NOT NULL UNIQUE,
        created_at      TEXT NOT NULL,
        source          TEXT NOT NULL,
        source_id       TEXT NOT NULL DEFAULT '',
        source_summary  TEXT NOT NULL DEFAULT '',
        action_type     TEXT NOT NULL,
        target          TEXT NOT NULL DEFAULT '',
        worker_id       TEXT NOT NULL DEFAULT '',
        payload         TEXT NOT NULL DEFAULT '',
        motivation      TEXT NOT NULL DEFAULT '',
        status          TEXT NOT NULL DEFAULT 'pending',
        auto_level      TEXT NOT NULL DEFAULT 'auto',
        session_id      TEXT,
        result_json     TEXT,
        executed_at     TEXT,
        updated_at      TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_auto_actions_status ON autonomous_actions(status);
    CREATE INDEX IF NOT EXISTS idx_auto_actions_source ON autonomous_actions(source);
    CREATE INDEX IF NOT EXISTS idx_auto_actions_created ON autonomous_actions(created_at);
"""


def ensure_autonomous_actions_table(conn) -> None:
    """Create the autonomous_actions table if it doesn't exist.

    Safe to call multiple times (IF NOT EXISTS).
    """
    try:
        conn.executescript(_AUTO_TABLE)
        conn.commit()
    except Exception:
        conn.rollback()


def _insert_plan(conn, plan: ActionPlan) -> None:
    conn.execute(
        """INSERT INTO autonomous_actions
           (plan_id, created_at, source, source_id, source_summary,
            action_type, target, worker_id, payload, motivation,
            status, auto_level, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (plan.plan_id, plan.created_at, plan.source, plan.source_id,
         plan.source_summary, plan.action_type, plan.target, plan.worker_id,
         plan.payload, plan.motivation, plan.status, plan.auto_level,
         plan.created_at),
    )
    conn.commit()


def _update_plan_status(conn, plan_id: str, status: str,
                        session_id: Optional[str] = None,
                        result: Optional[dict] = None) -> None:
    now = now_iso()
    if session_id:
        conn.execute(
            "UPDATE autonomous_actions SET status=?, session_id=?, "
            "executed_at=?, updated_at=? WHERE plan_id=?",
            (status, session_id, now, now, plan_id))
    elif result:
        conn.execute(
            "UPDATE autonomous_actions SET status=?, result_json=?, "
            "executed_at=?, updated_at=? WHERE plan_id=?",
            (status, json.dumps(result), now, now, plan_id))
    else:
        conn.execute(
            "UPDATE autonomous_actions SET status=?, updated_at=? WHERE plan_id=?",
            (status, now, plan_id))
    conn.commit()


def get_pending_plans(conn, source: Optional[str] = None) -> list[ActionPlan]:
    """Return all pending (not yet approved/rejected/executed) plans."""
    if source:
        rows = conn.execute(
            "SELECT * FROM autonomous_actions WHERE status='pending' AND source=? "
            "ORDER BY created_at DESC", (source,)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM autonomous_actions WHERE status='pending' "
            "ORDER BY created_at DESC").fetchall()
    return [_row_to_plan(r) for r in rows]


def get_plan_history(conn, limit: int = 20) -> list[ActionPlan]:
    """Return recent plans (all statuses), newest first."""
    rows = conn.execute(
        "SELECT * FROM autonomous_actions ORDER BY created_at DESC LIMIT ?",
        (limit,)).fetchall()
    return [_row_to_plan(r) for r in rows]


def _row_to_plan(r) -> ActionPlan:
    return ActionPlan(
        plan_id=r["plan_id"],
        created_at=r["created_at"],
        source=r["source"],
        source_id=r["source_id"],
        source_summary=r["source_summary"],
        action_type=r["action_type"],
        target=r["target"],
        worker_id=r["worker_id"],
        payload=r["payload"],
        motivation=r["motivation"],
        status=r["status"],
        auto_level=r["auto_level"],
        session_id=r.get("session_id"),
        result=json.loads(r["result_json"]) if r.get("result_json") else None,
    )


# ──────────────────────────────────────────────────────────────────────
# Planning — convert analysis findings into ActionPlans
# ──────────────────────────────────────────────────────────────────────


def plan_from_drift(skill_id: int, skill_name: str,
                    health: str, report: Any) -> list[ActionPlan]:
    """If a skill is unhealthy/degrading, plan to repair it.

    The plan carries the drift report data so dispatch_plan() can read
    it back from the DB and perform actual diagnostics + repair.
    """
    import uuid
    now = now_iso()
    plans: list[ActionPlan] = []

    if health in ("unhealthy", "degrading"):
        plans.append(ActionPlan(
            plan_id=f"drift:{uuid.uuid4().hex[:12]}",
            created_at=now,
            source="drift",
            source_id=str(skill_id),
            source_summary=f"Skill '{skill_name}' is {health}",
            action_type="skill_execute",
            target=skill_name,
            worker_id="worker:shell",
            payload=json.dumps({
                "op": "repair_skill",
                "skill_id": skill_id,
                "skill_name": skill_name,
                "health": health,
            }),
            motivation=f"{skill_name} has degraded ({health}). "
                       f"Diagnose why and suggest remediation.",
            auto_level="confirm",
            requires_confirm=True,
        ))

    return plans


def plan_from_gaps(gaps: list[Any]) -> list[ActionPlan]:
    """For new capability gaps, plan to research or suggest filing.

    Gaps are abstract (not directly automatable), so the plan is to
    log a structured note and surface it in the feed.
    """
    import uuid
    now = now_iso()
    plans: list[ActionPlan] = []
    for gap in gaps[:3]:
        if hasattr(gap, "capability"):
            gap_name = str(gap.capability)[:60]
        elif isinstance(gap, dict) and gap.get("capability"):
            gap_name = str(gap["capability"])[:60]
        else:
            gap_name = str(gap)[:60]
        plans.append(ActionPlan(
            plan_id=f"gap:{uuid.uuid4().hex[:12]}",
            created_at=now,
            source="gap",
            source_id=str(getattr(gap, "id", "")),
            source_summary=f"Capability gap: {gap_name}",
            action_type="documentation",
            target="gaps.md",
            worker_id="worker:documentation",
            payload=json.dumps({
                "path": "docs/gaps.md",
                "content": f"# Capability Gap: {gap_name}\n\n"
                           f"Identified: {now}\n"
                           f"Description: {getattr(gap, 'description', '')}\n\n"
                           f"Potential approach: {getattr(gap, 'suggestion', '')}\n",
            }),
            motivation=f"Document the '{gap_name}' gap so it can be "
                       f"addressed systematically.",
            auto_level="notify",
            requires_confirm=False,
        ))
    return plans


def plan_from_suggestions(suggestions: list[Any]) -> list[ActionPlan]:
    """For high-severity cross-project suggestions, plan execution.

    Suggestions have concrete action steps (unlike gaps), so they can
    be dispatched directly when autonomy level allows.
    """
    import uuid
    now = now_iso()
    plans: list[ActionPlan] = []
    for sug in suggestions[:3]:
        sug_id = getattr(sug, "id", str(hash(str(sug))))
        sug_title = getattr(sug, "title", str(sug))[:60]
        sug_detail = getattr(sug, "detail", "")

        plans.append(ActionPlan(
            plan_id=f"sug:{uuid.uuid4().hex[:12]}",
            created_at=now,
            source="suggestion",
            source_id=str(sug_id),
            source_summary=sug_title,
            action_type="filesystem",
            target=sug_title,
            worker_id="worker:filesystem",
            payload=json.dumps({
                "op": "noop",
                "suggestion_id": str(sug_id),
                "detail": sug_detail,
            }),
            motivation=sug_detail or sug_title,
            auto_level="confirm",
            requires_confirm=True,
        ))
    return plans


def plan_from_initiatives(initiatives: list[Any]) -> list[ActionPlan]:
    """For new high-priority initiatives, plan the first task."""
    import uuid
    now = now_iso()
    plans: list[ActionPlan] = []
    for init in initiatives[:2]:
        init_id = getattr(init, "id", str(hash(str(init))))
        init_title = getattr(init, "title", str(init))[:60]
        init_goal = getattr(init, "goal", getattr(init, "description", ""))

        plans.append(ActionPlan(
            plan_id=f"init:{uuid.uuid4().hex[:12]}",
            created_at=now,
            source="initiative",
            source_id=str(init_id),
            source_summary=f"Initiative: {init_title}",
            action_type="skill_execute",
            target=init_title,
            worker_id="worker:shell",
            payload=json.dumps({
                "op": "plan_initiative",
                "initiative_id": str(init_id),
                "title": init_title,
                "goal": init_goal,
            }),
            motivation=f"Start work on initiative: {init_title}",
            auto_level="confirm",
            requires_confirm=True,
        ))
    return plans


# ──────────────────────────────────────────────────────────────────────
# Skill Repair — diagnose and fix drifted/unhealthy skills
# ──────────────────────────────────────────────────────────────────────


def _diagnose_skill(conn, skill_id: int, skill_name: str,
                    health: str) -> dict:
    """Run targeted diagnostics on a specific drifted skill.

    Reads the drift report data from the DB, examines per-step breakdowns
    and exemplar stability, and returns a diagnosis dict.

    The diagnosis includes:
      - skill_id, skill_name, health
      - invocation_count, recent_invocations, overall_success_rate
      - step_breakdown: per-step analysis with failure counts
      - exemplar_stability: which steps have stable exemplars
      - recommended_strategy: what to do (skip_steps | re_form | delete | escalate)

    This is the "Friday diagnoses why" step — always runs before any
    repair action is taken.
    """
    diagnosis: dict = {
        "skill_id": skill_id,
        "skill_name": skill_name,
        "health": health,
        "invocation_count": 0,
        "recent_invocations": 0,
        "overall_success_rate": 0.0,
        "step_count": 0,
        "step_breakdown": [],
        "exemplar_stability": {},
        "recommended_strategy": "escalate",
        "reasoning": "",
    }

    try:
        # 1. Read the skill's drift data from detect_skill_drift.
        # We call it directly to get fresh per-step breakdowns.
        from .skill_formation import detect_skill_drift
        reports = detect_skill_drift(conn)
        drift_report = None
        for r in reports or []:
            if r.skill_id == skill_id:
                drift_report = r
                break

        if drift_report is None:
            diagnosis["reasoning"] = f"Skill #{skill_id} has fewer than 3 invocations — not enough data for drift analysis. No repair needed."
            diagnosis["recommended_strategy"] = "none"
            return diagnosis

        diagnosis["invocation_count"] = drift_report.invocation_count
        diagnosis["recent_invocations"] = drift_report.recent_invocations
        diagnosis["overall_success_rate"] = drift_report.overall_success_rate
        diagnosis["step_breakdown"] = drift_report.step_breakdown
        diagnosis["step_count"] = len(drift_report.step_breakdown)

        # 2. Read exemplar stability from the formed_skills table.
        skill_row = conn.execute(
            "SELECT exemplars, task_graph FROM formed_skills WHERE id = ?",
            (skill_id,)
        ).fetchone()
        if skill_row:
            exemplars_raw = skill_row["exemplars"] or "{}"
            try:
                exemplars = json.loads(exemplars_raw) if isinstance(exemplars_raw, str) else exemplars_raw
            except (json.JSONDecodeError, TypeError):
                exemplars = {}

            for pos_key, pos_data in exemplars.items():
                if isinstance(pos_data, dict):
                    diagnosis["exemplar_stability"][str(pos_key)] = {
                        "stable": pos_data.get("stable", False),
                        "consensus": pos_data.get("consensus", 1.0),
                        "default": pos_data.get("default", ""),
                    }

        # 3. Determine the recommended strategy.
        #    - unhealthy + low success rate + failing steps → re-form or delete
        #    - degrading with specific failing steps → skip those steps
        #    - unhealthy and already demoted (was stable, now beta) → escalate
        step_breakdown = diagnosis["step_breakdown"]
        success_rate = diagnosis["overall_success_rate"]
        max_step_failures = max((s.get("failures", 0) for s in step_breakdown), default=0)
        failing_steps = [s for s in step_breakdown if s.get("success_rate", 1.0) < 0.5]

        # Check if the skill was already demoted from stable.
        worker_row = conn.execute(
            "SELECT status FROM workers WHERE manifest_ref = ?",
            (f"formed_skill:{skill_id}",)
        ).fetchone()
        current_status = worker_row["status"] if worker_row else "unknown"

        if health == "healthy":
            diagnosis["recommended_strategy"] = "none"
            diagnosis["reasoning"] = "Skill is healthy. No repair needed."
        elif health == "unhealthy" and success_rate < 0.3:
            diagnosis["recommended_strategy"] = "delete"
            diagnosis["reasoning"] = (
                f"Success rate is {success_rate:.0%} — critical degradation. "
                f"Will delete the formed skill and let it re-form naturally "
                f"on the next daemon cycle from current workflow patterns."
            )
        elif health == "unhealthy" and failing_steps and current_status == "beta":
            diagnosis["recommended_strategy"] = "re_form"
            diagnosis["reasoning"] = (
                f"Skill was already demoted to beta and is still unhealthy "
                f"({success_rate:.0%} success rate). {len(failing_steps)} "
                f"step(s) are failing. Re-forming from the latest mined pattern "
                f"may capture an evolved workflow."
            )
        elif health == "degrading" and failing_steps:
            diagnosis["recommended_strategy"] = "skip_steps"
            failing_indices = [s["step_idx"] for s in failing_steps]
            diagnosis["reasoning"] = (
                f"Degrading with {len(failing_steps)} failing step(s): "
                f"{failing_indices}. Will mark those steps for auto-skip "
                f"in future replays and keep the rest of the skill intact."
            )
        elif health == "degrading":
            diagnosis["recommended_strategy"] = "none"
            diagnosis["reasoning"] = (
                f"Degrading ({success_rate:.0%} success rate) but no specific "
                f"step is failing consistently. Monitoring recommended."
            )
        else:
            diagnosis["recommended_strategy"] = "escalate"
            diagnosis["reasoning"] = (
                f"Skill is {health} with {success_rate:.0%} success rate over "
                f"{diagnosis['recent_invocations']} invocations. "
                f"Escalating to operator for manual review."
            )

    except Exception as exc:
        diagnosis["reasoning"] = f"Diagnosis failed: {exc}"
        diagnosis["recommended_strategy"] = "escalate"

    return diagnosis


def _execute_skill_repair(conn, plan: ActionPlan, diagnosis: dict) -> dict:
    """Execute the actual skill repair based on the diagnosis.

    Performs one of:
      - skip_steps:   Updates the task_graph to remove failing steps.
                      The skill continues running without those steps.
      - re_form:      Deletes the current formed_skill + worker, then
                      re-forms from the latest mined pattern.
      - delete:       Deletes the formed_skill and worker entirely.
                      The next daemon cycle may re-form it naturally.
      - escalate:     No auto-repair. The plan is left as pending for
                      operator review via friday act.

    Returns a result dict with details of what was done.
    """
    strategy = diagnosis.get("recommended_strategy", "escalate")
    skill_id = plan.source_id
    try:
        skill_id_int = int(skill_id)
    except (ValueError, TypeError):
        skill_id_int = 0

    result: dict = {
        "strategy": strategy,
        "diagnosis": {
            "health": diagnosis.get("health", "unknown"),
            "success_rate": diagnosis.get("overall_success_rate", 0.0),
            "invocation_count": diagnosis.get("invocation_count", 0),
            "step_count": diagnosis.get("step_count", 0),
            "failing_steps": [
                s for s in diagnosis.get("step_breakdown", [])
                if s.get("success_rate", 1.0) < 0.5
            ],
        },
        "actions_taken": [],
        "reasoning": diagnosis.get("reasoning", ""),
    }

    if strategy == "none":
        result["success"] = True
        result["summary"] = "No repair needed — skill is healthy or only briefly degraded."
        return result

    if skill_id_int <= 0:
        result["success"] = False
        result["summary"] = "Cannot repair: invalid skill_id"
        return result

    # --- skip_steps: remove failing steps from the task_graph ---
    if strategy == "skip_steps":
        try:
            conn.execute("BEGIN")
            skill_row = conn.execute(
                "SELECT task_graph FROM formed_skills WHERE id = ?",
                (skill_id_int,)
            ).fetchone()
            if skill_row:
                task_graph_raw = skill_row["task_graph"] or "[]"
                task_graph = json.loads(task_graph_raw) if isinstance(task_graph_raw, str) else task_graph_raw

                skip_indices = set()
                for s in diagnosis.get("step_breakdown", []):
                    if s.get("success_rate", 1.0) < 0.5:
                        skip_indices.add(s["step_idx"])

                original_count = len(task_graph)
                new_graph = [
                    step for i, step in enumerate(task_graph)
                    if i not in skip_indices
                ]
                skipped_count = original_count - len(new_graph)

                if skipped_count > 0:
                    now = now_iso()
                    conn.execute(
                        "UPDATE formed_skills SET task_graph=?, updated_at=? WHERE id=?",
                        (json.dumps(new_graph), now, skill_id_int)
                    )

                    # Canary mode: reset invocation_count so the skill must
                    # prove itself again from scratch. The existing
                    # _check_canary_promotion() in skill_formation.py will
                    # auto-promote from beta back to stable after 5
                    # successful post-repair executions with >=80% success.
                    conn.execute(
                        "UPDATE formed_skills SET invocation_count = 0, "
                        "updated_at = ? WHERE id = ?",
                        (now, skill_id_int)
                    )

                    try:
                        from .action_log import ActionEvent, log_action
                        log_action(conn, ActionEvent(
                            source="friday",
                            action_type="skill_repair",
                            target=json.dumps({
                                "skill_id": skill_id_int,
                                "strategy": "skip_steps",
                                "skipped": skipped_count,
                                "remaining": len(new_graph),
                                "skip_indices": sorted(skip_indices),
                                "canary_promotion_threshold": 5,
                            }),
                            detail=json.dumps({
                                "original_graph": task_graph,
                                "new_graph": new_graph,
                                "reasoning": diagnosis.get("reasoning", ""),
                                "note": "Invocation count reset for canary monitoring. "
                                        "Will auto-promote from beta to stable after "
                                        "5 clean post-repair executions.",
                            }),
                            confidence="observed",
                            observed_at=now,
                        ))
                    except Exception:
                        pass

                    conn.commit()
                    result["success"] = True
                    result["summary"] = (
                        f"Skipped {skipped_count} failing step(s) out of "
                        f"{original_count} total. {len(new_graph)} step(s) remain. "
                        f"Invocation count reset to 0 for canary monitoring — "
                        f"will auto-promote after 5 clean executions."
                    )
                    result["actions_taken"].append({
                        "action": "skip_steps",
                        "skipped_count": skipped_count,
                        "remaining_steps": len(new_graph),
                        "skip_indices": sorted(skip_indices),
                        "canary_monitoring": True,
                    })
                else:
                    conn.commit()
                    result["success"] = True
                    result["summary"] = "No failing steps to skip."
            else:
                conn.commit()
                result["success"] = False
                result["summary"] = f"Formed skill #{skill_id_int} not found."
        except Exception as exc:
            conn.rollback()
            result["success"] = False
            result["summary"] = f"Skip-steps repair failed: {exc}"
            result["error"] = str(exc)[:500]

        return result

    # --- re_form: delete and re-form from latest pattern ---
    if strategy == "re_form":
        try:
            conn.execute("BEGIN")
            from .skill_formation import form_skills
            from .db import delete_formed_skill

            worker_row = conn.execute(
                "SELECT id FROM workers WHERE manifest_ref = ?",
                (f"formed_skill:{skill_id_int}",)
            ).fetchone()
            if worker_row:
                conn.execute("DELETE FROM workers WHERE id = ?", (worker_row["id"],))
            delete_formed_skill(conn, skill_id_int)

            # Re-form from current workflow intents. Since we deleted the old
            # formed_skill + worker above, form_skills(force=False) will
            # naturally discover the intent needs a new skill.
            formed = form_skills(conn, force=False)

            new_skill_id = None
            for fs in formed:
                if fs.get("skill_id"):
                    new_skill_id = fs["skill_id"]

            now = now_iso()
            try:
                from .action_log import ActionEvent, log_action
                log_action(conn, ActionEvent(
                    source="friday",
                    action_type="skill_repair",
                    target=json.dumps({
                        "skill_id": skill_id_int,
                        "strategy": "re_form",
                        "new_skill_id": new_skill_id,
                        "formed_count": len(formed),
                    }),
                    detail=json.dumps({
                        "reasoning": diagnosis.get("reasoning", ""),
                    }),
                    confidence="observed",
                    observed_at=now,
                ))
            except Exception:
                pass

            conn.commit()
            result["success"] = True
            result["summary"] = (
                f"Deleted skill #{skill_id_int} and re-formed {len(formed)} "
                f"new skill(s) from current workflow patterns."
            )
            result["actions_taken"].append({
                "action": "re_form",
                "deleted_skill_id": skill_id_int,
                "new_skill_ids": [fs.get("skill_id") for fs in formed],
            })

        except Exception as exc:
            conn.rollback()
            result["success"] = False
            result["summary"] = f"Re-form repair failed: {exc}"
            result["error"] = str(exc)[:500]

        return result

    # --- delete: remove the skill entirely ---
    if strategy == "delete":
        try:
            conn.execute("BEGIN")
            from .db import delete_formed_skill

            worker_row = conn.execute(
                "SELECT id FROM workers WHERE manifest_ref = ?",
                (f"formed_skill:{skill_id_int}",)
            ).fetchone()
            if worker_row:
                conn.execute("DELETE FROM workers WHERE id = ?", (worker_row["id"],))
            delete_formed_skill(conn, skill_id_int)

            now = now_iso()
            try:
                from .action_log import ActionEvent, log_action
                log_action(conn, ActionEvent(
                    source="friday",
                    action_type="skill_repair",
                    target=json.dumps({
                        "skill_id": skill_id_int,
                        "strategy": "delete",
                    }),
                    detail=json.dumps({
                        "reasoning": diagnosis.get("reasoning", ""),
                    }),
                    confidence="observed",
                    observed_at=now,
                ))
            except Exception:
                pass

            conn.commit()
            result["success"] = True
            result["summary"] = (
                f"Deleted skill #{skill_id_int} (diagnosed as irrecoverable: "
                f"{diagnosis.get('overall_success_rate', 0):.0%} success rate). "
                f"The next daemon cycle may re-form it if the workflow pattern persists."
            )
            result["actions_taken"].append({
                "action": "delete",
                "deleted_skill_id": skill_id_int,
            })

        except Exception as exc:
            conn.rollback()
            result["success"] = False
            result["summary"] = f"Delete repair failed: {exc}"
            result["error"] = str(exc)[:500]

        return result

    # --- escalate: leave for operator review ---
    result["success"] = False
    result["summary"] = diagnosis.get("reasoning", "Escalating to operator for review.")
    result["actions_taken"].append({
        "action": "escalate",
        "reason": "Auto-repair not possible or not appropriate.",
    })

    return result


def _repair_skill(conn, plan: ActionPlan) -> dict:
    """Main skill repair orchestrator.

    Called by dispatch_plan() when the plan's action_type is 'skill_execute'
    and the payload contains {'op': 'repair_skill', ...}.

    Pipeline:
      1. Parse the payload to get skill_id, skill_name, health
      2. Run _diagnose_skill() to get the full diagnosis
      3. Run _execute_skill_repair() to perform the actual repair
      4. Return a structured result

    Always succeeds at returning a dict — the plan's status is updated
    by the caller based on this result's 'success' field.
    """
    try:
        payload = json.loads(plan.payload) if isinstance(plan.payload, str) else plan.payload
    except (json.JSONDecodeError, TypeError):
        payload = {}

    skill_id = payload.get("skill_id", plan.source_id or 0)
    skill_name = payload.get("skill_name", plan.target or "unknown")
    health = payload.get("health", "unknown")

    try:
        skill_id_int = int(skill_id)
    except (ValueError, TypeError):
        skill_id_int = 0

    if skill_id_int <= 0:
        return {
            "success": False,
            "skill_id": 0,
            "skill_name": skill_name,
            "diagnosis": {},
            "repair": {},
            "summary": f"Cannot repair skill: invalid skill_id ({skill_id})",
        }

    # Step 1: Diagnose.
    diagnosis = _diagnose_skill(conn, skill_id_int, skill_name, health)

    # Step 2: Execute repair based on diagnosis.
    repair_result = _execute_skill_repair(conn, plan, diagnosis)

    # Step 3: Re-run drift detection immediately after successful repair
    # to confirm the skill is healthy now — instead of waiting for the
    # next full daemon cycle. This gives the operator immediate feedback
    # that the repair worked (or didn't).
    post_repair_drift: Optional[dict] = None
    _REPAIR_STRATEGIES = ("skip_steps", "re_form", "delete")
    if repair_result.get("success", False) and (
        diagnosis.get("recommended_strategy") in _REPAIR_STRATEGIES
    ):
        try:
            from .skill_formation import detect_skill_drift
            fresh_reports = detect_skill_drift(conn) or []
            for r in fresh_reports:
                if r.skill_id == skill_id_int:
                    post_repair_drift = {
                        "health": r.overall_health,
                        "success_rate": r.overall_success_rate,
                        "invocation_count": r.invocation_count,
                        "recent_invocations": r.recent_invocations,
                        "step_count": len(r.step_breakdown),
                        "failing_steps": [
                            s for s in r.step_breakdown
                            if s.get("success_rate", 1.0) < 0.5
                        ],
                    }
                    break
            # If the skill was deleted, it won't appear in drift reports.
            if post_repair_drift is None and diagnosis.get("recommended_strategy") == "delete":
                post_repair_drift = {
                    "health": "deleted",
                    "success_rate": 0.0,
                    "invocation_count": 0,
                    "recent_invocations": 0,
                    "step_count": 0,
                    "failing_steps": [],
                    "note": "Skill was deleted as part of repair strategy.",
                }
        except Exception as exc:
            post_repair_drift = {"error": f"Post-repair drift check failed: {exc}"}

    return {
        "success": repair_result.get("success", False),
        "skill_id": skill_id_int,
        "skill_name": skill_name,
        "diagnosis": diagnosis,
        "repair": repair_result,
        "post_repair_drift": post_repair_drift,
        "summary": repair_result.get("summary", ""),
    }


# ──────────────────────────────────────────────────────────────────────
# Dispatch — run a plan through the autonomy gates + executor
# ──────────────────────────────────────────────────────────────────────


def _resolve_auto_level(action_type: str) -> str:
    """Get the effective autonomy level for an action type.

    Checks autonomy permissions (user override → auto-downgrade → default).
    """
    try:
        from .autonomy import get_action_permission
        return get_action_permission(action_type).effective_level
    except Exception:
        from .runtime.confirm_gate import get_action_level
        return get_action_level(action_type).value


def _should_execute(plan: ActionPlan, conn) -> bool:
    """Check if a plan should be executed now based on its autonomy level.

    Returns True if the plan can proceed immediately (auto or notify level)
    or has been operator-approved. Returns False if it needs confirmation.
    """
    if plan.requires_confirm and plan.status != "approved":
        return False

    try:
        from .autonomy import is_kill_switch_active
        if is_kill_switch_active(conn):
            return False
    except Exception:
        pass

    return True


def _build_runtime_task(plan: ActionPlan) -> Any:
    """Build a minimal RuntimeTask-like object for the executor.

    The task only needs ``runtime_payload``, ``worker_id``, and ``task_id``
    to work with the existing executor dispatch path.
    """
    return _MiniTask(
        task_id=plan.plan_id,
        worker_id=plan.worker_id,
        runtime_payload=plan.payload,
        execution_id=plan.plan_id,
        title=plan.source_summary,
        task_type=plan.action_type,
        timeout=120,
        dependencies=[],
        outputs=[],
        acceptance_criteria=[],
        verification=[],
        symbolic={},
        dependency_summaries={},
        goal=plan.motivation,
        session_id="",
        schedule_id=plan.plan_id,
        wave=1,
    )


def dispatch_plan(plan: ActionPlan, conn) -> Optional[dict]:
    """Execute one ActionPlan through the executor pipeline.

    For skill_execute plans with op='repair_skill', calls the real skill
    repair pipeline (_repair_skill()) instead of generic executor dispatch.
    All other plans are dispatched through the standard executor path.

    Returns the execution result dict, or None if the plan was skipped
    (needs confirmation, kill switch active, etc.).
    """
    if not _should_execute(plan, conn):
        return None

    # ── Skill Repair Path ──────────────────────────────────────────
    # When a drift plan is approved, run the real skill repair pipeline
    # instead of dispatching to a generic shell executor.
    if plan.source == "drift" and plan.action_type == "skill_execute":
        try:
            repair_result = _repair_skill(conn, plan)
            status = "succeeded" if repair_result.get("success") else "failed"
            _update_plan_status(conn, plan.plan_id, status, result=repair_result)
            return repair_result
        except Exception as exc:
            err_result = {"success": False, "error": str(exc)[:500],
                          "summary": f"Skill repair crashed: {exc}"}
            _update_plan_status(conn, plan.plan_id, "failed", result=err_result)
            return err_result

    # ── Standard Executor Dispatch Path ─────────────────────────────
    try:
        from .runtime.executors import resolve_executor
        from .runtime.dispatcher import dispatch

        task = _build_runtime_task(plan)
        worker = resolve_executor(plan.worker_id)
        if worker is None:
            _update_plan_status(conn, plan.plan_id, "failed",
                                result={"error": f"no worker for {plan.worker_id}"})
            return {"error": f"no worker for {plan.worker_id}"}

        # Autonomy gate check for action workers.
        try:
            from .runtime.confirm_gate import is_action_worker
            if is_action_worker(plan.worker_id):
                from .runtime.confirm_gate import prompt_confirm
                confirmed = prompt_confirm(
                    action=plan.action_type,
                    target=plan.target,
                    worker_id=plan.worker_id,
                    skip_prompt=True,
                    conn=conn,
                )
                if not confirmed:
                    _update_plan_status(conn, plan.plan_id, "rejected",
                                        result={"reason": "autonomy gate blocked"})
                    return {"error": "autonomy gate blocked"}
        except Exception:
            pass

        # Execute.
        result = dispatch(task, worker)
        status = "succeeded" if result.success else "failed"
        _update_plan_status(conn, plan.plan_id, status, result={
            "success": result.success,
            "stdout": (result.stdout or "")[:500],
            "stderr": (result.stderr or "")[:500],
            "error": result.error or "",
            "duration_ms": result.duration_ms,
            "exit_code": result.exit_code,
        })

        return {
            "success": result.success,
            "stdout": (result.stdout or "")[:200],
            "error": result.error or "",
            "duration_ms": result.duration_ms,
        }

    except Exception as exc:
        _update_plan_status(conn, plan.plan_id, "failed",
                            result={"error": str(exc)[:500]})
        return {"error": str(exc)[:500]}


# ──────────────────────────────────────────────────────────────────────
# Top-level entrypoint — called from daemon post-cycle
# ──────────────────────────────────────────────────────────────────────


def plan_and_dispatch(conn, cycle: dict) -> dict:
    """Review daemon cycle findings and plan/dispatch autonomous actions.

    Called after each daemon cycle completes. Reviews drift, gaps,
    suggestions, and initiatives from the cycle result, creates action
    plans for actionable findings, and dispatches plans that can run
    without operator confirmation (auto/notify level).

    Returns a summary dict with counts of what was planned and dispatched.

    Best-effort: never raises. Failures in individual plan creation or
    dispatch are logged via the plan's status and do not break the cycle.
    """
    result: dict = {
        "planned": 0,
        "dispatched": 0,
        "succeeded": 0,
        "pending_confirm": 0,
        "plans": [],
    }

    try:
        ensure_autonomous_actions_table(conn)
    except Exception:
        return result

    if not cycle:
        return result

    plans: list[ActionPlan] = []

    # 1. Drift — unhealthy/degrading skills need repair.
    drifted = cycle.get("drifted_skills", 0)
    if drifted > 0:
        try:
            from .skill_formation import detect_skill_drift
            drift_reports = detect_skill_drift(conn)
            for report in drift_reports or []:
                if report.overall_health in ("unhealthy", "degrading"):
                    skill_id = getattr(report, "skill_id", 0)
                    skill_name = getattr(report, "skill_name",
                                         getattr(report, "worker_name", "?"))
                    plans.extend(plan_from_drift(
                        skill_id, skill_name, report.overall_health, report))
        except Exception:
            pass

    # 2. Gaps — new capability gaps worth documenting.
    new_gaps = cycle.get("new_gaps", 0)
    if new_gaps > 0:
        try:
            from .meta.gap_analyzer import analyze
            gap_report = analyze(conn)
            if gap_report and hasattr(gap_report, "new_gaps") and gap_report.new_gaps:
                plans.extend(plan_from_gaps(gap_report.capabilities or []))
        except Exception:
            pass

    # 3. Suggestions — high-severity cross-project suggestions.
    new_suggestions = cycle.get("new_suggestions", 0)
    high_sev = cycle.get("high_severity_suggestions", 0)
    if high_sev > 0:
        try:
            from .cli_suggest import generate_suggestions
            sug_result = generate_suggestions(conn)
            high_sev_sugs = [
                s for s in (sug_result.suggestions or [])
                if getattr(s, "severity", "") == "high"
            ]
            plans.extend(plan_from_suggestions(high_sev_sugs))
        except Exception:
            pass

    # 4. Initiatives — new high-priority initiatives.
    new_pending = cycle.get("new_pending_initiatives", 0)
    if new_pending > 0:
        try:
            from .db import get_pending_initiatives
            initiatives = get_pending_initiatives(conn) or []
            plans.extend(plan_from_initiatives(initiatives[:2]))
        except Exception:
            pass

    # Persist and dispatch each plan.
    for plan in plans:
        plan.auto_level = _resolve_auto_level(plan.action_type)
        can_dispatch = plan.auto_level in ("auto", "notify") and not plan.requires_confirm
        plan.requires_confirm = not can_dispatch

        try:
            _insert_plan(conn, plan)
            result["planned"] += 1
            result["plans"].append({
                "plan_id": plan.plan_id,
                "source": plan.source,
                "action_type": plan.action_type,
                "status": "pending",
                "auto_level": plan.auto_level,
            })

            if can_dispatch:
                _update_plan_status(conn, plan.plan_id, "approved")
                dispatch_result = dispatch_plan(plan, conn)
                if dispatch_result:
                    result["dispatched"] += 1
                    if dispatch_result.get("success"):
                        result["succeeded"] += 1
                        # Collect post-repair drift data so the daemon can
                        # log it and push events without re-querying.
                        post_drift = dispatch_result.get("post_repair_drift")
                        if post_drift:
                            result.setdefault("post_repair_drifts", []).append({
                                "skill_id": dispatch_result.get("skill_id"),
                                "skill_name": dispatch_result.get("skill_name", ""),
                                "strategy": dispatch_result.get("repair", {}).get("strategy", ""),
                                "pre_health": dispatch_result.get("diagnosis", {}).get("health"),
                                "post_health": post_drift.get("health"),
                                "post_success_rate": post_drift.get("success_rate"),
                            })
            else:
                result["pending_confirm"] += 1

        except Exception:
            pass

    if result["planned"] > 0:
        from .daemon import _log
        _log(f"Autonomous planner: {result['planned']} plan(s) created, "
             f"{result['dispatched']} dispatched "
             f"({result['succeeded']} succeeded), "
             f"{result['pending_confirm']} pending confirmation.")

        post_drifts = result.get("post_repair_drifts", [])
        if post_drifts:
            for d in post_drifts:
                sn = d.get("skill_name", "?")
                pre = d.get("pre_health", "?")
                post = d.get("post_health", "?")
                strat = d.get("strategy", "?")
                _log(f"Post-repair drift: skill '{sn}' went from {pre} -> {post} "
                     f"({strat})")

    return result
