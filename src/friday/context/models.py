"""Engineering Context models (Milestone 7.2).

A layer ABOVE the Observation Engine. Observations are raw facts; Engineering
Context groups them into *sessions* of real work, correlates each session to an
evidence-backed activity, and summarizes the engineering day.

Everything is deterministic and evidence-backed. No LLM, no embeddings, no
planner. A session never duplicates raw observations — it references their ids.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional


class SessionActivity(str, Enum):
    """Conservative, evidence-backed activity labels.

    A label is only assigned when the evidence unambiguously supports it. When
    observations could belong to different activities, the conservative choice
    is UNKNOWN (so the session stays a neutral container that can be merged
    later). Splitting is always preferred over fusing.
    """

    UNKNOWN = "unknown"
    COMMITTING = "committing"
    FEATURE_WORK = "feature implementation"
    DOCUMENTATION = "documentation"
    DEBUGGING = "debugging"
    TESTING = "testing"
    REFACTORING = "refactoring"
    REVIEW = "review"
    IDLE = "idle"

    @classmethod
    def from_str(cls, s: str) -> "SessionActivity":
        s = (s or "").strip().lower()
        for a in cls:
            if a.value.lower() == s:
                return a
        return cls.UNKNOWN


class Confidence(str, Enum):
    """Confidence that the session's activity label is correct.

    Alias of the Observation Engine's Confidence so the chain has ONE vocabulary.
    """

    OBSERVED = "Observed"
    DERIVED = "Derived"
    INFERRED = "Inferred"

    @classmethod
    def from_str(cls, s: str) -> "Confidence":
        s = (s or "").strip().lower()
        for c in cls:
            if c.value.lower() == s:
                return c
        return cls.OBSERVED


# Single source of truth: the Observation Engine's Confidence enum.
from ..observation.model import Confidence as _ObsConfidence  # noqa: E402

Confidence = _ObsConfidence  # type: ignore[assignment,misc]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class EngineeringSession:
    """One contiguous stretch of engineering work on one or more repositories.

    Built deterministically from a window of observations. References (does NOT
    copy) the observation ids that justify it.
    """

    start_time: str
    end_time: str
    repositories: List[str]
    observations: List[str]  # observation ids (evidence)
    activity: SessionActivity = SessionActivity.UNKNOWN
    confidence: Confidence = Confidence.DERIVED
    primary_repo: Optional[str] = None
    branch: Optional[str] = None
    summary: Optional[str] = None
    built_at: str = field(default_factory=now_iso)

    @property
    def duration_min(self) -> float:
        try:
            a = datetime.fromisoformat(self.start_time)
            b = datetime.fromisoformat(self.end_time)
        except ValueError:
            return 0.0
        return round((b - a).total_seconds() / 60.0, 2)

    @property
    def id(self) -> str:
        repo = self.primary_repo or ",".join(sorted(self.repositories))
        return f"{self.built_at}:{repo}:{self.start_time}"

    def to_row(self):
        from ..db import SessionRow

        return SessionRow(
            id=self.id,
            start_time=self.start_time,
            end_time=self.end_time,
            repositories=",".join(self.repositories),
            primary_repo=self.primary_repo,
            observations=",".join(self.observations),
            activity=self.activity.value,
            confidence=self.confidence.value,
            duration_min=self.duration_min,
            branch=self.branch,
            summary=self.summary,
            built_at=self.built_at,
        )

    @classmethod
    def from_row(cls, row) -> "EngineeringSession":
        return cls(
            start_time=row.start_time,
            end_time=row.end_time,
            repositories=[r for r in (row.repositories or "").split(",") if r],
            observations=[o for o in (row.observations or "").split(",") if o],
            activity=SessionActivity.from_str(row.activity),
            confidence=Confidence.from_str(row.confidence),
            primary_repo=row.primary_repo,
            branch=row.branch,
            summary=row.summary,
            built_at=row.built_at,
        )


@dataclass
class TimelineEntry:
    """One slot on the engineering timeline.

    Either a session (work happened) or an idle gap (no work observed).
    """

    kind: str  # "session" or "idle"
    start_time: str
    end_time: str
    label: str
    detail: Optional[str] = None
    session: Optional[EngineeringSession] = None

    @property
    def duration_min(self) -> float:
        try:
            a = datetime.fromisoformat(self.start_time)
            b = datetime.fromisoformat(self.end_time)
        except ValueError:
            return 0.0
        return round((b - a).total_seconds() / 60.0, 2)


@dataclass
class ContextSummary:
    """Deterministic summary of a day (or window) of engineering work."""

    day: str
    session_count: int
    repositories: List[str]
    estimated_active_min: float
    context_switches: int
    longest_session_min: float
    most_active_repo: Optional[str]
    current_focus: Optional[str]
    sessions: List[EngineeringSession] = field(default_factory=list)
