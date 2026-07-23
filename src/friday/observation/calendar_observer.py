"""CalendarObserver (Milestone 7.7).

A NEW observer for the frozen Observation Engine. It observes *engineering
commitments* from calendar sources (deadlines, sprints, reviews, releases,
deployments, talks, exams, assignments) and emits deterministic engineering
observations that plug into the existing engine. No engine, context, or brain
changes.

DESIGN (privacy-first, metadata-only):

  This observer is a PURE READER. It reads a list of calendar *event* records
  through one of several interchangeable providers and maps each to Observation
  facts:

    - FixtureProvider   — offline list of event dicts (default; tests).
    - ICSProvider       — parses an .ics export file (deterministic, stdlib
                          only; opt-in via FRIDAY_CALENDAR_ICS).
    - Google/Outlook export providers — future, same seam.

  Only whitelisted METADATA is ever read or emitted: title, start, end,
  duration, category, location, recurring, cancelled, deadline, reminder,
  project. NOTES/BODY, attendees, email addresses, transcripts, and attachments
  are NEVER read and structurally cannot be emitted — the observer maps only the
  allow-listed fields and ignores everything else.

Observations emitted per event (subject = stable event id):
  title, start, end, duration_min, category, location, recurring, cancelled,
  deadline, reminder, project.

Run-level engineering signals (evidence-backed, no LLM):
  deadline_approaching, meeting_heavy_week, release_week, exam_period,
  planning_session, review_workload, engineering_focus_window.
  Thresholds are frozen; causes cite the evidence.

Confidence follows the Observation Engine vocabulary (Observed/Derived/Inferred).
No LLM, no embeddings, no planner, no agents, no OAuth, no daemon.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Protocol

from .interface import Health, Observer, ObserverHealth
from .model import Confidence, Observation

# --- Config ----------------------------------------------------------------

CALENDAR_ICS_ENV = "FRIDAY_CALENDAR_ICS"

# Signal thresholds (frozen, evidence-backed).
DEADLINE_SOON_DAYS = 7        # deadline within this span -> approaching
MEETING_HEAVY_COUNT = 4       # >= this many meetings in the window
REVIEW_WORKLOAD_COUNT = 3     # >= this many reviews in the window
FOCUS_WINDOW_DAYS = 7         # "upcoming" / focus window length


# ---------------------------------------------------------------------------
# Classification (deterministic, frozen, no LLM)
# ---------------------------------------------------------------------------


class CalendarCategory:
    DEADLINE = "Deadline"
    MEETING = "Meeting"
    SPRINT = "Sprint"
    REVIEW = "Review"
    RELEASE = "Release"
    DEPLOYMENT = "Deployment"
    ASSIGNMENT = "Assignment"
    EXAM = "Exam"
    CONFERENCE = "Conference"
    PRESENTATION = "Presentation"
    REMINDER = "Reminder"
    PERSONAL = "Personal"
    UNKNOWN = "Unknown"


# Title keyword -> category hint (deterministic, no LLM).
# Source of truth in vocabulary.py — kept here for backward compat imports.
from ..vocabulary import TITLE_CATEGORY


def classify_event(title: str, category: Optional[str] = None) -> str:
    """Deterministic title/category -> CalendarCategory. Unknown maps to Unknown."""
    if category and category in vars(CalendarCategory).values():
        return category
    t = (title or "").lower()
    for needle, cat in TITLE_CATEGORY:
        if needle in t:
            return cat
    return CalendarCategory.UNKNOWN


# ---------------------------------------------------------------------------
# Calendar event model
# ---------------------------------------------------------------------------


class CalendarEvent:
    """One engineering commitment. Built from a provider dict. Metadata only."""

    def __init__(
        self,
        uid: str,
        title: str = "",
        start: Optional[str] = None,
        end: Optional[str] = None,
        category: Optional[str] = None,
        location: Optional[str] = None,
        recurring: bool = False,
        cancelled: bool = False,
        deadline: bool = False,
        reminder: bool = False,
        project: Optional[str] = None,
    ) -> None:
        self.uid = uid
        self.title = title or ""
        self.start = start
        self.end = end
        self.location = location
        self.recurring = recurring
        self.cancelled = cancelled
        self.reminder = reminder
        self.project = project
        # CalendarCategory: explicit > deadline flag > title heuristic > unknown.
        if category and category in vars(CalendarCategory).values():
            self.category = category
        elif deadline:
            self.category = CalendarCategory.DEADLINE
        else:
            self.category = classify_event(self.title, category)
        # A Deadline-category event is itself a deadline.
        self.deadline = bool(deadline) or (self.category == CalendarCategory.DEADLINE)

    @property
    def duration_min(self) -> Optional[int]:
        s, e = _parse_date(self.start), _parse_date(self.end)
        if s is None or e is None:
            return None
        return max(0, int((e - s).total_seconds() // 60))

    @classmethod
    def from_dict(cls, d: dict) -> "CalendarEvent":
        return cls(
            uid=str(d.get("uid") or d.get("id") or ""),
            title=d.get("title", ""),
            start=d.get("start"),
            end=d.get("end"),
            category=d.get("category"),
            location=d.get("location"),
            recurring=bool(d.get("recurring", False)),
            cancelled=bool(d.get("cancelled", False)),
            deadline=bool(d.get("deadline", False)),
            reminder=bool(d.get("reminder", False)),
            project=d.get("project"),
        )


# ---------------------------------------------------------------------------
# Provider seam (mirrors GitHub/Research observers)
# ---------------------------------------------------------------------------


class CalendarProvider(Protocol):
    def fetch(self) -> list[dict]:
        ...

    def describe(self) -> str:
        ...


class FixtureProvider:
    """Offline provider: returns pre-built event dicts or a JSON file."""

    def __init__(self, events: list[dict] | Path) -> None:
        self._source = events

    def fetch(self) -> list[dict]:
        if isinstance(self._source, Path):
            return _load_json(self._source)
        return list(self._source)

    def describe(self) -> str:
        if isinstance(self._source, Path):
            return f"fixture: {self._source}"
        return f"fixture: {len(self._source)} event(s)"


class ICSProvider:
    """Parses an .ics export (opt-in). Stdlib only; metadata only.

    Only allow-listed fields (uid, summary, dtstart, dtend, location, rrule,
    status) are read. DESCRIPTION, attendees, and attachments are ignored.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def fetch(self) -> list[dict]:
        text = _read_text(self.path)
        if not text:
            return []
        return [_ics_event_to_dict(e) for e in _split_ics_events(text)
                if e.get("UID") or e.get("SUMMARY")]

    def describe(self) -> str:
        return f"ics: {self.path}"


def _split_ics_events(text: str) -> list[dict]:
    """Split a VCALENDAR into per-VEVENT dicts of raw uppercase keys."""
    events: list[dict] = []
    current: Optional[dict] = None
    for raw in text.splitlines():
        line = raw.strip()
        if line == "BEGIN:VEVENT":
            current = {}
        elif line == "END:VEVENT":
            if current is not None:
                events.append(current)
            current = None
        elif current is not None and ":" in line:
            key, _, val = line.partition(":")
            current[key.strip().upper()] = val.strip()
    return events


def _ics_event_to_dict(e: dict) -> dict:
    uid = e.get("UID", "")
    # Strip attendee/private leakage: never read ATTENDEE/DESCRIPTION/ORGANIZER.
    return {
        "uid": uid,
        "title": e.get("SUMMARY", ""),
        "start": _ics_date(e.get("DTSTART", "")),
        "end": _ics_date(e.get("DTEND", "")),
        "location": e.get("LOCATION", "") or None,
        "recurring": bool(e.get("RRULE")),
        "cancelled": (e.get("STATUS", "").upper() == "CANCELLED"),
        "deadline": "due" in (e.get("SUMMARY", "").lower())
        or "deadline" in (e.get("SUMMARY", "").lower()),
        "category": None,  # classify from title later
        "project": None,
    }


_ICS_DATE_RE = re.compile(r"(\d{4})(\d{2})(\d{2})(T(\d{2})(\d{2})(\d{2}))?")


def _ics_date(value: str) -> Optional[str]:
    """Convert an ICS date (20260714T100000Z or 20260714) to ISO 8601."""
    value = (value or "").strip()
    if not value:
        return None
    value = value.replace("Z", "")
    # Normalize to a form with a literal T so the regex always matches:
    # "20260721090000" -> "20260721T090000".
    if "T" not in value and len(value) == 14:
        value = value[:8] + "T" + value[8:]
    m = _ICS_DATE_RE.match(value)
    if not m:
        return None
    y, mo, d, _, hh, mm, ss = (
        m.group(1), m.group(2), m.group(3), None,
        m.group(5) or "00", m.group(6) or "00", m.group(7) or "00")
    return f"{y}-{mo}-{d}T{hh}:{mm}:{ss}+00:00"


def _configured_ics() -> Optional[Path]:
    raw = os.environ.get(CALENDAR_ICS_ENV)
    return Path(raw).expanduser() if raw else None


def _load_json(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, ValueError, TypeError):
        return []
    if isinstance(data, dict):
        for key in ("events", "items"):
            if isinstance(data.get(key), list):
                return [d for d in data[key] if isinstance(d, dict)]
        return [data]
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    return []


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def default_provider() -> CalendarProvider:
    ics = _configured_ics()
    if ics:
        return ICSProvider(ics)
    return FixtureProvider([])  # healthy: nothing configured to observe


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_date(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    s = (value or "").strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _days_until(value: Optional[str]) -> Optional[int]:
    dt = _parse_date(value)
    if dt is None:
        return None
    return (dt - datetime.now(timezone.utc)).days


def _is_upcoming(value: Optional[str]) -> bool:
    d = _days_until(value)
    return d is not None and 0 <= d <= FOCUS_WINDOW_DAYS


# ---------------------------------------------------------------------------
# The observer
# ---------------------------------------------------------------------------


class CalendarObserver(Observer):
    name = "calendar"

    def __init__(self, provider: Optional[CalendarProvider] = None) -> None:
        # A provider is the ONLY input. Tests inject FixtureProvider.
        self.provider = provider or default_provider()
        self._at = _now()

    # --- Observer interface --------------------------------------------------

    def health(self, conn) -> ObserverHealth:
        events = self._safe_fetch()
        method = self.provider.describe()
        if not events:
            return ObserverHealth(
                True, Health.HEALTHY, method,
                "no calendar events configured to observe.")
        return ObserverHealth(True, Health.HEALTHY, method,
                              f"observing {len(events)} event(s).")

    def collect(self, conn) -> list[Observation]:
        events = [CalendarEvent.from_dict(d) for d in self._safe_fetch()]
        observations: list[Observation] = []
        self._at = _now()
        best: Optional[str] = None
        for e in events:
            if e.start and (best is None or e.start > best):
                best = e.start
        if best:
            self._at = best
        for e in events:
            observations.extend(self._event_facts(e))
        observations.extend(self._signals(events))
        observations.append(self._ws(len(events)))
        return observations

    def summarize(self, conn) -> str:
        events = [CalendarEvent.from_dict(d) for d in self._safe_fetch()]
        counts: dict[str, int] = {}
        upcoming = 0
        for e in events:
            if e.cancelled:
                continue
            counts[e.category] = counts.get(e.category, 0) + 1
            if _is_upcoming(e.start):
                upcoming += 1
        lines = [f"{label}\n{counts.get(cat, 0)}" for cat, label in (
            (CalendarCategory.DEADLINE, "Deadlines"),
            (CalendarCategory.MEETING, "Meetings"),
            (CalendarCategory.RELEASE, "Releases"),
            (CalendarCategory.ASSIGNMENT, "Assignments"),
            (CalendarCategory.EXAM, "Exams"),
            (CalendarCategory.REVIEW, "Reviews"),
        )]
        return (
            "Calendar Observer\n"
            "Healthy\n"
            f"Engineering events\n{len([e for e in events if not e.cancelled])}\n"
            + "\n".join(lines) + "\n"
            f"Upcoming\n{upcoming}"
        )

    # --- internals ----------------------------------------------------------

    def _safe_fetch(self) -> list[dict]:
        try:
            return self.provider.fetch()
        except Exception:
            return []

    def _obs(self, subject, aspect, value, conf, cause=None) -> Observation:
        return Observation(
            source=self.name, subject=subject, aspect=aspect, value=str(value),
            confidence=conf, observed_at=self._at, scope="", cause=cause,
        )

    def _event_facts(self, e: CalendarEvent) -> list[Observation]:
        subj = e.uid or e.title or "calendar"
        rows = [
            self._obs(subj, "title", e.title, Confidence.OBSERVED),
            self._obs(subj, "start", e.start or "", Confidence.OBSERVED),
            self._obs(subj, "end", e.end or "", Confidence.OBSERVED),
            self._obs(subj, "category", e.category, Confidence.OBSERVED),
            self._obs(subj, "recurring", "true" if e.recurring else "false",
                      Confidence.OBSERVED),
            self._obs(subj, "cancelled", "true" if e.cancelled else "false",
                      Confidence.OBSERVED),
            self._obs(subj, "deadline", "true" if e.deadline else "false",
                      Confidence.OBSERVED),
            self._obs(subj, "reminder", "true" if e.reminder else "false",
                      Confidence.OBSERVED),
        ]
        if e.location is not None:
            rows.append(self._obs(subj, "location", e.location,
                                  Confidence.OBSERVED))
        dur = e.duration_min
        if dur is not None:
            rows.append(self._obs(subj, "duration_min", str(dur),
                                  Confidence.OBSERVED))
        if e.project is not None:
            rows.append(self._obs(subj, "project", e.project,
                                  Confidence.OBSERVED))
        return rows

    def _signals(self, events: list[CalendarEvent]) -> list[Observation]:
        rows: list[Observation] = []
        meetings = reviews = releases = exams = sprints = 0
        deadlines_soon = 0
        focus_start: Optional[datetime] = None
        focus_end: Optional[datetime] = None
        for e in events:
            if e.cancelled:
                continue
            cat = e.category
            if cat == CalendarCategory.MEETING:
                meetings += 1
            elif cat == CalendarCategory.REVIEW:
                reviews += 1
            elif cat == CalendarCategory.RELEASE:
                releases += 1
            elif cat == CalendarCategory.EXAM:
                exams += 1
            elif cat == CalendarCategory.SPRINT:
                sprints += 1
            if e.deadline or cat == CalendarCategory.DEADLINE:
                du = _days_until(e.start)
                if du is not None and 0 <= du <= DEADLINE_SOON_DAYS:
                    deadlines_soon += 1
            s = _parse_date(e.start)
            if s is not None:
                if focus_start is None or s < focus_start:
                    focus_start = s
                if focus_end is None or s > focus_end:
                    focus_end = s

        if deadlines_soon >= 1:
            rows.append(self._obs(
                "calendar", "deadline_approaching", "true", Confidence.INFERRED,
                cause=f"{deadlines_soon} deadline(s) within "
                      f"{DEADLINE_SOON_DAYS} days."))
        if meetings >= MEETING_HEAVY_COUNT:
            rows.append(self._obs(
                "calendar", "meeting_heavy_week", "true", Confidence.DERIVED,
                cause=f"{meetings} meetings in the observed window "
                      f"(>= {MEETING_HEAVY_COUNT})."))
        if releases >= 1:
            rows.append(self._obs(
                "calendar", "release_week", "true", Confidence.DERIVED,
                cause=f"{releases} release event(s) scheduled."))
        if exams >= 1:
            rows.append(self._obs(
                "calendar", "exam_period", "true", Confidence.DERIVED,
                cause=f"{exams} exam(s) in the observed window."))
        if sprints >= 1:
            rows.append(self._obs(
                "calendar", "planning_session", "true", Confidence.DERIVED,
                cause=f"{sprints} sprint/planning event(s) scheduled."))
        if reviews >= REVIEW_WORKLOAD_COUNT:
            rows.append(self._obs(
                "calendar", "review_workload", "true", Confidence.DERIVED,
                cause=f"{reviews} review event(s) in the window "
                      f"(>= {REVIEW_WORKLOAD_COUNT})."))
        if focus_start is not None and focus_end is not None:
            span = (focus_end - focus_start).days + 1
            if span <= FOCUS_WINDOW_DAYS:
                rows.append(self._obs(
                    "calendar", "engineering_focus_window", str(span),
                    Confidence.DERIVED,
                    cause=f"engineering commitments span {span} day(s)."))
        return rows

    def _ws(self, n: int) -> Observation:
        return Observation(
            source=self.name, subject="calendar", aspect="events",
            value=str(n), confidence=Confidence.OBSERVED, observed_at=self._at,
            scope="", cause=None,
        )
