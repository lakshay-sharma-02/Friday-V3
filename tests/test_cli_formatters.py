"""Tests for cli/formatters/ — domain object to view model conversion."""
from __future__ import annotations

from friday.cli.formatters.execution import (
    execution_result_to_view,
    task_to_worker_view,
)
from friday.cli.models import MissionPhase, WorkerStatus, MissionView


def test_execution_result_to_view_has_required_fields():
    mv = execution_result_to_view(
        mission_id="m1",
        goal="test",
        phase="implementation",
        progress=0.5,
    )
    assert isinstance(mv, MissionView)
    assert mv.id == "m1"
    assert mv.goal == "test"
    assert mv.phase == MissionPhase.IMPLEMENTATION
    assert mv.progress == 0.5
    assert mv.workers == []
    assert mv.timeline == []
    assert mv.elapsed_seconds >= 0


def test_task_to_worker_view_running():
    wv = task_to_worker_view(
        worker_id="w1", name="Shell", status="running",
        current_task="echo hello",
    )
    assert wv.id == "w1"
    assert wv.status == WorkerStatus.RUNNING


def test_task_to_worker_view_completed():
    wv = task_to_worker_view(
        worker_id="w2", name="Git", status="success",
        current_task="commit",
    )
    assert wv.status == WorkerStatus.COMPLETED


def test_task_to_worker_view_failed():
    wv = task_to_worker_view(
        worker_id="w3", name="Test", status="failed",
        current_task="run pytest",
    )
    assert wv.status == WorkerStatus.FAILED
