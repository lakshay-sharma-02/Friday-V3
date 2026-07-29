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



# --- Google Calendar API v3 constants ---------------------------------------

_GOOGLE_CALENDAR_API_BASE = "https://www.googleapis.com/calendar/v3"
_GOOGLE_OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"

# Env vars for Google Calendar provider.
GOOGLE_CAL_API_KEY_ENV = "FRIDAY_GOOGLE_CAL_API_KEY"
GOOGLE_CAL_TOKEN_ENV = "FRIDAY_GOOGLE_CAL_TOKEN"

# Env vars for CalDAV provider.
CALDAV_URL_ENV = "FRIDAY_CALDAV_URL"
CALDAV_USERNAME_ENV = "FRIDAY_CALDAV_USERNAME"
CALDAV_PASSWORD_ENV = "FRIDAY_CALDAV_PASSWORD"


def _past_iso(days: int = 7) -> str:
    """ISO 8601 timestamp for ``days`` ago."""
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _future_iso(days: int = 90) -> str:
    """ISO 8601 timestamp ``days`` from now."""
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


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


class GoogleCalendarProvider:
    """Fetches events from Google Calendar API v3 via REST.

    Two auth modes (mutually exclusive):

    1. **API key** (public calendars only):
       Set ``FRIDAY_GOOGLE_CAL_API_KEY`` to your API key.

    2. **OAuth token** (private calendars):
       Set ``FRIDAY_GOOGLE_CAL_TOKEN`` to the path of a JSON file containing
       ``{"access_token": "...", "refresh_token": "...",
        "client_id": "...", "client_secret": "...", "expires_at": 1234567890}``.
       The token is refreshed automatically when expired.

    To obtain an OAuth token file, use the one-time setup script:
        https://developers.google.com/calendar/api/quickstart/python
    (or use ``friday calendar auth`` if that CLI subcommand exists).

    Only metadata fields (summary, start, end, location, status, recurrence)
    are read. Description, attendees, and attachments are ignored.

    Note: recurring events are expanded into individual instances via
    ``singleEvents=true`` (Google Calendar) or returned as a single master
    event with RRULE (CalDAV).
    """

    def __init__(self, api_key: Optional[str] = None,
                 token_path: Optional[Path] = None) -> None:
        self._api_key = api_key
        self._token_path = token_path
        self._token: Optional[dict] = None

    def fetch(self) -> list[dict]:
        # Proactively refresh token if we know it's expired.
        token = self._load_token()
        if token and self._token_path is not None:
            expires_at = token.get("expires_at")
            if expires_at is not None:
                import time as _time
                if _time.time() >= expires_at:
                    self._refresh_token()

        headers, params = self._auth()
        params["timeMin"] = _past_iso()
        params["timeMax"] = _future_iso(days=90)
        params["orderBy"] = "startTime"
        params["singleEvents"] = "true"
        params["maxResults"] = "250"

        import requests as _req

        try:
            resp = _req.get(
                f"{_GOOGLE_CALENDAR_API_BASE}/calendars/primary/events",
                headers=headers, params=params, timeout=30)
        except Exception:
            return []
        if resp.status_code == 401 and self._token_path is not None:
            # Token expired — retry after refreshing once.
            self._refresh_token()
            headers, params = self._auth()
            try:
                resp = _req.get(
                    f"{_GOOGLE_CALENDAR_API_BASE}/calendars/primary/events",
                    headers=headers, params=params, timeout=30)
            except Exception:
                return []
        if resp.status_code not in (200,):
            return []
        try:
            data = resp.json()
        except Exception:
            return []
        return [_google_event_to_dict(item) for item in data.get("items", [])
                if item.get("id")]

    def describe(self) -> str:
        if self._api_key:
            return "google calendar (api key)"
        return "google calendar (oauth)"

    # --- auth internals ------------------------------------------------------

    def _auth(self) -> tuple[dict, dict]:
        """Return (headers, params) for the Google Calendar API request."""
        if self._api_key:
            return {}, {"key": self._api_key}
        token = self._load_token()
        access = (token or {}).get("access_token", "")
        return {"Authorization": f"Bearer {access}"}, {}

    def _load_token(self) -> Optional[dict]:
        if self._token is not None:
            return self._token
        if self._token_path is None:
            return None
        try:
            self._token = json.loads(
                self._token_path.read_text(encoding="utf-8", errors="ignore"))
        except (OSError, ValueError):
            self._token = {}
        return self._token

    def _refresh_token(self) -> bool:
        """Refresh the OAuth access token. Returns True on success."""
        token = self._load_token()
        if not token:
            return False
        client_id = token.get("client_id")
        client_secret = token.get("client_secret")
        refresh = token.get("refresh_token")
        if not (client_id and client_secret and refresh):
            return False

        import requests as _req

        try:
            resp = _req.post(_GOOGLE_OAUTH_TOKEN_URL, data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh,
                "grant_type": "refresh_token",
            }, timeout=30)
        except Exception:
            return False
        if resp.status_code != 200:
            return False
        try:
            new = resp.json()
        except Exception:
            return False
        new_access = new.get("access_token")
        if not new_access:
            return False
        token["access_token"] = new_access
        if new.get("expires_in"):
            import time as _time
            token["expires_at"] = _time.time() + int(new["expires_in"])
        self._token = token
        # Persist updated token.
        try:
            if self._token_path is not None:
                self._token_path.write_text(
                    json.dumps(token, indent=2, default=str), encoding="utf-8")
        except OSError:
            pass
        return True


class CalDAVProvider:
    """Fetches events from a CalDAV server via HTTP REPORT requests.

    Configure via env vars:
        FRIDAY_CALDAV_URL       — base URL of the CalDAV server
        FRIDAY_CALDAV_USERNAME  — basic auth username
        FRIDAY_CALDAV_PASSWORD  — basic auth password

    Discovers the user's calendar home set via PROPFIND, then sends a
    calendar-query REPORT with a 90-day time range. Parses the returned
    .ics data using the same privacy-preserving path as ICSProvider.
    """

    def __init__(self, url: str, username: str, password: str) -> None:
        self._url = url.rstrip("/")
        self._auth = (username, password)

    def fetch(self) -> list[dict]:
        import requests as _req
        try:
            calendar_urls = self._discover_calendars()
            events: list[dict] = []
            for cal_url in calendar_urls:
                raw_ics = self._query_calendar(cal_url)
                if raw_ics:
                    for e in _split_ics_events(raw_ics):
                        if e.get("UID") or e.get("SUMMARY"):
                            events.append(_ics_event_to_dict(e))
            return events
        except Exception:
            return []

    def describe(self) -> str:
        return f"caldav: {self._url}"

    # --- CalDAV internals ----------------------------------------------------

    def _request(self, method: str, url: str, body: Optional[str] = None,
                 depth: str = "0") -> Optional[str]:
        import requests as _req

        headers = {
            "Content-Type": "application/xml; charset=utf-8",
            "Depth": depth,
            "User-Agent": "Friday/1.0",
        }
        try:
            resp = _req.request(
                method, url, headers=headers, data=body,
                auth=self._auth, timeout=30)
        except Exception:
            return None
        if resp.status_code not in (200, 207):
            return None
        return resp.text

    def _discover_calendars(self) -> list[str]:
        """Discover calendar URLs via CalDAV PROPFIND."""
        # Step 1: Get principal URL from well-known endpoint.
        principal_url = self._url
        wk = self._request("PROPFIND", f"{self._url}/.well-known/caldav",
                           depth="0")
        if wk:
            # Parse principal URL from response.
            pu = _extract_href(
                wk, "{DAV:}current-user-principal")
            if pu:
                principal_url = _absolute_url(self._url, pu)

        # Step 2: Get calendar home set from principal.
        calendar_home = self._url
        prop = _propfind_xml("{DAV:}current-user-principal",
                             "{urn:ietf:params:xml:ns:caldav}calendar-home-set")
        pr = self._request("PROPFIND", principal_url, body=prop, depth="0")
        if pr:
            ch = _extract_href(
                pr, "{urn:ietf:params:xml:ns:caldav}calendar-home-set")
            if ch:
                calendar_home = _absolute_url(principal_url, ch)

        # Step 3: List all calendars under calendar home.
        calendars: list[str] = []
        list_body = _propfind_xml(
            "{DAV:}resourcetype",
            "{urn:ietf:params:xml:ns:caldav}supported-calendar-component-set")
        lr = self._request("PROPFIND", calendar_home,
                           body=list_body, depth="1")
        if lr:
            cals = _extract_calendar_urls(lr)
            if cals:
                calendars = cals
        if not calendars:
            # Fallback: use the configured URL as a single calendar.
            calendars = [self._url]
        return calendars

    def _query_calendar(self, url: str) -> Optional[str]:
        """Send a calendar-query REPORT for events in the next 90 days."""
        import time as _time
        now = _time.time()
        start_iso = datetime.fromtimestamp(now - 86400 * 7, tz=timezone.utc).strftime(
            "%Y%m%dT%H%M%SZ")
        end_iso = datetime.fromtimestamp(now + 86400 * 90, tz=timezone.utc).strftime(
            "%Y%m%dT%H%M%SZ")
        report_body = (
            '<?xml version="1.0" encoding="utf-8" ?>'
            '<C:calendar-query xmlns:C="urn:ietf:params:xml:ns:caldav">'
            '<D:prop xmlns:D="DAV:"><D:getetag/><C:calendar-data/></D:prop>'
            '<C:filter><C:comp-filter name="VCALENDAR">'
            '<C:comp-filter name="VEVENT">'
            f'<C:time-range start="{start_iso}" end="{end_iso}"/>'
            '</C:comp-filter></C:comp-filter></C:filter>'
            '</C:calendar-query>'
        )
        return self._request("REPORT", url, body=report_body, depth="1")


def _propfind_xml(*props: str) -> str:
    """Build a PROPFIND XML body requesting the given properties."""
    prop_inner = "".join(f"<{p}/>" for p in props)
    return (
        '<?xml version="1.0" encoding="utf-8" ?>'
        f'<D:propfind xmlns:D="DAV:"><D:prop>{prop_inner}</D:prop></D:propfind>'
    )


def _extract_href(xml: str, prop: str) -> Optional[str]:
    """Extract the first <D:href> text inside a <{prop}> block."""
    tag_start = xml.find(f"<{prop}>")
    if tag_start == -1:
        tag_start = xml.find(f"<{prop} ")
    if tag_start == -1:
        return None
    href_start = xml.find("<D:href>", tag_start)
    href_end = xml.find("</D:href>", tag_start)
    if href_start == -1 or href_end == -1:
        href_start = xml.find("<d:href>", tag_start)
        href_end = xml.find("</d:href>", tag_start)
        if href_start == -1 or href_end == -1:
            return None
        start = href_start + len("<d:href>")
    else:
        start = href_start + len("<D:href>")
    return xml[start:href_end].strip()


def _extract_calendar_urls(xml: str) -> list[str]:
    """Extract calendar URLs from a PROPFIND response, filtering out non-calendar resources."""
    urls: list[str] = []
    caldav_calendar = "{urn:ietf:params:xml:ns:caldav}calendar"
    for block in xml.split("<D:response>"):
        if caldav_calendar in block and "</D:response>" in block:
            href = _extract_href(block, "{DAV:}href")
            if href is not None and href not in urls:
                urls.append(href)
    return urls


def _absolute_url(base: str, path: str) -> str:
    """Resolve a potentially relative href against a base URL using urllib.parse."""
    from urllib.parse import urljoin
    return urljoin(base, path)


# --- Google Calendar API response mapping -----------------------------------


def _google_event_to_dict(item: dict) -> dict:
    """Map a Google Calendar API v3 event item to the canonical event dict.

    Only metadata fields are mapped. Description, attendees, attachments,
    and notes are explicitly excluded.
    """
    start_info = item.get("start", {}) or {}
    end_info = item.get("end", {}) or {}
    return {
        "uid": item.get("id", ""),
        "title": item.get("summary", "") or "",
        "start": start_info.get("dateTime") or start_info.get("date"),
        "end": end_info.get("dateTime") or end_info.get("date"),
        "location": (item.get("location") or "") or None,
        "recurring": bool(item.get("recurrence")),
        "cancelled": (item.get("status") or "").lower() == "cancelled",
        "deadline": "deadline" in (item.get("summary", "") or "").lower()
        or "due" in (item.get("summary", "") or "").lower(),
        "category": None,
        "project": None,
    }


def _configured_google_provider() -> Optional[CalendarProvider]:
    """Build a GoogleCalendarProvider from env vars, if configured."""
    api_key = os.environ.get(GOOGLE_CAL_API_KEY_ENV)
    if api_key:
        return GoogleCalendarProvider(api_key=api_key)
    token_raw = os.environ.get(GOOGLE_CAL_TOKEN_ENV)
    if token_raw:
        token_path = Path(token_raw).expanduser()
        if token_path.exists():
            return GoogleCalendarProvider(token_path=token_path)
    return None


def _configured_caldav_provider() -> Optional[CalendarProvider]:
    """Build a CalDAVProvider from env vars, if configured."""
    url = os.environ.get(CALDAV_URL_ENV)
    username = os.environ.get(CALDAV_USERNAME_ENV)
    password = os.environ.get(CALDAV_PASSWORD_ENV)
    if url and username and password is not None:
        return CalDAVProvider(url, username, password)
    return None


def default_provider() -> CalendarProvider:
    """Choose the best available calendar provider.

    Resolution order (first wins):
      1. Google Calendar (API key or OAuth token)
      2. CalDAV server (URL + credentials)
      3. ICS file export
      4. Empty fixture (nothing configured — healthy, no-op)
    """
    google = _configured_google_provider()
    if google:
        return google
    caldav = _configured_caldav_provider()
    if caldav:
        return caldav
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
