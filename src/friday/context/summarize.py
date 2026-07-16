"""Context summaries (Milestone 7.2).

Deterministic, evidence-only summaries over a window of sessions: today's work,
current focus, repositories touched, estimated active time, context switches,
longest uninterrupted session, most active repository. No LLM, no narrative
generation beyond labeling derived from session activities.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import List, Optional

from .models import ContextSummary, EngineeringSession, SessionActivity


def _day_of(ts: str) -> str:
    try:
        dt = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%Y-%m-%d")


def summarize_day(sessions: List[EngineeringSession], day: Optional[str] = None
                  ) -> ContextSummary:
    """Summarize a list of sessions (assumed to be one day)."""
    sessions = sorted(sessions, key=lambda s: s.start_time)
    if not sessions:
        return ContextSummary(
            day=day or "",
            session_count=0,
            repositories=[],
            estimated_active_min=0.0,
            context_switches=0,
            longest_session_min=0.0,
            most_active_repo=None,
            current_focus=None,
            sessions=[],
        )

    day = day or _day_of(sessions[0].start_time)
    repos: list[str] = []
    for s in sessions:
        for r in s.repositories:
            if r not in repos:
                repos.append(r)
    active = round(sum(s.duration_min for s in sessions), 2)
    longest = max((s.duration_min for s in sessions), default=0.0)

    # Most active repository: by summed session duration.
    repo_minutes: Counter[str] = Counter()
    for s in sessions:
        if s.primary_repo:
            repo_minutes[s.primary_repo] += s.duration_min
    most_active = repo_minutes.most_common(1)[0][0] if repo_minutes else None

    # Current focus: the most recent session's primary repo + activity.
    latest = sessions[-1]
    focus = latest.primary_repo
    if latest.activity not in (SessionActivity.UNKNOWN,):
        focus = f"{latest.primary_repo} ({latest.activity.value})" \
            if latest.primary_repo else latest.activity.value

    # Context switches: adjacent sessions whose primary repo changes.
    switches = 0
    prev_repo = None
    for s in sessions:
        if s.primary_repo and prev_repo and s.primary_repo != prev_repo:
            switches += 1
        if s.primary_repo:
            prev_repo = s.primary_repo

    return ContextSummary(
        day=day,
        session_count=len(sessions),
        repositories=repos,
        estimated_active_min=active,
        context_switches=switches,
        longest_session_min=longest,
        most_active_repo=most_active,
        current_focus=focus,
        sessions=sessions,
    )


def _fmt_min(minutes: float) -> str:
    if minutes < 60:
        return f"{round(minutes)} minutes"
    h = int(minutes // 60)
    m = round(minutes % 60)
    if m == 0:
        return f"{h} hour" + ("s" if h != 1 else "")
    return f"{h}h {m}m"
