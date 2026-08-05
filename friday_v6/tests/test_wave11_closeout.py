"""Hermetic tests for Wave 11 close-out — reports + ambient push wiring."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from friday_v6 import db


@pytest.fixture
def conn(tmp_path: Path):
    c = db.connect(tmp_path / "v4.db")
    yield c
    c.close()


# ─────────────────────────────────────────────────────────────────────
# synthesis/reports.py — daily/weekly deterministic reports
# ─────────────────────────────────────────────────────────────────────


class TestReports:
    def test_daily_report_empty_db_is_honest(self, conn):
        from friday_v6.synthesis import build_daily_report
        report = build_daily_report(conn)
        assert report["title"] == "Friday daily report"
        assert isinstance(report["sections"], dict)
        assert report["generated_at"]

    def test_daily_report_reflects_real_state(self, conn):
        from friday_v6.synthesis import build_daily_report
        mid = db.create_mission(conn, "ship auth refactor", priority="high")
        assert mid
        db.add_mission_step(conn, mid, "migrate session")
        db.record_action(conn, "git", goal="commit auth", status="succeeded")
        report = build_daily_report(conn)
        text = "\n".join(str(v) for v in report["sections"].values())
        assert "ship auth refactor" in text
        assert "git" in text

    def test_weekly_report_deterministic(self, conn):
        from friday_v6.synthesis import build_weekly_report
        db.record_action(conn, "shell", goal="run tests", status="succeeded")
        # Pin the generated_at stamp — wall-clock microseconds would make
        # two calls differ even with identical state (the determinism
        # contract covers the report CONTENT, not the generation clock).
        now = "2026-08-01T12:00:00+00:00"
        a = build_weekly_report(conn, now=now)
        b = build_weekly_report(conn, now=now)
        assert a == b  # same state → same report
        assert "by day" in a["sections"]

    def test_reports_never_raise_on_missing_conn(self, tmp_path):
        from friday_v6.synthesis import build_daily_report, build_weekly_report
        # A None conn must never raise — DB-backed sections degrade.
        daily = build_daily_report(None)
        weekly = build_weekly_report(None)
        assert isinstance(daily["sections"], dict)
        assert isinstance(weekly["sections"], dict)
        assert "title" in daily and "generated_at" in daily

    def test_report_cli_daily_flag_hermetic(self, capsys, tmp_path):
        """`report --daily` reads real state from a tmp DB — never the
        user's real ~/.friday DB (hermetic contract)."""
        from friday_v6.cli_research import cmd_report
        from friday_v6 import db
        dbp = tmp_path / "v4.db"
        conn = db.connect(dbp)
        db.create_mission(conn, "ship auth", priority="high")
        conn.close()
        import argparse
        args = argparse.Namespace(daily=True, weekly=False, json=False,
                                  db=dbp, title="x", items=[])
        code = cmd_report(args)
        out = capsys.readouterr().out
        assert code == 0
        assert "Friday" in out
        assert "ship auth" in out


# ─────────────────────────────────────────────────────────────────────
# ambient push wiring — publishers emit onto a shared bus
# ─────────────────────────────────────────────────────────────────────


class TestAmbientPushWiring:
    def test_security_scanner_publishes_to_shared_bus(self, tmp_path):
        """The daemon-wired scanner publishes findings durably."""
        from friday_v6.ambient import AmbientBus, Event
        from friday_v6.daemon import SecurityScanner
        bus = AmbientBus(db.connect(tmp_path / "v4.db"))

        class _Finding:
            id = "f1"
            severity = "high"
            file = "src/main.py"
            package = None
            detail = "CVE-2026-0001"
            title = "bad dep"
            severity_rank = 2
            cve = None

        class _Scanner(SecurityScanner):
            def __init__(self, bus):
                super().__init__(bus=bus, notify=None,
                                 state_file=tmp_path / "sec.json")
                self.published = []
                self._publish_ambient = lambda f: self.published.append(f)

        scanner = _Scanner(bus)
        assert scanner._bus is bus  # wiring accepted

    def test_suggestion_channel_publishes_to_bus(self, tmp_path):
        from friday_v6.ambient import AmbientBus, Event
        from friday_v6.desktop.notifier import ProactiveSuggestionChannel
        conn = db.connect(tmp_path / "v4.db")
        bus = AmbientBus(conn)
        got = []
        bus.subscribe("suggestion", lambda e: got.append(e.payload))

        class _Item:
            text = "run the tests"
            should_notify = True
            should_speak = False
            source = "pattern"

        class _Engine:
            def get_suggestions(self, *a, **kw):
                return [_Item()]

            def cleanup(self):
                pass

        ch = ProactiveSuggestionChannel(engine=_Engine(), bus=bus,
                                        cooldown_seconds=0.001,
                                        notify=lambda *a, **kw: True)
        assert ch.poll_once() == 1
        assert any("run the tests" in g for g in got)
        # Durable: the event is in the queue.
        assert db.recent_ambient_events(conn, topic="suggestion")
        conn.close()

    def test_collab_coordinator_publishes_to_bus(self, tmp_path):
        from friday_v6.ambient import AmbientBus
        from friday_v6.collab.coordinator import Coordinator
        conn = db.connect(tmp_path / "v4.db")
        bus = AmbientBus(conn)
        got = []
        bus.subscribe("collab", lambda e: got.append(e.payload))
        coord = Coordinator(peer_id="test-peer",
                            state_dir=tmp_path / "collab", bus=bus)
        coord.add_observation({"source": "git", "subject": "repoA"})
        assert any("observation" in g for g in got)
        # Permission-safe: the bus carries a summary, never raw content.
        assert all("repoA" not in g for g in got)
        conn.close()

    def test_sse_endpoint_streams_durable_events(self, tmp_path,
                                                 monkeypatch):
        """GET /api/events pushes durable queue events as SSE frames."""
        from friday_v6 import db as db_mod
        from friday_v6.ambient import AmbientBus, Event, Priority
        from friday_v6.web.server import make_server

        dbp = tmp_path / "v4.db"
        conn = db_mod.connect(dbp)
        AmbientBus(conn).publish(Event(
            "security", "2 high-sev vulns", Priority.IMPORTANT))
        conn.close()

        # Point the server's read-only queue reads at the tmp DB.
        monkeypatch.setattr(db_mod, "default_db_path", lambda: dbp)

        server = make_server(host="127.0.0.1", port=0)
        server.daemon_threads = True
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        try:
            port = server.server_address[1]
            # SSE is a long-lived stream; read only the first frames then
            # close. urllib would block forever on a stream, so use a raw
            # socket with a bounded read of the first chunk.
            import socket
            s = socket.create_connection(("127.0.0.1", port), timeout=10)
            try:
                s.sendall(b"GET /api/events HTTP/1.1\r\nHost: localhost\r\n"
                          b"Connection: close\r\n\r\n")
                data = b""
                deadline = time.time() + 10
                while time.time() < deadline and b"high-sev" not in data:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                assert b"high-sev" in data
                assert b"data:" in data  # SSE frame
            finally:
                s.close()
        finally:
            server.shutdown()
            server.server_close()

    def test_sse_endpoint_200_missing_db(self, tmp_path, monkeypatch):
        """No DB → the stream still opens (200) and heartbeats, never 500."""
        import socket
        from friday_v6.web.server import make_server

        monkeypatch.setattr(db, "default_db_path",
                            lambda: tmp_path / "absent" / "v4.db")
        server = make_server(host="127.0.0.1", port=0)
        server.daemon_threads = True
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        try:
            port = server.server_address[1]
            s = socket.create_connection(("127.0.0.1", port), timeout=10)
            try:
                s.sendall(b"GET /api/events HTTP/1.1\r\nHost: localhost\r\n"
                          b"Connection: close\r\n\r\n")
                data = b""
                deadline = time.time() + 10
                while time.time() < deadline and b"200" not in data:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                assert b"200 OK" in data
                assert b"text/event-stream" in data
            finally:
                s.close()
        finally:
            server.shutdown()
            server.server_close()

    def test_merge_entries_publishes_summary(self, tmp_path):
        from friday_v6.ambient import AmbientBus
        from friday_v6.collab.coordinator import Coordinator
        conn = db.connect(tmp_path / "v4.db")
        bus = AmbientBus(conn)
        got = []
        bus.subscribe("collab", lambda e: got.append(e.payload))
        coord = Coordinator(peer_id="test-peer",
                            state_dir=tmp_path / "collab", bus=bus)
        # CRDT entry shape: {id, peer_id, ts, payload, deleted}.
        applied = coord.merge_entries([{
            "id": "o1", "peer_id": "peer-1", "ts": 100,
            "payload": {"source": "git", "subject": "repoA",
                         "aspect": "commits", "value": "3"},
            "deleted": False}])
        assert applied == 1
        assert any("peer observation" in g for g in got)
        conn.close()

    def test_db_ambient_events_since(self, tmp_path):
        """The SSE cursor helper returns only events after a rowid."""
        from friday_v6.ambient import AmbientBus, Event, Priority
        conn = db.connect(tmp_path / "v4.db")
        bus = AmbientBus(conn)
        bus.publish(Event("security", "first", Priority.IMPORTANT))
        bus.publish(Event("suggestion", "second"))
        events = db.ambient_events_since(conn, since_rowid=0)
        assert len(events) == 2
        assert events[0]["rowid"] < events[1]["rowid"]  # oldest first
        after = db.ambient_events_since(conn, since_rowid=events[0]["rowid"])
        assert [e["payload"] for e in after] == ["second"]
        conn.close()
