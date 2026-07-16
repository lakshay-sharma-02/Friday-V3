"""Engineering Context layer (Milestone 7.2).

Sits ABOVE the frozen Observation Engine and BELOW the (untouched) Brain. Turns
raw observations into engineering sessions, correlates them to evidence-backed
activities, and summarizes the day. Deterministic, append-only, no LLM.

Public surface:
  - EngineeringSession / SessionActivity / ContextSummary / TimelineEntry
  - build_sessions (grouping)
  - correlate / build_correlated (activity labeling)
  - build_timeline
  - summarize_day
  - ContextEngine
"""

from __future__ import annotations

from .correlate import build_correlated, correlate
from .engine import ContextEngine
from .models import (
    Confidence,
    ContextSummary,
    EngineeringSession,
    SessionActivity,
    TimelineEntry,
    now_iso,
)
from .session import build_sessions
from .summarize import summarize_day
from .timeline import build_timeline

__all__ = [
    "EngineeringSession",
    "SessionActivity",
    "Confidence",
    "ContextSummary",
    "TimelineEntry",
    "build_sessions",
    "correlate",
    "build_correlated",
    "build_timeline",
    "summarize_day",
    "ContextEngine",
    "now_iso",
]
