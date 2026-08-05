"""Relationship Layer — how close Friday and the operator are (Wave 10 §3.3).

Rebuilds V3's relationship/sentiment stack V4-native on the existing
``relationships`` table: depth is computed from *real interaction data*
(exchanges, sessions, mission completions, disclosed facts) and mapped
to tone + verbosity + briefing length. Depth is monotonic — more
interaction → deeper, never suddenly shallower.

    depth (depth.py) — RelationshipEngine: signals → depth → persist
    tones (tones.py) — tone/verbosity/briefing by depth (morning brevity)

Usage:
    from friday_v6.relationship import RelationshipEngine
    engine = RelationshipEngine(conn)
    status = engine.refresh()   # recompute from real data + persist
"""

from __future__ import annotations

try:
    from .depth import (
        DEFAULT_PEER,
        RelationshipEngine,
        compute_depth,
        level_name,
    )
    from .tones import (
        DIRECTION_TONES,
        MORNING_UNTIL_HOUR,
        ToneDirection,
        ToneSelector,
        briefing_length,
        effective_tone,
        effective_verbosity,
        tone_for,
        verbosity_for,
    )
    _RELATIONSHIP_AVAILABLE = True
except ImportError:  # pragma: no cover - defensive stub
    DEFAULT_PEER = "operator"  # type: ignore
    RelationshipEngine = None  # type: ignore
    compute_depth = None  # type: ignore
    level_name = None  # type: ignore
    DIRECTION_TONES = ("casual", "formal", "warm", "friendly", "close",
                       "neutral")  # type: ignore
    MORNING_UNTIL_HOUR = 11  # type: ignore
    ToneDirection = None  # type: ignore
    ToneSelector = None  # type: ignore
    briefing_length = None  # type: ignore
    effective_tone = None  # type: ignore
    effective_verbosity = None  # type: ignore
    tone_for = None  # type: ignore
    verbosity_for = None  # type: ignore
    _RELATIONSHIP_AVAILABLE = False


def is_available() -> bool:
    """Whether the relationship layer is implemented yet."""
    return _RELATIONSHIP_AVAILABLE


__all__ = [
    "DIRECTION_TONES",
    "DEFAULT_PEER",
    "RelationshipEngine",
    "compute_depth",
    "level_name",
    "MORNING_UNTIL_HOUR",
    "ToneDirection",
    "ToneSelector",
    "briefing_length",
    "effective_tone",
    "effective_verbosity",
    "tone_for",
    "verbosity_for",
    "is_available",
    "_RELATIONSHIP_AVAILABLE",
]
