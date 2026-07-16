"""Initiative models (Milestone 8.4).

An initiative is a long-running engineering objective, derived ONLY from
Understanding (plus knowledge-evolution events and knowledge). It is the layer
above Understanding. It never reads observations, context, git, READMEs, or
repositories directly. It never calls an LLM.

Design mirrors the Understanding Engine (8.3) and Knowledge Engine (8.1):
append-only rows, deterministic ids, a lifecycle Candidate→Started→Active→
Blocked→Review→Completed→Dormant→Archived, confidence derived from evidence
(here: understanding/knowledge agreement + cross-project reinforcement), history
and evolution (including merge/split) preserved forever.

Initiative TITLES are semantic ("Authentication Infrastructure", "AI Routing"),
never repository names — so they stay stable if repositories are renamed or
split. That is precisely why initiatives exist as a layer separate from repos.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional

from ..db import InitiativeRow


class InitiativeType(str, Enum):
    """Deterministic initiative categories derived from understanding/knowledge.

    Each must trace to understanding/knowledge entries. Never LLM-generated.
    """

    FEATURE = "feature"
    INFRASTRUCTURE = "infrastructure"
    ARCHITECTURE = "architecture"
    RESEARCH = "research"
    MIGRATION = "migration"
    REFACTOR = "refactor"
    COMMERCIAL = "commercial"
    LEARNING = "learning"
    OPTIMIZATION = "optimization"
    PLATFORM = "platform"
    INTEGRATION = "integration"
    AUTOMATION = "automation"
    DOCUMENTATION = "documentation"
    TESTING = "testing"
    DEPLOYMENT = "deployment"
    RELEASE = "release"
    MAINTENANCE = "maintenance"

    @classmethod
    def from_str(cls, s: str) -> "InitiativeType":
        s = (s or "").strip().lower()
        for it in cls:
            if it.value == s:
                return it
        raise ValueError(f"{cls.__name__} has no member {s!r}")


class InitiativeStatus(str, Enum):
    """Lifecycle status — evidence-driven, never clock-driven.

    Transitions are derived from understanding/knowledge agreement and evolution
    (e.g. Completed requires evidence that work ceased *because* the underlying
    understanding stabilized, not because N days passed).
    """

    CANDIDATE = "candidate"
    STARTED = "started"
    ACTIVE = "active"
    BLOCKED = "blocked"
    REVIEW = "review"
    COMPLETED = "completed"
    DORMANT = "dormant"
    ARCHIVED = "archived"

    @classmethod
    def from_str(cls, s: str) -> "InitiativeStatus":
        s = (s or "").strip().lower()
        for is_ in cls:
            if is_.value == s:
                return is_
        raise ValueError(f"{cls.__name__} has no member {s!r}")


class InitiativeConfidence(str, Enum):
    """Confidence derived from evidence reinforcement (never guessed)."""

    WEAK = "weak"
    MEDIUM = "medium"
    STRONG = "strong"

    @classmethod
    def from_str(cls, s: str) -> "InitiativeConfidence":
        s = (s or "").strip().lower()
        for ic in cls:
            if ic.value == s:
                return ic
        raise ValueError(f"{cls.__name__} has no member {s!r}")


class InitiativeLifecycleRank:
    """Status ordering for lifecycle advancement (mirrors lower layers)."""

    RANK = {
        InitiativeStatus.CANDIDATE: 0,
        InitiativeStatus.STARTED: 1,
        InitiativeStatus.ACTIVE: 2,
        InitiativeStatus.BLOCKED: 3,
        InitiativeStatus.REVIEW: 4,
        InitiativeStatus.COMPLETED: 5,
        InitiativeStatus.DORMANT: 6,
        InitiativeStatus.ARCHIVED: 7,
    }

    @classmethod
    def rank(cls, s: InitiativeStatus) -> int:
        return cls.RANK[s]

    @classmethod
    def order(cls, c: InitiativeConfidence) -> int:
        return {
            InitiativeConfidence.WEAK: 0,
            InitiativeConfidence.MEDIUM: 1,
            InitiativeConfidence.STRONG: 2,
        }[c]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Initiative:
    """One derived long-running engineering initiative.

    Never manually entered. Title is semantic (never a repo name). Every
    initiative cites understanding ids (and knowledge ids). Build-only derived;
    the Brain consumes it as evidence, never computes it.
    """

    # Contract version (Law 24).
    SCHEMA_VERSION = "1.0"

    type: InitiativeType
    title: str
    status: InitiativeStatus
    confidence: InitiativeConfidence
    participating_repositories: List[str]
    understanding_ids: List[str]
    knowledge_ids: List[str]
    build_at: str
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    statement: str = field(default="")
    schema_version: str = SCHEMA_VERSION
    id: Optional[str] = None

    @property
    def understanding_count(self) -> int:
        return len(self.understanding_ids)

    @property
    def knowledge_count(self) -> int:
        return len(self.knowledge_ids)

    @property
    def repo_count(self) -> int:
        return len(self.participating_repositories)

    def to_row(self) -> InitiativeRow:
        return InitiativeRow(
            id=self.id or self._generate_id(),
            title=self.title,
            statement=self.statement,
            initiative_type=self.type.value,
            status=self.status.value,
            confidence=self.confidence.value,
            started_at=self.started_at,
            updated_at=self.updated_at,
            completed_at=self.completed_at,
            participating_repositories=",".join(self.participating_repositories),
            understanding_ids=",".join(self.understanding_ids),
            knowledge_ids=",".join(self.knowledge_ids),
            build_at=self.build_at,
            created_at=self.created_at,
            schema_version=self.schema_version,
        )

    def _generate_id(self) -> str:
        """Deterministic id from type + title. Stable across builds (the (type,
        title) key is the dedup key in build()), so repeated builds REPLACE the
        same row instead of inserting a new one. created_at varies per build and
        must NOT be part of the id or idempotency breaks (duplicate rows)."""
        return f"{self.type.value}:{self.title}"

    @classmethod
    def from_row(cls, row) -> "Initiative":
        if isinstance(row, InitiativeRow):
            return cls(
                id=row.id, type=InitiativeType.from_str(row.initiative_type),
                title=row.title, status=InitiativeStatus.from_str(row.status),
                confidence=InitiativeConfidence.from_str(row.confidence),
                participating_repositories=[
                    r for r in (row.participating_repositories or "").split(",") if r
                ],
                understanding_ids=[
                    k for k in (row.understanding_ids or "").split(",") if k
                ],
                knowledge_ids=[
                    k for k in (row.knowledge_ids or "").split(",") if k
                ],
                build_at=row.build_at, started_at=row.started_at,
                completed_at=row.completed_at,
                created_at=row.created_at, updated_at=row.updated_at,
                schema_version=cls._coerce_version(row),
            )
        return cls(
            id=row["id"], type=InitiativeType.from_str(row["initiative_type"]),
            title=row["title"], status=InitiativeStatus.from_str(row["status"]),
            confidence=InitiativeConfidence.from_str(row["confidence"]),
            participating_repositories=[
                r for r in (row["participating_repositories"] or "").split(",") if r
            ],
            understanding_ids=[
                k for k in (row["understanding_ids"] or "").split(",") if k
            ],
            knowledge_ids=[
                k for k in (row["knowledge_ids"] or "").split(",") if k
            ],
            build_at=row["build_at"], started_at=row["started_at"],
            completed_at=row["completed_at"], created_at=row["created_at"],
            updated_at=row["updated_at"],
            schema_version=cls._coerce_version(row),
        )

    @classmethod
    def _coerce_version(cls, row) -> str:
        """Reject stored initiative whose contract version is unknown (Law 24).

        Rows predating versioning are backfilled with the current version.
        """
        if isinstance(row, InitiativeRow):
            version = getattr(row, "schema_version", None)
        else:
            version = row["schema_version"] if "schema_version" in row.keys() else None
        if version is None:
            return cls.SCHEMA_VERSION
        if version != cls.SCHEMA_VERSION:
            raise ValueError(
                f"Initiative schema_version {version!r} != current {cls.SCHEMA_VERSION!r}")
        return version
