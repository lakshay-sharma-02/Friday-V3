"""Planning Engine models (Milestone 9.0).

A Plan is a DETERMINISTIC, STRUCTURED engineering strategy. It is NOT prose, NOT
an LLM response, NOT an essay. The engine first builds an internal Plan object
(milestones, dependencies, risks, verification, rollback, evidence references),
and only THEN renders it into human-readable text. This separation is
deliberate: the next milestone (Worker Orchestration) must consume plans
PROGRAMMATICALLY — it reads the structured fields, never parses prose.

The Planning Engine is WRITE-ONLY and sits on top of Insights / Initiatives /
Understanding / Knowledge. It NEVER reads observations, context, git, READMEs,
or repositories directly. It NEVER executes, edits files, calls workers, or
invokes an LLM. Every plan cites evidence ids from the lower layers.

Lifecycle: Planned -> Refined -> Approved -> Superseded. Append-only history +
evolution. Plans are not ephemeral (unlike insights): a regenerated plan with
the same goal REPLACES the prior row (idempotent on id) and records the change
in plan_evolution, but is never auto-deleted.

Field names mirror the spec exactly: id, goal, plan_type, confidence,
affected_initiatives, affected_understanding, affected_knowledge, milestones,
dependencies, risks, verification, rollback, estimated_complexity,
estimated_effort, history, created, updated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional

from ..db import PlanRow


class PlanType(str, Enum):
    """Deterministic planning categories. Never LLM-derived."""

    FEATURE = "feature"
    BUG_FIX = "bug_fix"
    RESEARCH = "research"
    MIGRATION = "migration"
    REFACTOR = "refactor"
    ARCHITECTURE = "architecture"
    INFRASTRUCTURE = "infrastructure"
    OPTIMIZATION = "optimization"
    RELEASE = "release"
    MAINTENANCE = "maintenance"
    DOCUMENTATION = "documentation"
    TESTING = "testing"
    LEARNING = "learning"
    INTEGRATION = "integration"
    COMMERCIAL = "commercial"

    @classmethod
    def from_str(cls, s: str) -> "PlanType":
        s = (s or "").strip().lower()
        for pt in cls:
            if pt.value == s:
                return pt
        raise ValueError(f"{cls.__name__} has no member {s!r}")

    @classmethod
    def from_goal(cls, goal: str) -> "PlanType":
        """Deterministic category inferred from goal keywords. Deterministic
        keyword routing only — never an LLM. Specific keywords are checked
        BEFORE generic verbs (build/add/create) so 'build worker system' maps to
        INFRASTRUCTURE, not FEATURE."""
        g = (goal or "").lower()
        # Ordered: most specific first (from vocabulary.py). Generic verbs
        # (implement/build/add/create) are last so a specific keyword always wins.
        from ..vocabulary import PLAN_TYPE_KEYWORDS
        for kw, pt_name in PLAN_TYPE_KEYWORDS:
            if kw in g:
                try:
                    return cls.from_str(pt_name)
                except ValueError:
                    continue
        return cls.FEATURE  # safe inference default for unmatched goals


class PlanConfidence(str, Enum):
    """Confidence derived from evidence reinforcement (never guessed)."""

    WEAK = "weak"
    MEDIUM = "medium"
    STRONG = "strong"

    @classmethod
    def from_str(cls, s: str) -> "PlanConfidence":
        s = (s or "").strip().lower()
        for pc in cls:
            if pc.value == s:
                return pc
        raise ValueError(f"{cls.__name__} has no member {s!r}")


class PlanStatus(str, Enum):
    """Plan lifecycle — evidence/approval-driven, never clock-driven."""

    PLANNED = "planned"
    REFINED = "refined"
    APPROVED = "approved"
    SUPERSEDED = "superseded"

    @classmethod
    def from_str(cls, s: str) -> "PlanStatus":
        s = (s or "").strip().lower()
        for ps in cls:
            if ps.value == s:
                return ps
        raise ValueError(f"{cls.__name__} has no member {s!r}")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Plan:
    """One derived engineering plan. STRUCTURED first, text rendered later.

    Every collection field (milestones/dependencies/risks/verification/rollback)
    is a list of structured records so downstream workers can consume them. The
    `render_text()` method produces the human-readable plan; it is the LAST step.
    """

    # Contract version (Law 24).
    SCHEMA_VERSION = "1.0"

    goal: str
    plan_type: PlanType
    confidence: PlanConfidence
    status: PlanStatus
    affected_initiative_ids: List[str] = field(default_factory=list)
    affected_insight_ids: List[str] = field(default_factory=list)
    affected_understanding_ids: List[str] = field(default_factory=list)
    affected_knowledge_ids: List[str] = field(default_factory=list)
    milestones: List[dict] = field(default_factory=list)
    dependencies: List[dict] = field(default_factory=list)
    risks: List[dict] = field(default_factory=list)
    verification: List[dict] = field(default_factory=list)
    rollback: List[dict] = field(default_factory=list)
    estimated_complexity: str = "medium"
    estimated_effort: str = "medium"
    plan_text: str = ""
    schema_version: str = SCHEMA_VERSION
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    id: Optional[str] = None

    # --- structured helpers -------------------------------------------------

    def _join(self, xs: List[str]) -> str:
        return ",".join(xs)

    @property
    def initiative_count(self) -> int:
        return len(self.affected_initiative_ids)

    @property
    def insight_count(self) -> int:
        return len(self.affected_insight_ids)

    @property
    def understanding_count(self) -> int:
        return len(self.affected_understanding_ids)

    @property
    def knowledge_count(self) -> int:
        return len(self.affected_knowledge_ids)

    @property
    def milestone_count(self) -> int:
        return len(self.milestones)

    @property
    def dependency_count(self) -> int:
        return len(self.dependencies)

    @property
    def risk_count(self) -> int:
        return len(self.risks)

    @property
    def verification_count(self) -> int:
        return len(self.verification)

    @property
    def rollback_count(self) -> int:
        return len(self.rollback)

    def evidence_count(self) -> int:
        return (self.initiative_count + self.insight_count
                + self.understanding_count + self.knowledge_count)

    def _generate_id(self) -> str:
        """Deterministic id from goal. Stable across generations, so repeated
        planning of the same goal REPLACES the same row (idempotent). created_at
        varies per generation and must NOT be part of the id.

        Only the id is normalized: surrounding quotes are stripped so a goal
        passed as `"goal"` yields the same id as `goal` (the user-visible goal
        text is left untouched)."""
        return f"plan:{self.goal.strip().strip('\"').strip().lower()}"

    def to_row(self) -> PlanRow:
        return PlanRow(
            id=self.id or self._generate_id(),
            goal=self.goal,
            plan_type=self.plan_type.value,
            confidence=self.confidence.value,
            status=self.status.value,
            affected_initiative_ids=self._join(self.affected_initiative_ids),
            affected_insight_ids=self._join(self.affected_insight_ids),
            affected_understanding_ids=self._join(self.affected_understanding_ids),
            affected_knowledge_ids=self._join(self.affected_knowledge_ids),
            milestones=_json(self.milestones),
            dependencies=_json(self.dependencies),
            risks=_json(self.risks),
            verification=_json(self.verification),
            rollback=_json(self.rollback),
            estimated_complexity=self.estimated_complexity,
            estimated_effort=self.estimated_effort,
            plan_text=self.plan_text or self.render_text(),
            schema_version=self.schema_version,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )

    @classmethod
    def from_row(cls, row) -> "Plan":
        if isinstance(row, PlanRow):
            return cls(
                id=row.id, goal=row.goal,
                plan_type=PlanType.from_str(row.plan_type),
                confidence=PlanConfidence.from_str(row.confidence),
                status=PlanStatus.from_str(row.status),
                affected_initiative_ids=_split(row.affected_initiative_ids),
                affected_insight_ids=_split(row.affected_insight_ids),
                affected_understanding_ids=_split(row.affected_understanding_ids),
                affected_knowledge_ids=_split(row.affected_knowledge_ids),
                milestones=_loads(row.milestones),
                dependencies=_loads(row.dependencies),
                risks=_loads(row.risks),
                verification=_loads(row.verification),
                rollback=_loads(row.rollback),
                estimated_complexity=row.estimated_complexity or "medium",
                estimated_effort=row.estimated_effort or "medium",
                plan_text=row.plan_text or "",
                created_at=row.created_at, updated_at=row.updated_at,
            )
        return cls(
            id=row["id"], goal=row["goal"],
            plan_type=PlanType.from_str(row["plan_type"]),
            confidence=PlanConfidence.from_str(row["confidence"]),
            status=PlanStatus.from_str(row["status"]),
            affected_initiative_ids=_split(row["affected_initiative_ids"]),
            affected_insight_ids=_split(row["affected_insight_ids"]),
            affected_understanding_ids=_split(row["affected_understanding_ids"]),
            affected_knowledge_ids=_split(row["affected_knowledge_ids"]),
            milestones=_loads(row["milestones"]),
            dependencies=_loads(row["dependencies"]),
            risks=_loads(row["risks"]),
            verification=_loads(row["verification"]),
            rollback=_loads(row["rollback"]),
            estimated_complexity=row["estimated_complexity"] or "medium",
            estimated_effort=row["estimated_effort"] or "medium",
            plan_text=row["plan_text"] or "",
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    # --- rendering (LAST step; structured object already complete) -----------

    def render_text(self) -> str:
        """Render the structured plan into human-readable text. This is the
        only place prose is produced; the structured Plan is the source of
        truth."""
        lines = [
            f"Plan: {self.goal}",
            f"Type: {self.plan_type.value}",
            f"Confidence: {self.confidence.value} (evidence: "
            f"{self.initiative_count} initiative, {self.insight_count} insight, "
            f"{self.understanding_count} understanding, {self.knowledge_count} "
            f"knowledge)",
            f"Complexity: {self.estimated_complexity}  Effort: {self.estimated_effort}",
        ]
        if self.affected_initiative_ids:
            lines.append("Affected initiatives: "
                         + ", ".join(self.affected_initiative_ids))
        if self.affected_insight_ids:
            lines.append("Affected insights: "
                         + ", ".join(self.affected_insight_ids))
        if self.affected_understanding_ids:
            lines.append("Supporting understanding: "
                         + ", ".join(self.affected_understanding_ids))
        if self.affected_knowledge_ids:
            lines.append("Supporting knowledge: "
                         + ", ".join(self.affected_knowledge_ids))
        lines.append("")
        lines.append("Milestones:")
        for m in self.milestones:
            lines.append(f"  {m.get('order')}. {m.get('title')}"
                         + (f" — {m.get('detail')}" if m.get("detail") else ""))
        lines.append("")
        lines.append("Dependencies:")
        for d in self.dependencies:
            lines.append(f"  - {d.get('kind')}: {d.get('target')}"
                         + (f" ({d.get('reason')})" if d.get("reason") else ""))
        if not self.dependencies:
            lines.append("  (none identified)")
        lines.append("")
        lines.append("Risks:")
        for r in self.risks:
            lines.append(f"  - [{r.get('severity','medium')}] {r.get('kind')}: "
                         + r.get("detail", ""))
        if not self.risks:
            lines.append("  (none identified)")
        lines.append("")
        lines.append("Verification:")
        for v in self.verification:
            lines.append(f"  - {v.get('method')}: {v.get('detail','')}")
        lines.append("")
        lines.append("Rollback:")
        for rb in self.rollback:
            lines.append(f"  - {rb.get('strategy')}: {rb.get('detail','')}")
        return "\n".join(lines)


import json  # noqa: E402  (kept below dataclass for readability)


def _json(xs: list) -> str:
    try:
        return json.dumps(xs, separators=(",", ":"))
    except (TypeError, ValueError):
        return "[]"


def _loads(s: str) -> list:
    if not s:
        return []
    try:
        out = json.loads(s)
        return out if isinstance(out, list) else []
    except (TypeError, ValueError):
        return []


def _split(s: str) -> List[str]:
    return [x for x in (s or "").split(",") if x]
