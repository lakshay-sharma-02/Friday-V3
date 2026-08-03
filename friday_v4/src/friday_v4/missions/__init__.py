"""Missions Layer — persistent goals with real progress (Wave 9).

"Friday, I need to ship the auth refactor by Friday" becomes a
persistent, restart-safe mission with ordered, schedulable steps —
and Friday actually *advances* it through the execution layer:

    planner (planner.py)  → goal → StepPlan list (deterministic)
    engine (engine.py)    → create / start / advance / adapt / replan
    scheduler (scheduler.py) → when each step runs, which is next
    progress (progress.py)   → the feed surfaces render

Steps with an ``action_type`` execute through
:func:`friday_v4.execution.execute` (gate → sandbox → audit); manual
steps are completed by the operator. Adaptation never silently rewrites
a plan — it reports "plan changed because …".

**Status:** Wave 9 — built (2026-08). Pure stdlib, hermetic tests. The
imports below stay guarded so importing this package never crashes the
rest of Friday V4.

Usage:
    from friday_v4.missions import MissionEngine, Planner
    engine = MissionEngine(conn)
    mission = engine.create("ship the auth refactor", planner=Planner())
    engine.start(mission.id)
    result = engine.advance(mission.id, confirm_fn=confirm)
"""

from __future__ import annotations

try:
    from .engine import AdaptationReport, AdvanceResult, MissionEngine
    from .models import (
        Mission,
        MissionStatus,
        MissionStep,
        StepStatus,
        make_step_payload,
    )
    from .claude_planner import ClaudePlanner, make_planner
    from .planner import Planner, StepPlan
    from .progress import ProgressReport, progress_feed, report, summary
    from .scheduler import Scheduler
    _MISSIONS_AVAILABLE = True
except ImportError:  # pragma: no cover - defensive stub
    AdaptationReport = None  # type: ignore
    AdvanceResult = None  # type: ignore
    MissionEngine = None  # type: ignore
    Mission = None  # type: ignore
    MissionStatus = None  # type: ignore
    MissionStep = None  # type: ignore
    StepStatus = None  # type: ignore
    make_step_payload = None  # type: ignore
    Planner = None  # type: ignore
    StepPlan = None  # type: ignore
    ClaudePlanner = None  # type: ignore
    make_planner = None  # type: ignore
    ProgressReport = None  # type: ignore
    progress_feed = None  # type: ignore
    report = None  # type: ignore
    summary = None  # type: ignore
    Scheduler = None  # type: ignore
    _MISSIONS_AVAILABLE = False


def is_available() -> bool:
    """Whether the missions layer is implemented yet."""
    return _MISSIONS_AVAILABLE


__all__ = [
    "AdaptationReport",
    "AdvanceResult",
    "MissionEngine",
    "Mission",
    "MissionStatus",
    "MissionStep",
    "StepStatus",
    "make_step_payload",
    "Planner",
    "StepPlan",
    "ClaudePlanner",
    "make_planner",
    "ProgressReport",
    "progress_feed",
    "report",
    "summary",
    "Scheduler",
    "is_available",
    "_MISSIONS_AVAILABLE",
]
