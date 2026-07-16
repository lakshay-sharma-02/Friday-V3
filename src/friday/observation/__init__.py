"""Observation Engine (Milestone 7).

Deterministic, pull-based understanding of the engineering environment. No
daemon, no scheduler, no watcher, no LLM. Run explicitly via `friday observe`,
`friday observers`, or `friday observer <name>`.

Public surface:
  - Observation / Change / Confidence / Health  (model)
  - Observer / ObserverHealth                    (interface)
  - GitObserver                                  (built-in observer)
  - ObserverRegistry / default_registry          (registration)
  - ObservationEngine / ObservationRun / diff_observations
"""

from __future__ import annotations

from .engine import (
    ObservationEngine,
    ObservationRun,
    ObserverResult,
    diff_observations,
    format_run,
)
from .artifact_observer import ArtifactObserver, classify
from .calendar_observer import CalendarCategory, CalendarEvent, CalendarObserver
from .git_observer import GitObserver
from .github_observer import GitHubObserver, RepositorySnapshot
from .interface import Health, Observer, ObserverHealth
from .model import Change, Confidence, Observation, now_iso
from .registry import ObserverRegistry, default_registry
from .research_observer import (
    Category,
    ResearchObserver,
    ResearchResource,
    classify_research,
    topic_of,
)
from .terminal_observer import TerminalObserver, categorize

__all__ = [
    "Observation",
    "Change",
    "Confidence",
    "Health",
    "Observer",
    "ObserverHealth",
    "GitObserver",
    "TerminalObserver",
    "GitHubObserver",
    "RepositorySnapshot",
    "ResearchObserver",
    "ResearchResource",
    "CalendarObserver",
    "CalendarEvent",
    "CalendarCategory",
    "Category",
    "ArtifactObserver",
    "categorize",
    "classify_research",
    "topic_of",
    "ObserverRegistry",
    "default_registry",
    "ObservationEngine",
    "ObservationRun",
    "ObserverResult",
    "diff_observations",
    "now_iso",
]
