"""Tests for cli/renderers/mission.py — live + static rendering."""
from __future__ import annotations
from io import StringIO

from rich.console import Console

from friday.cli.models import (
    MissionView, WorkerView, TimelineEventView, SummaryView,
    MissionPhase, WorkerStatus,
)
from friday.cli.renderers.mission import (
    MissionRenderer,
    render_mission_view,
)
from friday.cli.renderers.execution import render_execution_summary


def _render(renderable) -> str:
    buf = StringIO()
    Console(file=buf, width=120).print(renderable)
    return buf.getvalue()


def _sample_view() -> MissionView:
    return MissionView(
        id="m1", goal="Refactor architecture",
        phase=MissionPhase.ANALYSIS, progress=0.63,
        workers=[
            WorkerView(id="w1", name="Search", status=WorkerStatus.RUNNING,
                       current_task="Searching runtime/",
                       findings=["18 files scanned"]),
        ],
        timeline=[
            TimelineEventView(id="e1", timestamp="12:00", kind="phase",
                              message="Planning"),
        ],
        summary=SummaryView(files_modified=18, tests_passed=121),
        elapsed_seconds=134,
    )


def test_mission_renderer_renders_layout():
    mv = _sample_view()
    renderer = MissionRenderer()
    layout = renderer.render(mv)
    assert layout is not None


def test_mission_renderer_has_all_widgets():
    renderer = MissionRenderer()
    assert hasattr(renderer, "header")
    assert hasattr(renderer, "progress")
    assert hasattr(renderer, "workers")
    assert hasattr(renderer, "timeline")
    assert hasattr(renderer, "footer")


def test_render_mission_view_static():
    mv = _sample_view()
    layout = render_mission_view(mv)
    text = _render(layout)
    assert "Refactor architecture" in text
    assert "63%" in text


def test_execution_summary():
    mv = _sample_view()
    result = render_execution_summary(mv)
    text = _render(result)
    assert "18" in text
    assert "121" in text
    assert "Complete" in text


def test_status_line_running():
    view = _sample_view()
    line = MissionRenderer._status_line(view)
    assert "Search" in line


def test_status_line_complete():
    view = MissionView(
        id="m1", goal="test", phase=MissionPhase.COMPLETE, progress=1.0,
        workers=[
            WorkerView(id="w1", name="Search", status=WorkerStatus.COMPLETED,
                       current_task="done"),
        ],
        timeline=[], summary=SummaryView(), elapsed_seconds=10,
    )
    line = MissionRenderer._status_line(view)
    assert "complete" in line
