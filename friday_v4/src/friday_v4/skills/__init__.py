"""Skills & Self-Improvement — learned workflows, shadow-first (Wave 10 §3.4).

Rebuilds V3's ``skill_formation.py`` V4-native on the existing
``skills`` table: Friday watches the operator's real executed actions,
forms a skill from a repeated sequence, runs it in shadow mode (records
what it *would* do — never executes), and promotes it only after N
shadow matches + operator approval.

    registry (registry.py) — typed skill lifecycle (shadow → verified →
                            promoted, failure demotion)
    replay (replay.py)     — learn a skill from the audit log
    watcher (watcher.py)   — explicit "watch me" demonstration capture
    noticer (noticer.py)   — "I noticed you do this every time" offers
    shadow (shadow.py)     — shadow-first verification (no execution)
    dispatch (dispatch.py) — suggest the next step on context match

Usage:
    from friday_v4.skills import ReplayExecutor, ShadowExecutor
    replay = ReplayExecutor(conn)
    formed = replay.learn()          # shadow skills from real patterns
    shadow = ShadowExecutor(conn)
    matches = shadow.sweep()         # record what skills *would* do
"""

from __future__ import annotations

try:
    from .dispatch import SkillDispatcher
    from .registry import (
        DEFAULT_DEMOTE_FAILURES,
        DEFAULT_VERIFY_MATCHES,
        MATCH_CONFIDENCE_STEP,
        Skill,
        SkillRegistry,
        STATE_DEMOTED,
        STATE_PROMOTED,
        STATE_SHADOW,
        STATE_VERIFIED,
    )
    from .noticer import RepetitionNoticer
    from .replay import DEFAULT_LOOKBACK, DEFAULT_MIN_OCCURRENCES, ReplayExecutor
    from .shadow import ShadowExecutor
    from .watcher import WatchRecorder
    _SKILLS_AVAILABLE = True
except ImportError:  # pragma: no cover - defensive stub
    SkillDispatcher = None  # type: ignore
    DEFAULT_DEMOTE_FAILURES = 3  # type: ignore
    DEFAULT_VERIFY_MATCHES = 3  # type: ignore
    MATCH_CONFIDENCE_STEP = 0.05  # type: ignore
    Skill = None  # type: ignore
    SkillRegistry = None  # type: ignore
    STATE_DEMOTED = "demoted"  # type: ignore
    STATE_PROMOTED = "promoted"  # type: ignore
    STATE_SHADOW = "shadow"  # type: ignore
    STATE_VERIFIED = "verified"  # type: ignore
    DEFAULT_LOOKBACK = 200  # type: ignore
    DEFAULT_MIN_OCCURRENCES = 2  # type: ignore
    RepetitionNoticer = None  # type: ignore
    WatchRecorder = None  # type: ignore
    ReplayExecutor = None  # type: ignore
    ShadowExecutor = None  # type: ignore
    _SKILLS_AVAILABLE = False


def is_available() -> bool:
    """Whether the skills layer is implemented yet."""
    return _SKILLS_AVAILABLE


__all__ = [
    "SkillDispatcher",
    "DEFAULT_DEMOTE_FAILURES",
    "DEFAULT_VERIFY_MATCHES",
    "MATCH_CONFIDENCE_STEP",
    "Skill",
    "SkillRegistry",
    "STATE_DEMOTED",
    "STATE_PROMOTED",
    "STATE_SHADOW",
    "STATE_VERIFIED",
    "DEFAULT_LOOKBACK",
    "DEFAULT_MIN_OCCURRENCES",
    "RepetitionNoticer",
    "ReplayExecutor",
    "ShadowExecutor",
    "WatchRecorder",
    "is_available",
    "_SKILLS_AVAILABLE",
]
