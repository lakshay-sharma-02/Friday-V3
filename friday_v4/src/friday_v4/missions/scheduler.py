"""Mission scheduler — time-aware sequencing of steps (Wave 9).

Given a mission's steps, the scheduler decides *when* each step should
run and *which* step is next. Deterministic and hermetic (pure datetime
math, no I/O):

- :meth:`Scheduler.schedule` assigns each step a ``scheduled_at``
  timestamp (evenly spaced from a start time, honoring a daily window).
- :meth:`Scheduler.next_due` picks the next step to run: the earliest
  pending step whose scheduled time has arrived — falling back to the
  first pending step when nothing is due (a mission with no schedule
  simply runs in position order).

The schedule is stored in each step's payload (``scheduled_at`` as ISO
8601 UTC), so it survives restarts via the V4 DB.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

from .models import MissionStep, StepStatus

#: Workday window (local-naive math; times are stored as UTC ISO).
_DAY_START_HOUR = 9
_DAY_END_HOUR = 18


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Scheduler:
    """Assigns and selects step times for a mission."""

    def __init__(self, interval_hours: float = 4.0,
                 day_start_hour: int = _DAY_START_HOUR,
                 day_end_hour: int = _DAY_END_HOUR) -> None:
        self.interval = timedelta(hours=interval_hours)
        self.day_start = day_start_hour
        self.day_end = day_end_hour

    # ── Scheduling ────────────────────────────────────────────────────

    def schedule(self, steps: Sequence[MissionStep],
                 start: Optional[datetime] = None) -> dict[str, str]:
        """Assign a ``scheduled_at`` (ISO UTC) per step id.

        Steps are spaced by ``interval`` from ``start`` (default now),
        rolled forward into the working-day window so a 9pm start
        doesn't schedule steps at 1am.

        Returns ``{step_id: iso_timestamp}`` — the caller persists it
        into each step's payload.
        """
        if not steps:
            return {}
        cursor = self._roll_into_window(start or _now())
        out: dict[str, str] = {}
        for step in steps:
            cursor = self._roll_into_window(cursor)
            out[step.id] = cursor.isoformat(timespec="seconds")
            cursor += self.interval
        return out

    def _roll_into_window(self, dt: datetime) -> datetime:
        """Move ``dt`` into the working-day window (UTC-based, naive)."""
        hour = dt.hour + dt.minute / 60
        if dt.weekday() >= 5:            # weekend → next Monday 9am
            days = 7 - dt.weekday()
            return (dt + timedelta(days=days)).replace(
                hour=self.day_start, minute=0, second=0, microsecond=0)
        if hour < self.day_start:
            return dt.replace(hour=self.day_start, minute=0,
                              second=0, microsecond=0)
        if hour >= self.day_end:
            # Roll to tomorrow 9am (Monday if Friday).
            days = 1 if dt.weekday() < 4 else 3
            return (dt + timedelta(days=days)).replace(
                hour=self.day_start, minute=0, second=0, microsecond=0)
        return dt

    # ── Next-due selection ────────────────────────────────────────────

    def next_due(self, steps: Sequence[MissionStep],
                 now: Optional[datetime] = None) -> Optional[MissionStep]:
        """The next step to run, or None when none is pending.

        Prefers the earliest *pending* step whose ``scheduled_at`` has
        arrived. If no step has a due schedule (or the schedule is in
        the future), falls back to the first pending step so missions
        without scheduling still progress in position order.
        """
        pending = [s for s in steps if s.status == StepStatus.PENDING]
        if not pending:
            return None
        now = now or _now()

        due = [s for s in pending if self._scheduled_at(s) is not None
               and self._scheduled_at(s) <= now]
        if due:
            return min(due, key=lambda s: self._scheduled_at(s))
        # Nothing due yet → first pending (position order).
        return min(pending, key=lambda s: s.position)

    def _scheduled_at(self, step: MissionStep) -> Optional[datetime]:
        """The step's scheduled time as an *aware* UTC datetime.

        Stored ISO strings may be naive (no ``+00:00``); normalize them
        to UTC so comparisons against ``datetime.now(timezone.utc)``
        never raise ``TypeError``.
        """
        raw = (step.payload or {}).get("scheduled_at")
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(str(raw))
        except (ValueError, TypeError):
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt


__all__ = ["Scheduler"]
