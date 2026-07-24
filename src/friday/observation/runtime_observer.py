"""RuntimeObserver (Law 17 — Learning Loop).

Reads completed runtime sessions and their review verdicts (already-persisted,
already-objective data in runtime_sessions / runtime_tasks / runtime_results /
task_graphs / tasks tables) and emits Observation facts from them, exactly the
way GitObserver turns `git log` into Observation facts.

Design:
- Learning is not a new layer. It is a new Observer.
- No changes to Observation engine, Context, Knowledge, or anything frozen.
- Idempotent via a dedicated observed_session_ids table (Law 24 versioned,
  Law 18 layer-boundary compliant) — no timestamp mismatches possible.
- Facts carry correct Confidence per the spec:
    OBSERVED — outcomes read directly from runtime/review tables.
    DERIVED  — capability_reliability computed from task outcomes.
    INFERRED — never used here (Review already did judgment work).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from ..db import (
    get_runtime_sessions,
    get_runtime_tasks,
    get_runtime_results,
    get_task_graph_by_id,
    get_tasks_for_graph,
)
from .interface import Health, Observer, ObserverHealth
from .model import Confidence, Observation, now_iso


class RuntimeObserver(Observer):
    """Observes completed runtime execution sessions and emits factual outcomes.

    Reads already-persisted runtime/review tables only. Never executes, never
    writes to Knowledge directly, never calls an LLM.

    Idempotency: tracks observed session IDs in a dedicated per-observer table
    (observed_session_ids) rather than timestamp-based watermarks. This avoids
    the clock-mismatch off-by-one issue inherent in comparing a session's
    finished_at against an observation run's timestamp.
    """

    name = "runtime"

    # ------------------------------------------------------------------
    # Observer interface
    # ------------------------------------------------------------------

    def collect(self, conn) -> list[Observation]:
        """Return observations for completed runtime sessions not yet observed.

        Idempotency: tracks observed session IDs in the observed_session_ids
        table (composite PK on observer_name + session_id). New rows are
        inserted atomically alongside observation persistence so the cursor
        and its facts are always consistent.
        """
        observed_at = now_iso()
        observed_ids = self._load_observed_ids(conn)
        new_ids: list[str] = []
        rows: list[Observation] = []

        sessions = get_runtime_sessions(conn)
        for sess in sessions:
            if sess.get("state") != "finished":
                continue
            session_id = sess["session_id"]
            if session_id in observed_ids:
                continue
            finished_at = sess.get("finished_at")
            if not finished_at:
                continue

            schedule_id = sess.get("schedule_id", "")
            rows.extend(self._session_facts(
                conn, observed_at, session_id, schedule_id,
            ))
            new_ids.append(session_id)

        # Advance cursor: persist the new session IDs into the dedicated table.
        if new_ids:
            self._save_observed_ids(conn, observed_at, new_ids)

        return rows

    def summarize(self, conn) -> str:
        """One-line summary of observed runtime sessions."""
        sessions = get_runtime_sessions(conn)
        finished = [s for s in sessions if s.get("state") == "finished"]
        observed_ids = self._load_observed_ids(conn)
        unobserved = [s for s in finished if s["session_id"] not in observed_ids]

        if not finished:
            return "runtime: no completed execution sessions."
        total_tasks = sum(
            len(get_runtime_tasks(conn, s["session_id"])) for s in finished
        )
        return (
            f"runtime: {len(finished)} completed session(s), "
            f"{total_tasks} task(s) executed; "
            f"{len(unobserved)} new session(s) pending observation."
        )

    def health(self, conn) -> ObserverHealth:
        """Report healthy if we can read the runtime tables."""
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM runtime_sessions"
            ).fetchone()
            if row is None:
                return ObserverHealth.down(
                    self, "runtime_sessions table empty or missing.",
                    method="SELECT COUNT(*) FROM runtime_sessions",
                )
            return ObserverHealth(
                True, Health.HEALTHY,
                method="SELECT COUNT(*) FROM runtime_sessions",
                detail=f"{row['c']} session(s) in table.",
            )
        except Exception as exc:
            return ObserverHealth(
                False, Health.DOWN,
                method="SELECT COUNT(*) FROM runtime_sessions",
                detail=f"runtime_sessions table unavailable: {exc}",
            )

    # ------------------------------------------------------------------
    # Cursor helpers (dedicated observed_session_ids table — Law 18/24)
    # ------------------------------------------------------------------

    def _load_observed_ids(self, conn) -> set[str]:
        """Read the set of already-observed session IDs from the dedicated
        observed_session_ids table (observer_name = 'runtime').

        Returns an empty set on first run or if the table doesn't exist yet
        (CREATE IF NOT EXISTS handles idempotent creation on connect).
        """
        rows = conn.execute(
            "SELECT session_id FROM observed_session_ids WHERE observer_name = ?",
            (self.name,),
        ).fetchall()
        return {r["session_id"] for r in rows}

    def _save_observed_ids(self, conn, observed_at: str, new_ids: list[str]) -> None:
        """Persist newly observed session IDs into the observed_session_ids
        table. Runs inside the engine's transaction alongside insert_observations,
        so the cursor advances at the same logical point as observation
        persistence. INSERT OR REPLACE is used for idempotency within a single
        run (defensive — shouldn't collide in practice since we filter by
        unobserved IDs first).

        Also cleans up any stale legacy cursor entry from operator_preferences
        (Part 2's storage approach) — idempotent, safe even if no such entry
        exists.
        """
        # Clean up legacy operator_preferences cursor if it exists.
        conn.execute(
            "DELETE FROM operator_preferences WHERE key = ?",
            ("runtime_observer_observed_sessions",),
        )
        # Batch insert new session IDs.
        conn.executemany(
            """INSERT OR REPLACE INTO observed_session_ids
               (observer_name, session_id, observed_at, schema_version)
               VALUES (?, ?, ?, ?)""",
            [(self.name, sid, observed_at, "1") for sid in new_ids],
        )

    # ------------------------------------------------------------------
    # Fact builders
    # ------------------------------------------------------------------

    def _obs(
        self, at: str, subject: str, aspect: str, value: str,
        scope: str = "", conf: Confidence = Confidence.OBSERVED,
        cause: Optional[str] = None,
    ) -> Observation:
        return Observation(
            source=self.name, subject=subject, aspect=aspect, value=value,
            confidence=conf, observed_at=at, scope=scope, cause=cause,
        )

    def _session_facts(
        self, conn, observed_at: str, session_id: str, schedule_id: str,
    ) -> list[Observation]:
        """Emit facts for one completed runtime session."""
        rows: list[Observation] = []
        tasks = get_runtime_tasks(conn, session_id)
        results = get_runtime_results(conn, session_id)
        results_by_task: dict[str, dict] = {}
        for r in results:
            tid = r.get("task_id", "")
            if tid:
                results_by_task[tid] = r

        # Resolve graph ID from schedule_id (schedule_id IS the graph ID in the schema).
        graph_id = schedule_id
        # Get the graph's goal for scope.
        graph_goal = ""
        try:
            graph_row = get_task_graph_by_id(conn, graph_id)
            if graph_row:
                graph_goal = graph_row.goal
        except Exception:
            pass

        # Derive graph-level execution outcome from task states.
        outcome = self._compute_outcome(tasks)

        # --- Graph-level facts ---
        rows.append(self._obs(
            observed_at, graph_id, "execution_outcome", outcome,
            scope=graph_goal, conf=Confidence.OBSERVED,
            cause=(f"Session {session_id}: {outcome} "
                   f"({len([t for t in tasks if t['status'] == 'success'])}"
                   f"/{len(tasks)} tasks succeeded)."),
        ))

        # Repair required: any task with verification_passed=0?
        repair = self._check_repair_needed(tasks, results_by_task)
        if repair:
            rows.append(self._obs(
                observed_at, graph_id, "repair_required", "true",
                scope=graph_goal, conf=Confidence.OBSERVED,
                cause=(f"Session {session_id}: at least one task failed "
                       f"verification, indicating repair was needed."),
            ))
        else:
            rows.append(self._obs(
                observed_at, graph_id, "repair_required", "false",
                scope=graph_goal, conf=Confidence.OBSERVED,
                cause=(f"Session {session_id}: all tasks passed verification."),
            ))

        # --- Per-task facts ---
        for task in tasks:
            tid = task["task_id"]
            task_status = task.get("status", "unknown")
            # Map runtime task status to outcome value.
            if task_status == "success":
                task_outcome = "success"
            elif task_status == "failed":
                task_outcome = "failed"
            else:
                # cancelled, pending, running — still emit as-is.
                task_outcome = task_status

            rows.append(self._obs(
                observed_at, tid, "task_outcome", task_outcome,
                scope=graph_id, conf=Confidence.OBSERVED,
                cause=(f"Session {session_id}: task {tid} "
                       f"finished with status {task_status}."),
            ))

        # --- Capability reliability facts (DERIVED) ---
        rows.extend(self._capability_reliability(
            conn, observed_at, graph_id, tasks,
        ))

        return rows

    def _compute_outcome(self, tasks: list[dict]) -> str:
        """Derive graph-level outcome: success / failed / cancelled."""
        if not tasks:
            return "cancelled"
        # Have to check for any failure first.
        has_failure = any(t.get("status") == "failed" for t in tasks)
        has_cancelled = any(t.get("status") == "cancelled" for t in tasks)
        all_success = all(t.get("status") == "success" for t in tasks)

        if all_success:
            return "success"
        elif has_failure:
            return "failed"
        elif has_cancelled:
            return "cancelled"
        else:
            # Mixed non-terminal states (shouldn't happen for finished sessions).
            return "failed"

    @staticmethod
    def _check_repair_needed(
        tasks: list[dict], results_by_task: dict[str, dict],
    ) -> bool:
        """Check if any task in the session needed repair (verification failed)."""
        for task in tasks:
            tid = task["task_id"]
            if tid in results_by_task:
                vp = results_by_task[tid].get("verification_passed")
                if vp is not None and vp == 0:
                    return True
            if task.get("status") == "failed":
                return True
        return False

    def _capability_reliability(
        self, conn, observed_at: str, graph_id: str, tasks: list[dict],
    ) -> list[Observation]:
        """Compute DERIVED capability reliability from task outcomes.

        Reads the original task definitions (from tasks table) to get
        required_capabilities. Cross-references with runtime task outcomes
        to emit "{success_count}/{total_count} success" per capability.
        """
        rows: list[Observation] = []
        try:
            graph_tasks = get_tasks_for_graph(conn, graph_id)
        except Exception:
            return rows

        # Build map: original task_id -> capabilities list
        task_caps: dict[str, list[str]] = {}
        for t in graph_tasks:
            caps_str = (t.required_capabilities or "").strip()
            if caps_str:
                task_caps[t.id] = [c.strip() for c in caps_str.split(",") if c.strip()]

        # Map runtime task_id -> outcome (only terminal states)
        task_outcomes: dict[str, str] = {}
        for t in tasks:
            status = t.get("status", "")
            if status in ("success", "failed"):
                task_outcomes[t["task_id"]] = status

        # Count successes and total per capability
        cap_success: dict[str, int] = {}
        cap_total: dict[str, int] = {}
        for tid, caps in task_caps.items():
            outcome = task_outcomes.get(tid)
            if outcome is None:
                continue
            for cap in caps:
                cap_total[cap] = cap_total.get(cap, 0) + 1
                if outcome == "success":
                    cap_success[cap] = cap_success.get(cap, 0) + 1

        for cap in sorted(cap_total):
            total = cap_total[cap]
            successes = cap_success.get(cap, 0)
            value = f"{successes}/{total} success"
            rows.append(self._obs(
                observed_at, cap, "capability_reliability", value,
                conf=Confidence.DERIVED,
                cause=(f"Capability '{cap}' had {successes}/{total} "
                       f"successful task executions in graph {graph_id}."),
            ))

        return rows
