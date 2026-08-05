"""Wave 22 — CLAUDE: bridge (PWA → persistent Claude Code session).

The operator types ``CLAUDE: <text>`` in the companion chat; the bridge
forwards it to ONE long-lived Claude Code session (Agent SDK spawning
the same ``claude`` CLI, same 9router settings) until ``CLAUDE END``.
Tool-permission asks become durable ``permission_requests`` (source=
``bridge``) surfaced on the ambient bus; "yes, run it"/"no" resolves
them through ``AutonomyAgent.accept/deny``.

All hermetic: the Agent SDK is never imported for real — every test
injects a fake ``claude_agent_sdk`` module so the bridge's lazy import
finds a stand-in and nothing touches a real model. DB is tmp_path.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types

import pytest

from friday_v6.agent import (ClaudeBridge, CLAUDE_END, CLAUDE_PREFIX,
                             get_bridge, is_claude_end, is_claude_message)
from friday_v6.agent import permissions as P
from friday_v6 import db


# ── fake Agent SDK ──────────────────────────────────────────────────


class _FakeClient:
    """Minimal stand-in for ClaudeSDKClient (records the session)."""

    def __init__(self, options=None):
        self.options = options
        self.prompts: list[tuple[str, str]] = []
        self.connected = False
        self.disconnected = False
        self._messages = []

    async def connect(self, prompt=None):
        # The bridge connects with the streaming-input generator
        # (``prompt=``) so can_use_tool fires — accept and ignore it.
        self.connected = True

    async def query(self, prompt=None, session_id="default"):
        # The bridge streams prompts as async generators (can_use_tool
        # requires streaming input) — consume it to get the real text.
        text = ""
        if prompt is not None:
            try:
                async for item in prompt:
                    message = item.get("message", {})
                    text += message.get("content", "") if isinstance(
                        message, dict) else str(message)
            except TypeError:
                text = str(prompt)
        self.prompts.append((text, session_id))

    async def receive_messages(self):
        # The bridge drains per-turn and stops at ResultMessage (the
        # real SDK emits one when a turn ends); yield one and return
        # so the turn's drain completes (no infinite tail).
        yield _FakeResultMessage()

    async def disconnect(self):
        self.disconnected = True


class _FakeAssistantMessage:
    def __init__(self, text):
        self.message = types.SimpleNamespace(content=[
            types.SimpleNamespace(type="text", text=text)])


class _FakeResultMessage:
    """Signals a turn's end (the bridge stops draining on this)."""

    @property
    def name(self):
        return "ResultMessage"

    def __repr__(self):
        return "<ResultMessage>"


def _install_fake_sdk(monkeypatch, client=None):
    """Inject a fake claude_agent_sdk into sys.modules (lazy import)."""
    client = client or _FakeClient()
    captured = {"client": client}

    fake_types = types.ModuleType("claude_agent_sdk.types")
    fake_types.ClaudeAgentOptions = _FakeOptions
    fake_client_mod = types.ModuleType("claude_agent_sdk.client")
    fake_client_mod.ClaudeSDKClient = lambda options=None: (
        captured["client"] if client is not None else None)

    fake = types.ModuleType("claude_agent_sdk")
    fake.types = fake_types
    fake.client = fake_client_mod
    fake.ClaudeAgentOptions = _FakeOptions
    fake.ClaudeSDKClient = fake_client_mod.ClaudeSDKClient
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake)
    monkeypatch.setitem(sys.modules, "claude_agent_sdk.types", fake_types)
    monkeypatch.setitem(sys.modules, "claude_agent_sdk.client",
                        fake_client_mod)
    return captured


class _FakeOptions:
    """ClaudeAgentOptions stand-in that keeps its kwargs."""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


@pytest.fixture
def db_path(tmp_path):
    p = tmp_path / "v4.db"
    conn = db.connect(str(p))
    conn.close()
    return str(p)


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    """Each test starts with an empty permission registry + fresh bridge
    singleton (the bridge caches SDK availability, so a fake SDK from
    one test must never leak into the next)."""
    import friday_v6.agent.bridge as B
    P.registry._asks.clear()
    monkeypatch.setattr(B, "_bridge", None)
    yield
    P.registry._asks.clear()
    B._bridge = None


# ── prefix parsing ──────────────────────────────────────────────────


class TestParsing:
    def test_claude_prefix_detected(self):
        assert is_claude_message("CLAUDE: list files")
        assert is_claude_message("claude: fix the test")
        assert is_claude_message("  CLAUDE: hi  ")

    def test_non_claude_text_untouched(self):
        assert not is_claude_message("claude is my favorite")
        assert not is_claude_message("open brave")
        assert not is_claude_message("CLAUDE-END")

    def test_claude_end_detected(self):
        assert is_claude_end("CLAUDE END")
        assert is_claude_end("claude end")
        assert not is_claude_end("CLAUDE: end this session")


# ── session lifecycle (fake SDK) ────────────────────────────────────


class TestBridgeSession:
    def test_available_false_without_sdk(self, monkeypatch):
        # None in sys.modules forces ImportError on the next import —
        # the reliable way to simulate "not installed" even when the
        # real SDK is present in the environment (delitem would let
        # Python re-import it from disk).
        monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)
        b = ClaudeBridge()
        assert b.available() is False
        r = b.send("CLAUDE: hi")
        assert r["ok"] is False
        assert "isn't available" in r["response"] or "Couldn't" in r["response"]

    def test_send_forwards_stripped_prompt_to_one_session(
            self, monkeypatch, db_path):
        cap = _install_fake_sdk(monkeypatch)
        b = ClaudeBridge(db_path=db_path)
        assert b.available() is True
        assert b.send("CLAUDE: list files")["ok"] is True
        _wait_active(b)
        assert b.status()["active"] is True
        # wait for the worker to consume the queue
        _wait_prompts(cap["client"], 1)
        prompts = cap["client"].prompts
        assert prompts and prompts[0][0] == "list files"
        assert prompts[0][1] == "friday-bridge"  # constant session id

    def test_end_closes_session(self, monkeypatch, db_path):
        cap = _install_fake_sdk(monkeypatch)
        b = ClaudeBridge(db_path=db_path)
        b.send("CLAUDE: hello")
        _wait_prompts(cap["client"], 1)
        r = b.end()
        assert r["ended"] is True
        _wait_disconnect(cap["client"])
        assert cap["client"].disconnected is True
        assert b.status()["active"] is False

    def test_multiple_prompts_share_session(self, monkeypatch, db_path):
        cap = _install_fake_sdk(monkeypatch)
        b = ClaudeBridge(db_path=db_path)
        b.send("CLAUDE: first task")
        b.send("CLAUDE: continue the work")
        _wait_active(b)
        _wait_prompts(cap["client"], 2)
        prompts = cap["client"].prompts
        assert [p[0] for p in prompts] == ["first task", "continue the work"]
        # one client, one session id — context accumulates
        assert {p[1] for p in prompts} == {"friday-bridge"}


def _wait_active(bridge, timeout=5.0):
    import time
    end = time.monotonic() + timeout
    while time.monotonic() < end and not bridge.status()["active"]:
        time.sleep(0.02)


def _wait_prompts(client, n, timeout=5.0):
    import time
    end = time.monotonic() + timeout
    while time.monotonic() < end and len(client.prompts) < n:
        time.sleep(0.02)


def _wait_disconnect(client, timeout=5.0):
    import time
    end = time.monotonic() + timeout
    while time.monotonic() < end and not client.disconnected:
        time.sleep(0.02)


# ── routing through the mobile API ──────────────────────────────────


class TestTalkRouting:
    def _api(self, tmp_path):
        from friday_v6.mobile import MobileAPI
        return MobileAPI(db_path=str(tmp_path / "v4.db"))

    def test_claude_message_routes_to_bridge(self, monkeypatch, tmp_path):
        cap = _install_fake_sdk(monkeypatch)
        api = self._api(tmp_path)
        out = api.talk("CLAUDE: refactor the auth module")
        assert out["intent"] == "agent"
        assert out["action"] == "agent"
        assert "Claude Code" in out["response"] or "on it" in out["response"]
        _wait_prompts(cap["client"], 1)
        assert cap["client"].prompts[0][0] == "refactor the auth module"

    def test_claude_end_routes_to_bridge_close(self, monkeypatch, tmp_path):
        cap = _install_fake_sdk(monkeypatch)
        api = self._api(tmp_path)
        api.talk("CLAUDE: hello")
        _wait_prompts(cap["client"], 1)
        out = api.talk("CLAUDE END")
        assert out["intent"] == "agent"
        assert "closed" in out["response"].lower()

    def test_normal_text_still_nl_router(self, tmp_path):
        api = self._api(tmp_path)
        out = api.talk("hello")
        assert out["intent"] != "agent"
        assert out["action"] in ("chat", "greeting", "asked")

    def test_agent_status_endpoint(self, monkeypatch, tmp_path):
        _install_fake_sdk(monkeypatch)
        api = self._api(tmp_path)
        st = api.agent_status()
        # Wave 5: the status badge also carries the kill-switch state.
        assert set(st) == {"available", "active", "busy", "aborted"}
        assert st["available"] is True
        assert st["aborted"] is False

    def test_agent_status_degrades(self, monkeypatch, tmp_path):
        monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)
        api = self._api(tmp_path)
        st = api.agent_status()
        assert st["available"] is False and st["active"] is False


# ── permission weave (can_use_tool → durable ask → resolve) ─────────


class TestPermissionWeave:
    def test_can_use_tool_creates_bridge_ask_and_denies_when_unresolved(
            self, monkeypatch, db_path):
        cap = _install_fake_sdk(monkeypatch)
        loop = asyncio.new_event_loop()

        async def scenario():
            can_use_tool = P.make_can_use_tool(db_path, loop, timeout=1.0)
            task = asyncio.ensure_future(
                can_use_tool("Bash", {"command": "ls"}, None))
            # let the ask land in the registry
            await asyncio.sleep(0.1)
            conn = db.connect(db_path)
            pending = db.pending_permission_requests(conn, limit=10)
            conn.close()
            assert pending, "no durable ask was recorded"
            assert pending[0]["source"] == "bridge"
            assert pending[0]["action_type"] == "claude_tool:Bash"
            result = await task  # never resolved → deny (fail closed)
            return result, pending

        try:
            result, pending = loop.run_until_complete(scenario())
        finally:
            loop.close()
        assert result["behavior"] == "deny"

    def test_can_use_tool_allow_via_registry(self, monkeypatch, db_path):
        cap = _install_fake_sdk(monkeypatch)
        loop = asyncio.new_event_loop()

        async def scenario():
            can_use_tool = P.make_can_use_tool(db_path, loop, timeout=30.0)
            task = asyncio.ensure_future(
                can_use_tool("Edit", {"file_path": "x.py"}, None))
            await asyncio.sleep(0.1)
            conn = db.connect(db_path)
            pending = db.pending_permission_requests(conn, limit=1)
            conn.close()
            rid = pending[0]["id"]
            assert P.registry.resolve(rid, True) is True
            result = await task
            return result

        try:
            result = loop.run_until_complete(scenario())
        finally:
            loop.close()
        assert result["behavior"] == "allow"

    def test_can_use_tool_deny_via_registry(self, monkeypatch, db_path):
        cap = _install_fake_sdk(monkeypatch)
        loop = asyncio.new_event_loop()

        async def scenario():
            can_use_tool = P.make_can_use_tool(db_path, loop, timeout=30.0)
            task = asyncio.ensure_future(
                can_use_tool("Write", {"file_path": "x.py"}, None))
            await asyncio.sleep(0.1)
            conn = db.connect(db_path)
            pending = db.pending_permission_requests(conn, limit=1)
            conn.close()
            rid = pending[0]["id"]
            assert P.registry.resolve(rid, False) is True
            result = await task
            return result

        try:
            result = loop.run_until_complete(scenario())
        finally:
            loop.close()
        assert result["behavior"] == "deny"

    def test_double_resolve_is_noop(self, db_path):
        loop = asyncio.new_event_loop()
        future = loop.create_future()
        from friday_v6.agent.permissions import BridgePermission
        P.registry.register(BridgePermission("rid-1", "Bash: ls",
                                             future, loop))
        assert P.registry.resolve("rid-1", True) is True
        assert P.registry.resolve("rid-1", False) is False  # already gone


# ── autonomy hook (yes/no resolves the SDK future) ──────────────────


class TestAutonomyBridgeHook:
    def _make_bridge_ask(self, db_path):
        conn = db.connect(db_path)
        rid = db.create_permission_request(
            conn, "Claude Code wants to Bash: ls",
            "claude_tool:Bash", command='{"command": "ls"}',
            source="bridge")
        conn.close()
        return rid

    def test_accept_resolves_bridge_without_execute(
            self, monkeypatch, db_path):
        from friday_v6.autonomy import AutonomyAgent
        rid = self._make_bridge_ask(db_path)
        loop = asyncio.new_event_loop()

        async def scenario():
            future = loop.create_future()
            from friday_v6.agent.permissions import BridgePermission
            P.registry.register(BridgePermission(rid, "Bash: ls",
                                                 future, loop))
            outcome = AutonomyAgent(conn=db.connect(db_path)).accept(rid)
            assert outcome["status"] == "succeeded"
            allow, _reason = await future
            return allow

        try:
            allow = loop.run_until_complete(scenario())
        finally:
            loop.close()
        assert allow is True
        conn = db.connect(db_path)
        assert db.get_permission_request(conn, rid)["status"] == "approved"
        conn.close()

    def test_deny_resolves_bridge_as_no(self, monkeypatch, db_path):
        from friday_v6.autonomy import AutonomyAgent
        rid = self._make_bridge_ask(db_path)
        loop = asyncio.new_event_loop()

        async def scenario():
            future = loop.create_future()
            from friday_v6.agent.permissions import BridgePermission
            P.registry.register(BridgePermission(rid, "Bash: ls",
                                                 future, loop))
            assert AutonomyAgent(conn=db.connect(db_path)).deny(rid) is True
            allow, _reason = await future
            return allow

        try:
            allow = loop.run_until_complete(scenario())
        finally:
            loop.close()
        assert allow is False
        conn = db.connect(db_path)
        assert db.get_permission_request(conn, rid)["status"] == "denied"
        conn.close()

    def test_accept_missing_future_is_honest(self, db_path):
        from friday_v6.autonomy import AutonomyAgent
        rid = self._make_bridge_ask(db_path)
        # no future registered (e.g. bridge session ended) → honest fail
        outcome = AutonomyAgent(conn=db.connect(db_path)).accept(rid)
        assert outcome["status"] == "failed"
        conn = db.connect(db_path)
        assert db.get_permission_request(conn, rid)["status"] == "denied"
        conn.close()


# ── talk → autonomy round trip (the PWA yes/no path) ────────────────


class TestTalkYesNoRoundTrip:
    def test_yes_resolves_bridge_ask_through_talk(
            self, monkeypatch, tmp_path):
        """The PWA's 'yes, run it' resolves the bridge ask — same path
        the durable daemon asks use (no real SDK needed)."""
        dbp = str(tmp_path / "v4.db")
        conn = db.connect(dbp)
        rid = db.create_permission_request(
            conn, "Claude Code wants to Bash: ls",
            "claude_tool:Bash", command='{"command": "ls"}',
            source="bridge")
        conn.close()

        from friday_v6.nl_router import TextCommandHandler
        handler = TextCommandHandler(conn=db.connect(dbp))
        result = handler.handle("yes, run it")
        assert result.action in ("executed", "chat", "failed")
        conn = db.connect(dbp)
        row = db.get_permission_request(conn, rid)
        conn.close()
        # the row resolved one way or the other (never left pending)
        assert row["status"] in ("approved", "denied")


# ── degraded: no SDK anywhere ───────────────────────────────────────


class TestDegraded:
    def test_send_without_sdk_is_neutral(self, monkeypatch, db_path):
        monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)
        b = ClaudeBridge(db_path=db_path)
        out = b.send("CLAUDE: do something")
        assert out["ok"] is False
        assert out["response"]

    def test_status_without_sdk_is_false(self, monkeypatch, db_path):
        monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)
        b = ClaudeBridge(db_path=db_path)
        st = b.status()
        assert st["available"] is False
