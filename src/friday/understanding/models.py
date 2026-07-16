"""Understanding models (Milestone 8.3).

Understanding is durable engineering *meaning* derived ONLY from knowledge
(plus knowledge-evolution events). It is the layer above Knowledge. It never
reads observations, context, git, or READMEs directly. It never calls an LLM.

Design mirrors the Knowledge Engine (8.1): append-only rows, deterministic
ids, lifecycle Candidate→Observed→Verified→Stable→Retired, confidence derived
from evidence (here: knowledge reinforcement), history preserved forever.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional

from ..db import UnderstandingRow


class UnderstandingType(str, Enum):
    """Stable engineering-meaning categories derived from knowledge.

    Each must trace to one or more knowledge entries. Never invented facts.
    """

    ENGINEERING_DIRECTION = "engineering_direction"
    ENGINEERING_PHILOSOPHY = "engineering_philosophy"
    SKILL_DEVELOPMENT = "skill_development"
    TECHNOLOGY_PREFERENCE = "technology_preference"
    TECHNOLOGY_SHIFT = "technology_shift"
    PROJECT_CONVERGENCE = "project_convergence"
    PROJECT_DIVERGENCE = "project_divergence"
    EMERGING_EXPERTISE = "emerging_expertise"
    ARCHITECTURAL_STYLE = "architectural_style"
    ENGINEERING_IDENTITY = "engineering_identity"
    LONG_TERM_INITIATIVE = "long_term_initiative"
    INVESTMENT_TREND = "investment_trend"
    COMMERCIAL_DIRECTION = "commercial_direction"
    RESEARCH_DIRECTION = "research_direction"
    ENGINEERING_HABIT = "engineering_habit"
    ENGINEERING_RISK = "engineering_risk"
    ENGINEERING_OPPORTUNITY = "engineering_opportunity"
    ENGINEERING_BLIND_SPOT = "engineering_blind_spot"
    ENGINEERING_STRENGTH = "engineering_strength"
    ENGINEERING_WEAKNESS = "engineering_weakness"

    @classmethod
    def from_str(cls, s: str) -> "UnderstandingType":
        s = (s or "").strip().lower()
        for ut in cls:
            if ut.value == s:
                return ut
        raise ValueError(f"{cls.__name__} has no member {s!r}")


class UnderstandingStatus(str, Enum):
    """Lifecycle status — exactly mirrors KnowledgeStatus."""

    CANDIDATE = "candidate"
    OBSERVED = "observed"
    VERIFIED = "verified"
    STABLE = "stable"
    RETIRED = "retired"

    @classmethod
    def from_str(cls, s: str) -> "UnderstandingStatus":
        s = (s or "").strip().lower()
        for us in cls:
            if us.value == s:
                return us
        raise ValueError(f"{cls.__name__} has no member {s!r}")


class UnderstandingConfidence(str, Enum):
    """Confidence derived from knowledge reinforcement (never guessed)."""

    WEAK = "weak"
    MEDIUM = "medium"
    STRONG = "strong"

    @classmethod
    def from_str(cls, s: str) -> "UnderstandingConfidence":
        s = (s or "").strip().lower()
        for uc in cls:
            if uc.value == s:
                return uc
        raise ValueError(f"{cls.__name__} has no member {s!r}")


class UnderstandingLifecycleRank:
    """Status ordering for lifecycle advancement (mirrors knowledge evolution)."""

    RANK = {
        UnderstandingStatus.CANDIDATE: 0,
        UnderstandingStatus.OBSERVED: 1,
        UnderstandingStatus.VERIFIED: 2,
        UnderstandingStatus.STABLE: 3,
        UnderstandingStatus.RETIRED: 4,
    }

    @classmethod
    def rank(cls, s: UnderstandingStatus) -> int:
        return cls.RANK[s]

    @classmethod
    def order(cls, c: UnderstandingConfidence) -> int:
        return {
            UnderstandingConfidence.WEAK: 0,
            UnderstandingConfidence.MEDIUM: 1,
            UnderstandingConfidence.STRONG: 2,
        }[c]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# Contract version (Law 24).
SCHEMA_VERSION = "1.0"


@dataclass
class Understanding:
    """One derived engineering understanding.

    Never manually entered. Every statement cites knowledge ids. Build-only
    derived; the Brain consumes it as evidence, never computes it.
    """

    SCHEMA_VERSION = SCHEMA_VERSION

    type: UnderstandingType
    subject: str
    statement: str
    confidence: UnderstandingConfidence
    status: UnderstandingStatus
    knowledge_ids: List[str]
    build_at: str
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    retired_at: Optional[str] = None
    schema_version: str = SCHEMA_VERSION
    id: Optional[str] = None

    @property
    def knowledge_count(self) -> int:
        return len(self.knowledge_ids)

    def to_row(self):
        from ..db import UnderstandingRow

        return UnderstandingRow(
            id=self.id or self._generate_id(),
            type=self.type.value,
            subject=self.subject,
            statement=self.statement,
            confidence=self.confidence.value,
            status=self.status.value,
            knowledge_ids=",".join(self.knowledge_ids),
            created_at=self.created_at,
            updated_at=self.updated_at,
            build_at=self.build_at,
            retired_at=self.retired_at,
            schema_version=self.schema_version,
        )

    def _generate_id(self) -> str:
        """Deterministic id from created_at + type + subject. Stable across
        builds (created_at is fixed on first creation and reused on update via
        the (type, subject) key), so repeated builds are idempotent."""
        return f"{self.created_at}:{self.type.value}:{self.subject}"

    @classmethod
    def from_row(cls, row) -> "Understanding":
        # Accept either a sqlite3.Row (subscriptable) or an UnderstandingRow.
        if isinstance(row, UnderstandingRow):
            return cls(
                id=row.id, type=UnderstandingType.from_str(row.type),
                subject=row.subject, statement=row.statement,
                confidence=UnderstandingConfidence.from_str(row.confidence),
                status=UnderstandingStatus.from_str(row.status),
                knowledge_ids=[k for k in (row.knowledge_ids or "").split(",") if k],
                build_at=row.build_at, created_at=row.created_at,
                updated_at=row.updated_at, retired_at=row.retired_at,
                schema_version=cls._coerce_version(row),
            )
        return cls(
            id=row["id"],
            type=UnderstandingType.from_str(row["type"]),
            subject=row["subject"],
            statement=row["statement"],
            confidence=UnderstandingConfidence.from_str(row["confidence"]),
            status=UnderstandingStatus.from_str(row["status"]),
            knowledge_ids=[k for k in (row["knowledge_ids"] or "").split(",") if k],
            build_at=row["build_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            retired_at=row["retired_at"],
            schema_version=cls._coerce_version(row),
        )

    @classmethod
    def _coerce_version(cls, row) -> str:
        """Reject stored understanding whose contract version is unknown (Law 24).

        Rows predating versioning are backfilled with the current version.
        """
        if isinstance(row, UnderstandingRow):
            version = getattr(row, "schema_version", None)
        else:
            version = row["schema_version"] if "schema_version" in row.keys() else None
        if version is None:
            return SCHEMA_VERSION
        if version != SCHEMA_VERSION:
            raise ValueError(
                f"Understanding schema_version {version!r} != current {SCHEMA_VERSION!r}")
        return version
