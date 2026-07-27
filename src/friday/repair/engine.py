"""Repair Engine (Law 16 — Repair Is Evidence Driven).

Detects failed runtime sessions from Review verdicts, evaluates whether a
repair attempt is warranted (not recurring bottleneck, within depth cap),
drafts proposals for human approval, and — on approval — re-enters the
normal Planning pipeline with a `source="repair:<...>"` tag.

Key design: Repair is NOT a new execution path. It is a new Planning cycle
triggered by a failed Review verdict, going through the exact same pipeline
(Planning → Task Graph → Resolver → Scheduler → Runtime → Review).
"""

from __future__ import annotations

import json
from typing import List, Optional

from ..db import (
    connect,
    get_runtime_sessions,
    get_runtime_tasks,
    get_runtime_results,
    get_tasks_for_graph,
    now_iso,
)
from ..knowledge import get_knowledge_by_type, KnowledgeType
from .models import RepairCandidateEvent, RepairProposal

# Maximum repair depth. A graph whose source already indicates
# repair_depth >= 2 will escalate rather than auto-drafting.
# Maximum repair depth. Beyond this, the system escalates rather than
# auto-drafting proposals. Set higher than AUTO_APPROVE_DEPTH so there's
# still an upper bound beyond auto-approval.
MAX_REPAIR_DEPTH = 5

# Repair depth threshold for auto-approval (Gap #6 — Repair Escalation). (Gap #6 — Repair Escalation).
# When a repair candidate's depth reaches AUTO_APPROVE_DEPTH, the repair is
# auto-approved and auto-executed instead of creating a pending proposal that
# requires human approval. This prevents the system from requiring manual
# intervention after repeated failures.
#
# With depth=2:
#   Original run fails   → depth=0 → pending (human approves)
#   First repair fails   → depth=1 → pending (human approves)
#   Second repair fails  → depth=2 → AUTO-APPROVE + AUTO-EXECUTE
#
# Concretely: after 3 consecutive failures on the same (graph, task), the
# system recovers without manual intervention.
AUTO_APPROVE_DEPTH = 2


def _get_repair_depth(conn, graph_id: str) -> int:
    """Determine the repair depth of a graph from its source tag.

    source="repair:<original_graph_id>:<original_task_id>" indicates depth = 1.
    If the original_graph_id itself is a repair graph, depth increments.
    Recursively follows the chain until it hits a non-repair source.
    """
    row = conn.execute(
        "SELECT source FROM task_graphs WHERE id = ?", (graph_id,)
    ).fetchone()
    if row is None or not row["source"]:
        return 0
    source = row["source"]
    parts = source.split(":")
    if len(parts) >= 3 and parts[0] == "repair":
        original_gid = parts[1]
        return 1 + _get_repair_depth(conn, original_gid)
    return 0


def _is_already_proposed(conn, task_id: str) -> bool:
    """Check if a task_id already has a pending or approved proposal."""
    row = conn.execute(
        "SELECT 1 FROM repair_proposals "
        "WHERE original_task_id = ? AND status IN ('pending', 'approved')",
        (task_id,),
    ).fetchone()
    return row is not None


def detect_repair_candidates(conn) -> List[RepairCandidateEvent]:
    """Scan recent Runtime sessions for failed tasks not yet proposed for repair.

    Returns one RepairCandidateEvent per failed task not already covered by
    a pending/approved repair proposal.

    Deterministic. Reads runtime_sessions + runtime_tasks + runtime_results only.
    """
    candidates: List[RepairCandidateEvent] = []

    sessions = get_runtime_sessions(conn)
    for sess in sessions:
        if sess.get("state") != "finished":
            continue
        session_id = sess["session_id"]
        schedule_id = sess.get("schedule_id", "")

        tasks = get_runtime_tasks(conn, session_id)
        results = get_runtime_results(conn, session_id)
        results_by_task: dict[str, dict] = {}
        for r in results:
            tid = r.get("task_id", "")
            if tid:
                results_by_task[tid] = r

        for task in tasks:
            tid = task["task_id"]
            if task.get("status") != "failed":
                continue
            if _is_already_proposed(conn, tid):
                continue

            # Determine the capability from the task definition.
            capability = ""
            try:
                graph_tasks = get_tasks_for_graph(conn, schedule_id)
                for gt in graph_tasks:
                    if gt.id == tid:
                        caps = (gt.required_capabilities or "").strip()
                        if caps:
                            capability = caps.split(",")[0].strip()
                        break
            except Exception:
                pass

            # Determine failure reason from the result.
            failure_reason = "task execution failed"
            result = results_by_task.get(tid)
            if result:
                vp = result.get("verification_passed")
                if vp is not None and vp == 0:
                    failure_reason = "verification failed"
                elif result.get("error"):
                    failure_reason = result["error"]

            depth = _get_repair_depth(conn, schedule_id)

            candidates.append(RepairCandidateEvent(
                original_graph_id=schedule_id,
                original_task_id=tid,
                failure_reason=failure_reason,
                capability=capability,
                repair_depth=depth,
            ))

    return candidates


def evaluate_repair(
    conn, candidate: RepairCandidateEvent,
) -> RepairProposal:
    """Apply the escalation boundary decision procedure.

    Deterministic. Reads Knowledge (execution_bottleneck, capability_reliability).
    No LLM in this decision path (Law 21).
    """
    evidence_ids: List[str] = []
    proposal_id = f"repair-proposal:{candidate.original_graph_id}:{candidate.original_task_id}"
    proposed_goal = f"Repair: fix {candidate.original_task_id} ({candidate.failure_reason})"

    # 1. Check depth cap FIRST — hard safety valve against infinite repair loops.
    #    Beyond MAX_REPAIR_DEPTH, the system escalates (rejects the repair)
    #    regardless of auto-approval settings. This check takes priority over
    #    auto-approve so the hard ceiling is always enforced.
    if candidate.repair_depth >= MAX_REPAIR_DEPTH:
        return RepairProposal(
            id=proposal_id,
            candidate=candidate,
            decision="escalate_depth_cap",
            evidence_ids=list(evidence_ids),
            proposed_goal=proposed_goal,
        )

    # 2. Check auto-approval threshold (Gap #6).
    #    When repair_depth >= AUTO_APPROVE_DEPTH, skip human approval and
    #    auto-approve + auto-execute. This allows the system to recover from
    #    repeated failures without manual intervention.
    #    Must be AFTER the depth cap check so escalation takes priority.
    if candidate.repair_depth >= AUTO_APPROVE_DEPTH:
        return RepairProposal(
            id=proposal_id,
            candidate=candidate,
            decision="auto_approve",
            evidence_ids=list(evidence_ids),
            proposed_goal=proposed_goal,
        )

    # 2. Check for existing bottleneck Knowledge for this capability.
    if candidate.capability:
        try:
            bottleneck_knowledge = get_knowledge_by_type(
                conn, KnowledgeType.EXECUTION_BOTTLENECK.value
            )
            for bk in bottleneck_knowledge:
                if candidate.capability in bk.subject:
                    evidence_ids.extend(bk.evidence_ids)
                    return RepairProposal(
                        id=proposal_id,
                        candidate=candidate,
                        decision="escalate_bottleneck",
                        evidence_ids=list(evidence_ids),
                        proposed_goal=proposed_goal,
                    )
        except Exception:
            pass

    # 3. Check capability_reliability Knowledge.
    if candidate.capability:
        try:
            cap_knowledge = get_knowledge_by_type(
                conn, KnowledgeType.CAPABILITY_RELIABILITY.value
            )
            for ck in cap_knowledge:
                if candidate.capability in ck.subject:
                    evidence_ids.extend(ck.evidence_ids)
        except Exception:
            pass

    # No bottleneck, within depth cap → auto-eligible (still requires approval).
    return RepairProposal(
        id=proposal_id,
        candidate=candidate,
        decision="auto_eligible",
        evidence_ids=list(evidence_ids),
        proposed_goal=proposed_goal,
    )


def _persist_proposal(conn, proposal: RepairProposal) -> None:
    """Persist a repair proposal (all decision types) and log to history."""
    now = now_iso()
    conn.execute(
        """INSERT OR REPLACE INTO repair_proposals
           (id, original_graph_id, original_task_id, failure_reason,
            capability, repair_depth, decision, evidence_ids,
            proposed_goal, status, created_at, schema_version)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            proposal.id,
            proposal.candidate.original_graph_id,
            proposal.candidate.original_task_id,
            proposal.candidate.failure_reason,
            proposal.candidate.capability,
            proposal.candidate.repair_depth,
            proposal.decision,
            json.dumps(proposal.evidence_ids),
            proposal.proposed_goal,
            "pending",
            now,
            "1",
        ),
    )
    # Log to history.
    conn.execute(
        """INSERT INTO repair_history
           (proposal_id, event_type, detail, recorded_at)
           VALUES (?, ?, ?, ?)""",
        (proposal.id, "proposed",
         f"Decision: {proposal.decision}, "
         f"evidence: {len(proposal.evidence_ids)} id(s)",
         now),
    )
    conn.commit()


def propose_repair(conn, candidate: RepairCandidateEvent,
                   workspace: str = ".") -> Optional[str]:
    """Detect + evaluate + persist (or auto-approve + auto-execute).

    Returns proposal id (or graph id for auto-approved proposals).

    Gap #6 — Repair Escalation:
    When ``candidate.repair_depth >= AUTO_APPROVE_DEPTH``, the decision is
    ``auto_approve``. Instead of creating a pending proposal, the repair is
    automatically approved (re-enters Planning to create a new task graph)
    AND automatically executed through the RuntimeEngine. This allows the
    system to recover from repeated failures without manual intervention.

    All candidates with depth < AUTO_APPROVE_DEPTH are persisted as pending
    proposals, regardless of decision type, so the human can see them via
    ``friday repair pending``.
    """
    proposal = evaluate_repair(conn, candidate)

    # Gap #6: auto-approve + auto-execute when repair depth reaches threshold.
    if proposal.decision == "auto_approve":
        # Persist the proposal as 'auto_approved' for history/audit.
        _persist_proposal(conn, proposal)

        # Mark as approved immediately (no human review needed).
        now = now_iso()
        conn.execute(
            "UPDATE repair_proposals SET status = 'approved', reviewed_at = ? "
            "WHERE id = ?",
            (now, proposal.id),
        )
        conn.execute(
            """INSERT INTO repair_history
               (proposal_id, event_type, detail, recorded_at)
               VALUES (?, ?, ?, ?)""",
            (proposal.id, "auto_approved",
             f"Auto-approved at depth={candidate.repair_depth} "
             f"(threshold={AUTO_APPROVE_DEPTH})",
             now),
        )
        conn.commit()

        # Generate the repair graph via the normal approval pipeline.
        graph_id = approve_repair(conn, proposal.id)
        if graph_id is None:
            # Planning pipeline couldn't produce a graph. Log and return None.
            conn.execute(
                """INSERT INTO repair_history
                   (proposal_id, event_type, detail, recorded_at)
                   VALUES (?, ?, ?, ?)""",
                (proposal.id, "auto_approve_failed",
                 f"Planning pipeline failed to produce a graph for "
                 f"{proposal.proposed_goal}",
                 now_iso()),
            )
            conn.commit()
            return None

        # Auto-execute the approved repair graph.
        report = _auto_execute_repair(conn, graph_id, workspace=workspace)

        # Record execution outcome in repair_history.
        outcome = (
            "succeeded" if report.failed == 0 else
            f"completed with {report.failed} failure(s)"
        )
        conn.execute(
            """INSERT INTO repair_history
               (proposal_id, event_type, detail, recorded_at)
               VALUES (?, ?, ?, ?)""",
            (proposal.id, "auto_executed",
             f"Graph {graph_id} auto-executed: {report.succeeded} succeeded, "
             f"{report.failed} failed, {report.cancelled} cancelled ({outcome})",
             now_iso()),
        )
        conn.commit()

        return graph_id

    # Normal path: depth < threshold — persist as pending for human review.
    _persist_proposal(conn, proposal)
    return proposal.id


def _auto_execute_repair(conn, graph_id: str,
                          workspace: str = ".") -> "ExecutionReport":
    """Auto-execute a repair graph through the normal execution pipeline.

    Gap #6: after auto-approval creates a new task graph, this function
    resolves capabilities, schedules, and runs the graph through the
    RuntimeEngine — the same path every other goal goes through.

    Returns an ExecutionReport with the outcome. Never raises (exceptions
    are caught and returned as a failed report).
    """
    from ..resolver import CapabilityResolver
    from ..scheduler.engine import TaskScheduler
    from ..runtime import RuntimeEngine
    from ..runtime.models import ExecutionReport

    try:
        # 1. Resolve capabilities for the new graph.
        CapabilityResolver(conn).resolve_graph(graph_id)

        # 2. Schedule the graph.
        sched_result = TaskScheduler(conn).schedule_graph(graph_id)
        if not sched_result or not sched_result.schedule:
            return ExecutionReport(
                session_id="", schedule_id=graph_id,
                state="failed", started_at="", finished_at="",
                wave_count=0, duration_ms=0, verification_time_ms=0,
                stopped_at=graph_id,
                stop_reason="scheduling failed for auto-executed repair graph",
                executed=0, succeeded=0, failed=1, cancelled=0,
                tasks=[], workers_used=[], artifacts=[],
            )

        # 3. Execute through RuntimeEngine.
        eng = RuntimeEngine(conn, workspace=workspace)
        report = eng.run(sched_result.schedule)
        return report
    except Exception as exc:
        import logging
        logging.getLogger(__name__).exception(
            "auto_execute_repair: exception for graph %s: %s", graph_id, exc)
        return ExecutionReport(
            session_id="", schedule_id=graph_id,
            state="failed", started_at="", finished_at="",
            wave_count=0, duration_ms=0, verification_time_ms=0,
            stopped_at=graph_id,
            stop_reason=f"auto-execute exception: {exc}",
            executed=0, succeeded=0, failed=1, cancelled=0,
            tasks=[], workers_used=[], artifacts=[],
        )


def approve_repair(conn, proposal_id: str) -> Optional[str]:
    """On human approval: hand proposed_goal to Planning pipeline.

    Produces a new TaskGraphRow with source="repair:<original_gid>:<original_tid>".
    Returns the new graph id, or None if validation failed.

    The approval merely unblocks the repair — the human can inspect the
    generated graph before running it. Execution must be triggered separately.
    """
    row = conn.execute(
        "SELECT * FROM repair_proposals WHERE id = ? AND status = 'pending'",
        (proposal_id,),
    ).fetchone()
    if row is None:
        return None

    now = now_iso()

    # Mark the proposal as approved.
    conn.execute(
        "UPDATE repair_proposals SET status = 'approved', reviewed_at = ? WHERE id = ?",
        (now, proposal_id),
    )

    # Create a plan + task graph for the repair goal using the same pipeline
    # every other goal goes through (Planning -> Task Graph).
    from ..planning import TaskGraphEngine
    graph_eng = TaskGraphEngine(conn)
    try:
        graph = graph_eng.generate(row["proposed_goal"])
    except (ValueError, TypeError) as exc:
        # Planning pipeline couldn't produce a graph (e.g. empty evidence).
        # Log the reason and reject the proposal rather than silently succeeding.
        import logging
        logging.getLogger(__name__).warning(
            "approve_repair: planning pipeline failed for proposal %s: %s",
            proposal_id, exc)
        graph = None

    if graph is None:
        conn.execute(
            "UPDATE repair_proposals SET status = 'rejected', reviewed_at = ? WHERE id = ?",
            (now, proposal_id),
        )
        return None

    # Tag the graph's source as "repair:<original_gid>:<original_tid>".
    source = f"repair:{row['original_graph_id']}:{row['original_task_id']}"
    conn.execute(
        "UPDATE task_graphs SET source = ? WHERE id = ?", (source, graph.id))
    conn.commit()

    # Record in repair_history.
    conn.execute(
        """INSERT INTO repair_history
           (proposal_id, event_type, detail, recorded_at)
           VALUES (?, ?, ?, ?)""",
        (proposal_id, "approved",
         f"Graph {graph.id} created with source={source}", now),
    )
    conn.commit()

    return graph.id


def get_pending_proposals(conn) -> List[dict]:
    """Get all pending repair proposals for CLI display."""
    rows = conn.execute(
        "SELECT * FROM repair_proposals WHERE status = 'pending' ORDER BY created_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def get_all_candidates(conn) -> List[dict]:
    """Get all repair candidates (including escalated) for CLI display."""
    # First, run detection to ensure new failures are captured.
    candidates = detect_repair_candidates(conn)
    for c in candidates:
        propose_repair(conn, c)

    # Return all proposals regardless of decision/status.
    rows = conn.execute(
        "SELECT * FROM repair_proposals ORDER BY created_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]
