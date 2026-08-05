"""Hermetic tests for Wave 5 — the abort kill switch (V3's discipline).

``friday6 abort`` arms a durable kill switch that stops the Claude
bridge mid-session: while armed, the bridge's tool hook
(``make_can_use_tool``) denies EVERY tool call immediately — no ask is
recorded, no SDK future is created — and new ``CLAUDE:`` prompts are
refused until cleared.

Covers:
- abort.py KillSwitch: arm/is_armed/clear/status round-trip, idempotent
  re-arm, durable file format, never-crash on a missing/corrupt flag
- The ambient KILL_SWITCH event (armed + cleared) on the durable bus
- The bridge tool hook denies when armed (no ask recorded) — the W5
  exit criterion, mid-session stop
- The bridge refuses new prompts while armed; status() reports aborted
- ``abort_now`` arms + best-effort ends a (mocked) bridge session
- cli_abort: `friday6 abort` / `--clear` / `--status`, JSON purity

Safety laws verified:
- The flag file is the source of truth (read by every process).
- Armed → fail-closed: deny everything until the operator clears it.
- All hermetic: tmp flag files + tmp DBs — never the real ~/.friday.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types

import pytest

from friday_v6 import db
from friday_v6.abort import KillSwitch, abort_now, kill_switch


def _flag(tmp_path):
    return tmp_path / "v6_abort.json"


def _db(tmp_path):
    return str(tmp_path / "v4.db")


# ==========================================================================
# abort.py — KillSwitch core
# ==========================================================================


class TestKillSwitch:
    def test_arm_is_armed_clear_roundtrip(self, tmp_path):
        ks = KillSwitch(path=_flag(tmp_path), db_path=_db(tmp_path))
        assert ks.is_armed() is False
        assert ks.arm("stop the deploy") is True       # newly armed
        assert ks.is_armed() is True
        st = ks.status()
        assert st["armed"] is True
        assert st["reason"] == "stop the deploy"
        assert st["at"]
        assert ks.clear() is True                       # was armed
        assert ks.is_armed() is False
        assert ks.clear() is False                      # nothing to clear

    def test_ream_arm_is_idempotent(self, tmp_path):
        ks = KillSwitch(path=_flag(tmp_path), db_path=_db(tmp_path))
        assert ks.arm("first") is True
        assert ks.arm("second") is False                # already armed
        assert ks.status()["reason"] == "second"        # reason updated
        assert ks.status()["armed"] is True

    def test_flag_file_is_durable_and_readable(self, tmp_path):
        """A second KillSwitch over the SAME file sees the armed state —
        the flag is the cross-process source of truth."""
        p = _flag(tmp_path)
        KillSwitch(path=p, db_path=_db(tmp_path)).arm("why")
        assert KillSwitch(path=p, db_path=_db(tmp_path)).is_armed() is True
        data = json.loads(p.read_text(encoding="utf-8"))
        assert data["armed"] is True and data["reason"] == "why"

    def test_missing_flag_is_not_armed(self, tmp_path):
        ks = KillSwitch(path=_flag(tmp_path), db_path=_db(tmp_path))
        assert ks.is_armed() is False
        assert ks.status() == {"armed": False, "reason": "", "at": ""}

    def test_arm_never_claims_success_on_write_failure(self, tmp_path):
        """A kill switch that can't persist must report failure — never
        a lying '⛔ ABORT — armed' while the bridge stays live."""
        # A path under a FILE (not a dir) makes mkdir/write fail.
        blocker = tmp_path / "blocker"
        blocker.write_text("x", encoding="utf-8")
        ks = KillSwitch(path=blocker / "v6_abort.json",
                        db_path=_db(tmp_path))
        assert ks.arm("cannot persist") is False
        assert ks.is_armed() is False          # never claims armed
        assert ks.clear() is False             # nothing to clear either

    def test_corrupt_flag_never_crashes(self, tmp_path):
        p = _flag(tmp_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{not json!!", encoding="utf-8")
        ks = KillSwitch(path=p, db_path=_db(tmp_path))
        assert ks.is_armed() is False   # never crash, honest value

    def test_arm_publishes_kill_switch_event(self, tmp_path):
        """Arming publishes a CRITICAL ambient event the Live feed sees
        (the V3 KILL_SWITCH_ACTIVATED pattern, durable on the bus)."""
        dbp = _db(tmp_path)
        ks = KillSwitch(path=_flag(tmp_path), db_path=dbp)
        ks.arm("stop everything")
        conn = db.connect(dbp)
        try:
            from friday_v6.ambient import AmbientBus, Priority
            evs = AmbientBus(conn).replay(topic="system")
            assert any("Kill switch" in e.payload
                       and "stop everything" in e.payload
                       for e in evs)
            assert all(e.priority == Priority.CRITICAL for e in evs)
        finally:
            conn.close()

    def test_clear_publishes_deactivated_event(self, tmp_path):
        dbp = _db(tmp_path)
        ks = KillSwitch(path=_flag(tmp_path), db_path=dbp)
        ks.arm("x")
        ks.clear()
        conn = db.connect(dbp)
        try:
            from friday_v6.ambient import AmbientBus
            evs = AmbientBus(conn).replay(topic="system")
            texts = [e.payload for e in evs]
            assert any("activated" in t for t in texts)
            assert any("deactivated" in t for t in texts)
        finally:
            conn.close()

    def test_module_kill_switch_returns_shared_singleton(self, tmp_path):
        a = kill_switch()
        b = kill_switch()
        assert a is b
        # A tmp-path request builds a FRESH switch (never the singleton).
        c = kill_switch(path=str(_flag(tmp_path)))
        assert c is not a


# ==========================================================================
# The W5 exit criterion — the bridge tool hook denies when armed
# ==========================================================================


class TestBridgeHook:
    def test_can_use_tool_denies_immediately_when_armed(self, tmp_path):
        """Armed → EVERY tool call is denied with NO ask recorded and NO
        SDK future — the session stops mid-turn (fail-closed)."""
        from friday_v6.agent import permissions as P
        dbp = _db(tmp_path)
        conn = db.connect(dbp)
        db.create_permission_request(
            conn, "pre-existing unrelated ask", "shell",
            command="echo hi", source="other")
        conn.close()
        _arm(tmp_path)

        loop = asyncio.new_event_loop()

        async def scenario():
            can_use_tool = P.make_can_use_tool(dbp, loop, timeout=30.0)
            return await can_use_tool("Bash", {"command": "rm -rf /"}, None)

        try:
            result = loop.run_until_complete(scenario())
        finally:
            loop.close()

        assert result["behavior"] == "deny"
        assert "aborted" in result.get("message", "")
        # No NEW ask was recorded for this tool call (the ask count is
        # unchanged), and no SDK future was created for it.
        conn = db.connect(dbp)
        pending = db.pending_permission_requests(conn, limit=10)
        conn.close()
        assert all(r["source"] != "bridge" for r in pending)

    def test_can_use_tool_normal_when_not_armed(self, tmp_path):
        """Not armed → the normal bridge ask path still works."""
        from friday_v6.agent import permissions as P
        dbp = _db(tmp_path)
        loop = asyncio.new_event_loop()

        async def scenario():
            can_use_tool = P.make_can_use_tool(dbp, loop, timeout=1.0)
            task = asyncio.ensure_future(
                can_use_tool("Bash", {"command": "ls"}, None))
            await asyncio.sleep(0.1)
            conn = db.connect(dbp)
            pending = db.pending_permission_requests(conn, limit=10)
            conn.close()
            assert any(r["source"] == "bridge" for r in pending)
            return await task

        try:
            result = loop.run_until_complete(scenario())
        finally:
            loop.close()
        assert result["behavior"] == "deny"  # unresolved ask → fail closed


def _arm(tmp_path):
    """Arm the kill switch so the (module-level) hook sees it.

    The bridge hook and CLI call ``kill_switch()`` (no args) which
    returns the module singleton — point that singleton at the tmp
    flag file so hermetic tests never touch the real ~/.friday state.
    """
    switch = KillSwitch(path=_flag(tmp_path), db_path=_db(tmp_path))
    switch.arm("test abort")
    import friday_v6.abort as A
    A._switch = switch


# ==========================================================================
# ClaudeBridge integration — refuses prompts while armed
# ==========================================================================


class _FakeClient:
    """Minimal stand-in for ClaudeSDKClient (records the session)."""

    def __init__(self, options=None):
        self.options = options
        self.prompts: list[tuple[str, str]] = []
        self.connected = False
        self.disconnected = False
        self._messages = []

    async def connect(self, prompt=None):
        self.connected = True

    async def query(self, prompt=None, session_id="default"):
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
        for m in self._messages:
            yield m
        while True:
            await asyncio.sleep(3600)

    async def disconnect(self):
        self.disconnected = True


class _FakeOptions:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def _install_fake_sdk(monkeypatch, client=None):
    """Inject a fake claude_agent_sdk into sys.modules (lazy import)."""
    client = client or _FakeClient()
    captured = {"client": client}
    fake_types = types.ModuleType("claude_agent_sdk.types")
    fake_types.ClaudeAgentOptions = _FakeOptions
    fake_client_mod = types.ModuleType("claude_agent_sdk.client")
    fake_client_mod.ClaudeSDKClient = lambda options=None: client
    fake = types.ModuleType("claude_agent_sdk")
    fake.types = fake_types
    fake.client = fake_client_mod
    fake.ClaudeAgentOptions = _FakeOptions
    fake.ClaudeSDKClient = fake_client_mod.ClaudeSDKClient
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake)
    monkeypatch.setitem(sys.modules, "claude_agent_sdk.types", fake_types)
    monkeypatch.setitem(sys.modules, "claude_agent_sdk.client", fake_client_mod)
    return captured


class TestBridgeAbort:
    def _bridge(self, tmp_path, monkeypatch):
        import friday_v6.agent.bridge as B
        monkeypatch.setattr(B, "_bridge", None)
        _install_fake_sdk(monkeypatch)
        return B.ClaudeBridge(db_path=str(tmp_path / "v4.db"))

    def test_send_refuses_new_prompts_while_armed(self, tmp_path, monkeypatch):
        _arm(tmp_path)
        b = self._bridge(tmp_path, monkeypatch)
        out = b.send("CLAUDE: keep going")
        assert out["ok"] is False
        assert "abort" in out["response"].lower()
        assert b.status()["aborted"] is True

    def test_status_reports_aborted(self, tmp_path, monkeypatch):
        b = self._bridge(tmp_path, monkeypatch)
        assert b.status()["aborted"] is False
        _arm(tmp_path)
        assert b.status()["aborted"] is True

    def test_abort_now_arms_and_stops_bridge_mid_session(
            self, tmp_path, monkeypatch):
        """The W5 exit criterion: `friday6 abort` stops a (mocked) bridge
        session mid-turn — the SHARED bridge (the one the PWA uses) ends,
        and the flag is armed."""
        import friday_v6.agent.bridge as B
        monkeypatch.setattr(B, "_bridge", None)
        _install_fake_sdk(monkeypatch)
        # abort_now ends get_bridge() — the module singleton the surfaces
        # use — so the test must drive THAT same bridge.
        b = B.get_bridge(db_path=str(tmp_path / "v4.db"))
        assert b.send("CLAUDE: hello")["ok"] is True
        _wait_active(b)
        assert b.status()["active"] is True
        assert abort_now("stop the run", path=_flag(tmp_path),
                         db_path=_db(tmp_path)) is True
        # The shared bridge session was ended (fresh context next time).
        assert b.status()["active"] is False
        # And the flag is armed durably.
        assert KillSwitch(path=_flag(tmp_path)).is_armed() is True

    def test_abort_now_without_sdk_is_still_armed(self, tmp_path, monkeypatch):
        """Never-crash: no SDK → the arm still happens, the bridge end
        is skipped silently."""
        import friday_v6.agent.bridge as B
        monkeypatch.setattr(B, "_bridge", None)
        monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)
        assert abort_now("no sdk here", path=_flag(tmp_path),
                         db_path=_db(tmp_path)) is True
        assert KillSwitch(path=_flag(tmp_path)).is_armed() is True


def _wait_active(bridge, timeout=5.0):
    import time
    end = time.monotonic() + timeout
    while time.monotonic() < end and not bridge.status()["active"]:
        time.sleep(0.02)


# ==========================================================================
# cli_abort — friday6 abort / --clear / --status
# ==========================================================================


@pytest.fixture(autouse=True)
def _reset_module_switch(tmp_path, monkeypatch):
    """Each test starts with the module singleton pointed at a TMP flag
    file — so even "not armed" tests that call ``kill_switch()`` (the
    bridge hook, status) read a tmp path, never the real ~/.friday state,
    and the tmp-path ``_arm`` hook from one test never leaks into the
    next."""
    import friday_v6.abort as A
    monkeypatch.setattr(
        A, "_switch",
        KillSwitch(path=tmp_path / "v6_abort.json",
                   db_path=str(tmp_path / "v4.db")))


class TestAbortCLI:
    def _args(self, tmp_path, **kw):
        from types import SimpleNamespace
        base = {"reason": [], "clear": False, "status": False,
                "flag": str(_flag(tmp_path)), "db": _db(tmp_path),
                "json": False}
        base.update(kw)
        return SimpleNamespace(**base)

    def test_abort_arms_and_clear_disarms(self, tmp_path, capsys):
        from friday_v6.cli_abort import cmd_abort
        assert cmd_abort(self._args(tmp_path, reason=["stop", "the", "deploy"])) == 0
        assert "ABORT" in capsys.readouterr().out
        assert KillSwitch(path=_flag(tmp_path)).is_armed() is True
        assert cmd_abort(self._args(tmp_path, clear=True)) == 0
        assert "cleared" in capsys.readouterr().out
        assert KillSwitch(path=_flag(tmp_path)).is_armed() is False

    def test_abort_status_reports_state(self, tmp_path, capsys):
        from friday_v6.cli_abort import cmd_abort
        assert cmd_abort(self._args(tmp_path, status=True)) == 0
        assert "not armed" in capsys.readouterr().out
        KillSwitch(path=_flag(tmp_path), db_path=_db(tmp_path)).arm("why")
        assert cmd_abort(self._args(tmp_path, status=True)) == 0
        assert "ARMED" in capsys.readouterr().out

    def test_abort_json_is_pure(self, tmp_path, capsys):
        from friday_v6.cli_abort import cmd_abort
        assert cmd_abort(self._args(tmp_path, reason=["x"], json=True)) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["armed"] is True and data["reason"] == "x"
        assert cmd_abort(self._args(tmp_path, status=True, json=True)) == 0
        assert json.loads(capsys.readouterr().out)["armed"] is True
        assert cmd_abort(self._args(tmp_path, clear=True, json=True)) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["cleared"] is True and data["was_armed"] is True

    def test_abort_command_registered_in_cli(self, tmp_path):
        """`friday6 abort` parses through the ONE entry point."""
        from friday_v6.cli_talk import main as cli_main
        rc = cli_main(["abort", "--status", "--flag",
                       str(_flag(tmp_path)), "--db", _db(tmp_path),
                       "--json"])
        assert rc == 0
