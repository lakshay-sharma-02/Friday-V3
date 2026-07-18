"""Milestone 7.7 — Calendar Observer tests.

Deterministic tests for CalendarObserver: it reads engineering *commitment*
metadata (deadlines, sprints, reviews, releases, exams, assignments) through an
offline FixtureProvider (and an .ics parser) and emits engineering observations
that plug into the frozen Observation Engine. No live calendar, no OAuth, no
network, no LLM.

Coverage: deadline, meeting, release, exam, assignment, recurring, cancelled,
privacy (no notes/attendees/email), registration, health, summary, engine
integration, offline fixtures, ICS parsing, and derived engineering signals.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from friday.db import connect, observations_all
from friday.observation import (
    CalendarObserver,
    CalendarEvent,
    Confidence,
    ObservationEngine,
    ObserverRegistry,
    default_registry,
)
from friday.observation.calendar_observer import (
    CalendarCategory,
    FixtureProvider,
    ICSProvider,
    classify_event,
)


def _ev(uid, **over):
    base = dict(uid=uid, title="", start="2026-07-20T10:00:00+00:00",
                end="2026-07-20T11:00:00+00:00", category=None, location=None,
                recurring=False, cancelled=False, deadline=False,
                reminder=False, project=None)
    base.update(over)
    return base


def _observer(events):
    return CalendarObserver(FixtureProvider(events))


def _soon(days: int) -> str:
    """ISO timestamp `days` from now (UTC) so date-relative signals don't rot."""
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%S+00:00")


# --- Classification (deterministic, no LLM) --------------------------------


def test_classify_deadline():
    assert classify_event("Project deadline") == CalendarCategory.DEADLINE
    assert classify_event("Assignment due") == CalendarCategory.DEADLINE


def test_classify_meeting():
    assert classify_event("Standup") == CalendarCategory.MEETING
    assert classify_event("Sync with team") == CalendarCategory.MEETING


def test_classify_release_review():
    assert classify_event("v1.2 release") == CalendarCategory.RELEASE
    assert classify_event("Code review session") == CalendarCategory.REVIEW


def test_classify_exam_assignment():
    assert classify_event("Final exam") == CalendarCategory.EXAM
    assert classify_event("Homework assignment") == CalendarCategory.ASSIGNMENT


def test_classify_explicit_category_overrides():
    assert classify_event("mystery", CalendarCategory.DEPLOYMENT) == \
        CalendarCategory.DEPLOYMENT


def test_classify_unknown():
    assert classify_event("Lunch") == CalendarCategory.UNKNOWN


# --- Deadline --------------------------------------------------------------


def test_deadline_event_observed():
    obs = {(o.subject, o.aspect): o for o in
           _observer([_ev("d1", title="Project deadline",
                          start="2026-07-20T10:00:00+00:00")]).collect(None)}
    assert obs[("d1", "category")].value == CalendarCategory.DEADLINE
    assert obs[("d1", "deadline")].value == "true"
    assert obs[("d1", "deadline")].confidence is __import__(
        "friday.observation", fromlist=["Confidence"]).Confidence.OBSERVED


# --- Meeting ---------------------------------------------------------------


def test_meeting_event_observed():
    obs = {(o.subject, o.aspect): o for o in
           _observer([_ev("m1", title="Weekly team sync",
                          location="Room 4")]).collect(None)}
    assert obs[("m1", "category")].value == CalendarCategory.MEETING
    assert obs[("m1", "location")].value == "Room 4"
    assert obs[("m1", "duration_min")].value == "60"


# --- Release ---------------------------------------------------------------


def test_release_event_observed():
    obs = {(o.subject, o.aspect): o for o in
           _observer([_ev("r1", title="v2.0 release",
                          start="2026-07-21T09:00:00+00:00")]).collect(None)}
    assert obs[("r1", "category")].value == CalendarCategory.RELEASE


# --- Exam ------------------------------------------------------------------


def test_exam_event_observed():
    obs = {(o.subject, o.aspect): o for o in
           _observer([_ev("e1", title="Midterm exam")]).collect(None)}
    assert obs[("e1", "category")].value == CalendarCategory.EXAM


# --- Assignment ------------------------------------------------------------


def test_assignment_event_observed():
    obs = {(o.subject, o.aspect): o for o in
           _observer([_ev("a1", title="Homework assignment 3")]).collect(None)}
    assert obs[("a1", "category")].value == CalendarCategory.ASSIGNMENT


# --- Recurring -------------------------------------------------------------


def test_recurring_event_observed():
    obs = {(o.subject, o.aspect): o for o in
           _observer([_ev("m1", title="Weekly standup", recurring=True)]).collect(None)}
    assert obs[("m1", "recurring")].value == "true"


# --- Cancelled -------------------------------------------------------------


def test_cancelled_event_observed():
    obs = {(o.subject, o.aspect): o for o in
           _observer([_ev("m1", title="Meeting", cancelled=True)]).collect(None)}
    assert obs[("m1", "cancelled")].value == "true"
    # Cancelled events are still emitted (facts), but excluded from signals/summary.
    assert ("m1", "title") in obs


# --- Privacy ---------------------------------------------------------------


def test_no_notes_attendees_email_emitted():
    ev = _ev("x1", title="Review", description="secret notes",
             attendees=["alice@example.com", "bob@work.com"],
             organizer="boss@corp.com", body="hidden transcript",
             attachments=["file.pdf"])
    obs = CalendarObserver(FixtureProvider([ev])).collect(None)
    blob = json.dumps([o.__dict__ for o in obs])
    assert "secret notes" not in blob
    assert "alice@example.com" not in blob
    assert "bob@work.com" not in blob
    assert "boss@corp.com" not in blob
    assert "hidden transcript" not in blob
    assert "file.pdf" not in blob
    ALLOWED = {
        "title", "start", "end", "category", "recurring", "cancelled",
        "deadline", "reminder", "location", "duration_min", "project",
        "events",
        "deadline_approaching", "meeting_heavy_week", "release_week",
        "exam_period", "planning_session", "review_workload",
        "engineering_focus_window",
    }
    assert all(o.aspect in ALLOWED for o in obs)


# --- Registration ----------------------------------------------------------


def test_calendar_registered_in_default_registry():
    assert "calendar" in default_registry()
    assert "research" in default_registry()


def test_register_duplicate_raises():
    reg = ObserverRegistry()
    reg.register(CalendarObserver(FixtureProvider([])))
    with pytest.raises(ValueError):
        reg.register(CalendarObserver(FixtureProvider([])))


# --- Health ----------------------------------------------------------------


def test_health_healthy_with_events():
    h = _observer([_ev("m1", title="Meeting")]).health(None)
    assert h.healthy is True
    assert h.status.value == "healthy"


def test_health_healthy_when_empty():
    h = _observer([]).health(None)
    assert h.healthy is True
    assert h.status.value == "healthy"


# --- Engine integration ----------------------------------------------------


def test_end_to_end_through_observation_engine(tmp_path):
    conn = connect(tmp_path / "kb.db")
    reg = ObserverRegistry()
    reg.register(_observer([_ev("d1", title="Project deadline"),
                            _ev("m1", title="Sprint meeting")]))
    run = ObservationEngine(reg, conn).run()
    conn.close()
    assert run.observers[0].name == "calendar"
    assert run.observers[0].health.healthy
    stored = observations_all(connect(tmp_path / "kb.db"))
    aspects = {(o.subject, o.aspect) for o in stored}
    assert ("d1", "category") in aspects
    assert ("m1", "category") in aspects
    assert all(o.source == "calendar" for o in stored)


def test_observation_ids_deterministic_and_idempotent(tmp_path):
    obs = _observer([_ev("d1", title="Project deadline")])
    conn = connect(tmp_path / "kb.db")
    reg = ObserverRegistry()
    reg.register(obs)
    ObservationEngine(reg, conn).run()
    ids1 = {o.id for o in observations_all(conn)}
    ObservationEngine(reg, conn).run()
    ids2 = {o.id for o in observations_all(conn)}
    assert ids1 == ids2


# --- Offline fixtures ------------------------------------------------------


def test_offline_fixture_file(tmp_path):
    snap = tmp_path / "cal.json"
    snap.write_text(json.dumps([_ev("d1", title="deadline")]), encoding="utf-8")
    obs = {(o.subject, o.aspect): o for o in
           CalendarObserver(FixtureProvider(snap)).collect(None)}
    assert obs[("d1", "category")].value == CalendarCategory.DEADLINE


def test_offline_ics_env_used(tmp_path):
    ics = tmp_path / "cal.ics"
    ics.write_text(
        "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:rel1\nSUMMARY:v1.2 release\n"
        "DTSTART:20260721T090000Z\nDTEND:20260721T100000Z\nEND:VEVENT\n"
        "END:VCALENDAR\n", encoding="utf-8")
    import os
    os.environ["FRIDAY_CALENDAR_ICS"] = str(ics)
    try:
        from friday.observation.calendar_observer import default_provider
        obs = {(o.subject, o.aspect): o for o in
               CalendarObserver(default_provider()).collect(None)}
    finally:
        os.environ.pop("FRIDAY_CALENDAR_ICS", None)
    assert obs[("rel1", "category")].value == CalendarCategory.RELEASE
    assert obs[("rel1", "start")].value == "2026-07-21T09:00:00+00:00"


def test_empty_fixture_yields_only_event_count():
    obs = {(o.subject, o.aspect): o for o in _observer([]).collect(None)}
    assert list(obs.keys()) == [("calendar", "events")]
    assert obs[("calendar", "events")].value == "0"


# --- ICS parsing -----------------------------------------------------------


def test_ics_parsing_metadata_only(tmp_path):
    ics = tmp_path / "cal.ics"
    ics.write_text(
        "BEGIN:VCALENDAR\n"
        "BEGIN:VEVENT\nUID:a1\nSUMMARY:Sprint planning meeting\n"
        "DTSTART:20260720T100000Z\nDTEND:20260720T110000Z\n"
        "LOCATION:Room 4\nRRULE:FREQ=WEEKLY\nEND:VEVENT\n"
        "BEGIN:VEVENT\nUID:a2\nSUMMARY:Secret 1:1\nDESCRIPTION:private notes\n"
        "ATTENDEE:alice@example.com\nDTSTART:20260721T090000Z\n"
        "DTEND:20260721T093000Z\nEND:VEVENT\n"
        "END:VCALENDAR\n", encoding="utf-8")
    out = ICSProvider(ics).fetch()
    by_uid = {e["uid"]: e for e in out}
    assert by_uid["a1"]["title"] == "Sprint planning meeting"
    assert by_uid["a1"]["recurring"] is True
    assert by_uid["a1"]["location"] == "Room 4"
    assert by_uid["a2"]["title"] == "Secret 1:1"
    # Description / attendees must NOT survive into parsed data.
    blob = json.dumps(out)
    assert "private notes" not in blob
    assert "alice@example.com" not in blob


def test_ics_cancelled_status(tmp_path):
    ics = tmp_path / "cal.ics"
    ics.write_text(
        "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:c1\nSUMMARY:Review\n"
        "DTSTART:20260720T100000Z\nDTEND:20260720T110000Z\n"
        "STATUS:CANCELLED\nEND:VEVENT\nEND:VCALENDAR\n", encoding="utf-8")
    out = ICSProvider(ics).fetch()
    assert out[0]["cancelled"] is True


# --- Engineering signals (derived / inferred) ------------------------------


def test_deadline_approaching_inferred():
    evs = [_ev(f"d{i}", title=f"Deadline {i}",
               start=_soon(3)) for i in range(2)]
    obs = {(o.subject, o.aspect): o for o in _observer(evs).collect(None)}
    assert obs[("calendar", "deadline_approaching")].value == "true"
    assert obs[("calendar", "deadline_approaching")].confidence is __import__(
        "friday.observation", fromlist=["Confidence"]).Confidence.INFERRED


def test_meeting_heavy_week_derived():
    evs = [_ev(f"m{i}", title=f"Meeting {i}") for i in range(4)]
    obs = {(o.subject, o.aspect): o for o in _observer(evs).collect(None)}
    assert obs[("calendar", "meeting_heavy_week")].value == "true"
    assert obs[("calendar", "meeting_heavy_week")].confidence is __import__(
        "friday.observation", fromlist=["Confidence"]).Confidence.DERIVED


def test_release_week_derived():
    obs = {(o.subject, o.aspect): o for o in
           _observer([_ev("r1", title="v2 release")]).collect(None)}
    assert obs[("calendar", "release_week")].value == "true"


def test_exam_period_derived():
    obs = {(o.subject, o.aspect): o for o in
           _observer([_ev("e1", title="Final exam")]).collect(None)}
    assert obs[("calendar", "exam_period")].value == "true"


def test_planning_session_derived():
    obs = {(o.subject, o.aspect): o for o in
           _observer([_ev("s1", title="Sprint planning")]).collect(None)}
    assert obs[("calendar", "planning_session")].value == "true"


def test_review_workload_derived():
    evs = [_ev(f"rv{i}", title=f"Code review {i}") for i in range(3)]
    obs = {(o.subject, o.aspect): o for o in _observer(evs).collect(None)}
    assert obs[("calendar", "review_workload")].value == "true"


def test_cancelled_excluded_from_signals_and_summary():
    obs = _observer([_ev("m1", title="Meeting", cancelled=True)]).collect(None)
    # No meeting_heavy_week etc. from a single cancelled meeting.
    assert not any(o.aspect == "meeting_heavy_week" for o in obs)
    summary = CalendarObserver(FixtureProvider(
        [_ev("m1", title="Meeting", cancelled=True)])).summarize(None)
    assert "Engineering events\n0" in summary


# --- Event model -----------------------------------------------------------


def test_event_from_dict_normalizes():
    e = CalendarEvent.from_dict({"uid": "x", "title": "deadline", "deadline": True})
    assert e.category == CalendarCategory.DEADLINE
    assert e.duration_min is None  # no end


def test_event_duration_min():
    e = CalendarEvent.from_dict({"uid": "x", "title": "m",
                                  "start": "2026-07-20T10:00:00+00:00",
                                  "end": "2026-07-20T11:30:00+00:00"})
    assert e.duration_min == 90


# --- Summary ---------------------------------------------------------------


def test_summary_counts_and_upcoming(tmp_path):
    conn = connect(tmp_path / "kb.db")
    evs = [
        _ev("d1", title="Project deadline", start=_soon(2)),
        _ev("d2", title="Assignment due", start=_soon(3)),
        _ev("d3", title="Exam", start="2026-08-01T10:00:00+00:00"),
        _ev("m1", title="Meeting one", start="2026-09-01T10:00:00+00:00"),
        _ev("m2", title="Meeting two", start="2026-09-02T10:00:00+00:00"),
        _ev("m3", title="Meeting three", start="2026-09-03T10:00:00+00:00"),
        _ev("r1", title="v1 release", start="2026-09-04T10:00:00+00:00"),
        _ev("a1", title="Assignment 1", start="2026-09-05T10:00:00+00:00"),
        _ev("a2", title="Assignment 2", start="2026-09-06T10:00:00+00:00"),
    ]
    summary = _observer(evs).summarize(conn)
    conn.close()
    assert "Engineering events\n9" in summary
    assert "Deadlines\n2" in summary
    assert "Meetings\n3" in summary
    assert "Releases\n1" in summary
    assert "Assignments\n2" in summary
    assert "Exams\n1" in summary
    # d1, d2 within the focus window -> upcoming; d3 (Aug 1) not.
    assert "Upcoming\n2" in summary


def test_summary_healthy_header():
    assert CalendarObserver(FixtureProvider([])).summarize(None).startswith(
        "Calendar Observer\nHealthy")
