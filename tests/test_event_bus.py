"""Tests for runtime/event_bus.py — in-process pub/sub with typed events."""
from __future__ import annotations
from datetime import datetime

import pytest

from friday.runtime.event_bus import (
    Event,
    EventBus,
    MissionStarted,
    MissionCompleted,
    PhaseChanged,
    WorkerSpawned,
    WorkerReady,
    WorkerStarted,
    WorkerProgress,
    WorkerWaiting,
    WorkerCompleted,
    WorkerFailed,
    ToolStarted,
    ToolCompleted,
    LogMessage,
    MissionPhase,
)


def test_event_is_dataclass():
    e = MissionStarted(mission_id="m1", timestamp=datetime.now(), goal="test")
    assert e.mission_id == "m1"
    assert e.goal == "test"


def test_event_immutable():
    e = MissionStarted(mission_id="m1", timestamp=datetime.now(), goal="test")
    with pytest.raises(AttributeError):
        e.goal = "changed"


def test_event_bus_publish_subscribe():
    bus = EventBus()
    received = []

    def handler(event):
        received.append(event)

    bus.subscribe(WorkerStarted, handler)
    e = WorkerStarted(mission_id="m1", timestamp=datetime.now(),
                      worker_id="w1", task_description="analyze")
    bus.publish(e)
    assert len(received) == 1
    assert received[0].worker_id == "w1"


def test_event_bus_untyped_events_not_delivered():
    bus = EventBus()
    received = []

    def handler(event):
        received.append(event)

    bus.subscribe(WorkerCompleted, handler)
    e = WorkerStarted(mission_id="m1", timestamp=datetime.now(),
                      worker_id="w1", task_description="analyze")
    bus.publish(e)
    assert len(received) == 0


def test_event_bus_multiple_subscribers():
    bus = EventBus()
    r1, r2 = [], []

    bus.subscribe(LogMessage, r1.append)
    bus.subscribe(LogMessage, r2.append)
    e = LogMessage(mission_id="m1", timestamp=datetime.now(),
                   level="info", message="hello")
    bus.publish(e)
    assert len(r1) == 1
    assert len(r2) == 1


def test_mission_phase_enum():
    assert MissionPhase.PLANNING.value == "planning"
    assert MissionPhase.COMPLETE.value == "complete"
    assert len(MissionPhase) == 7


def test_phase_changed_carries_phases():
    e = PhaseChanged(
        mission_id="m1", timestamp=datetime.now(),
        previous=MissionPhase.PLANNING,
        current=MissionPhase.DISCOVERY,
    )
    assert e.previous == MissionPhase.PLANNING
    assert e.current == MissionPhase.DISCOVERY


def test_worker_progress_optional_total():
    e = WorkerProgress(mission_id="m1", timestamp=datetime.now(),
                       worker_id="w1", current=5, total=None, message="scanning")
    assert e.total is None
    e2 = WorkerProgress(mission_id="m1", timestamp=datetime.now(),
                        worker_id="w1", current=5, total=10, message="scanning")
    assert e2.total == 10


def test_empty_bus_publish_no_error():
    bus = EventBus()
    e = LogMessage(mission_id="m1", timestamp=datetime.now(),
                   level="info", message="nobody listening")
    bus.publish(e)  # should not raise
