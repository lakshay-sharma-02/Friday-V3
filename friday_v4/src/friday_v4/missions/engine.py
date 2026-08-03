"""MissionEngine — persistent missions that actually make progress (Wave 9).

The orchestrator over the V4 ``missions`` tables. It creates goals as
persistent missions, advances them step by step *through the execution
layer* (``friday_v4.execution.execute`` — gate → sandbox → audit), and
never silently changes a plan: adaptation always reports *why*.

Design rules (per WAVE_9_AGENCY_CORE.md §4.3):
- Missions persist in the V4 DB; restart-safe (state is always reloaded).
- Steps with an ``action_type`` execute through ``execution.execute``;
  steps without one are *manual* (operator completes them).
- **Adaptation is explicit:** ``adapt`` / ``replan`` replace the step
  list and return an :class:`AdaptationReport` saying
  "plan changed because …" — a mission never silently rewrites itself.
- Never crash: every method degrades gracefully (returns None/False/{}).

Usage:
    engine = MissionEngine(conn)
    mission = engine.create("ship the auth refactor", planner=Planner())
    engine.start(mission.id)
    result = engine.advance(mission.id, confirm_fn=confirm)   # executes a step
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from .. import db
from .claude_planner import make_planner
from .models import (
    Mission,
    MissionStatus,
    MissionStep,
    StepStatus,
    make_step_payload,
)
from .planner import Planner, StepPlan
from .scheduler import Scheduler

logger = logging.getLogger("friday_v4.missions.engine")


@dataclass
class AdvanceResult:
    """Outcome of advancing a mission one step."""

    mission_id: str
    action: str                    # executed | manual_completed | none_pending | denied | failed | not_active
    step_id: Optional[str] = None
    execution: Optional[dict] = None   # execution result (to_dict) when run
    message: str = ""


@dataclass
class AdaptationReport:
    """Why and how a mission's plan changed."""

    mission_id: str
    changed: bool
    reason: str
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    message: str = ""


class MissionEngine:
    """Persistent missions over the V4 DB, advancing through execution."""

    def __init__(self, conn,
                 planner: Optional[Planner] = None,
                 scheduler: Optional[Scheduler] = None,
                 cwd: Optional[str] = None) -> None:
        self.conn = conn
        # The planner default is the single ``make_planner`` point: with
        # ``FRIDAY_V4_CLAUDE_PLANNER`` set, ``create``/``replan``
        # decompose through the Claude Code CLI (gated/sandboxed/
        # audited); otherwise the deterministic planner stands. ``cwd``
        # roots the claude subprocess (the workspace being acted on).
        self.planner = planner or make_planner(cwd=cwd, conn=conn)
        self.scheduler = scheduler or Scheduler()

    # ── Lifecycle ─────────────────────────────────────────────────────

    def create(self, goal: str, title: Optional[str] = None,
               description: str = "", priority: str = "medium",
               plan: Optional[list[StepPlan]] = None,
               cwd: Optional[str] = None,
               schedule: bool = True) -> Optional[Mission]:
        """Create a persistent mission from a goal.

        ``plan`` may be supplied directly; otherwise the goal is passed
        through the planner. Returns the Mission (or None on DB failure).
        """
        steps = plan if plan is not None else self.planner.plan(goal, cwd=cwd)
        mid = db.create_mission(
            self.conn, title or goal, description=description,
            priority=priority, status=MissionStatus.PLANNED.value)
        if not mid:
            return None

        step_ids: list[str] = []
        for i, step in enumerate(steps):
            sid = db.add_mission_step(
                self.conn, mid, step.title, position=i,
                payload=step.to_payload())
            if sid:
                step_ids.append(sid)
        if not step_ids and steps:
            db.delete_mission(self.conn, mid)
            return None

        if schedule:
            self._apply_schedule(mid)
        return self.get(mid)

    def _apply_schedule(self, mission_id: str) -> None:
        mission = self.get(mission_id)
        if not mission or not mission.steps:
            return
        times = self.scheduler.schedule(mission.steps)
        for step in mission.steps:
            if step.id in times:
                payload = dict(step.payload)
                payload["scheduled_at"] = times[step.id]
                db.update_mission_step(self.conn, step.id, payload=payload)

    def get(self, mission_id: str) -> Optional[Mission]:
        row = db.get_mission(self.conn, mission_id)
        if not row:
            return None
        steps = [MissionStep.from_row(s)
                 for s in db.list_mission_steps(self.conn, mission_id)]
        return Mission.from_row(row, steps=steps)

    def list(self, status: Optional[str] = None,
             limit: int = 50) -> list[Mission]:
        rows = db.list_missions(self.conn, status=status, limit=limit)
        return [self.get(r["id"]) for r in rows if r.get("id")]

    def set_status(self, mission_id: str, status: MissionStatus) -> bool:
        return bool(db.update_mission(self.conn, mission_id,
                                      status=status.value))

    def start(self, mission_id: str) -> bool:
        return self.set_status(mission_id, MissionStatus.ACTIVE)

    def pause(self, mission_id: str) -> bool:
        return self.set_status(mission_id, MissionStatus.PAUSED)

    def cancel(self, mission_id: str) -> bool:
        return self.set_status(mission_id, MissionStatus.CANCELLED)

    def complete(self, mission_id: str) -> bool:
        return self.set_status(mission_id, MissionStatus.COMPLETED)

    def delete(self, mission_id: str) -> bool:
        return bool(db.delete_mission(self.conn, mission_id))

    # ── Advancement ───────────────────────────────────────────────────

    def next_step(self, mission_id: str) -> Optional[MissionStep]:
        """The next step to run (scheduler-aware), or None."""
        mission = self.get(mission_id)
        if not mission:
            return None
        return self.scheduler.next_due(mission.steps)

    def advance(self, mission_id: str, *,
                confirm_fn=None, force: bool = False,
                manual_result: str = "") -> AdvanceResult:
        """Run the next step of a mission (through execution).

        - Executable steps → ``friday_v4.execution.execute``.
        - Manual steps → marked completed with ``manual_result``.
        - A completed final step completes the mission; a failed step
          fails it (adapt to recover).

        ``confirm_fn`` / ``force`` pass through to the execution gate.
        """
        mission = self.get(mission_id)
        if not mission:
            return AdvanceResult(mission_id, "none_pending",
                                 message="mission not found")
        if mission.status != MissionStatus.ACTIVE:
            return AdvanceResult(mission_id, "not_active",
                                 message=f"mission is {mission.status.value}")

        step = self.next_step(mission_id)
        if not step:
            self._maybe_finish(mission_id)
            return AdvanceResult(mission_id, "none_pending",
                                 message="all steps done")

        if step.is_executable:
            return self._advance_executable(mission, step,
                                            confirm_fn=confirm_fn,
                                            force=force)
        return self._advance_manual(mission, step, manual_result)

    def _advance_executable(self, mission: Mission, step: MissionStep, *,
                            confirm_fn, force: bool) -> AdvanceResult:
        db.update_mission_step(self.conn, step.id,
                               status=StepStatus.RUNNING.value)
        try:
            from ..execution import execute
            result = execute(
                step.action_type, step.command,
                cwd=step.cwd or None,
                conn=self.conn,
                confirm_fn=confirm_fn,
                force=force,
                goal=step.title,
            )
        except Exception as exc:  # defensive — never crash
            logger.warning(f"mission {mission.id} step {step.id} "
                           f"execution failed: {exc}")
            db.update_mission_step(self.conn, step.id,
                                   status=StepStatus.FAILED.value,
                                   result=str(exc))
            self.set_status(mission.id, MissionStatus.FAILED)
            return AdvanceResult(mission.id, "failed", step_id=step.id,
                                 message=str(exc))

        outcome = result.to_dict() if hasattr(result, "to_dict") else {}
        status = outcome.get("status", "failed")
        if status == "succeeded":
            db.update_mission_step(self.conn, step.id,
                                   status=StepStatus.COMPLETED.value,
                                   result=outcome.get("output", ""))
            self._maybe_finish(mission.id)
            return AdvanceResult(mission.id, "executed", step_id=step.id,
                                 execution=outcome,
                                 message="step executed successfully")
        if status == "denied":
            db.update_mission_step(self.conn, step.id,
                                   status=StepStatus.PENDING.value,
                                   result="denied by gate")
            return AdvanceResult(mission.id, "denied", step_id=step.id,
                                 execution=outcome,
                                 message="operator declined the action")
        # failed / timed_out
        db.update_mission_step(self.conn, step.id,
                               status=StepStatus.FAILED.value,
                               result=outcome.get("output", "") or
                                      outcome.get("error", "") or status)
        self.set_status(mission.id, MissionStatus.FAILED)
        return AdvanceResult(mission.id, "failed", step_id=step.id,
                             execution=outcome, message=status)

    def _advance_manual(self, mission: Mission, step: MissionStep,
                        manual_result: str) -> AdvanceResult:
        db.update_mission_step(self.conn, step.id,
                               status=StepStatus.COMPLETED.value,
                               result=manual_result or "completed by operator")
        self._maybe_finish(mission.id)
        return AdvanceResult(mission.id, "manual_completed", step_id=step.id,
                             message="manual step completed by operator")

    def _maybe_finish(self, mission_id: str) -> None:
        """Complete the mission when every step is done."""
        mission = self.get(mission_id)
        if not mission or not mission.steps:
            return
        if all(s.status == StepStatus.COMPLETED for s in mission.steps):
            self.set_status(mission_id, MissionStatus.COMPLETED)

    # ── Adaptation ────────────────────────────────────────────────────

    def adapt(self, mission_id: str, plan: list[StepPlan],
              reason: str = "mission replanned") -> AdaptationReport:
        """Replace a mission's steps, reporting the change explicitly.

        The mission's plan is rebuilt from ``plan`` (planner output);
        the returned report says exactly what changed and why.
        """
        mission = self.get(mission_id)
        if not mission:
            return AdaptationReport(mission_id, False, reason,
                                    message="mission not found")

        old_titles = {s.title for s in mission.steps}
        new_titles = [s.title for s in plan]
        added = [t for t in new_titles if t not in old_titles]
        removed = [t for t in old_titles if t not in new_titles]

        db.delete_mission_steps(self.conn, mission_id)
        for i, step in enumerate(plan):
            db.add_mission_step(self.conn, mission_id, step.title,
                                position=i, payload=step.to_payload())

        # Restart progress tracking: reset to active so it can advance.
        self.set_status(mission_id, MissionStatus.ACTIVE)
        self._apply_schedule(mission_id)

        changed = bool(added or removed) or len(new_titles) != len(old_titles)
        msg = f"plan changed because: {reason}"
        if added:
            msg += f" · added {len(added)} step(s)"
        if removed:
            msg += f" · removed {len(removed)} step(s)"
        return AdaptationReport(mission_id, changed, reason,
                                added=added, removed=removed, message=msg)

    def replan(self, mission_id: str, goal: str, reason: str,
               cwd: Optional[str] = None) -> AdaptationReport:
        """Re-run the planner on a (new) goal and adapt the mission."""
        plan = self.planner.plan(goal, cwd=cwd)
        return self.adapt(mission_id, plan, reason=reason)


__all__ = ["MissionEngine", "AdvanceResult", "AdaptationReport"]
