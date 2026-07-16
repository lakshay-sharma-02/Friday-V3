"""Insight models (Milestone 8.5).

An insight is a high-value engineering observation that deserves human
attention, derived ONLY from Understanding (plus Initiatives and
Knowledge-Evolution/Knowledge). It is the layer above Initiatives. It never
reads observations, context, git, READMEs, or repositories directly. It never
calls an LLM.

Design mirrors the Initiative Engine (8.4) and Understanding Engine (8.3):
append-only rows, deterministic ids, a lifecycle Candidate->Observed->Verified->
Stable->Retired, confidence derived from evidence agreement (here: understanding/
initiative/knowledge reinforcement, never guessed), and history + evolution
preserved forever.

Insights are intentionally RARE and EPHEMERAL. A stable id (type + title, no
created_at — see _generate_id) makes repeated builds idempotent: a build that
no longer finds the triggering conditions RETIRES the insight rather than
re-emitting it, so the layer stays a live "what deserves attention now" feed
instead of accumulating static facts.

Titles are semantic ("Extract shared Rust crates", "Authentication subsystem"),
never repository names.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional

from ..db import InsightRow


class InsightType(str, Enum):
    """Deterministic insight categories derived from evidence. Never LLM."""

    OPPORTUNITY = "engineering_opportunity"
    RISK = "engineering_risk"
    RECOMMENDATION = "engineering_recommendation"
    CONVERGENCE = "engineering_convergence"
    DIVERGENCE = "engineering_divergence"
    BOTTLENECK = "engineering_bottleneck"
    BLIND_SPOT = "engineering_blind_spot"
    DEBT = "engineering_debt"
    REUSE = "engineering_reuse"
    MOMENTUM = "engineering_momentum"
    DRIFT = "engineering_drift"
    INVESTMENT = "engineering_investment"
    WARNING = "engineering_warning"
    BREAKTHROUGH = "engineering_breakthrough"
    EFFICIENCY = "engineering_efficiency"
    FOCUS = "engineering_focus"

    @classmethod
    def from_str(cls, s: str) -> "InsightType":
        s = (s or "").strip().lower()
        for it in cls:
            if it.value == s:
                return it
        return cls.OPPORTUNITY


class InsightStatus(str, Enum):
    """Lifecycle status — evidence-driven, never clock-driven."""

    CANDIDATE = "candidate"
    OBSERVED = "observed"
    VERIFIED = "verified"
    STABLE = "stable"
    RETIRED = "retired"

    @classmethod
    def from_str(cls, s: str) -> "InsightStatus":
        s = (s or "").strip().lower()
        for is_ in cls:
            if is_.value == s:
                return is_
        return cls.CANDIDATE


class InsightConfidence(str, Enum):
    """Confidence derived from evidence reinforcement (never guessed)."""

    WEAK = "weak"
    MEDIUM = "medium"
    STRONG = "strong"

    @classmethod
    def from_str(cls, s: str) -> "InsightConfidence":
        s = (s or "").strip().lower()
        for ic in cls:
            if ic.value == s:
                return ic
        return cls.WEAK


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Insight:
    """One derived engineering insight worth human attention.

    Never manually entered. Title is semantic (never a repo name). Every
    insight cites understanding ids (and/or initiative ids and/or knowledge
    ids). Build-only derived; the Brain consumes it as evidence.
    """

    type: InsightType
    title: str
    statement: str
    status: InsightStatus
    confidence: InsightConfidence
    understanding_ids: List[str]
    initiative_ids: List[str]
    knowledge_ids: List[str]
    build_at: str
    started_at: Optional[str] = None
    retired_at: Optional[str] = None
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    id: Optional[str] = None

    @property
    def understanding_count(self) -> int:
        return len(self.understanding_ids)

    @property
    def initiative_count(self) -> int:
        return len(self.initiative_ids)

    @property
    def knowledge_count(self) -> int:
        return len(self.knowledge_ids)

    def to_row(self) -> InsightRow:
        return InsightRow(
            id=self.id or self._generate_id(),
            title=self.title,
            insight_type=self.type.value,
            statement=self.statement,
            status=self.status.value,
            confidence=self.confidence.value,
            started_at=self.started_at,
            updated_at=self.updated_at,
            retired_at=self.retired_at,
            understanding_ids=",".join(self.understanding_ids),
            initiative_ids=",".join(self.initiative_ids),
            knowledge_ids=",".join(self.knowledge_ids),
            build_at=self.build_at,
            created_at=self.created_at,
        )

    def _generate_id(self) -> str:
        """Deterministic id from type + title. Stable across builds (the
        (type, title) key is the dedup key in build()), so repeated builds
        REPLACE the same row instead of inserting a new one. created_at varies
        per build and must NOT be part of the id or idempotency breaks."""
        return f"{self.type.value}:{self.title}"

    @classmethod
    def from_row(cls, row) -> "Insight":
        if isinstance(row, InsightRow):
            return cls(
                id=row.id, type=InsightType.from_str(row.insight_type),
                title=row.title, statement=row.statement,
                status=InsightStatus.from_str(row.status),
                confidence=InsightConfidence.from_str(row.confidence),
                understanding_ids=[
                    k for k in (row.understanding_ids or "").split(",") if k
                ],
                initiative_ids=[
                    k for k in (row.initiative_ids or "").split(",") if k
                ],
                knowledge_ids=[
                    k for k in (row.knowledge_ids or "").split(",") if k
                ],
                build_at=row.build_at, started_at=row.started_at,
                retired_at=row.retired_at, created_at=row.created_at,
                updated_at=row.updated_at,
            )
        return cls(
            id=row["id"], type=InsightType.from_str(row["insight_type"]),
            title=row["title"], statement=row["statement"],
            status=InsightStatus.from_str(row["status"]),
            confidence=InsightConfidence.from_str(row["confidence"]),
            understanding_ids=[
                k for k in (row["understanding_ids"] or "").split(",") if k
            ],
            initiative_ids=[
                k for k in (row["initiative_ids"] or "").split(",") if k
            ],
            knowledge_ids=[
                k for k in (row["knowledge_ids"] or "").split(",") if k
            ],
            build_at=row["build_at"], started_at=row["started_at"],
            retired_at=row["retired_at"], created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
