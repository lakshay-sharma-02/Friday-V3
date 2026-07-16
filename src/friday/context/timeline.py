"""Engineering timeline (Milestone 7.2).

Arranges correlated sessions on a single deterministic chronological axis and
inserts idle gaps where no work was observed. The timeline never reorders and
never infers work that was not observed — gaps are explicit "idle" entries.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

from .models import EngineeringSession, TimelineEntry

# A gap longer than this between sessions is reported as an idle entry.
IDLE_GAP_MIN = 30


def _parse(ts: str) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def build_timeline(sessions: List[EngineeringSession]) -> List[TimelineEntry]:
    """Chronological timeline: sessions + idle gaps, oldest first."""
    ordered = sorted(sessions, key=lambda s: s.start_time)
    entries: List[TimelineEntry] = []
    prev_end: Optional[datetime] = None

    for s in ordered:
        start = _parse(s.start_time)
        if start is None:
            continue
        if prev_end is not None:
            gap_min = (start - prev_end).total_seconds() / 60.0
            if gap_min >= IDLE_GAP_MIN:
                entries.append(TimelineEntry(
                    kind="idle",
                    start_time=_iso(prev_end),
                    end_time=_iso(start),
                    label="Idle",
                    detail=f"no engineering activity observed for "
                           f"{round(gap_min)} minutes.",
                ))
        entries.append(TimelineEntry(
            kind="session",
            start_time=s.start_time,
            end_time=s.end_time,
            label=s.activity.value,
            detail=(s.summary or f"worked on {s.primary_repo or 'workspace'}"),
            session=s,
        ))
        end = _parse(s.end_time)
        if end is not None:
            prev_end = end

    return entries


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()
