"""Knowledge models (Milestone 8.1).

Knowledge is stable understanding that emerges from repeated observations and
sessions. Unlike observations (facts) or context (work sessions), knowledge
represents long-term patterns, trends, habits, and relationships.

Everything is evidence-backed. No predictions, no advice, no LLM generation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional

from ..db import KnowledgeRow


class KnowledgeType(str, Enum):
    """Types of knowledge Friday accumulates."""

    ENGINEERING_TREND = "engineering_trend"
    ENGINEERING_HABIT = "engineering_habit"
    ENGINEERING_INTEREST = "engineering_interest"
    PROJECT_RELATIONSHIP = "project_relationship"
    PROJECT_EVOLUTION = "project_evolution"
    ENGINEERING_PREFERENCE = "engineering_preference"
    RECURRING_PATTERN = "recurring_pattern"
    RECURRING_BOTTLENECK = "recurring_bottleneck"
    TECHNOLOGY_INVESTMENT = "technology_investment"
    STABLE_DIRECTION = "stable_direction"
    PROJECT_IDENTITY = "project_identity"
    PROJECT_ARCHITECTURE = "project_architecture"
    PROJECT_STACK = "project_stack"
    PORTFOLIO_TECHNOLOGY = "portfolio_technology"
    PORTFOLIO_INTEGRATION = "portfolio_integration"

    @classmethod
    def from_str(cls, s: str) -> "KnowledgeType":
        s = (s or "").strip().lower()
        for kt in cls:
            if kt.value == s:
                return kt
        raise ValueError(f"{cls.__name__} has no member {s!r}")


class KnowledgeStatus(str, Enum):
    """Knowledge lifecycle status (Part C — extended by M8.2)."""

    CANDIDATE = "candidate"
    OBSERVED = "observed"
    VERIFIED = "verified"
    STABLE = "stable"
    DORMANT = "dormant"
    RETIRED = "retired"

    @classmethod
    def from_str(cls, s: str) -> "KnowledgeStatus":
        s = (s or "").strip().lower()
        for ks in cls:
            if ks.value == s:
                return ks
        raise ValueError(f"{cls.__name__} has no member {s!r}")


class KnowledgeConfidence(str, Enum):
    """Confidence based on evidence count and verification."""

    WEAK = "weak"
    MEDIUM = "medium"
    STRONG = "strong"

    @classmethod
    def from_str(cls, s: str) -> "KnowledgeConfidence":
        s = (s or "").strip().lower()
        for kc in cls:
            if kc.value == s:
                return kc
        raise ValueError(f"{cls.__name__} has no member {s!r}")


class TrendDirection(str, Enum):
    """Direction of a trend over time."""

    INCREASING = "increasing"
    STABLE = "stable"
    DECREASING = "decreasing"
    DORMANT = "dormant"
    EMERGING = "emerging"

    @classmethod
    def from_str(cls, s: str) -> "TrendDirection":
        s = (s or "").strip().lower()
        for td in cls:
            if td.value == s:
                return td
        raise ValueError(f"{cls.__name__} has no member {s!r}")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# Contract version (Law 24). Bump when the persisted shape changes.
SCHEMA_VERSION = "1.0"


@dataclass
class Knowledge:
    """One piece of accumulated engineering knowledge.

    Knowledge emerges from observations and sessions. It is never manually
    entered or LLM-generated. Every knowledge statement must be backed by
    evidence (observation or session ids).
    """

    # Contract version (Law 24), exposed as a class attribute.
    SCHEMA_VERSION = SCHEMA_VERSION

    type: KnowledgeType
    subject: str
    statement: str
    confidence: KnowledgeConfidence
    evidence_ids: List[str]
    status: KnowledgeStatus = KnowledgeStatus.CANDIDATE
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    last_verified: Optional[str] = None
    verification_count: int = 0
    is_static: bool = False
    schema_version: str = SCHEMA_VERSION
    id: Optional[str] = None

    @property
    def evidence_count(self) -> int:
        return len(self.evidence_ids)

    def to_row(self):
        return KnowledgeRow(
            id=self.id or self._generate_id(),
            type=self.type.value,
            subject=self.subject,
            statement=self.statement,
            confidence=self.confidence.value,
            evidence_ids=",".join(self.evidence_ids),
            status=self.status.value,
            created_at=self.created_at,
            updated_at=self.updated_at,
            last_verified=self.last_verified,
            verification_count=self.verification_count,
            is_static=int(bool(self.is_static)),
            schema_version=self.schema_version,
        )

    def _generate_id(self) -> str:
        """Deterministic ID based on type and subject."""
        return f"{self.created_at}:{self.type.value}:{self.subject}"

    @classmethod
    def from_row(cls, row) -> "Knowledge":
        if isinstance(row, KnowledgeRow):
            return cls(
                id=row.id,
                type=KnowledgeType.from_str(row.type),
                subject=row.subject,
                statement=row.statement,
                confidence=KnowledgeConfidence.from_str(row.confidence),
                evidence_ids=[e for e in (row.evidence_ids or "").split(",") if e],
                status=KnowledgeStatus.from_str(row.status),
                created_at=row.created_at,
                updated_at=row.updated_at,
                last_verified=row.last_verified,
                verification_count=row.verification_count or 0,
                is_static=bool(row.is_static),
                schema_version=cls._coerce_version(row),
            )
        return cls(
            id=row["id"],
            type=KnowledgeType.from_str(row["type"]),
            subject=row["subject"],
            statement=row["statement"],
            confidence=KnowledgeConfidence.from_str(row["confidence"]),
            evidence_ids=[e for e in (row["evidence_ids"] or "").split(",") if e],
            status=KnowledgeStatus.from_str(row["status"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_verified=row["last_verified"],
            verification_count=row["verification_count"] or 0,
            is_static=bool(row["is_static"]),
            schema_version=cls._coerce_version(row),
        )

    @staticmethod
    def _coerce_version(row) -> str:
        """Reject stored knowledge whose contract version is unknown.

        Law 24: older/incompatible versions must fail cleanly, not silently
        load under the current schema. Rows predating versioning (no column)
        are backfilled with the current version — they were written by this
        same schema and remain valid.
        """
        version = getattr(row, "schema_version", None)
        if version is None:
            # sqlite3.Row exposes columns via .keys(); named rows lack it.
            if hasattr(row, "keys") and "schema_version" not in row.keys():
                return SCHEMA_VERSION
            return SCHEMA_VERSION
        if version != SCHEMA_VERSION:
            raise ValueError(
                f"Knowledge schema_version {version!r} != current {SCHEMA_VERSION!r}")
        return version


@dataclass
class Trend:
    """A detected trend in engineering activity."""

    subject: str
    direction: TrendDirection
    evidence_count: int
    first_seen: str
    last_seen: str
    detail: Optional[str] = None


@dataclass
class Relationship:
    """A detected relationship between projects."""

    project_a: str
    project_b: str
    kind: str
    strength: str
    evidence_count: int
    sessions: List[str]
