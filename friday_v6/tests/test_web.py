"""Tests for the `friday6 web` dashboard — data payloads + HTTP server.

The dashboard must render even when every subsystem is missing
(graceful degradation) and must never raise on broken state files.
Server tests spin up the real ``http.server`` on an ephemeral port.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from friday_v6.web import dashboard

# ---------------------------------------------------------------------------
# Data accessors
# ---------------------------------------------------------------------------


class TestDashboardData:
    def test_security_state_missing_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(dashboard, "_SECURITY_STATE",
                            tmp_path / "v4_security_last.json")
        state = dashboard.security_state()
        assert state["scans"] == 0
        assert state["report"] is None
        assert state["last_error"] is None

    def test_security_state_reads_persisted_report(self, tmp_path,
                                                   monkeypatch):
        state_file = tmp_path / "v4_security_last.json"
        state_file.write_text(json.dumps({
            "scans": 2, "last_error": None,
            "report": {"grade": "B", "score": 85,
                       "counts_by_severity": {"critical": 0, "high": 1}},
        }))
        monkeypatch.setattr(dashboard, "_SECURITY_STATE", state_file)
        state = dashboard.security_state()
        assert state["scans"] == 2
        assert state["report"]["grade"] == "B"

    def test_security_state_corrupt_file_is_graceful(self, tmp_path,
                                                     monkeypatch):
        state_file = tmp_path / "v4_security_last.json"
        state_file.write_text("{ not json")
        monkeypatch.setattr(dashboard, "_SECURITY_STATE", state_file)
        state = dashboard.security_state()
        assert state["report"] is None  # defaults, no crash

    def test_daemon_state_shape(self):
        # Must always include the running flag (True/False).
        state = dashboard.daemon_state()
        assert "running" in state

    def test_intelligence_state_graceful_without_stores(self, tmp_path,
                                                        monkeypatch):
        # Point the stores at a missing dir; accessor must not raise.
        monkeypatch.setattr(
            "friday_v6.intelligence.drift._DRIFT_FILE",
            tmp_path / "none" / "drift.json")
        monkeypatch.setattr(
            "friday_v6.intelligence.anomaly._ANOMALY_FILE",
            tmp_path / "none" / "anomaly.json")
        state = dashboard.intelligence_state()
        assert "available" in state

    def test_overview_aggregates_all_subsystems(self):
        ov = dashboard.overview()
        for key in ("daemon", "autonomy", "security", "intelligence",
                    "proactive", "memory", "v3", "voice"):
            assert key in ov

    def test_autonomy_state_graceful_without_db(self, monkeypatch):
        """No V4 DB → the autonomy card renders empty, never a crash."""
        def _boom(*a, **kw):
            raise OSError("no db")
        monkeypatch.setattr("friday_v6.db.connect", _boom)
        state = dashboard.autonomy_state()
        assert state["available"] is False
        assert state["pending"] == []

    def test_autonomy_state_reads_pending_read_only(self, tmp_path,
                                                    monkeypatch):
        """The autonomy card lists the loop's durable asks via a
        read-only probe."""
        from friday_v6 import db
        dbp = tmp_path / "v4.db"
        conn = db.connect(dbp)
        db.create_permission_request(conn, "run the tests", "testing",
                                     command="pytest -q")
        db.record_override(conn, "git", "push origin main", reason="no")
        conn.close()

        real_connect = db.connect
        monkeypatch.setattr("friday_v6.db.connect",
                            lambda **kw: real_connect(dbp, **kw))
        state = dashboard.autonomy_state()
        assert state["available"] is True
        assert any(r["command"] == "pytest -q" for r in state["pending"])
        assert state["overrides"] == 1

    def test_autonomy_approve_deny_route_through_agent(self, tmp_path,
                                                       monkeypatch):
        """The web yes/no resolves a pending ask through AutonomyAgent."""
        from friday_v6 import db
        dbp = tmp_path / "v4.db"
        conn = db.connect(dbp)
        rid = db.create_permission_request(conn, "say hi", "shell",
                                           command="echo hi")
        conn.close()

        accepted = []

        class _FakeAgent:
            def __init__(self):
                pass

            def accept(self, request_id, force=False):
                accepted.append((request_id, force))
                return {"status": "succeeded", "action_id": "a9",
                        "output": "ok"}

            def deny(self, request_id, reason=""):
                return request_id == rid

        monkeypatch.setattr("friday_v6.autonomy.AutonomyAgent", _FakeAgent)
        assert dashboard.autonomy_approve(rid)["ok"] is True
        assert accepted == [(rid, False)]
        assert dashboard.autonomy_deny(rid)["ok"] is True

    def test_memory_state_graceful_without_db(self, monkeypatch):
        """No V4 DB → memory card renders empty, never a crash."""
        def _boom(*a, **kw):
            raise OSError("no db")
        monkeypatch.setattr("friday_v6.db.connect", _boom)
        state = dashboard.memory_state()
        assert state["available"] is False
        assert state["facts"] == []
        assert state["working"] == ""

    def test_memory_state_reads_facts_read_only(self, tmp_path,
                                                monkeypatch):
        """The memory card reads the typed layer (facts + working memory)
        through a read-only probe — no writes to the DB."""
        from friday_v6 import db
        from friday_v6.memory import FactMemory, WorkingMemory
        dbp = tmp_path / "v4.db"
        conn = db.connect(dbp)
        FactMemory(conn).remember("operator", "name", "Lakshay",
                                  source="voice:2026-08-01", confidence=0.95)
        WorkingMemory(conn).set("current_task", "Refactoring auth",
                                priority=3)
        conn.close()

        # Capture the real connect BEFORE patching — a lambda referencing
        # db.connect after the patch would call itself (recursion).
        # Kwargs are forwarded so the read_only=True probe from
        # memory_state() reaches the real mode=ro path (the DB exists,
        # so the read-only open works and is genuinely exercised).
        real_connect = db.connect
        monkeypatch.setattr("friday_v6.db.connect",
                            lambda **kw: real_connect(dbp, **kw))
        state = dashboard.memory_state()
        assert state["available"] is True
        assert any(f["key"] == "operator.name" for f in state["facts"])
        assert "Refactoring auth" in state["working"]

    def test_voice_state_graceful(self):
        state = dashboard.voice_state()
        assert "available" in state

    def test_talk_returns_talk_result_payload(self, monkeypatch):
        """dashboard.talk routes text through the NLU brain and returns
        a TalkResult dict (never touches the real ~/.friday DB or runs
        a real pytest)."""
        class _FakeResult:
            # _run_execution reads these via getattr — no to_dict needed.
            status = "succeeded"
            output = "1 passed"
            action_id = "a1"

        class _FakeConn:
            """dashboard.talk closes the connection in a finally block."""

            def close(self):
                pass

        # talk() imports db inside the function; nl_router._run_execution
        # lazily imports ``from .execution import execute`` at CALL time,
        # so friday_v6.execution.execute is the true resolution point.
        monkeypatch.setattr("friday_v6.db.connect",
                            lambda **kw: _FakeConn())
        monkeypatch.setattr("friday_v6.execution.execute",
                            lambda *a, **kw: _FakeResult())
        payload = dashboard.talk("run the tests")
        assert payload["action"] == "executed"
        assert "Done" in payload["response"]


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------


class _Server:
    """Live server on an ephemeral port, torn down after the test."""

    def __init__(self):
        from friday_v6.web.server import make_server
        self.server = make_server(host="127.0.0.1", port=0)
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


class _DummyScanner:
    """SecurityScanner stand-in that records the path it was told to scan."""

    def __init__(self, path="."):
        self.path = path

    def scan_once(self):
        return 0


class TestWebServer:
    @pytest.fixture
    def server(self):
        srv = _Server()
        yield srv
        srv.close()

    def test_root_serves_html(self, server):
        status, body = server.get("/")
        assert status == 200
        assert b"<!DOCTYPE html>" in body
        assert b"FRIDAY" in body

    def test_api_overview_returns_json(self, server):
        status, body = server.get("/api/overview")
        assert status == 200
        data = json.loads(body)
        for key in ("daemon", "security", "intelligence",
                    "proactive", "memory", "v3", "voice"):
            assert key in data

    def test_api_memory_returns_state(self, server):
        status, body = server.get("/api/memory")
        assert status == 200
        assert "available" in json.loads(body)

    def test_api_security_returns_state(self, server):
        status, body = server.get("/api/security")
        assert status == 200
        assert "scans" in json.loads(body)

    def test_api_projects_returns_directory_list(self, server):
        status, body = server.get("/api/projects")
        assert status == 200
        projects = json.loads(body)
        assert isinstance(projects, list)
        assert all(isinstance(p, str) for p in projects)
        # The repo we're scanning is a git project, so it must appear.
        assert any(p.endswith("friday_v6") for p in projects)

    def test_scan_endpoint_starts_background_scan(self, server, monkeypatch):
        # Keep the test hermetic: a real SecurityScanner would write the
        # user's actual ~/.friday state file and may fire notifications.
        from friday_v6 import daemon as daemon_mod

        scanned = []

        class _FakeScanner(_DummyScanner):
            def scan_once(self):
                scanned.append(self.path)

        monkeypatch.setattr(daemon_mod, "SecurityScanner", _FakeScanner)
        status, body = server.post("/api/scan")
        assert status == 200
        assert json.loads(body) == {"started": True}
        # Poll-wait for the background thread (avoids a timing flake).
        for _ in range(50):
            if scanned:
                break
            time.sleep(0.01)
        assert scanned == ["."]  # no path sent → server cwd

    def test_scan_endpoint_passes_path_to_scanner(self, server, monkeypatch):
        from friday_v6 import daemon as daemon_mod

        scanned = []

        class _FakeScanner(_DummyScanner):
            def scan_once(self):
                scanned.append(self.path)

        monkeypatch.setattr(daemon_mod, "SecurityScanner", _FakeScanner)
        status, body = server.post("/api/scan", {"path": str(Path.cwd())})
        assert status == 200
        for _ in range(50):
            if scanned:
                break
            time.sleep(0.01)
        assert scanned == [str(Path.cwd())]

    def test_scan_endpoint_rejects_invalid_path(self, server, monkeypatch):
        from friday_v6 import daemon as daemon_mod

        monkeypatch.setattr(daemon_mod, "SecurityScanner", _DummyScanner)
        with pytest.raises(urllib.error.HTTPError) as exc:
            server.post("/api/scan", {"path": "/definitely/not/a/dir"})
        assert exc.value.code == 400
        assert "not a directory" in exc.value.read().decode()

    def test_scan_endpoint_rejects_concurrent_scan(self, server, monkeypatch):
        """A second POST while a scan is in flight returns 409."""
        from friday_v6 import daemon as daemon_mod
        from friday_v6.web import server as server_mod

        monkeypatch.setattr(daemon_mod, "SecurityScanner", _DummyScanner)
        # Lock held → endpoint must refuse with 409 (urlopen raises
        # HTTPError for non-2xx; read the response to assert the body).
        with server_mod._scan_lock, pytest.raises(urllib.error.HTTPError) as exc:
            server.post("/api/scan")
        assert exc.value.code == 409
        assert json.loads(exc.value.read()) == {
            "started": False, "error": "scan already running"}

    def test_talk_endpoint_returns_response(self, server, monkeypatch):
        """POST /api/talk routes through dashboard.talk (mocked — no
        real DB writes) and returns the response JSON."""
        import friday_v6.web.dashboard as dash

        monkeypatch.setattr(dash, "talk", lambda text: {
            "action": "executed", "intent": "execute",
            "response": f"Done: {text}"})
        status, body = server.post("/api/talk", {"text": "run the tests"})
        assert status == 200
        data = json.loads(body)
        assert data["response"] == "Done: run the tests"

    def test_talk_endpoint_empty_text_400(self, server):
        with pytest.raises(urllib.error.HTTPError) as exc:
            server.post("/api/talk", {"text": "   "})
        assert exc.value.code == 400

    def test_api_autonomy_returns_state(self, server):
        status, body = server.get("/api/autonomy")
        assert status == 200
        data = json.loads(body)
        assert "pending" in data and "available" in data

    def test_autonomy_approve_endpoint(self, server, monkeypatch):
        """POST /api/autonomy/approve routes the web yes through
        dashboard.autonomy_approve (mocked — no real DB writes)."""
        import friday_v6.web.dashboard as dash

        monkeypatch.setattr(dash, "autonomy_approve", lambda rid: {
            "ok": True, "response": f"Done. ({rid})"})
        status, body = server.post(
            "/api/autonomy/approve", {"request_id": "req-123"})
        assert status == 200
        assert json.loads(body)["ok"] is True

    def test_autonomy_deny_endpoint(self, server, monkeypatch):
        import friday_v6.web.dashboard as dash

        monkeypatch.setattr(dash, "autonomy_deny", lambda rid: {
            "ok": True, "response": "Declined."})
        status, body = server.post(
            "/api/autonomy/deny", {"request_id": "req-123"})
        assert status == 200
        assert json.loads(body)["ok"] is True

    def test_autonomy_approve_missing_id_400(self, server):
        with pytest.raises(urllib.error.HTTPError) as exc:
            server.post("/api/autonomy/approve", {"text": "nope"})
        assert exc.value.code == 400

    def test_unknown_route_404(self, server):
        with pytest.raises(urllib.error.HTTPError) as exc:
            server.get("/api/nope")
        assert exc.value.code == 404
