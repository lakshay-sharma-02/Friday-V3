"""Tests for cli/widgets/ — each widget renders known input to Rich renderable."""
from __future__ import annotations
from io import StringIO

from rich.console import Console

from friday.cli.models import (
    MissionPhase, WorkerStatus,
    WorkerView, TimelineEventView, ProgressView,
)
from friday.cli.widgets.header import HeaderWidget
from friday.cli.widgets.footer import FooterWidget
from friday.cli.widgets.progress import ProgressWidget
from friday.cli.widgets.workers import WorkersWidget
from friday.cli.widgets.timeline import TimelineWidget
from friday.cli.widgets.mission_graph import MissionGraphWidget
from friday.cli.widgets.panels import info_panel, error_panel
from friday.cli.widgets.tables import data_table, key_value_table


def _render(renderable) -> str:
    """Render a Rich renderable to plain text via StringIO."""
    buf = StringIO()
    Console(file=buf, width=120).print(renderable)
    return buf.getvalue()


def test_header_renders_mission_id():
    w = HeaderWidget()
    text = _render(w.render(mission_id="m42", elapsed_seconds=90))
    assert "m42" in text
    assert "FRIDAY" in text


def test_header_shows_elapsed():
    w = HeaderWidget()
    text = _render(w.render(mission_id="m1", elapsed_seconds=125))
    assert "02:05" in text


def test_footer_renders_status_line():
    w = FooterWidget()
    text = _render(w.render(status="Searching runtime/"))
    assert "Searching runtime/" in text


def test_progress_widget_shows_percentage():
    w = ProgressWidget()
    text = _render(w.render(progress=0.63, goal="Refactor architecture"))
    assert "63%" in text


def test_workers_widget_shows_workers():
    w = WorkersWidget()
    workers = [
        WorkerView(id="w1", name="Search", status=WorkerStatus.RUNNING,
                   current_task="Scanning", progress=ProgressView(5, 10)),
        WorkerView(id="w2", name="Analyze", status=WorkerStatus.COMPLETED,
                   current_task="Done", findings=["found 2 issues"]),
    ]
    text = _render(w.render(workers))
    assert "Search" in text
    assert "Analyze" in text
    assert "Scanning" in text


def test_timeline_widget_events_in_order():
    w = TimelineWidget()
    events = [
        TimelineEventView(id="e1", timestamp="12:00", kind="phase", message="Planning"),
        TimelineEventView(id="e2", timestamp="12:01", kind="worker", message="Search started"),
    ]
    text = _render(w.render(events))
    assert "12:00" in text
    assert "12:01" in text


def test_mission_graph_shows_phases():
    w = MissionGraphWidget()
    text = _render(w.render(current_phase=MissionPhase.ANALYSIS))
    assert "Planning" in text
    assert "Analysis" in text
    assert "Complete" in text


def test_info_panel():
    text = _render(info_panel("Test", "hello world"))
    assert "hello world" in text


def test_error_panel():
    text = _render(error_panel("Error", "something broke"))
    assert "something broke" in text


def test_data_table():
    text = _render(data_table("List", ["Name", "Value"], [["a", "1"], ["b", "2"]]))
    assert "a" in text and "1" in text


def test_key_value_table():
    text = _render(key_value_table([("key1", "val1"), ("key2", "val2")]))
    assert "key1" in text
    assert "val1" in text
