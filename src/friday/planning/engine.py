"""Planning Engine (Milestone 9.0).

WRITE-ONLY layer on top of Insights / Initiatives / Understanding / Knowledge.
It derives a STRUCTURED engineering plan and persists it. It NEVER executes,
edits files, calls workers, or uses an LLM. It NEVER reads observations,
context, git, READMEs, or repositories directly.

Idempotent: planning the same goal REPLACES the same row (id = plan:<goal>),
records the prior version in plan_history, and emits a plan_evolution event
when confidence/status/evidence change. Plans are not ephemeral: they are not
auto-deleted, but are superseded when regenerated with materially different
evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

from ..db import (
    atomic,
    PlanEvolutionRow,
    PlanHistoryRow,
    get_all_plans,
    get_plan_by_id,
    insert_plan,
    insert_plan_evolution,
    insert_plan_history,
    latest_plan_snapshot,
    plan_evolution_for,
    update_plan_status,
)
from ..initiative import InitiativeEngine
from ..insight import InsightEngine
from ..knowledge.store import get_all_knowledge
from ..understanding import UnderstandingEngine
from .derive import Evidence, plan as derive_plan
from .models import Plan, PlanConfidence, PlanStatus, now_iso


@dataclass
class PlanBuildResult:
    total: int
    created: int
    updated: int
    active: int
    events: int = 0

    def to_text(self) -> str:
        lines = [
            "Planning Engine",
            "",
            f"Total plans: {self.total}",
            f"Created: {self.created}",
            f"Updated: {self.updated}",
            f"Active: {self.active}",
            f"Evolution events: {self.events}",
            "",
            "Done.",
        ]
        return "\n".join(lines) + "\n"


class PlanEngine:
    """Derives and stores plans. WRITE entrypoint: generate()."""

    def __init__(self, conn) -> None:
        self.conn = conn

    # --- READ (never mutate) --------------------------------------------------

    def all_plans(self) -> List[Plan]:
        return [Plan.from_row(r) for r in get_all_plans(self.conn)]

    def active_plans(self) -> List[Plan]:
        return [p for p in self.all_plans()
                if p.status != PlanStatus.SUPERSEDED]

    def plan_by_id(self, pid: str) -> Optional[Plan]:
        row = get_plan_by_id(self.conn, pid)
        return Plan.from_row(row) if row else None

    def plans_by_type(self, ptype: str) -> List[Plan]:
        from ..db import get_plans_by_type
        rows = get_plans_by_type(self.conn, ptype)
        return [Plan.from_row(r) for r in rows]

    def explain(self, pid: str) -> Tuple[Optional[Plan], List, List, List, List]:
        """Return (plan, milestones, dependencies, risks, verification)."""
        p = self.plan_by_id(pid)
        if p is None:
            return None, [], [], [], []
        return (p, p.milestones, p.dependencies, p.risks, p.verification)

    def evolution(self) -> List[PlanEvolutionRow]:
        from ..db import plan_evolution_all
        return plan_evolution_all(self.conn)

    # --- WRITE ----------------------------------------------------------------

    def generate(self, goal: str, generated_at: Optional[str] = None) -> Plan:
        """Derive and persist ONE plan for `goal`. Idempotent on goal."""
        if generated_at is None:
            generated_at = now_iso()

        evidence = self._gather_evidence()
        valid_i = set(evidence.initiatives_by_id)
        valid_ins = set(evidence.insights_by_id)
        valid_u = set(evidence.understanding_by_id)
        valid_k = set(evidence.knowledge_by_id)

        structured = derive_plan(goal, evidence)

        # Drop dangling citations; if none remain, still produce a plan (a plan
        # can be evidence-light) but keep only valid ids.
        structured.affected_initiative_ids = [
            x for x in structured.affected_initiative_ids if x in valid_i]
        structured.affected_insight_ids = [
            x for x in structured.affected_insight_ids if x in valid_ins]
        structured.affected_understanding_ids = [
            x for x in structured.affected_understanding_ids if x in valid_u]
        structured.affected_knowledge_ids = [
            x for x in structured.affected_knowledge_ids if x in valid_k]

        pid = structured._generate_id()
        prev_row = get_plan_by_id(self.conn, pid)
        prev = Plan.from_row(prev_row) if prev_row else None

        if prev is None:
            structured.id = pid
            structured.created_at = generated_at
            structured.updated_at = generated_at
            created = 1
            updated = 0
        else:
            structured.id = pid
            structured.created_at = prev.created_at
            structured.updated_at = generated_at
            created = 0
            updated = 1

        # Whole plan generation is one atomic transaction (Part F).
        with atomic(self.conn):
            insert_plan(self.conn, [structured.to_row()])
            self._record_history(generated_at, structured)
            events = self._record_evolution(generated_at, structured, prev)

        all_p = get_all_plans(self.conn)
        active_n = sum(1 for p in all_p if p.status != PlanStatus.SUPERSEDED.value)
        # (result object not returned here; generate returns the Plan)
        return structured

    # --- internals ------------------------------------------------------------

    def _gather_evidence(self) -> Evidence:
        insights = InsightEngine(self.conn).active_insights()
        initiatives = InitiativeEngine(self.conn).all_initiatives()
        understanding = UnderstandingEngine(self.conn).all_understanding()
        knowledge = get_all_knowledge(self.conn)
        ev = Evidence(insights=insights, initiatives=initiatives,
                      understanding=understanding, knowledge=knowledge)
        ev.insights_by_id = {x.id for x in insights if x.id}
        ev.initiatives_by_id = {x.id for x in initiatives if x.id}
        ev.understanding_by_id = {x.id for x in understanding if x.id}
        ev.knowledge_by_id = {x.id for x in knowledge if x.id}
        return ev

    def _record_history(self, generated_at: str, p: Plan) -> None:
        insert_plan_history(self.conn, [PlanHistoryRow(
            generated_at=generated_at,
            plan_id=p.id or p._generate_id(),
            goal=p.goal,
            plan_type=p.plan_type.value,
            confidence=p.confidence.value,
            status=p.status.value,
            affected_initiative_ids=",".join(p.affected_initiative_ids),
            affected_insight_ids=",".join(p.affected_insight_ids),
            affected_understanding_ids=",".join(p.affected_understanding_ids),
            affected_knowledge_ids=",".join(p.affected_knowledge_ids),
            milestones=p.to_row().milestones,
            dependencies=p.to_row().dependencies,
            risks=p.to_row().risks,
            verification=p.to_row().verification,
            rollback=p.to_row().rollback,
            estimated_complexity=p.estimated_complexity,
            estimated_effort=p.estimated_effort,
        )])

    def _record_evolution(
        self, generated_at: str, p: Plan, prev: Optional[Plan]
    ) -> int:
        events: List[PlanEvolutionRow] = []
        pid = p.id or p._generate_id()
        if prev is None:
            events.append(self._event(
                generated_at, "Created", pid, None, p.status.value, None,
                p.confidence.value, f"Plan created for goal: {p.goal}",
                p.affected_initiative_ids, p.affected_insight_ids,
                p.affected_understanding_ids, p.affected_knowledge_ids))
            insert_plan_evolution(self.conn, events)
            return len(events)

        prev_conf = prev.confidence.value
        if _order(p.confidence) > _order(prev.confidence):
            events.append(self._event(
                generated_at, "Strengthened", pid, prev.status.value,
                p.status.value, prev_conf, p.confidence.value,
                f"Confidence {prev_conf}->{p.confidence.value}.",
                p.affected_initiative_ids, p.affected_insight_ids,
                p.affected_understanding_ids, p.affected_knowledge_ids))
        if _status_rank(p.status) > _status_rank(prev.status):
            et = ("Approved" if p.status == PlanStatus.APPROVED
                  else "Refined" if p.status == PlanStatus.REFINED else "Advanced")
            events.append(self._event(
                generated_at, et, pid, prev.status.value, p.status.value,
                prev_conf, p.confidence.value,
                f"Lifecycle {prev.status.value}->{p.status.value}.",
                p.affected_initiative_ids, p.affected_insight_ids,
                p.affected_understanding_ids, p.affected_knowledge_ids))
        if (set(p.affected_insight_ids) != set(prev.affected_insight_ids)
                or set(p.affected_initiative_ids) != set(prev.affected_initiative_ids)
                or set(p.affected_understanding_ids) != set(prev.affected_understanding_ids)
                or set(p.affected_knowledge_ids) != set(prev.affected_knowledge_ids)):
            events.append(self._event(
                generated_at, "Re-evidenced", pid, prev.status.value,
                p.status.value, prev_conf, p.confidence.value,
                "Evidence set changed on regeneration.",
                p.affected_initiative_ids, p.affected_insight_ids,
                p.affected_understanding_ids, p.affected_knowledge_ids))
        insert_plan_evolution(self.conn, events)
        return len(events)

    @staticmethod
    def _event(gen_at, etype, pid, prev_status, new_status, prev_conf,
               new_conf, reason, iids, ins_ids, uids, k_ids):
        return PlanEvolutionRow(
            id=f"{gen_at}:{etype}:{pid}",
            generated_at=gen_at, event_type=etype, plan_id=pid,
            previous_status=prev_status, new_status=new_status,
            previous_confidence=prev_conf, new_confidence=new_conf,
            reason=reason, timestamp=gen_at,
            affected_initiative_ids=",".join(iids),
            affected_insight_ids=",".join(ins_ids),
            affected_understanding_ids=",".join(uids),
            affected_knowledge_ids=",".join(k_ids),
        )


def _order(c: PlanConfidence) -> int:
    return {"weak": 0, "medium": 1, "strong": 2}[c]


def _status_rank(s: PlanStatus) -> int:
    return {
        PlanStatus.PLANNED: 0, PlanStatus.REFINED: 1,
        PlanStatus.APPROVED: 2, PlanStatus.SUPERSEDED: 3,
    }[s]
