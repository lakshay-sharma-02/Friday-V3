"""Wave 7 — Mobile Companion App: server-side pairing + push tests.

Covers the device registry (db migration), one-time-code pairing, the
companion API's device endpoints, the Expo push transporter, the
fan-out to paired devices, the daemon worker's default destination, and
the CLI surface — all hermetic (tmp_path DBs, monkeypatched state
files, captured HTTP posts; no real ~/.friday writes, no real network).
"""

from __future__ import annotations

import json
import sys
import threading
import urllib.request

import pytest

from friday_v6 import db


@pytest.fixture(autouse=True)
def _pin_deterministic_llm(monkeypatch):
    """Hermetic: every phone surface test routes through the
    deterministic floor, never a live LLM. The phone's routing behavior
    (desktop/durable-ask/screen) is what these tests cover — a live
    9router proxy must not change their outcome."""
    import friday_v6.nlu as nlu
    monkeypatch.setattr(nlu, "LLMClient", lambda *a, **k: None)


def _real_find_tool(name):
    """The real tool finder (saved so tests can patch just 'claude')."""
    from friday_v6.security.tooling import find_tool
    return find_tool(name)


def _publish_event(dbp, topic="test", payload="hello from the queue",
                   priority=None):
    """Publish one durable ambient event through the real AmbientBus."""
    from friday_v6.ambient import AmbientBus, Event, Priority
    if priority is None:
        p = Priority.IMPORTANT
    elif isinstance(priority, int):
        p = Priority(priority)          # int → enum (never int .value crash)
    else:
        p = priority
    conn = db.connect(dbp)
    try:
        AmbientBus(conn).publish(Event(topic=topic, payload=payload,
                                       priority=p, source="test"))
    finally:
        conn.close()


# ── device registry (db migration v9) ─────────────────────────────────


class TestDeviceRegistry:
    def test_migration_creates_table(self, tmp_path):
        dbp = tmp_path / "v4.db"
        conn = db.connect(dbp)
        try:
            assert db.schema_version(conn) >= 9
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='mobile_devices'").fetchall()
            assert rows
        finally:
            conn.close()

    def test_add_list_remove(self, tmp_path):
        dbp = tmp_path / "v4.db"
        conn = db.connect(dbp)
        try:
            did = db.add_device(conn, "ExponentPushToken[aaa]",
                                platform="ios", name="Tony's iPhone")
            assert did
            rows = db.list_devices(conn)
            assert len(rows) == 1
            assert rows[0]["token"] == "ExponentPushToken[aaa]"

            # Idempotent per token — re-pairing updates, never duplicates.
            did2 = db.add_device(conn, "ExponentPushToken[aaa]",
                                 platform="ios", name="Tony's iPhone")
            assert did2 == did
            assert len(db.list_devices(conn)) == 1

            assert db.remove_device(conn, did) is True
            assert db.list_devices(conn) == []
            assert db.remove_device(conn, did) is False
        finally:
            conn.close()

    def test_get_and_touch(self, tmp_path):
        dbp = tmp_path / "v4.db"
        conn = db.connect(dbp)
        try:
            did = db.add_device(conn, "tok-1", platform="android")
            assert db.get_device(conn, did)["token"] == "tok-1"
            assert db.get_device(conn, "nope") is None
            db.touch_device(conn, did)  # must not raise
        finally:
            conn.close()


# ── pairing service ───────────────────────────────────────────────────


class TestPairing:
    @pytest.fixture
    def service(self, tmp_path, monkeypatch):
        from friday_v6.mobile.pairing import PairingService
        return PairingService(db_path=tmp_path / "v4.db",
                              state_file=tmp_path / "pair.json")

    def test_code_lifecycle(self, service):
        code = service.generate()
        assert len(code) == 6
        assert service.verify(code) is True
        assert service.verify("XXXXXX") is False
        assert service.verify("") is False

    def test_one_time_use(self, service):
        code = service.generate()
        assert service.consume(code) is True
        assert service.verify(code) is False  # consumed — no replay

    def test_expired_code_rejected(self, tmp_path):
        from friday_v6.mobile.pairing import PairingService
        # ttl=0 → the code is born expired.
        service = PairingService(db_path=tmp_path / "v4.db",
                                 state_file=tmp_path / "pair.json",
                                 ttl_seconds=0.0)
        code = service.generate()
        assert service.verify(code) is False

    def test_register_binds_device(self, service):
        code = service.generate()
        did = service.register(code, "ExponentPushToken[x]",
                               platform="android", name="Pixel")
        assert did
        devices = service.devices()
        assert len(devices) == 1
        assert devices[0]["name"] == "Pixel"
        assert devices[0]["platform"] == "android"

        # The code is spent — a second register with the same code fails.
        assert service.register(code, "ExponentPushToken[y]") is None
        assert len(service.devices()) == 1

    def test_register_requires_valid_code_and_token(self, service):
        assert service.register("NOPE", "token") is None
        code = service.generate()
        assert service.register(code, "   ") is None

    def test_remove(self, service):
        code = service.generate()
        did = service.register(code, "tok", name="phone")
        assert service.remove(did) is True
        assert service.devices() == []
        assert service.remove(did) is False


# ── companion API endpoints ───────────────────────────────────────────


class _Server:
    def __init__(self, db_path, pair_state):
        from friday_v6.mobile import create_api_server
        self.server = create_api_server(host="127.0.0.1", port=0,
                                        db_path=db_path)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.pair_state = pair_state

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def _request(self, method, path, body=None):
        import urllib.error
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data,
                                     method=method,
                                     headers={"Content-Type":
                                              "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def post(self, path, body):
        return self._request("POST", path, body)

    def get(self, path):
        return self._request("GET", path)

    def delete(self, path):
        return self._request("DELETE", path)


class TestDeviceAPI:
    @pytest.fixture
    def server(self, tmp_path):
        s = _Server(tmp_path / "v4.db", tmp_path / "pair.json")
        yield s
        s.close()

    def test_register_device_endpoint(self, server, tmp_path, monkeypatch):
        from friday_v6.mobile.pairing import PairingService
        monkeypatch.setattr("friday_v6.mobile.pairing._DEFAULT_STATE",
                            tmp_path / "pair.json")
        service = PairingService(db_path=tmp_path / "v4.db",
                                 state_file=tmp_path / "pair.json")
        code = service.generate()
        status, body = server.post("/api/devices/register", {
            "code": code, "token": "ExponentPushToken[z]",
            "platform": "ios", "name": "iPhone"})
        assert status == 200
        assert body["ok"] is True
        assert body["device_id"]

        # Bad code → 401, honest error.
        status, body = server.post("/api/devices/register", {
            "code": "BADBAD", "token": "ExponentPushToken[w]"})
        assert status == 401
        assert body["ok"] is False

    def test_list_and_remove_device_endpoint(self, server, tmp_path,
                                             monkeypatch):
        from friday_v6.mobile.pairing import PairingService
        service = PairingService(db_path=tmp_path / "v4.db",
                                 state_file=tmp_path / "pair.json")
        code = service.generate()
        did = service.register(code, "tok1", name="phone one")

        status, body = server.get("/api/devices")
        assert status == 200
        assert len(body["devices"]) == 1
        assert body["devices"][0]["id"] == did
        assert "token" not in body["devices"][0]  # never leaked

        status, body = server.delete(f"/api/devices/{did}")
        assert status == 200 and body["removed"] is True
        assert server.get("/api/devices")[1]["devices"] == []


class TestSSEStream:
    """The durable-queue SSE push the phone's live feed depends on.

    Regression: ``_stream_events`` used ``time.monotonic``/``time.sleep``
    without importing ``time`` — the handler wrote headers and then died
    with NameError, so the phone's live feed silently never delivered.
    """

    @pytest.fixture
    def server(self, tmp_path):
        s = _Server(tmp_path / "v4.db", tmp_path / "pair.json")
        yield s
        s.close()

    def _read_sse_frame(self, port, since=0, timeout=5.0):
        import socket as _socket
        s = _socket.create_connection(("127.0.0.1", port), timeout=5)
        s.sendall(
            f"GET /api/events?since={since} HTTP/1.1\r\nHost: x\r\n"
            "Accept: text/event-stream\r\nConnection: close\r\n\r\n".encode())
        s.settimeout(timeout)
        buf = b""
        import time as _time
        end = _time.monotonic() + timeout
        while _time.monotonic() < end and b"data:" not in buf:
            try:
                chunk = s.recv(4096)
            except _socket.timeout:
                break
            if not chunk:
                break
            buf += chunk
        s.close()
        return buf

    def test_events_stream_with_a_queued_event(self, server, tmp_path):
        from friday_v6 import db
        conn = db.connect(tmp_path / "v4.db")
        try:
            conn.execute(
                "INSERT INTO ambient_events "
                "(topic, payload, priority, source, created_at) "
                "VALUES (?,?,?,?,datetime('now'))",
                ("security.scan",
                 json.dumps({"summary": "2 repos clean"}),
                 "normal", "test"))
            conn.commit()
        finally:
            conn.close()

        buf = self._read_sse_frame(server.port)
        assert b"text/event-stream" in buf
        assert b"security.scan" in buf
        assert b"id: 1" in buf

    def test_events_stream_empty_is_idle_not_crash(self, server):
        """No queued events → headers + silence, never an exception."""
        buf = self._read_sse_frame(server.port, timeout=2.0)
        assert b"text/event-stream" in buf
        assert b"data:" not in buf


# ── Expo push transporter ─────────────────────────────────────────────


class TestExpoTransporter:
    def test_posts_to_expo(self, monkeypatch):
        from friday_v6.mobile import Notification, expo_transporter
        from friday_v6.mobile.push import EXPO_PUSH_URL
        sent = {}

        def _fake_post(url, payload, timeout_seconds):
            sent["url"] = url
            sent["payload"] = payload
            sent["timeout"] = timeout_seconds
            return '{"data": [{"status": "ok"}]}'

        t = expo_transporter("ExponentPushToken[t]", post=_fake_post)
        t(Notification(topic="security", payload="2 new findings",
                       priority=1, source="daemon"))
        assert sent["url"] == EXPO_PUSH_URL
        assert sent["payload"]["to"] == "ExponentPushToken[t]"
        assert "security" in sent["payload"]["title"]
        assert sent["payload"]["body"] == "2 new findings"

    def test_empty_token_is_noop(self):
        from friday_v6.mobile import Notification, expo_transporter
        calls = []
        t = expo_transporter("  ",
                             post=lambda *a, **k: calls.append(a))
        t(Notification(topic="x", payload="y"))
        assert calls == []

    def test_network_failure_never_raises(self, monkeypatch):
        from friday_v6.mobile import Notification, expo_transporter
        monkeypatch.setattr("friday_v6.mobile.push._post",
                            lambda *a, **k: None)
        t = expo_transporter("ExponentPushToken[t]")
        t(Notification(topic="x", payload="y"))  # must not raise


class TestFanout:
    def test_pushes_to_every_paired_device(self, tmp_path, monkeypatch):
        from friday_v6.mobile import Notification, fanout_transporter
        dbp = tmp_path / "v4.db"
        conn = db.connect(dbp)
        db.add_device(conn, "ExponentPushToken[a]", platform="ios", name="A")
        db.add_device(conn, "ExponentPushToken[b]", platform="android", name="B")
        conn.close()

        sent: list[dict] = []

        def _fake_post(url, payload, timeout_seconds):
            sent.append(payload)

        monkeypatch.setattr("friday_v6.mobile.push._post", _fake_post)
        t = fanout_transporter(db_path=dbp)
        t(Notification(topic="collab", payload="2 peer obs",
                       priority=1, source="daemon"))
        assert len(sent) == 2
        tokens = {p["to"] for p in sent}
        assert tokens == {"ExponentPushToken[a]", "ExponentPushToken[b]"}

    def test_no_devices_is_silent(self, tmp_path):
        from friday_v6.mobile import Notification, fanout_transporter
        t = fanout_transporter(db_path=tmp_path / "v4.db")
        t(Notification(topic="x", payload="y"))  # must not raise


# ── phone → desktop control (Wave 19 wiring) ──────────────────────────


class TestPhoneDesktopControl:
    """The phone's Chat tab controls the PC it is paired to.

    The companion server runs ON the operator's machine, so "open brave"
    from the phone routes through the same ``desktop_text_command`` the
    CLI (``friday6 talk``) and the web dashboard use — Friday focuses /
    launches Brave here. Hermetic: the WM layer is monkeypatched; no
    real desktop is ever touched.
    """

    def test_talk_routes_open_brave_to_desktop(self, tmp_path, monkeypatch):
        import friday_v6.desktop.wm_abstraction as wm_mod
        calls: list[str] = []

        def _fake_desktop(text: str) -> str:
            calls.append(text)
            return "Launching brave."

        monkeypatch.setattr(wm_mod, "desktop_text_command", _fake_desktop)
        from friday_v6.mobile.api import MobileAPI
        api = MobileAPI(db_path=tmp_path / "v4.db")
        result = api.talk("open brave")
        assert result["action"] == "desktop"
        assert result["response"] == "Launching brave."
        assert calls == ["open brave"]  # the exact phone utterance reached

    def test_focus_command_through_http_endpoint(self, tmp_path,
                                                 monkeypatch):
        import friday_v6.desktop.wm_abstraction as wm_mod
        calls: list[str] = []

        def _fake_desktop(text: str) -> str:
            calls.append(text)
            return "Focused Code."

        monkeypatch.setattr(wm_mod, "desktop_text_command", _fake_desktop)
        s = _Server(tmp_path / "v4.db", tmp_path / "pair.json")
        try:
            # The exact POST the PWA's Chat tab sends.
            status, body = s.post("/api/talk", {"text": "focus code editor"})
            assert status == 200
            assert body["action"] == "desktop"
            assert body["response"] == "Focused Code."
            assert calls == ["focus code editor"]
        finally:
            s.close()

    def test_desktop_unavailable_is_honest_not_crash(self, tmp_path,
                                                     monkeypatch):
        """No WM / unavailable desktop → honest message, never a crash
        (the desktop handler degrades; the phone still gets a reply).

        Hermetic: the WM is patched to an unavailable stub so the test
        NEVER touches a real desktop — otherwise "open brave" could
        actually launch a browser on the machine running the suite.
        """
        import friday_v6.desktop.wm_abstraction as wm_mod

        class _UnavailableWM:
            @property
            def is_available(self):
                return False

        monkeypatch.setattr(wm_mod, "WindowManager",
                            lambda *a, **k: _UnavailableWM())
        from friday_v6.mobile.api import MobileAPI
        api = MobileAPI(db_path=tmp_path / "v4.db")
        result = api.talk("open brave")
        assert result["action"] == "desktop"
        assert "isn't available" in result["response"]
        assert result["response"]


# ── phone confirm: CONFIRM → durable ask (Wave 20) ─────────────────────

class TestPhoneAsk:
    """The phone's CONFIRM actions become durable asks, not dead-end
    denials. The reply carries a request_id the PWA renders as inline
    Yes/No buttons; 'yes, run it' resolves the ask through the real
    gate. Hermetic: tmp_path DB, no real desktop/network."""

    def test_confirm_action_asks_durably(self, tmp_path):
        from friday_v6.mobile.api import MobileAPI
        api = MobileAPI(db_path=tmp_path / "v4.db")
        result = api.talk("clone it https://github.com/a/b.git")
        assert result["action"] == "asked"
        assert result["status"] == "asked"
        assert result["request_id"]
        assert "yes, run it" in result["response"]
        assert result["action_type"] == "claude"
        # The ask persisted + is pending (the PWA's buttons resolve it).
        conn = db.connect(tmp_path / "v4.db")
        try:
            pending = db.pending_permission_requests(conn, limit=10)
            assert any(p["id"] == result["request_id"] for p in pending)
            assert pending[0]["description"]
        finally:
            conn.close()

    def test_yes_resolves_the_ask(self, tmp_path, monkeypatch):
        """After the ask, 'yes, run it' resolves it through the real
        gate (the PWA's Yes button posts exactly this). The claude CLI
        is patched away so the test never clones a real repo — the
        executor degrades fast to a structured failure."""
        import friday_v6.execution.executors as ex
        monkeypatch.setattr(ex, "find_tool",
                            lambda name: None if name == "claude"
                            else _real_find_tool(name))
        from friday_v6.mobile.api import MobileAPI
        api = MobileAPI(db_path=tmp_path / "v4.db")
        result = api.talk("clone it https://github.com/a/b.git")
        assert result["action"] == "asked"
        conn = db.connect(tmp_path / "v4.db")
        try:
            pending = db.pending_permission_requests(conn, limit=10)
            assert pending
        finally:
            conn.close()
        # 'yes, run it' routes to ACCEPT → approves the pending ask.
        yes = api.talk("yes, run it")
        # ACCEPT approves the durable ask; the claude executor runs and
        # fails fast ("claude CLI not found") — a resolved ask, not a
        # dead-end denial.
        assert yes["action"] in ("executed", "failed", "chat", "denied")
        conn = db.connect(tmp_path / "v4.db")
        try:
            assert db.pending_permission_requests(conn, limit=10) == []
        finally:
            conn.close()

    def test_deny_resolves_the_ask(self, tmp_path):
        from friday_v6.mobile.api import MobileAPI
        api = MobileAPI(db_path=tmp_path / "v4.db")
        api.talk("clone it https://github.com/a/b.git")
        api.talk("no")
        conn = db.connect(tmp_path / "v4.db")
        try:
            pending = db.pending_permission_requests(conn, limit=10)
            assert not pending  # the 'no' resolved the ask
        finally:
            conn.close()


# ── daemon worker default destination ─────────────────────────────────


class TestWorker:
    def test_pushes_to_paired_devices_by_default(self, tmp_path, monkeypatch):
        from friday_v6.daemon import MobilePushWorker
        dbp = tmp_path / "v4.db"
        conn = db.connect(dbp)
        db.add_device(conn, "ExponentPushToken[w]", platform="ios", name="watch")
        conn.close()
        _publish_event(dbp, topic="security", payload="push me",
                       priority=1)

        sent: list[dict] = []
        monkeypatch.setattr("friday_v6.mobile.push._post",
                            lambda url, payload, timeout_seconds:
                            sent.append(payload))
        worker = MobilePushWorker(db_path=dbp,
                                  state_file=tmp_path / "cursor.json")
        delivered = worker.poll_once()
        assert delivered == 1
        assert sent and sent[0]["to"] == "ExponentPushToken[w]"
        assert worker.last_report["delivered"] == 1

    def test_unpaired_falls_back_to_logger(self, tmp_path):
        from friday_v6.daemon import MobilePushWorker
        dbp = tmp_path / "v4.db"
        _publish_event(dbp, topic="t", payload="p", priority=1)
        worker = MobilePushWorker(db_path=dbp,
                                  state_file=tmp_path / "cursor.json")
        delivered = worker.poll_once()  # no devices → log transporter
        assert delivered == 1  # still drained, no crash

    def test_missing_db_degrades(self, tmp_path):
        """A not-yet-created DB auto-creates and drains to 0 — never a
        crash, and the report is honest ('delivered 0')."""
        from friday_v6.daemon import MobilePushWorker
        worker = MobilePushWorker(db_path=tmp_path / "missing" / "v4.db",
                                  state_file=tmp_path / "cursor.json")
        assert worker.poll_once() == 0
        assert worker.last_report == {"delivered": 0, "cursor": 0,
                                      "delivered_total": 0}


# ── CLI ───────────────────────────────────────────────────────────────


class TestAPITokenAuth:
    """Optional bearer token: the API (the power) is gated when a token
    is configured, the PWA shell stays public (Wave 22 — anywhere
    access: safe public exposure over a tunnel)."""

    @pytest.fixture
    def tserver(self, tmp_path):
        from friday_v6.mobile import create_api_server
        server = create_api_server(host="127.0.0.1", port=0,
                                   db_path=str(tmp_path / "v4.db"),
                                   token="s3cr3t")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_address[1]}"
        finally:
            server.shutdown()
            server.server_close()

    def _get(self, base, path, token=None):
        import urllib.error
        req = urllib.request.Request(base + path)
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def test_api_requires_token_when_configured(self, tserver):
        status, body = self._get(tserver, "/api/status")
        assert status == 401
        assert body.get("error") == "unauthorized"

    def test_wrong_token_rejected(self, tserver):
        status, _ = self._get(tserver, "/api/status", token="nope")
        assert status == 401

    def test_bearer_token_allows(self, tserver):
        status, body = self._get(tserver, "/api/status", token="s3cr3t")
        assert status == 200
        assert body.get("available") is True

    def test_token_query_param_allows(self, tserver):
        """EventSource can't set headers — the same gate via ?token=."""
        status, body = self._get(tserver, "/api/status?token=s3cr3t")
        assert status == 200
        assert body.get("available") is True

    def test_talk_gated(self, tserver):
        import urllib.request
        req = urllib.request.Request(
            tserver + "/api/talk", data=json.dumps({"text": "hello"}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                assert r.status == 401
        except urllib.error.HTTPError as exc:
            assert exc.code == 401

    def test_pwa_shell_stays_public(self, tserver):
        """The PWA must load so the operator can enter the token — the
        UI is a shell, the API is the power (never-crash, public shell)."""
        import urllib.error
        req = urllib.request.Request(tserver + "/")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                body = r.read().decode(errors="replace")
                assert r.status == 200
                assert "<html" in body.lower() or "FRIDAY" in body
        except urllib.error.HTTPError as exc:
            assert exc.code != 401  # the shell is NOT gated
        # The JS bundle rides along too (raw read — it's JS, not JSON).
        req = urllib.request.Request(tserver + "/app.js")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                assert r.status == 200
                body = r.read()
                assert b"CLAUDE" in body or b"token" in body
        except urllib.error.HTTPError as exc:
            assert exc.code != 401

    def test_open_when_no_token(self, tmp_path):
        from friday_v6.mobile import create_api_server
        server = create_api_server(host="127.0.0.1", port=0,
                                   db_path=str(tmp_path / "v4.db"))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"
            status, _ = self._get(base, "/api/status")
            assert status == 200
        finally:
            server.shutdown()
            server.server_close()


class TestBindNormalization:
    """`_normalize_bind` — --host accepts a bare address, host:port, or
    a full URL. Regression: the operator pasted ``100.74.85.17:8900/``
    (exactly what `friday6 mobile remote` prints) and got
    ``Name or service not known`` — the CLI must not die on that."""

    def _norm(self, host, port=8900):
        from friday_v6.cli_mobile import _normalize_bind
        return _normalize_bind(host, port)

    def test_bare_host(self):
        assert self._norm("0.0.0.0") == ("0.0.0.0", 8900)
        assert self._norm("100.74.85.17") == ("100.74.85.17", 8900)
        assert self._norm("localhost") == ("localhost", 8900)

    def test_host_port(self):
        assert self._norm("100.74.85.17:8900") == ("100.74.85.17", 8900)
        assert self._norm("127.0.0.1:9000", 8900) == ("127.0.0.1", 9000)

    def test_full_url_with_trailing_slash(self):
        """The exact operator error — a pasted URL with trailing slash."""
        assert self._norm("100.74.85.17:8900/") == ("100.74.85.17", 8900)
        assert self._norm("http://100.74.85.17:8900/") == \
            ("100.74.85.17", 8900)
        assert self._norm("https://100.74.85.17:8900") == \
            ("100.74.85.17", 8900)

    def test_url_with_path_is_stripped(self):
        assert self._norm("http://100.74.85.17:8900/api/status") == \
            ("100.74.85.17", 8900)

    def test_ipv6_bracketed(self):
        assert self._norm("[::1]:8900") == ("::1", 8900)

    def test_bare_ipv6_untouched(self):
        # Invalid as a URL host, but never mis-split into host+port.
        assert self._norm("fe80::1") == ("fe80::1", 8900)

    def test_empty_host_falls_back(self):
        assert self._norm("") == ("0.0.0.0", 8900)


class TestAutostart:
    """`friday6 mobile autostart|no-autostart` — the 9router-style XDG
    entry that starts the companion + tray on every login (hermetic:
    XDG_CONFIG_HOME is redirected to tmp_path)."""

    def _cfg(self, monkeypatch, tmp_path):
        cfg = tmp_path / "cfg"
        monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg))
        return cfg

    def test_autostart_writes_entry(self, monkeypatch, tmp_path, capsys):
        cfg = self._cfg(monkeypatch, tmp_path)
        from friday_v6.cli_talk import main
        rc = main(["mobile", "autostart"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "Autostart entry written" in out
        path = cfg / "autostart" / "friday6-mobile.desktop"
        assert path.exists()
        content = path.read_text()
        assert "[Desktop Entry]" in content
        assert "mobile serve --host 0.0.0.0 --tray" in content
        assert "X-GNOME-Autostart-enabled=true" in content

    def test_autostart_bakes_token_and_tunnel(self, monkeypatch, tmp_path,
                                              capsys):
        cfg = self._cfg(monkeypatch, tmp_path)
        from friday_v6.cli_talk import main
        rc = main(["mobile", "autostart", "--token", "sekrit",
                   "--tunnel", "cloudflare"])
        assert rc == 0
        content = (cfg / "autostart" /
                   "friday6-mobile.desktop").read_text()
        assert "--token sekrit" in content
        assert "--tunnel cloudflare" in content

    def test_no_autostart_removes_idempotently(self, monkeypatch,
                                               tmp_path, capsys):
        cfg = self._cfg(monkeypatch, tmp_path)
        from friday_v6.cli_talk import main
        assert main(["mobile", "autostart"]) == 0
        assert (cfg / "autostart" /
                "friday6-mobile.desktop").exists()
        assert main(["mobile", "no-autostart"]) == 0
        assert not (cfg / "autostart" /
                    "friday6-mobile.desktop").exists()
        # second removal is idempotent, rc 0
        assert main(["mobile", "no-autostart"]) == 0


class TestCloudflareTunnel:
    """--tunnel cloudflare on serve (hermetic: cloudflared is faked)."""

    def test_spawn_returns_none_without_cloudflared(self, monkeypatch):
        import friday_v6.cli_mobile as cli
        monkeypatch.setattr(cli.shutil, "which", lambda _n: None)
        proc, thread = cli._spawn_cloudflare_tunnel(8900)
        assert proc is None and thread is None

    def test_spawn_launches_and_reads_url(self, monkeypatch, capsys):
        import io
        import subprocess
        import friday_v6.cli_mobile as cli
        monkeypatch.setattr(cli.shutil, "which", lambda _n: "/usr/bin/cloudflared")

        class _FakeProc:
            def __init__(self, stdout):
                self.stdout = stdout
                self.terminated = False
            def terminate(self):
                self.terminated = True

        stream = io.StringIO(
            "2026-08-04 INFO  hello\n"
            "your quick tunnel has been created! "
            "https://abc-123.trycloudflare.com\n")
        fake = _FakeProc(stream)
        monkeypatch.setattr(subprocess, "Popen",
                            lambda *a, **k: fake)
        proc, thread = cli._spawn_cloudflare_tunnel(8900)
        assert proc is not None and thread is not None
        thread.join(timeout=5)
        out = capsys.readouterr().out
        assert "https://abc-123.trycloudflare.com" in out

class TestMobileTray:
    """MobileTray — degrades to unavailable without pystray (never crash)."""

    def test_tray_unavailable_without_pystray(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "pystray", None)
        from friday_v6.mobile.tray import MobileTray
        tray = MobileTray(base_url="http://0.0.0.0:8900")
        assert tray.available is False
        assert tray.start() is False
        tray.stop()  # no crash

    def test_tray_menu_has_mobile_actions(self):
        from friday_v6.mobile.tray import MobileTray
        tray = MobileTray()
        labels = [label for label, _ in tray._menu_items()]
        assert any("dashboard" in l.lower() for l in labels)
        assert any("pair" in l.lower() for l in labels)
        assert any("url" in l.lower() for l in labels)


class TestMobileRemoteCLI:
    """`friday6 mobile remote` — the anywhere-access answer (hermetic:
    LAN/tailscale detection is patched, no real network)."""

    def test_remote_prints_lan_and_guidance(self, tmp_path, capsys,
                                            monkeypatch):
        import friday_v6.cli_mobile as cli
        monkeypatch.setattr(cli, "_lan_ips", lambda: ["192.168.1.20"])
        monkeypatch.setattr(cli, "_tailscale_ip", lambda: "")

        from friday_v6.cli_talk import main
        rc = main(["mobile", "remote", "--db", str(tmp_path / "v4.db")])
        out = capsys.readouterr().out
        assert rc == 0
        assert "http://192.168.1.20:8900" in out
        assert "Tailscale" in out
        assert "cloudflared tunnel" in out
        assert "--token" in out  # security guidance

    def test_remote_prints_tailscale_url(self, tmp_path, capsys,
                                         monkeypatch):
        import friday_v6.cli_mobile as cli
        monkeypatch.setattr(cli, "_lan_ips", lambda: ["192.168.1.20"])
        monkeypatch.setattr(cli, "_tailscale_ip",
                            lambda: "100.64.0.5")

        from friday_v6.cli_talk import main
        rc = main(["mobile", "remote"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "http://100.64.0.5:8900" in out

    def test_remote_degrades_when_nothing_detectable(self, capsys,
                                                     monkeypatch):
        import friday_v6.cli_mobile as cli
        monkeypatch.setattr(cli, "_lan_ips", lambda: [])
        monkeypatch.setattr(cli, "_tailscale_ip", lambda: "")

        from friday_v6.cli_talk import main
        rc = main(["mobile", "remote"])
        out = capsys.readouterr().out
        assert rc == 1
        assert "Couldn't detect" in out

    def test_remote_notes_token_when_set(self, tmp_path, capsys,
                                         monkeypatch):
        import friday_v6.cli_mobile as cli
        monkeypatch.setattr(cli, "_lan_ips", lambda: ["192.168.1.20"])
        monkeypatch.setattr(cli, "_tailscale_ip", lambda: "")

        from friday_v6.cli_talk import main
        rc = main(["mobile", "remote", "--token", "abc123"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "one is set now" in out  # token-aware guidance


class TestCLI:
    def test_pair_devices_unpair_roundtrip(self, tmp_path, capsys,
                                           monkeypatch):
        monkeypatch.setattr("friday_v6.mobile.pairing._DEFAULT_STATE",
                            tmp_path / "pair.json")
        from friday_v6.cli_talk import main

        rc = main(["mobile", "pair", "--db", str(tmp_path / "v4.db")])
        out = capsys.readouterr().out
        assert rc == 0
        assert "pairing code" in out.lower()

        # The code is real — register with it through the service.
        from friday_v6.mobile import PairingService
        service = PairingService(db_path=tmp_path / "v4.db",
                                 state_file=tmp_path / "pair.json")
        code = service.generate()
        service.register(code, "tok-cli", name="cli phone")

        rc = main(["mobile", "devices", "--db", str(tmp_path / "v4.db")])
        out = capsys.readouterr().out
        assert rc == 0
        assert "cli phone" in out

    def test_cli_importable(self):
        from friday_v6.cli_mobile import (build_mobile_parser,
                                          cmd_mobile_autostart,
                                          cmd_mobile_devices,
                                          cmd_mobile_no_autostart,
                                          cmd_mobile_pair,
                                          cmd_mobile_remote,
                                          cmd_mobile_unpair)
        assert build_mobile_parser is not None
        assert cmd_mobile_pair is not None
        assert cmd_mobile_devices is not None
        assert cmd_mobile_unpair is not None
        assert cmd_mobile_remote is not None
        assert cmd_mobile_autostart is not None
        assert cmd_mobile_no_autostart is not None

    def test_mobile_exports(self):
        from friday_v6.mobile import (PairingService, expo_transporter,
                                      fanout_transporter)
        assert PairingService is not None
        assert expo_transporter is not None
        assert fanout_transporter is not None
