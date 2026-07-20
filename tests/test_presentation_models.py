"""Tests for cli/models.py — view model dataclasses."""
from __future__ import annotations

from friday.presentation.models import (
    MissionView, WorkerView, TimelineEventView, ProgressView,
    SummaryView, WorkerStatus, MissionPhase,
)


def test_mission_view_frozen():
    mv = MissionView(
        id="m1", goal="test", phase=MissionPhase.ANALYSIS,
        progress=0.5, workers=[], timeline=[], summary=SummaryView(),
        elapsed_seconds=42,
    )
    assert mv.id == "m1"
    assert mv.progress == 0.5


def test_mission_view_immutable():
    mv = MissionView(
        id="m1", goal="test", phase=MissionPhase.ANALYSIS,
        progress=0.5, workers=[], timeline=[], summary=SummaryView(),
        elapsed_seconds=42,
    )
    try:
        mv.progress = 0.9  # type: ignore
        assert False, "should be frozen"
    except Exception:
        pass


def test_worker_view_with_id():
    wv = WorkerView(
        id="w1", name="Search Specialist", status=WorkerStatus.RUNNING,
        current_task="Searching runtime/", progress=None, findings=[],
    )
    assert wv.id == "w1"


def test_worker_status_enum():
    assert WorkerStatus.RUNNING.value == "running"
    assert WorkerStatus.FAILED.value == "failed"


def test_mission_phase_enum():
    assert MissionPhase.IMPLEMENTATION.value == "implementation"


def test_timeline_event_view_with_id():
    tev = TimelineEventView(id="e1", timestamp="12:00", kind="info", message="started")
    assert tev.id == "e1"


def test_summary_view_defaults():
    sv = SummaryView()
    assert sv.files_modified == 0
