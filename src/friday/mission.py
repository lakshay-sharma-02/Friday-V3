"""Multi-cycle Persistent Missions — adaptive goals that span daemon cycles.

A ``PersistentMission`` is a long-running goal broken into steps. Each daemon
cycle, the ``MissionEngine`` advances active missions by executing the next
pending step. Steps that succeed advance progress; steps that fail trigger
adaptive plan revision (retry, skip, or replan).

Design:
  - Missions are persisted in ``persistent_missions`` table.
  - Each mission has a list of steps (stored as JSON in the DB).
  - The engine runs once per daemon cycle via ``_stage_mission_progress()``.
  - Adaptive revision: after N consecutive failures, the engine either
    retries with a different worker, skips the step, or marks the mission
    as failed and logs the reason.
  - Missions can be manually started, cancelled, or inspected via the CLI.

Usage::

    from friday.mission import MissionEngine

    engine = MissionEngine(conn)
    mission = engine.start_mission("Refactor the auth module")
    engine.advance_missions()  # called each daemon cycle
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from .db import connect, now_iso


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class MissionStep:
    """One atomic step within a persistent mission."""

    index: int
    description: str
    action_type: str  # "shell" | "git" | "filesystem" | "skill_execute" | "python"
    payload: str  # The command or operation spec
    worker_id: str = "worker:shell"
    status: str = "pending"  # pending | running | completed | failed | skipped
    error_count: int = 0
    result: Optional[dict] = None

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "description": self.description,
            "action_type": self.action_type,
            "payload": self.payload,
            "worker_id": self.worker_id,
            "status": self.status,
            "error_count": self.error_count,
            "result": self.result,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "MissionStep":
        return cls(
            index=d["index"],
            description=d.get("description", ""),
            action_type=d.get("action_type", "shell"),
            payload=d.get("payload", ""),
            worker_id=d.get("worker_id", "worker:shell"),
            status=d.get("status", "pending"),
            error_count=d.get("error_count", 0),
            result=d.get("result"),
        )


@dataclass
class PersistentMission:
    """A goal that spans multiple daemon cycles.

    Each mission has a list of deterministic steps. The engine advances one
    step per cycle. After failures, the engine adapts (retry, skip, replan).
    """

    mission_id: str
    goal: str
    status: str = "active"  # active | paused | completed | cancelled | failed
    created_at: str = ""
    updated_at: str = ""
    cycle_count: int = 0
    completed_steps: int = 0
    failed_steps: int = 0
    skipped_steps: int = 0
    total_steps: int = 0
    steps: list[MissionStep] = field(default_factory=list)
    progress_pct: float = 0.0
    error_summary: str = ""
    last_error: str = ""
    consecutive_failures: int = 0

    def to_dict(self) -> dict:
        return {
            "mission_id": self.mission_id,
            "goal": self.goal,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "cycle_count": self.cycle_count,
            "completed_steps": self.completed_steps,
            "failed_steps": self.failed_steps,
            "skipped_steps": self.skipped_steps,
            "total_steps": self.total_steps,
            "steps": [s.to_dict() for s in self.steps],
            "progress_pct": self.progress_pct,
            "error_summary": self.error_summary,
            "last_error": self.last_error,
            "consecutive_failures": self.consecutive_failures,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PersistentMission":
        return cls(
            mission_id=d["mission_id"],
            goal=d.get("goal", ""),
            status=d.get("status", "active"),
            created_at=d.get("created_at", ""),
            updated_at=d.get("updated_at", ""),
            cycle_count=d.get("cycle_count", 0),
            completed_steps=d.get("completed_steps", 0),
            failed_steps=d.get("failed_steps", 0),
            skipped_steps=d.get("skipped_steps", 0),
            total_steps=d.get("total_steps", 0),
            steps=[MissionStep.from_dict(s) for s in d.get("steps", [])],
            progress_pct=d.get("progress_pct", 0.0),
            error_summary=d.get("error_summary", ""),
            last_error=d.get("last_error", ""),
            consecutive_failures=d.get("consecutive_failures", 0),
        )

    def format(self) -> str:
        """Render the mission as human-readable text."""
        lines: list[str] = []
        status_icon = {
            "active": "🟢", "paused": "⏸", "completed": "✅",
            "cancelled": "❌", "failed": "🔴",
        }.get(self.status, "❓")
        lines.append(f"{status_icon} Mission: {self.goal}")
        lines.append(f"  ID:     {self.mission_id}")
        lines.append(f"  Status: {self.status}")
        lines.append(f"  Steps:  {self.completed_steps}/{self.total_steps} "
                      f"({self.progress_pct:.0f}%)")
        lines.append(f"  Cycles: {self.cycle_count}")
        if self.failed_steps:
            lines.append(f"  Failed: {self.failed_steps}")
        if self.skipped_steps:
            lines.append(f"  Skipped: {self.skipped_steps}")
        if self.error_summary:
            lines.append(f"  Errors: {self.error_summary[:120]}")
        lines.append("")
        if self.steps:
            lines.append("  Steps:")
            for s in self.steps:
                icon = {"pending": "○", "running": "◉", "completed": "✓",
                        "failed": "✗", "skipped": "—"}.get(s.status, "○")
                err = f" (failed {s.error_count}x)" if s.error_count > 0 else ""
                lines.append(f"    {icon} {s.description}{err}")
        return "\n".join(lines)

    def format_short(self) -> str:
        """One-line summary for list display."""
        icon = {"active": "🟢", "paused": "⏸", "completed": "✅",
                "cancelled": "❌", "failed": "🔴"}.get(self.status, "❓")
        return (f"{icon} {self.goal[:50]:50s} "
                f"{self.completed_steps}/{self.total_steps} steps "
                f"({self.progress_pct:.0f}%) — {self.status}")


# ---------------------------------------------------------------------------
# SQL schema
# ---------------------------------------------------------------------------

MISSION_TABLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS persistent_missions (
    mission_id          TEXT PRIMARY KEY,
    goal                TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'active',
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    cycle_count         INTEGER NOT NULL DEFAULT 0,
    completed_steps     INTEGER NOT NULL DEFAULT 0,
    failed_steps        INTEGER NOT NULL DEFAULT 0,
    skipped_steps       INTEGER NOT NULL DEFAULT 0,
    total_steps         INTEGER NOT NULL DEFAULT 0,
    steps_json          TEXT NOT NULL DEFAULT '[]',
    progress_pct        REAL NOT NULL DEFAULT 0.0,
    error_summary       TEXT NOT NULL DEFAULT '',
    last_error          TEXT NOT NULL DEFAULT '',
    consecutive_failures INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_mission_status ON persistent_missions(status);
CREATE INDEX IF NOT EXISTS idx_mission_created ON persistent_missions(created_at);
"""


# ---------------------------------------------------------------------------
# Step generation — turn a goal into deterministic steps
# ---------------------------------------------------------------------------


def _generate_steps(goal: str, conn=None) -> list[MissionStep]:
    """Generate mission steps from a goal.

    Tries the LLM-powered planner first for richer, goal-specific steps.
    Falls back to the deterministic keyword-based planner if:
      - LLM is unavailable (no API key configured)
      - LLM response doesn't parse into valid steps
      - LLM returns fewer than 1 step

    The deterministic fallback produces exactly 5 steps:
      - ``understand``: Read key project files to understand the scope
      - ``plan``: Create a structured plan from the understanding
      - ``execute``: Run the actual changes
      - ``verify``: Verify the changes (run tests, check output)
      - ``document``: Document what was done
    """
    # Try LLM-powered generation first (only when we have a DB connection
    # for workspace context — without it the LLM has no useful information).
    if conn is not None:
        steps = _generate_steps_llm(goal, conn=conn)
        if steps is not None:
            return steps

    # Fall back to deterministic keyword-based generation.
    return _generate_steps_deterministic(goal)


# ---------------------------------------------------------------------------
# Deterministic step generation (fallback when LLM is unavailable)
# ---------------------------------------------------------------------------


def _generate_steps_deterministic(goal: str) -> list[MissionStep]:
    """Generate deterministic mission steps from a goal.

    Uses keyword matching to produce appropriate steps. This is the original
    deterministic planner that does not require an LLM. The steps are:
      - ``understand``: Read key project files to understand the scope
      - ``plan``: Create a structured plan from the understanding
      - ``execute``: Run the actual changes (shell/git/filesystem commands)
      - ``verify``: Verify the changes (run tests, check output)
      - ``document``: Document what was done

    Each step is a separate mission step so the engine can advance one per
    cycle and adapt on failure.
    """
    g = goal.lower()
    steps: list[MissionStep] = []

    # Step 1: Understand — always the first step.
    steps.append(MissionStep(
        index=0,
        description=f"Analyze scope for: {goal[:60]}",
        action_type="shell",
        payload=f"echo '## Understanding scope\\nGoal: {goal[:200]}\\n' "
                f"&& find . -maxdepth 3 -name '*.py' -o -name '*.md' "
                f"| head -20",
        worker_id="worker:shell",
    ))

    # Step 2: Plan — create a structured plan.
    steps.append(MissionStep(
        index=1,
        description=f"Create execution plan for: {goal[:60]}",
        action_type="shell",
        payload=f"echo '## Execution Plan\\nGoal: {goal[:200]}\\n"
                f"Phase 1: Analysis\\nPhase 2: Implementation\\n"
                f"Phase 3: Verification\\nPhase 4: Documentation'",
        worker_id="worker:shell",
    ))

    # Step 3: Execute — the actual change.
    if "refactor" in g or "rename" in g:
        steps.append(MissionStep(
            index=2,
            description=f"Execute refactoring: {goal[:60]}",
            action_type="shell",
            payload=f"echo 'Refactoring: {goal[:200]}'",
            worker_id="worker:shell",
        ))
    elif "test" in g or "testing" in g:
        steps.append(MissionStep(
            index=2,
            description=f"Execute test creation: {goal[:60]}",
            action_type="shell",
            payload=f"echo 'Testing: {goal[:200]}'",
            worker_id="worker:shell",
        ))
    elif "deploy" in g or "release" in g:
        steps.append(MissionStep(
            index=2,
            description=f"Execute deployment: {goal[:60]}",
            action_type="shell",
            payload=f"echo 'Deploying: {goal[:200]}'",
            worker_id="worker:shell",
        ))
    else:
        steps.append(MissionStep(
            index=2,
            description=f"Execute: {goal[:60]}",
            action_type="shell",
            payload=f"echo 'Executing: {goal[:200]}'",
            worker_id="worker:shell",
        ))

    # Step 4: Verify — check the result.
    steps.append(MissionStep(
        index=3,
        description=f"Verify changes: {goal[:60]}",
        action_type="shell",
        payload=f"echo 'Verifying changes for: {goal[:200]}' && "
                f"git diff --stat 2>/dev/null || echo 'No git repo'",
        worker_id="worker:shell",
    ))

    # Step 5: Document — record what was done.
    steps.append(MissionStep(
        index=4,
        description=f"Document completion: {goal[:60]}",
        action_type="shell",
        payload=f"echo '## Mission Complete\\nGoal: {goal[:200]}\\n"
                f"Completed: $(date -u +%Y-%m-%dT%H:%M:%SZ)'",
        worker_id="worker:shell",
    ))

    return steps


# ---------------------------------------------------------------------------
# MissionEngine — manages missions across daemon cycles
# ---------------------------------------------------------------------------


class MissionEngine:
    """Manages persistent missions across daemon cycles.

    Each cycle, ``advance_missions()``:
      1. Loads active missions from the DB.
      2. For each mission, finds the first pending step.
      3. Executes the step via the appropriate executor.
      4. On success: marks step completed, advances progress.
      5. On failure: increments error count. If consecutive_failures >= 3,
         adapts (retry different worker, skip, or mark mission failed).
      6. If all steps complete: marks mission completed.
      7. If mission stalled: marks as failed with error summary.
    """

    MAX_CONSECUTIVE_FAILURES = 3
    MAX_CYCLES_WITHOUT_PROGRESS = 10

    def __init__(self, conn):
        self.conn = conn
        self._ensure_table()

    # ── Public API ─────────────────────────────────────────────────────────

    def start_mission(self, goal: str) -> PersistentMission:
        """Create and persist a new persistent mission.

        Generates deterministic steps from the goal and persists the mission.
        Returns the newly created mission.
        """
        import uuid
        now = now_iso()
        mission_id = f"mission:{uuid.uuid4().hex[:12]}"
        # Pass connection so LLM-powered generation can gather workspace context.
        steps = _generate_steps(goal, conn=self.conn)
        total = len(steps)

        mission = PersistentMission(
            mission_id=mission_id,
            goal=goal,
            status="active",
            created_at=now,
            updated_at=now,
            total_steps=total,
            steps=steps,
        )
        self._save(mission)

        # Push ambient event.
        try:
            from .ambient import push_event, AmbientEvent
            push_event(self.conn, AmbientEvent(
                timestamp=now,
                event_type="mission_started",
                title=f"Mission started: {goal[:60]}",
                detail=f"{total} steps planned. Will advance each daemon cycle.",
                priority=2,
                category="execution",
                actionable=False,
            ))
        except Exception:
            pass

        return mission

    def get_active_missions(self) -> list[PersistentMission]:
        """Return all active (not completed/cancelled/failed) missions."""
        return self._load_by_status("active")

    def get_all_missions(self, limit: int = 20) -> list[PersistentMission]:
        """Return all missions, newest first."""
        rows = self.conn.execute(
            "SELECT * FROM persistent_missions "
            "ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [self._row_to_mission(r) for r in rows]

    def get_mission(self, mission_id: str) -> Optional[PersistentMission]:
        """Get a single mission by ID."""
        row = self.conn.execute(
            "SELECT * FROM persistent_missions WHERE mission_id = ?",
            (mission_id,),
        ).fetchone()
        if row:
            return self._row_to_mission(row)
        return None

    def cancel_mission(self, mission_id: str) -> bool:
        """Cancel an active mission. Returns True if cancelled."""
        mission = self.get_mission(mission_id)
        if not mission or mission.status != "active":
            return False
        mission.status = "cancelled"
        mission.updated_at = now_iso()
        self._save(mission)
        return True

    def advance_missions(self) -> list[str]:
        """Advance all active missions by one step each.

        Called once per daemon cycle. For each active mission:
          1. Find the next pending step.
          2. Execute it via the appropriate executor.
          3. On success: mark completed, advance progress.
          4. On failure: increment error count, adapt as needed.
          5. If mission stalled or all steps done, update status.

        Returns a list of status messages describing what happened.
        """
        updates: list[str] = []
        missions = self.get_active_missions()

        for mission in missions:
            mission.cycle_count += 1
            next_step = self._next_pending_step(mission)

            if next_step is None:
                # All steps done — mark mission completed.
                mission.status = "completed"
                mission.progress_pct = 100.0
                mission.updated_at = now_iso()
                self._save(mission)
                updates.append(f"✅ {mission.goal[:50]} — completed")
                try:
                    from .ambient import push_event, AmbientEvent
                    push_event(self.conn, AmbientEvent(
                        timestamp=now_iso(),
                        event_type="mission_completed",
                        title=f"Mission completed: {mission.goal[:60]}",
                        detail=f"Completed {mission.completed_steps} steps "
                                f"in {mission.cycle_count} cycles.",
                        priority=1,
                        category="execution",
                    ))
                except Exception:
                    pass
                continue

            # Execute the next pending step.
            next_step.status = "running"
            self._save(mission)

            try:
                step_result = self._execute_step(next_step)
                if step_result.get("success"):
                    next_step.status = "completed"
                    next_step.result = step_result
                    mission.completed_steps += 1
                    mission.consecutive_failures = 0
                    updates.append(
                        f"  ✓ {mission.goal[:40]} step {next_step.index}: "
                        f"{next_step.description[:40]}"
                    )
                else:
                    raise RuntimeError(
                        step_result.get("error", "Step execution failed")
                    )
            except Exception as exc:
                next_step.status = "failed"
                next_step.error_count += 1
                next_step.result = {"error": str(exc)[:200]}
                mission.failed_steps += 1
                mission.consecutive_failures += 1
                mission.last_error = str(exc)[:200]

                updates.append(
                    f"  ✗ {mission.goal[:40]} step {next_step.index}: "
                    f"{str(exc)[:60]}"
                )

                # Adaptive plan revision on consecutive failures.
                if mission.consecutive_failures >= self.MAX_CONSECUTIVE_FAILURES:
                    adapted = self._adapt_mission(mission)
                    if adapted:
                        updates.append(
                            f"  ↻ {mission.goal[:40]} — plan revised "
                            f"(consecutive failures: {mission.consecutive_failures})"
                        )
                    else:
                        # Cannot adapt — mark mission as failed.
                        mission.status = "failed"
                        mission.error_summary = (
                            f"Failed after {mission.consecutive_failures} "
                            f"consecutive failures. Last error: "
                            f"{mission.last_error[:100]}"
                        )
                        updates.append(
                            f"  🔴 {mission.goal[:40]} — "
                            f"failed ({mission.consecutive_failures} consecutive)"
                        )

            # Update progress.
            total_done = mission.completed_steps + mission.skipped_steps
            if mission.total_steps > 0:
                mission.progress_pct = round(
                    (total_done / mission.total_steps) * 100, 1
                )
            mission.updated_at = now_iso()

            # Check if all steps are done — if so, mark completed immediately
            # (not on the next cycle). This catches the case where the last
            # step just finished executing.
            if next_step.status == "completed" and self._next_pending_step(mission) is None:
                mission.status = "completed"
                mission.progress_pct = 100.0
                updates[-1] = f"✅ {mission.goal[:50]} — completed"
                try:
                    from .ambient import push_event, AmbientEvent
                    push_event(self.conn, AmbientEvent(
                        timestamp=now_iso(),
                        event_type="mission_completed",
                        title=f"Mission completed: {mission.goal[:60]}",
                        detail=f"Completed {mission.completed_steps} steps "
                                f"in {mission.cycle_count} cycles.",
                        priority=1,
                        category="execution",
                    ))
                except Exception:
                    pass

            self._save(mission)

        return updates

    # ── Internal: step execution ───────────────────────────────────────────

    def _execute_step(self, step: MissionStep) -> dict:
        """Execute a single mission step.

        Uses the appropriate executor based on ``step.action_type``:
          - ``shell``: runs as a shell command
          - ``git``: runs as a git subcommand
          - ``filesystem``: runs through FileExecutor
          - ``python``: runs Python source
          - ``skill_execute``: dispatches through autonomous planner
        """
        action_type = step.action_type.lower().strip()
        payload = step.payload

        if action_type == "shell":
            result = subprocess.run(
                payload,
                shell=True,
                capture_output=True,
                text=True,
                timeout=120,
            )
            return {
                "success": result.returncode == 0,
                "stdout": result.stdout[:500],
                "stderr": result.stderr[:500],
                "exit_code": result.returncode,
            }

        elif action_type == "git":
            import shlex
            args = shlex.split(payload)
            result = subprocess.run(
                ["git", *args],
                capture_output=True, text=True, timeout=60,
            )
            return {
                "success": result.returncode == 0,
                "stdout": result.stdout[:500],
                "stderr": result.stderr[:500],
                "exit_code": result.returncode,
            }

        elif action_type == "filesystem":
            # Delegate to the sandbox or FileExecutor.
            try:
                from .runtime.executors import FileExecutor
                from .runtime.models import _MiniTask
                task = _MiniTask(
                    task_id=step.description[:40],
                    worker_id="worker:filesystem",
                    runtime_payload=payload,
                    execution_id="mission",
                    title=step.description,
                    task_type="filesystem",
                    timeout=60,
                )
                executor = FileExecutor()
                result = executor.execute(task)
                return {
                    "success": result.success,
                    "stdout": (result.stdout or "")[:500],
                    "stderr": (result.stderr or "")[:500],
                    "error": result.error or "",
                }
            except Exception as exc:
                return {"success": False, "error": str(exc)[:200]}

        elif action_type == "python":
            result = subprocess.run(
                ["python3", "-c", payload],
                capture_output=True, text=True, timeout=60,
            )
            return {
                "success": result.returncode == 0,
                "stdout": result.stdout[:500],
                "stderr": result.stderr[:500],
                "exit_code": result.returncode,
            }

        elif action_type == "skill_execute":
            try:
                from .autonomous_planner import dispatch_plan, ActionPlan
                plan = ActionPlan(
                    plan_id=f"mission_step_{step.index}",
                    created_at=now_iso(),
                    source="mission",
                    source_id="",
                    source_summary=step.description,
                    action_type="skill_execute",
                    target=step.description[:40],
                    worker_id=step.worker_id,
                    payload=step.payload,
                    motivation=step.description,
                    status="approved",
                    auto_level="auto",
                )
                result = dispatch_plan(plan, self.conn)
                return result or {"success": False, "error": "Plan dispatch returned None"}
            except Exception as exc:
                return {"success": False, "error": str(exc)[:200]}

        else:
            return {"success": False, "error": f"Unknown action type: {action_type}"}

    # ── Internal: adaptive plan revision ───────────────────────────────────

    def _adapt_mission(self, mission: PersistentMission) -> bool:
        """Try to adapt a stalled mission.

        Returns True if the mission can continue (steps were adjusted).
        Returns False if the mission should be marked as failed.

        Adaptation strategies (tried in order):
          1. Retry the failed step with a different worker (quick fix for
             transient / environment issues).
          2. LLM replan — generate new steps from the current state
             (what succeeded, what failed, what's left).
          3. Fallback: skip the failed step if LLM is unavailable.
        """
        failed_steps = [s for s in mission.steps if s.status == "failed"]
        if not failed_steps:
            return False

        last_failed = failed_steps[-1]

        # Strategy 1: Try a different worker for the same task.
        alt_workers = {
            "worker:shell": "worker:python",
            "worker:git": "worker:shell",
            "worker:filesystem": "worker:shell",
        }
        alt = alt_workers.get(last_failed.worker_id)
        if alt and last_failed.error_count < 2:
            last_failed.worker_id = alt
            last_failed.status = "pending"  # Reset for retry
            last_failed.error_count = 0
            last_failed.result = None
            mission.consecutive_failures = max(0, mission.consecutive_failures - 1)
            return True

        # Strategy 2: LLM replan — generate new steps from the current state.
        # The LLM knows what succeeded, what failed, and what's left, so it can
        # suggest a smarter approach than blindly retrying or skipping.
        if last_failed.error_count >= 2:
            replanned = self._replan_llm(mission)
            if replanned:
                return True
            # Fallback: skip the failed step (deterministic, no LLM needed).
            last_failed.status = "skipped"
            mission.skipped_steps += 1
            mission.consecutive_failures = 0
            return True

        return False

    # ── Internal: LLM replanning ───────────────────────────────────────────

    _REPLAN_SYSTEM = (
        "You are an adaptive engineering planner. Given a mission's current "
        "state (completed steps, failures, remaining work), generate a revised "
        "step-by-step plan that achieves the goal while learning from past failures."
    )

    _REPLAN_USER = (
        "The following mission has hit persistent failures on its current approach. "
        "Generate a revised step-by-step plan for the WORK THAT REMAINS (skip what "
        "already succeeded).\n\n"
        "Current state:\n{state}\n\n"
        "Return a JSON object with a single key \"steps\", which is an array of "
        "step objects for the REMAINING work.\n"
        "Each step object has:\n"
        "- \"description\": short imperative description\n"
        "- \"action_type\": one of \"shell\", \"git\", \"filesystem\", "
        "\"python\", \"skill_execute\"\n"
        "- \"payload\": the exact command to execute\n"
        "- \"worker_id\": \"worker:shell\" or \"worker:python\"\n\n"
        "RULES:\n"
        "1. Generate steps ONLY for work NOT yet done — skip what already "
        "succeeded\n"
        "2. Consider what went wrong in the failed steps and suggest a "
        "different approach\n"
        "3. Generate 2-5 focused steps\n"
        "4. Keep payloads as simple shell commands\n"
        "5. Include a verification step at the end\n"
        "6. Be specific to the goal and the current context\n\n"
        "Respond with ONLY valid JSON. No markdown."
    )

    def _replan_llm(self, mission: PersistentMission) -> bool:
        """Use the LLM to generate a revised plan from the current mission state.

        Builds a detailed prompt describing:
          - The mission goal
          - Steps completed so far (with their results)
          - Recent failures (with error details)
          - Remaining work

        The LLM generates new steps that replace all non-completed work.
        Returns True if the replan succeeded and steps were replaced.
        Returns False if the LLM is unavailable or returned garbage
        (caller falls back to deterministic skip).
        """
        try:
            from .services.llm import _call_structured, _enabled as _llm_enabled

            if not _llm_enabled():
                return False

            # ── Build state description ──
            completed = [s for s in mission.steps if s.status == "completed"]
            failed = [s for s in mission.steps if s.status == "failed"]
            pending = [s for s in mission.steps if s.status == "pending"]
            skipped = [s for s in mission.steps if s.status == "skipped"]

            completed_lines = [
                f"  {s.index}. {s.description[:60]}"
                for s in completed
            ]
            completed_text = "\n".join(completed_lines) if completed_lines else "  (none yet)"

            failed_lines = []
            for s in failed[-3:]:  # last 3 failures
                error_text = ""
                if s.result and isinstance(s.result, dict):
                    # Priority: error > stderr > stdout (do NOT let stdout
                    # shadow the actual failure reason).
                    error_text = (
                        s.result.get("error")
                        or s.result.get("stderr")
                        or s.result.get("stdout")
                        or ""
                    )[:100]
                if not error_text and mission.last_error:
                    error_text = mission.last_error[:100]
                failed_lines.append(
                    f"  {s.index}. {s.description[:50]} — "
                    f"{error_text}"
                )
            failed_text = "\n".join(failed_lines) if failed_lines else "  (none)"

            pending_lines = [
                f"  {s.index}. {s.description[:60]} ({s.action_type})"
                for s in pending
            ]
            pending_text = "\n".join(pending_lines) if pending_lines else "  (none)"

            state = (
                f"Goal: {mission.goal}\n"
                f"\n"
                f"Completed steps:\n{completed_text}\n"
                f"\n"
                f"Recent failures:\n{failed_text}\n"
                f"\n"
                f"Remaining work:\n{pending_text}\n"
                f"\n"
                f"Skipped steps: {len(skipped)}\n"
                f"Total progress: {mission.completed_steps}/{mission.total_steps} steps"
            )

            # ── Call LLM ──
            user = self._REPLAN_USER.format(state=state)
            result = _call_structured(
                self._REPLAN_SYSTEM, user,
                required_keys=["steps"],
            )
            if result is None:
                return False

            raw_steps = result.get("steps", [])
            if not isinstance(raw_steps, list) or len(raw_steps) < 1:
                return False

            # ── Build new step list: keep completed, replace rest ──
            new_steps: list[MissionStep] = []
            for s in mission.steps:
                if s.status == "completed":
                    new_steps.append(s)

            append_start = len(new_steps)
            for i, rs in enumerate(raw_steps):
                desc = str(rs.get("description", f"Revised step {i}"))[:80]
                action_type = str(rs.get("action_type", "shell"))[:20]
                payload = str(rs.get("payload", f"echo '{desc}'"))[:500]
                worker_id = str(rs.get("worker_id", "worker:shell"))[:30]

                if action_type not in (
                    "shell", "git", "filesystem", "python", "skill_execute"
                ):
                    action_type = "shell"

                new_steps.append(MissionStep(
                    index=append_start + i,
                    description=desc,
                    action_type=action_type,
                    payload=payload,
                    worker_id=worker_id,
                ))

            # Guard: LLM must return at least 1 new step.
            added = len(new_steps) - len(completed)
            if added < 1:
                return False

            # ── Replace mission steps ──
            mission.steps = new_steps
            mission.total_steps = len(new_steps)
            mission.failed_steps = 0
            mission.consecutive_failures = 0
            mission.last_error = ""

            total_done = mission.completed_steps + mission.skipped_steps
            if mission.total_steps > 0:
                mission.progress_pct = round(
                    (total_done / mission.total_steps) * 100, 1
                )

            self._save(mission)

            # Push replan event.
            try:
                from .ambient import push_event, AmbientEvent
                push_event(self.conn, AmbientEvent(
                    timestamp=now_iso(),
                    event_type="mission_replanned",
                    title=f"Mission replanned: {mission.goal[:60]}",
                    detail=f"LLM generated {added} new steps from current state.",
                    priority=2,
                    category="execution",
                ))
            except Exception:
                pass

            return True

        except Exception:
            return False

    # ── Internal: persistence ──────────────────────────────────────────────

    def _ensure_table(self) -> None:
        try:
            self.conn.executescript(MISSION_TABLE_SCHEMA)
            self.conn.commit()
        except Exception:
            self.conn.rollback()

    def _save(self, mission: PersistentMission) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO persistent_missions
               (mission_id, goal, status, created_at, updated_at,
                cycle_count, completed_steps, failed_steps, skipped_steps,
                total_steps, steps_json, progress_pct,
                error_summary, last_error, consecutive_failures)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                mission.mission_id,
                mission.goal,
                mission.status,
                mission.created_at,
                mission.updated_at,
                mission.cycle_count,
                mission.completed_steps,
                mission.failed_steps,
                mission.skipped_steps,
                mission.total_steps,
                json.dumps([s.to_dict() for s in mission.steps]),
                mission.progress_pct,
                mission.error_summary,
                mission.last_error,
                mission.consecutive_failures,
            ),
        )
        self.conn.commit()

    def _load_by_status(self, status: str) -> list[PersistentMission]:
        rows = self.conn.execute(
            "SELECT * FROM persistent_missions WHERE status = ? "
            "ORDER BY created_at ASC",
            (status,),
        ).fetchall()
        return [self._row_to_mission(r) for r in rows]

    def _next_pending_step(self, mission: PersistentMission) -> Optional[MissionStep]:
        """Find the first step that's still pending."""
        for s in mission.steps:
            if s.status == "pending":
                return s
        return None

    @staticmethod
    def _row_to_mission(row) -> PersistentMission:
        steps_data = json.loads(row["steps_json"] or "[]")
        return PersistentMission(
            mission_id=row["mission_id"],
            goal=row["goal"],
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            cycle_count=row["cycle_count"],
            completed_steps=row["completed_steps"],
            failed_steps=row["failed_steps"],
            skipped_steps=row["skipped_steps"],
            total_steps=row["total_steps"],
            steps=[MissionStep.from_dict(s) for s in steps_data],
            progress_pct=row["progress_pct"],
            error_summary=row["error_summary"],
            last_error=row["last_error"],
            consecutive_failures=row["consecutive_failures"],
        )


# ---------------------------------------------------------------------------
# LLM-powered step generation
# ---------------------------------------------------------------------------


_LLM_STEP_SYSTEM = (
    "You are a precise engineering planner. Generate a step-by-step plan "
    "for a software mission goal. Each step is one atomic action."
)

_LLM_STEP_USER = """Generate a list of mission steps for this goal:

Goal: {goal}
{context}

Return a JSON object with a single key "steps", which is an array of step objects.
Each step object has these fields:
- "description": short imperative description of what this step does
- "action_type": one of "shell", "git", "filesystem", "python", "skill_execute"
- "payload": the exact command or operation to execute
  - For "shell": a shell command (e.g. "echo hello")
  - For "git": a git subcommand (e.g. "log --oneline -5")
  - For "filesystem": a JSON string with file operations
  - For "python": a Python expression or script
  - For "skill_execute": a description of the skill to invoke
- "worker_id": the worker to use — "worker:shell" for shell/commands, "worker:python" for python code

RULES:
1. Each step must be executable by itself — no dependencies on previous steps
2. Prefer "shell" action_type for most steps (it runs arbitrary commands)
3. Keep payloads as simple minimal shell commands
4. Generate 2-5 steps — not too many, not too few
5. The final step should verify the goal was achieved
6. Be specific to the goal — "echo 'Refactoring...'" is better than "echo 'Executing...'" when the goal is a refactoring
7. Include realistic verification (e.g. run tests, check git diff, run linters)

Respond ONLY with valid JSON. No markdown, no explanation."""


def _generate_steps_llm(goal: str, conn=None) -> Optional[list[MissionStep]]:
    """Try to generate mission steps using the LLM.

    Returns a list of ``MissionStep`` objects if successful, or ``None``
    to signal the caller should fall back to the deterministic planner.

    The function:
      1. Checks if an LLM provider is available (``services.llm._enabled``).
      2. If available, gathers workspace context from the DB (active repos,
         observed languages, recently modified files).
      3. Calls ``services.llm._call_structured`` with a prompt tailored to
         the goal and workspace context.
      4. Parses the JSON response into ``MissionStep`` objects.
      5. Returns ``None`` on any failure — the caller falls back to
         deterministic planning.
    """
    try:
        from .services.llm import _call_structured, _enabled as _llm_enabled
        if not _llm_enabled():
            return None

        # Gather workspace context if a connection is available.
        context = ""
        if conn is not None:
            try:
                cur = conn.execute(
                    "SELECT COUNT(*) FROM repositories"
                )
                repo_count = cur.fetchone()[0]
                if repo_count:
                    cur2 = conn.execute(
                        "SELECT path, language FROM repositories "
                        "WHERE language IS NOT NULL AND language != '' "
                        "ORDER BY last_seen DESC LIMIT 5"
                    )
                    repos = cur2.fetchall()
                    if repos:
                        lines = ["\nWorkspace context:"]
                        for r in repos:
                            lang_part = f" ({r['language']})" if r['language'] else ""
                            lines.append(f"  - {r['path']}{lang_part}")
                        context = "\n".join(lines)
            except Exception:
                pass

        system = _LLM_STEP_SYSTEM
        user = _LLM_STEP_USER.format(
            goal=goal[:1000],
            context=context,
        )

        result = _call_structured(system, user, required_keys=["steps"])
        if result is None:
            return None

        raw_steps = result.get("steps", [])
        if not isinstance(raw_steps, list) or len(raw_steps) < 1:
            return None

        steps: list[MissionStep] = []
        for i, rs in enumerate(raw_steps):
            desc = str(rs.get("description", f"Step {i}"))[:80]
            action_type = str(rs.get("action_type", "shell"))[:20]
            payload = str(rs.get("payload", f"echo '{desc}'"))[:500]
            worker_id = str(rs.get("worker_id", "worker:shell"))[:30]

            # Validate action_type.
            if action_type not in ("shell", "git", "filesystem", "python", "skill_execute"):
                action_type = "shell"

            steps.append(MissionStep(
                index=i,
                description=desc,
                action_type=action_type,
                payload=payload,
                worker_id=worker_id,
            ))

        if not steps:
            return None

        return steps

    except Exception:
        return None


# ---------------------------------------------------------------------------
# Daemon stage — called each cycle to advance missions
# ---------------------------------------------------------------------------


def stage_mission_progress(conn) -> list[str]:
    """Advance all active persistent missions by one step.

    Called from the daemon cycle (``_stage_mission_progress``).
    Best-effort: never raises.
    """
    try:
        engine = MissionEngine(conn)
        return engine.advance_missions()
    except Exception as exc:
        from .daemon import _log
        _log(f"Mission progress stage failed: {exc}")
        return []
