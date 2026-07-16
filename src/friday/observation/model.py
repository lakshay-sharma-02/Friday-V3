"""Observation data model (Milestone 7).

A single Observation is one fact about the engineering environment, produced by
one observer. Observations are intentionally flat and self-describing so any
future observer (Terminal, GitHub, Browser, Calendar, Filesystem, ...) plugs in
without touching the engine.

Confidence levels separate what we read directly from what we computed or
guessed:

  Observed  — directly measurable with the tool (git status, file mtime).
  Derived   — computed deterministically from observed facts within this run
              (commit-count delta, days-since-last-commit).
  Inferred  — a judgment call from observed/derived facts (dormant repo,
              repeated reverts). Always carries a cause so it is auditable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class Confidence(str, Enum):
    OBSERVED = "Observed"
    DERIVED = "Derived"
    INFERRED = "Inferred"

    @classmethod
    def from_str(cls, s: str) -> "Confidence":
        s = (s or "").strip().lower()
        for c in cls:
            if c.value.lower() == s:
                return c
        raise ValueError(f"{cls.__name__} has no member {s!r}")


class Health(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    DOWN = "down"

    @classmethod
    def from_str(cls, s: str) -> "Health":
        s = (s or "").strip()
        for h in cls:
            if h.value == s:
                return h
        raise ValueError(f"{cls.__name__} has no member {s!r}")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Observation:
    """One observation fact.

    `id` is deterministic (observed_at:source:subject:aspect) so re-writing the
    same fact in a run is idempotent. The engine diffs on (subject, aspect).
    """

    source: str
    subject: str
    aspect: str
    value: str
    confidence: Confidence = Confidence.OBSERVED
    observed_at: str = field(default_factory=now_iso)
    # `scope` qualifies the subject without overloading it: for a repo fact the
    # scope is the repository path; for a workspace fact scope may be empty.
    scope: str = ""
    detail: Optional[str] = None
    cause: Optional[str] = None

    @property
    def id(self) -> str:
        return f"{self.observed_at}:{self.source}:{self.subject}:{self.aspect}"

    def key(self) -> tuple[str, str]:
        return (self.subject, self.aspect)

    def to_row(self):
        from ..db import ObservationRow

        return ObservationRow(
            id=self.id,
            observed_at=self.observed_at,
            source=self.source,
            subject=self.subject,
            aspect=self.aspect,
            value=self.value,
            confidence=self.confidence.value,
            scope=self.scope,
            detail=self.detail,
        )

    @classmethod
    def from_row(cls, row) -> "Observation":
        return cls(
            source=row.source,
            subject=row.subject,
            aspect=row.aspect,
            value=row.value,
            confidence=Confidence.from_str(row.confidence),
            observed_at=row.observed_at,
            scope=row.scope or "",
            detail=row.detail,
            cause=row.detail,  # detail round-trips as the cause on read
        )


@dataclass
class Change:
    """A meaningful difference the engine emits between two runs.

    `kind` is the engineering concept in plain language (never internal vocab).
    `cause`, when present, is the evidence-backed reason (required for Inferred
    changes, recommended elsewhere).
    """

    subject: str
    kind: str
    old: Optional[str] = None
    new: Optional[str] = None
    cause: Optional[str] = None
    confidence: Confidence = Confidence.OBSERVED
    source: str = ""

    def to_text(self) -> str:
        parts = [f"{self.subject} {self.kind}"]
        if self.old is not None and self.new is not None:
            parts.append(f"({self.old} -> {self.new})")
        elif self.new is not None:
            parts.append(f"({self.new})")
        if self.cause:
            parts.append(f"because {self.cause}")
        return " ".join(parts) + "."
