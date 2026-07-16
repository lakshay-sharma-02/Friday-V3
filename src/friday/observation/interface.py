"""Observer interface (Milestone 7).

An Observer is a deterministic reader of one slice of the engineering
environment. It produces a flat list of Observation facts and can describe its
own health. Observers never mutate knowledge, run an LLM, or schedule work.

Future observers (Terminal, GitHub, Browser, Calendar, Filesystem) implement
exactly these four methods; the ObservationEngine never needs to change.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .model import Confidence, Health, Observation


@dataclass
class ObserverHealth:
    """Self-reported health of an observer at collection time."""

    healthy: bool
    status: Health = Health.HEALTHY
    method: str = ""  # how health was determined (e.g. "git available")
    detail: Optional[str] = None

    def degraded(self, detail: str, method: str = "") -> "ObserverHealth":
        return ObserverHealth(False, Health.DEGRADED, method, detail)

    def down(self, detail: str, method: str = "") -> "ObserverHealth":
        return ObserverHealth(False, Health.DOWN, method, detail)


class Observer:
    """Base class every observer extends.

    Subclasses MUST override `name`, `collect`, `summarize`, and `health`.
    `collect` returns only fresh observations for the current run; the engine
    handles persistence and diffing.
    """

    #: Unique, stable identifier (used as the Observation.source).
    name: str = "observer"

    def collect(self, conn) -> list[Observation]:
        """Return observations for this run. Deterministic; no side effects.

        `conn` is the live DB connection (read-only use expected). A healthy
        observer returns [] rather than raising when there is nothing to see.
        """
        raise NotImplementedError

    def summarize(self, conn) -> str:
        """A one-line human summary of what this observer currently sees."""
        raise NotImplementedError

    def health(self, conn) -> ObserverHealth:
        """Report whether this observer can do its job right now."""
        raise NotImplementedError
