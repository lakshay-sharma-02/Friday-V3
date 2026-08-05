"""Hermetic tests for the Wave 11 ambient layer (bus + durable queue)."""

from __future__ import annotations

from pathlib import Path

import pytest

from friday_v6 import db
from friday_v6.ambient import AmbientBus, ChannelRegistry, Event, Priority


@pytest.fixture
def conn(tmp_path: Path):
    c = db.connect(tmp_path / "v4.db")
    yield c
    c.close()


def test_publish_fans_out(conn):
    bus = AmbientBus(conn)
    got = []
    bus.subscribe("security", lambda e: got.append(e.payload))
    bus.publish(Event("security", "2 high-sev vulns", Priority.IMPORTANT))
    assert got == ["2 high-sev vulns"]


def test_durable_replay(conn):
    bus = AmbientBus(conn)
    bus.publish(Event("security", "cve-1", Priority.IMPORTANT))
    bus.publish(Event("mission", "step done", Priority.ROUTINE))
    events = bus.replay("security")
    assert any(e.payload == "cve-1" for e in events)


def test_priority(conn):
    e = Event("x", "urgent", Priority.CRITICAL)
    assert e.priority == Priority.CRITICAL


def test_subscribe_unsubscribe(conn):
    bus = AmbientBus(conn)
    got = []
    tok = bus.subscribe("a", lambda e: got.append(1))
    bus.publish(Event("a", "one"))
    assert got == [1]
    bus.unsubscribe(tok)
    bus.publish(Event("a", "two"))
    assert got == [1]  # no delivery after unsubscribe


def test_never_raises_on_bad_topic(conn):
    bus = AmbientBus(conn)
    # Subscriber raising must not break publish (daemon law).
    def boom(e):
        raise RuntimeError("x")
    bus.subscribe("a", boom)
    bus.publish(Event("a", "y"))  # must not raise


def test_channels_fanout():
    reg = ChannelRegistry()
    got = []
    reg.register("voice", lambda e: got.append(f"voice:{e.payload}"))
    reg.register("web", lambda e: got.append(f"web:{e.payload}"))
    reg.fanout(Event("security", "alert", Priority.CRITICAL))
    assert got == ["voice:alert", "web:alert"]


def test_channel_degrade(conn):
    bus = AmbientBus(conn)
    reg = ChannelRegistry()
    def boom(e):
        raise RuntimeError("surface down")
    reg.register("broken", boom)
    reg.fanout(Event("a", "x"))  # must not raise


def test_db_recent_ambient(conn):
    bus = AmbientBus(conn)
    bus.publish(Event("system", "hello", Priority.ROUTINE))
    rows = db.recent_ambient_events(conn)
    assert rows and rows[0]["payload"] == "hello"
    assert db.schema_version(conn) >= 4
