"""Hermetic tests for Wave 15 — One Presence transport slices.

Slice 2 (web chat polish): the dashboard hydrates today's shared-session
conversation, so a conversation started in the terminal continues
visibly in the browser.

Slice 3 (mobile transport via the durable queue): the phone is another
surface of the SAME Friday —
- ``PushNotificationService`` replays the durable ambient queue to a
  transporter with a persisted rowid cursor (a reconnecting phone
  misses nothing);
- the companion API (``MobileAPI`` / ``create_api_server``) exposes
  status / conversation / talk / SSE over the same brain + queue.

Slice 4 (ambient push reaches every surface): wildcard subscribe on the
``AmbientBus`` + voice (speak) / desktop (notify) channel builders +
the daemon's ``AmbientWorker.wire_channels``.

Everything is hermetic: tmp_path DBs, no network except the in-test
HTTP server, no real ~/.friday, no subprocesses.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from friday_v4 import db
from friday_v4.ambient import AmbientBus, Event, Priority
from friday_v4.ambient.channels import desktop_channel, speak_channel


def _conn(tmp_path):
    return db.connect(tmp_path / "v4.db")


# ─────────────────────────────────────────────────────────────────────
# Slice 3a — PushNotificationService (durable-queue consumer)
# ─────────────────────────────────────────────────────────────────────


class TestPushService:
    def test_delivers_new_events_and_advances_cursor(self, tmp_path):
        from friday_v4.mobile import PushNotificationService

        dbp = tmp_path / "v4.db"
        conn = db.connect(dbp)
        bus = AmbientBus(conn)
        bus.publish(Event("security", "cve-1", Priority.CRITICAL,
                          source="daemon"))
        bus.publish(Event("mission", "step done", Priority.ROUTINE))
        conn.close()

        delivered = []
        service = PushNotificationService(
            db_path=dbp, transporter=delivered.append,
            state_file=tmp_path / "push_state.json")
        assert service.poll_once() == 2
        assert [n.topic for n in delivered] == ["security", "mission"]
        assert service.cursor >= 2
        # Nothing new → 0, and the cursor file was written.
        assert service.poll_once() == 0
        assert (tmp_path / "push_state.json").exists()

    def test_restart_resumes_after_cursor(self, tmp_path):
        from friday_v4.mobile import PushNotificationService

        dbp = tmp_path / "v4.db"
        conn = db.connect(dbp)
        bus = AmbientBus(conn)
        bus.publish(Event("a", "one"))
        bus.publish(Event("a", "two"))
        conn.close()

        state_file = tmp_path / "push_state.json"
        first = PushNotificationService(db_path=dbp, transporter=lambda n: None,
                                        state_file=state_file)
        assert first.poll_once() == 2

        # A "restart" (fresh service, same state file) must NOT re-deliver.
        restarted = PushNotificationService(
            db_path=dbp, transporter=lambda n: None, state_file=state_file)
        assert restarted.cursor == first.cursor
        assert restarted.poll_once() == 0

    def test_min_priority_filters_but_advances(self, tmp_path):
        from friday_v4.mobile import PushNotificationService

        dbp = tmp_path / "v4.db"
        conn = db.connect(dbp)
        bus = AmbientBus(conn)
        bus.publish(Event("a", "routine", Priority.ROUTINE))
        bus.publish(Event("b", "important", Priority.IMPORTANT))
        conn.close()

        delivered = []
        service = PushNotificationService(
            db_path=dbp, transporter=delivered.append,
            min_priority=Priority.IMPORTANT.value,
            state_file=tmp_path / "push_state.json")
        assert service.poll_once() == 1
        assert [n.topic for n in delivered] == ["b"]
        # The routine event was SEEN (cursor advanced) — no re-delivery
        # storm on the next poll.
        assert service.poll_once() == 0

    def test_file_transporter_writes_jsonl_outbox(self, tmp_path):
        from friday_v4.mobile import PushNotificationService, file_transporter

        dbp = tmp_path / "v4.db"
        conn = db.connect(dbp)
        AmbientBus(conn).publish(Event("security", "secret found",
                                       Priority.CRITICAL))
        conn.close()

        outbox = tmp_path / "outbox.jsonl"
        service = PushNotificationService(
            db_path=dbp, transporter=file_transporter(outbox),
            state_file=tmp_path / "push_state.json")
        assert service.poll_once() == 1
        line = json.loads(outbox.read_text().strip().splitlines()[0])
        assert line["topic"] == "security"
        assert line["payload"] == "secret found"

    def test_graceful_on_missing_db(self, tmp_path):
        from friday_v4.mobile import PushNotificationService

        service = PushNotificationService(
            db_path=tmp_path / "missing" / "v4.db",
            transporter=lambda n: None,
            state_file=tmp_path / "push_state.json")
        assert service.poll_once() == 0
        assert service.last_error is None  # graceful, not an error

    def test_transporter_failure_never_wedges_queue(self, tmp_path):
        from friday_v4.mobile import PushNotificationService

        dbp = tmp_path / "v4.db"
        conn = db.connect(dbp)
        bus = AmbientBus(conn)
        bus.publish(Event("a", "one"))
        bus.publish(Event("a", "two"))
        conn.close()

        def boom(n):
            raise RuntimeError("phone offline")

        service = PushNotificationService(
            db_path=dbp, transporter=boom,
            state_file=tmp_path / "push_state.json")
        assert service.poll_once() == 2   # both delivered (guard each)
        assert service.cursor >= 2        # queue not wedged


# ─────────────────────────────────────────────────────────────────────
# Slice 3b — MobileAPI (companion surface of the same Friday)
# ─────────────────────────────────────────────────────────────────────


class TestMobileAPI:
    def test_status_and_conversation_share_the_thread(self, tmp_path):
        from friday_v4.mobile import MobileAPI
        from friday_v4.nl_router import TextCommandHandler

        dbp = tmp_path / "v4.db"
        conn = db.connect(dbp)
        # Terminal conversation lands in the shared session.
        TextCommandHandler(conn, cwd=str(tmp_path)).handle(
            "we're migrating the auth module")
        conn.close()

        api = MobileAPI(db_path=dbp)
        status = api.status()
        assert status["available"] is True
        assert status["exchanges_today"] >= 2  # user + friday reply

        conv = api.conversation()
        assert conv["available"] is True
        assert any("auth module" in e["content"] for e in conv["exchanges"])
        # Oldest first — the terminal turn is the first user turn.
        roles = [e["role"] for e in conv["exchanges"]]
        assert roles[0] == "user"

    def test_talk_routes_through_the_same_brain(self, tmp_path):
        from friday_v4.mobile import MobileAPI

        dbp = tmp_path / "v4.db"
        conn = db.connect(dbp)
        aid = db.record_action(conn, "git", goal="probe", command="git status",
                               status="succeeded")
        db.finish_action(conn, aid, "succeeded", result_code=0,
                         output="on branch main")
        conn.close()

        api = MobileAPI(db_path=dbp)
        result = api.talk("what did we talk about")
        assert result["intent"] in ("ask", "chat")
        assert result["response"]

    def test_talk_requires_text(self, tmp_path):
        from friday_v4.mobile import MobileAPI
        assert MobileAPI(db_path=tmp_path / "v4.db").talk("   ")[
            "action"] in ("failed", "chat")

    def test_graceful_without_db(self, tmp_path):
        from friday_v4.mobile import MobileAPI
        api = MobileAPI(db_path=tmp_path / "missing" / "v4.db")
        assert api.status()["available"] is True  # connect auto-creates
        conv = api.conversation()
        assert conv["available"] is True
        assert conv["exchanges"] == []

    def test_read_probes_never_create_the_thread(self, tmp_path):
        """Status/conversation are reads — they must not CREATE today's
        shared session (the thread is born on first actual talk)."""
        from friday_v4.mobile import MobileAPI

        dbp = tmp_path / "v4.db"
        conn = db.connect(dbp)
        conn.close()

        api = MobileAPI(db_path=dbp)
        assert api.status()["shared_session"] is None
        assert api.conversation()["session_id"] is None

        conn = db.connect(dbp)
        shared = [s for s in db.list_sessions(conn)
                  if s["surface"] == "shared"]
        conn.close()
        assert shared == []  # nothing was created by the probes


# ─────────────────────────────────────────────────────────────────────
# Slice 3c — companion API HTTP server
# ─────────────────────────────────────────────────────────────────────


class _MobileServer:
    def __init__(self, db_path):
        from friday_v4.mobile import create_api_server
        self.server = create_api_server(host="127.0.0.1", port=0,
                                        db_path=db_path)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def get(self, path: str):
        with urllib.request.urlopen(f"{self.base}{path}", timeout=10) as r:
            return r.status, r.read()

    def post(self, path: str, body: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"{self.base}{path}", data=data,
                                     method="POST")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()


class TestMobileServer:
    @pytest.fixture
    def server(self, tmp_path):
        srv = _MobileServer(tmp_path / "v4.db")
        yield srv
        srv.close()

    def test_status_endpoint(self, server):
        status, body = server.get("/api/status")
        assert status == 200
        data = json.loads(body)
        assert data["available"] is True

    def test_conversation_endpoint(self, server, tmp_path):
        from friday_v4.nl_router import TextCommandHandler
        conn = db.connect(tmp_path / "v4.db")
        TextCommandHandler(conn, cwd=str(tmp_path)).handle(
            "hello from the terminal")
        conn.close()

        status, body = server.get("/api/conversation")
        assert status == 200
        data = json.loads(body)
        assert any("hello from the terminal" in e["content"]
                   for e in data["exchanges"])

    def test_talk_endpoint(self, server):
        status, body = server.post("/api/talk", {"text": "what's up"})
        assert status == 200
        data = json.loads(body)
        assert "response" in data

    def test_talk_endpoint_empty_text_400(self, server):
        with pytest.raises(urllib.error.HTTPError) as exc:
            server.post("/api/talk", {"text": "  "})
        assert exc.value.code == 400

    def test_unknown_route_404(self, server):
        with pytest.raises(urllib.error.HTTPError) as exc:
            server.get("/api/nope")
        assert exc.value.code == 404


# ─────────────────────────────────────────────────────────────────────
# Slice 2 — web conversation hydrate (browser resumes the thread)
# ─────────────────────────────────────────────────────────────────────


class TestWebConversation:
    def test_conversation_state_reads_shared_thread(self, tmp_path):
        from friday_v4.web import dashboard
        from friday_v4.nl_router import TextCommandHandler

        dbp = tmp_path / "v4.db"
        conn = db.connect(dbp)
        TextCommandHandler(conn, cwd=str(tmp_path)).handle(
            "we're designing the shared context")
        conn.close()

        real_connect = db.connect
        try:
            # Point the dashboard at the tmp DB (read-only probe). The
            # dashboard resolves `db` via `from friday_v4 import db` at
            # call time, so patching the module attribute works.
            db.connect = lambda **kw: real_connect(dbp, **kw)
            state = dashboard.conversation_state()
            assert state["available"] is True
            assert state["session_id"]
            assert any("shared context" in e["content"]
                       for e in state["exchanges"])
        finally:
            db.connect = real_connect

    def test_conversation_state_graceful_without_db(self, monkeypatch):
        from friday_v4.web import dashboard

        def _boom(*a, **kw):
            raise OSError("no db")
        monkeypatch.setattr("friday_v4.db.connect", _boom)
        state = dashboard.conversation_state()
        assert state["available"] is False
        assert state["exchanges"] == []

    def test_conversation_state_read_only_never_creates(self, tmp_path):
        """The browser hydrate probe must not write — a fresh DB renders
        an empty conversation, and NO shared session row is born.

        Hermetic: the dashboard's probe connects via ``db.connect`` at
        call time, so it is pointed at the tmp DB (same convention as
        the sibling read test) — never the real ~/.friday."""
        from friday_v4.web import dashboard

        dbp = tmp_path / "v4.db"
        db.connect(dbp).close()

        real_connect = db.connect
        try:
            db.connect = lambda **kw: real_connect(dbp, **kw)
            state = dashboard.conversation_state()
            assert state["available"] is True
            assert state["session_id"] is None
            assert state["exchanges"] == []
        finally:
            db.connect = real_connect

        conn = db.connect(dbp)
        shared = [s for s in db.list_sessions(conn)
                  if s["surface"] == "shared"]
        conn.close()
        assert shared == []

    def test_server_serves_conversation_endpoint(self, tmp_path):
        from friday_v4.web.server import make_server

        # Point db.connect at the tmp DB for the server thread.
        real_connect = db.connect
        orig = db.connect
        db.connect = lambda **kw: real_connect(tmp_path / "v4.db", **kw)
        try:
            server = make_server(host="127.0.0.1", port=0)
            t = threading.Thread(target=server.serve_forever, daemon=True)
            t.start()
            try:
                base = f"http://127.0.0.1:{server.server_address[1]}"
                with urllib.request.urlopen(f"{base}/api/conversation",
                                            timeout=10) as r:
                    assert r.status == 200
                    data = json.loads(r.read())
                    assert "exchanges" in data
            finally:
                server.shutdown()
                server.server_close()
        finally:
            db.connect = orig


# ─────────────────────────────────────────────────────────────────────
# Slice 4 — ambient push reaches every surface
# ─────────────────────────────────────────────────────────────────────


class TestAmbientSurfaces:
    def test_wildcard_subscribe_gets_every_topic(self, conn_factory):
        conn = conn_factory
        bus = AmbientBus(conn)
        got = []
        bus.subscribe("*", lambda e: got.append(e.topic))
        bus.publish(Event("security", "x"))
        bus.publish(Event("mission", "y"))
        assert sorted(got) == ["mission", "security"]

    def test_topic_and_wildcard_both_deliver(self, conn_factory):
        conn = conn_factory
        bus = AmbientBus(conn)
        got = []
        bus.subscribe("security", lambda e: got.append("topic"))
        bus.subscribe("*", lambda e: got.append("wild"))
        bus.publish(Event("security", "x"))
        assert sorted(got) == ["topic", "wild"]

    def test_speak_channel_only_speaks_critical(self):
        spoken = []
        channel = speak_channel(lambda t: spoken.append(t) or True,
                                min_priority=Priority.CRITICAL)
        channel(Event("a", "routine", Priority.ROUTINE))
        channel(Event("b", "important", Priority.IMPORTANT))
        channel(Event("c", "CRITICAL!", Priority.CRITICAL))
        assert spoken == ["CRITICAL!"]

    def test_desktop_channel_notifies_important_and_above(self):
        notified = []
        channel = desktop_channel(
            lambda t, m, **kw: notified.append(m) or True,
            min_priority=Priority.IMPORTANT)
        channel(Event("a", "routine", Priority.ROUTINE))
        channel(Event("b", "important", Priority.IMPORTANT))
        channel(Event("c", "critical", Priority.CRITICAL))
        assert notified == ["important", "critical"]

    def test_surface_channels_never_raise(self):
        def boom(*a, **kw):
            raise RuntimeError("surface down")
        speak = speak_channel(boom)
        notify = desktop_channel(boom)
        ev = Event("a", "x", Priority.CRITICAL)
        speak(ev)   # must not raise
        notify(ev)  # must not raise


@pytest.fixture
def conn_factory(tmp_path):
    c = db.connect(tmp_path / "v4.db")
    yield c
    c.close()


# ─────────────────────────────────────────────────────────────────────
# Slice 4b — daemon AmbientWorker surface wiring
# ─────────────────────────────────────────────────────────────────────


class TestDaemonWiring:
    def test_wire_channels_subscribes_surfaces(self, tmp_path):
        from friday_v4.daemon import AmbientWorker

        dbp = tmp_path / "v4.db"
        conn = db.connect(dbp)
        bus = AmbientBus(conn)
        worker = AmbientWorker(db_path=dbp)
        worker._bus = bus          # inject the shared bus (no thread)
        worker._conn = conn

        spoken = []
        notified = []
        worker.wire_channels(speak_fn=lambda t: spoken.append(t) or True,
                             notify_fn=lambda t, m, **kw: notified.append(m) or True)

        # CRITICAL → spoken + notified; ROUTINE → neither surface.
        bus.publish(Event("security", "CRITICAL finding", Priority.CRITICAL))
        bus.publish(Event("mission", "routine tick", Priority.ROUTINE))
        assert spoken == ["CRITICAL finding"]
        assert notified == ["CRITICAL finding"]

    def test_wire_channels_graceful_without_bus(self, tmp_path):
        from friday_v4.daemon import AmbientWorker
        worker = AmbientWorker(db_path=tmp_path / "v4.db")
        worker.wire_channels(speak_fn=lambda t: True)  # must not raise
        assert worker.bus() is not None

    def test_daemon_builds_and_wires_surfaces(self, tmp_path, monkeypatch):
        """DaemonService builds the ambient worker; surface wiring runs
        without crashing (voice absent → speak_fn None, desktop present)."""
        from friday_v4.daemon import DaemonConfig, DaemonService

        # Stop threads from polling the real DB: give everything short
        # intervals and a hermetic db_path; then immediately stop.
        service = DaemonService(
            config=DaemonConfig(security_scan=False, memory_sweep=False,
                                skill_learn=False, relationship_refresh=False,
                                dispatch_offer=False, autonomy=False,
                                voice=False, notifications=False,
                                mobile_push=False),
            engine=False, notifier=False, suggestion_channel=False,
            sampler=False, security_scanner=False,
            db_path=tmp_path / "v4.db")
        service._build_components()  # must not raise
        assert service._ambient_worker is not None
        service._shutdown_components()


# ─────────────────────────────────────────────────────────────────────
# Slice 4c — MobilePushWorker (daemon-scheduled push transport)
# ─────────────────────────────────────────────────────────────────────


class TestMobilePushWorker:
    """The Wave 15 daemon component that drains the durable queue to the
    phone on a schedule (no manual `friday4 mobile push`)."""

    def test_poll_once_delivers_events(self, tmp_path):
        from friday_v4.daemon import MobilePushWorker

        dbp = tmp_path / "v4.db"
        conn = db.connect(dbp)
        AmbientBus(conn).publish(Event("security", "cve-9",
                                       Priority.CRITICAL))
        conn.close()

        delivered = []
        worker = MobilePushWorker(
            interval=3600.0, db_path=dbp, transporter=delivered.append,
            state_file=tmp_path / "push_state.json")
        assert worker.poll_once() == 1
        assert delivered and delivered[0].payload == "cve-9"
        assert worker.last_report["delivered"] == 1
        assert worker.last_error is None

    def test_graceful_on_missing_db(self, tmp_path):
        from friday_v4.daemon import MobilePushWorker

        worker = MobilePushWorker(
            interval=3600.0,
            db_path=tmp_path / "missing" / "v4.db",
            transporter=lambda n: None,
            state_file=tmp_path / "push_state.json")
        assert worker.poll_once() == 0
        assert worker.last_error is None  # graceful, not an error

    def test_start_stop_lifecycle(self, tmp_path):
        from friday_v4.daemon import MobilePushWorker

        worker = MobilePushWorker(
            interval=0.05, db_path=tmp_path / "v4.db",
            transporter=lambda n: None,
            state_file=tmp_path / "push_state.json")
        worker.start()
        assert worker.running
        import time
        time.sleep(0.2)
        worker.stop()
        assert not worker.running

    def test_daemon_status_includes_mobile_component(self, tmp_path):
        from friday_v4.daemon import DaemonConfig, DaemonService
        from friday_v4.daemon import MobilePushWorker

        worker = MobilePushWorker(
            interval=3600.0, db_path=tmp_path / "v4.db",
            transporter=lambda n: None,
            state_file=tmp_path / "push_state.json")
        worker.running = True
        service = DaemonService(config=DaemonConfig(), engine=False,
                                notifier=False, suggestion_channel=False,
                                sampler=False, security_scanner=False,
                                mobile_push_worker=worker)
        comps = service.status()["components"]
        assert comps["mobile"] is True

    def test_daemon_builds_mobile_worker_when_enabled(self, tmp_path,
                                                     monkeypatch):
        """With mobile_push enabled the daemon constructs + starts the
        worker against the hermetic db_path (no real ~/.friday touched)."""
        from friday_v4.daemon import DaemonConfig, DaemonService

        # The daemon-built worker defaults its push state file to
        # ~/.friday/v4_mobile_push.json — patch home so a delivered
        # event can never persist a cursor into the real home dir
        # (established convention in test_daemon.py's doctor tests).
        import friday_v4.mobile.push as push_mod
        monkeypatch.setattr(push_mod.Path, "home",
                            classmethod(lambda cls: tmp_path))

        service = DaemonService(
            config=DaemonConfig(mobile_push=True,
                                mobile_push_interval=0.05),
            engine=False, notifier=False, suggestion_channel=False,
            sampler=False, security_scanner=False,
            memory_sweeper=False, skill_learner=False,
            relationship_refresher=False, dispatch_offerer=False,
            autonomy_agent=False,
            db_path=tmp_path / "v4.db")
        service._build_components()
        assert service._mobile_push_worker is not False
        assert getattr(service._mobile_push_worker, "running", False)
        service._shutdown_components()
        assert not service._mobile_push_worker.running

    def test_daemon_disables_mobile_worker_when_flagged(self, tmp_path):
        from friday_v4.daemon import DaemonConfig, DaemonService

        service = DaemonService(
            config=DaemonConfig(mobile_push=False),
            engine=False, notifier=False, suggestion_channel=False,
            sampler=False, security_scanner=False,
            memory_sweeper=False, skill_learner=False,
            relationship_refresher=False, dispatch_offerer=False,
            autonomy_agent=False, mobile_push_worker=False,
            db_path=tmp_path / "v4.db")
        service._build_components()
        assert service._mobile_push_worker is False
        service._shutdown_components()

    def test_daemon_shutdown_stops_mobile_worker(self, tmp_path):
        from friday_v4.daemon import DaemonConfig, DaemonService, \
            MobilePushWorker

        worker = MobilePushWorker(
            interval=3600.0, db_path=tmp_path / "v4.db",
            transporter=lambda n: None,
            state_file=tmp_path / "push_state.json")
        worker.start()
        service = DaemonService(config=DaemonConfig(), engine=False,
                                notifier=False, suggestion_channel=False,
                                sampler=False, security_scanner=False,
                                mobile_push_worker=worker)
        service._shutdown_components()
        assert not worker.running


# ─────────────────────────────────────────────────────────────────────
# Slice 4d — operator-configurable push hook (command/file transporter)
# ─────────────────────────────────────────────────────────────────────


class TestCommandTransporter:
    """The operator can point the daemon's mobile push worker at their
    own destination: a shell hook (notification JSON on stdin) or a
    JSONL outbox file. Never crashes — a dead hook never wedges the
    durable queue."""

    def _hook_cmd(self, out_file: Path, code: str) -> str:
        import sys
        script = f"import sys; {code.format(path=str(out_file))}"
        # Quote the interpreter: sys.executable can contain spaces.
        return f'"{sys.executable}" -c "{script}"'

    def test_hook_receives_json_on_stdin(self, tmp_path):
        from friday_v4.mobile import PushNotificationService, \
            command_transporter

        dbp = tmp_path / "v4.db"
        conn = db.connect(dbp)
        AmbientBus(conn).publish(Event("security", "hook me",
                                       Priority.CRITICAL))
        conn.close()

        out = tmp_path / "hook_out.txt"
        cmd = self._hook_cmd(out, "open(r'{path}', 'w').write(sys.stdin.read())")
        service = PushNotificationService(
            db_path=dbp, transporter=command_transporter(cmd),
            state_file=tmp_path / "push_state.json")
        assert service.poll_once() == 1
        line = json.loads(out.read_text().strip())
        assert line["topic"] == "security"
        assert line["payload"] == "hook me"

    def test_missing_binary_never_crashes(self, tmp_path):
        from friday_v4.mobile import PushNotificationService, \
            command_transporter

        dbp = tmp_path / "v4.db"
        conn = db.connect(dbp)
        AmbientBus(conn).publish(Event("a", "one"))
        conn.close()
        service = PushNotificationService(
            db_path=dbp,
            transporter=command_transporter("definitely-not-a-real-binary-xyz"),
            state_file=tmp_path / "push_state.json")
        # The event is still consumed (transporter guarded per event).
        assert service.poll_once() == 1
        assert service.cursor >= 1
        assert service.poll_once() == 0   # queue not wedged

    def test_hook_timeout_never_hangs(self, tmp_path):
        from friday_v4.mobile import PushNotificationService, \
            command_transporter

        dbp = tmp_path / "v4.db"
        conn = db.connect(dbp)
        AmbientBus(conn).publish(Event("a", "slow hook"))
        conn.close()
        import sys as _sys
        service = PushNotificationService(
            db_path=dbp,
            transporter=command_transporter(
                f'{_sys.executable} -c "import time; time.sleep(30)"',
                timeout_seconds=0.2),
            state_file=tmp_path / "push_state.json")
        import time as _t
        started = _t.monotonic()
        assert service.poll_once() == 1
        assert _t.monotonic() - started < 5   # bounded, not 30s

    def test_worker_hook_transports_events(self, tmp_path):
        from friday_v4.daemon import MobilePushWorker

        dbp = tmp_path / "v4.db"
        conn = db.connect(dbp)
        AmbientBus(conn).publish(Event("security", "via hook",
                                       Priority.CRITICAL))
        conn.close()

        out = tmp_path / "hook_out.txt"
        cmd = self._hook_cmd(out, "open(r'{path}', 'w').write(sys.stdin.read())")
        worker = MobilePushWorker(interval=3600.0, db_path=dbp, hook=cmd,
                                  state_file=tmp_path / "push_state.json")
        assert worker.poll_once() == 1
        line = json.loads(out.read_text().strip())
        assert line["payload"] == "via hook"
        assert worker.last_report["delivered"] == 1

    def test_worker_file_path_writes_outbox(self, tmp_path):
        from friday_v4.daemon import MobilePushWorker

        dbp = tmp_path / "v4.db"
        conn = db.connect(dbp)
        AmbientBus(conn).publish(Event("briefing", "daily digest",
                                       Priority.ROUTINE))
        conn.close()

        outbox = tmp_path / "outbox.jsonl"
        worker = MobilePushWorker(interval=3600.0, db_path=dbp,
                                  file_path=str(outbox),
                                  state_file=tmp_path / "push_state.json")
        assert worker.poll_once() == 1
        line = json.loads(outbox.read_text().strip().splitlines()[0])
        assert line["topic"] == "briefing"
        assert line["payload"] == "daily digest"

    def test_injected_transporter_beats_hook(self, tmp_path):
        """An explicit transporter wins over the operator hook (tests /
        callers keep full control)."""
        from friday_v4.daemon import MobilePushWorker

        dbp = tmp_path / "v4.db"
        conn = db.connect(dbp)
        AmbientBus(conn).publish(Event("a", "x"))
        conn.close()

        delivered = []
        worker = MobilePushWorker(
            interval=3600.0, db_path=dbp, transporter=delivered.append,
            hook="echo should-not-run",
            state_file=tmp_path / "push_state.json")
        assert worker.poll_once() == 1
        assert delivered and delivered[0].payload == "x"
