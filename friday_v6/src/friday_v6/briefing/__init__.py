"""Briefing Layer — morning/evening briefings + day narrative (Wave 11 §3.3).

Briefings are built from **real V4 state** (missions, security findings,
actions, memory, ambient events) — never template fluff. Length adapts
to relationship depth + time of day (Wave 10 tone rules). Briefings are
*offered*, never forced — ambient-not-intrusive.

**Status:** Wave 11 — built (2026-08). Pure stdlib, hermetic tests.

Usage:
    from friday_v6.briefing import build_briefing
    briefing = build_briefing(conn, kind="morning")
    print(briefing.text)
"""

from __future__ import annotations

try:
    from .briefing import build_briefing, Briefing
    from .narrative import day_narrative
    _BRIEFING_AVAILABLE = True
except ImportError:  # pragma: no cover - defensive stub
    build_briefing = None  # type: ignore
    Briefing = None  # type: ignore
    day_narrative = None  # type: ignore
    _BRIEFING_AVAILABLE = False


def is_available() -> bool:
    return _BRIEFING_AVAILABLE


__all__ = ["build_briefing", "Briefing", "day_narrative", "is_available",
           "_BRIEFING_AVAILABLE"]
