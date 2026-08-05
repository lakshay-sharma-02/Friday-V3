"""Wave 7 — Mobile Companion APP: the phone surface itself (PWA + server).

Covers the companion PWA served by `friday6 mobile serve` — static app
files with correct content types, the installability contract (manifest
+ service worker + generated PNG icons), the app's pairing flow shape
(pwa-token devices register, list without leaking tokens, touch for
liveness), graceful degrade when the app dir is missing, and the fanout
guard (non-Expo tokens stream over SSE, they never hit exp.host).

All hermetic: tmp_path DBs, monkeypatched pairing state, captured HTTP
posts; no real ~/.friday writes, no real network.
"""

from __future__ import annotations

import json
import struct
import threading
import urllib.error
import urllib.request

import pytest

from friday_v6 import db
from friday_v6.mobile import create_api_server
from friday_v6.mobile.api import _APP_DIR, _APP_FILES


@pytest.fixture
def server(tmp_path):
    s = _Server(tmp_path)
    yield s
    s.close()


# ── tiny live server (mirrors test_wave7_mobile's helper) ──────────────


class _Server:
    def __init__(self, tmp_path, app_dir=None):
        self.server = create_api_server(host="127.0.0.1", port=0,
                                        db_path=tmp_path / "v4.db",
                                        app_dir=app_dir)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=10) as r:
            return r.status, r.read(), r.headers.get("Content-Type", "")

    def get_json(self, path):
        status, body, _ = self.get(path)
        return status, json.loads(body or b"{}")

    def post_json(self, path, obj):
        req = urllib.request.Request(
            self.base + path, data=json.dumps(obj).encode(),
            method="POST", headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def delete(self, path):
        req = urllib.request.Request(self.base + path, method="DELETE")
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")


# ── PWA static serving ─────────────────────────────────────────────────


class TestPWAServing:
    def test_root_serves_the_app_shell(self, server):
        status, body, ctype = server.get("/")
        assert status == 200
        assert "text/html" in ctype
        html = body.decode()
        assert "FRIDAY V6 — Companion" in html
        # The app shell references its assets + manifest.
        assert "/app.js" in html and "/manifest.json" in html

    def test_asset_content_types(self, server):
        for path, frag, ctype in [
            ("/app.js", "FRIDAY V6 — Companion", "javascript"),
            ("/app.css", "--cyan", "css"),
            ("/manifest.json", "short_name", "json"),
            ("/service-worker.js", "friday-companion", "javascript"),
        ]:
            status, body, ct = server.get(path)
            assert status == 200, path
            assert ctype in ct, f"{path}: {ct}"
            assert frag in body.decode(errors="replace"), path

    def test_icons_are_real_pngs(self, server):
        for path, size in [("/icon-192.png", 192), ("/icon-512.png", 512),
                           ("/apple-touch-icon.png", 180)]:
            status, body, ct = server.get(path)
            assert status == 200
            assert ct == "image/png"
            assert body[:8] == b"\x89PNG\r\n\x1a\n"
            w, h = struct.unpack(">II", body[16:24])
            assert (w, h) == (size, size)

    def test_favicon_serves_the_icon(self, server):
        status, body, _ = server.get("/favicon.ico")
        assert status == 200
        assert body[:8] == b"\x89PNG\r\n\x1a\n"

    def test_unknown_path_is_404(self, server):
        import urllib.error
        with pytest.raises(urllib.error.HTTPError) as ei:
            server.get("/etc/passwd")
        assert ei.value.code == 404

    def test_missing_app_dir_degrades_api_only(self, tmp_path):
        """The API must keep working even if the UI files are gone."""
        server = _Server(tmp_path, app_dir=tmp_path / "no-app-here")
        try:
            with pytest.raises(urllib.error.HTTPError) as ei:
                server.get("/")
            assert ei.value.code == 404   # honest — app unavailable
            status, payload = server.get_json("/api/status")
            assert status == 200          # ...but the API keeps working
            assert payload["available"] is True
        finally:
            server.close()


# ── installability contract (manifest + service worker) ────────────────


class TestInstallability:
    def test_manifest_contract(self):
        manifest = json.loads((_APP_DIR / "manifest.json").read_text())
        assert manifest["name"].startswith("FRIDAY")
        assert manifest["display"] == "standalone"
        assert manifest["start_url"] == "/"
        sizes = {i["sizes"] for i in manifest["icons"]}
        assert "192x192" in sizes and "512x512" in sizes
        purposes = {i["purpose"] for i in manifest["icons"]}
        assert "maskable" in purposes

    def test_service_worker_never_caches_the_api(self):
        sw = (_APP_DIR / "service-worker.js").read_text()
        assert 'startsWith("/api/")' in sw       # live data passes through
        assert '"/app.js"' in sw                 # shell IS cached
        assert "caches.match" in sw

    def test_every_manifest_asset_is_servable(self, server):
        manifest = json.loads((_APP_DIR / "manifest.json").read_text())
        for icon in manifest["icons"]:
            status, body, _ = server.get(icon["src"])
            assert status == 200, icon["src"]
            assert body[:8] == b"\x89PNG\r\n\x1a\n"

    def test_allowlist_covers_shell_assets(self):
        shell = ["", "index.html", "app.css", "app.js", "manifest.json",
                 "service-worker.js", "icon-192.png", "icon-512.png",
                 "apple-touch-icon.png", "favicon.ico"]
        for name in shell:
            assert name in _APP_FILES, name

    def test_feed_reconnect_reuses_latest_cursor(self):
        """The SSE feed must reconnect with the LATEST cursor, never
        the boot-time one (EventSource auto-reconnect re-opens the
        original URL and would replay everything since boot — the
        duplicate-feed bug the manual reconnect fixes)."""
        js = (_APP_DIR / "app.js").read_text()
        # The cursor is mutable and read at connect time...
        assert "let cursor = Number(store.get(\"feed_cursor\"" in js
        assert "\"/api/events?since=\" + cursor" in js
        # ...and a drop closes the stream and reconnects with it —
        # the server replays only what was missed, no duplicates.
        assert "es.close()" in js
        assert "setTimeout(connect, 3000)" in js
        # The buggy pattern (boot-time const + auto-reconnect) is gone.
        assert "const since = Number(store.get(\"feed_cursor\"" not in js


# ── app-shape pairing (the PWA's exact contract) ───────────────────────


class TestAppPairing:
    def test_pair_from_the_app_contract(self, server, tmp_path, monkeypatch):
        """The exact POST the PWA's Device screen sends."""
        # The server builds its own PairingService per request — point it
        # at the same tmp state file the test's code generator uses.
        monkeypatch.setattr("friday_v6.mobile.pairing._DEFAULT_STATE",
                            tmp_path / "pair.json")
        from friday_v6.mobile.pairing import PairingService
        service = PairingService(db_path=tmp_path / "v4.db",
                                 state_file=tmp_path / "pair.json")
        code = service.generate()

        status, body = server.post_json("/api/devices/register", {
            "code": code,
            "token": "pwa-0f8a2c91-bb7a-4d1e-9c23-000000000001",
            "platform": "web-pwa",
            "name": "Android Pixel",
        })
        assert status == 200
        assert body["ok"] is True and body["device_id"]

        # The device is listed — token never leaked, platform honest.
        status, payload = server.get_json("/api/devices")
        assert status == 200
        assert len(payload["devices"]) == 1
        dev = payload["devices"][0]
        assert dev["platform"] == "web-pwa"
        assert dev["name"] == "Android Pixel"
        assert "token" not in dev

    def test_touch_updates_last_seen(self, server, tmp_path, monkeypatch):
        from friday_v6.mobile.pairing import PairingService
        service = PairingService(db_path=tmp_path / "v4.db",
                                 state_file=tmp_path / "pair.json")
        code = service.generate()
        did = service.register(code, "pwa-touch-1", platform="web-pwa",
                               name="touch me")

        status, body = server.post_json("/api/devices/touch",
                                        {"device_id": did})
        assert status == 200
        assert body["ok"] is True and body["touched"] is True

        # Unknown device → honest False, never a crash.
        status, body = server.post_json("/api/devices/touch",
                                        {"device_id": "nope"})
        assert status == 200
        assert body["ok"] is False

    def test_unpair_from_the_app(self, server, tmp_path, monkeypatch):
        from friday_v6.mobile.pairing import PairingService
        service = PairingService(db_path=tmp_path / "v4.db",
                                 state_file=tmp_path / "pair.json")
        code = service.generate()
        did = service.register(code, "pwa-bye", platform="web-pwa")

        status, body = server.delete(f"/api/devices/{did}")
        assert status == 200 and body["removed"] is True


# ── fanout guard: SSE devices never hit exp.host ───────────────────────


class TestFanoutGuard:
    def test_pwa_device_is_skipped_by_fanout(self, tmp_path, monkeypatch):
        from friday_v6.mobile import Notification, fanout_transporter
        dbp = tmp_path / "v4.db"
        conn = db.connect(dbp)
        db.add_device(conn, "ExponentPushToken[native]", platform="ios",
                      name="native app")
        db.add_device(conn, "pwa-aaaa-bbbb", platform="web-pwa",
                      name="pwa phone")
        conn.close()

        sent = []

        def _fake_post(url, payload, timeout_seconds):
            sent.append(payload)

        monkeypatch.setattr("friday_v6.mobile.push._post", _fake_post)
        t = fanout_transporter(db_path=dbp)
        t(Notification(topic="security", payload="2 new findings",
                       priority=1, source="daemon"))
        # Only the real Expo client receives a push — the PWA streams
        # over its SSE connection instead (never a bogus exp.host POST).
        assert len(sent) == 1
        assert sent[0]["to"] == "ExponentPushToken[native]"
