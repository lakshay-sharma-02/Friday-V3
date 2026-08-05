"""Hermetic tests for the Wave 18 Claude Code hands executor.

Friday stays the brain (NLU → gate → audit); Claude Code CLI is the
hands for complex agentic goals ("figure out why the build fails and
fix it"). This suite verifies:

- the `claude` executor is registered and gated (CONFIRM default,
  destructive task phrases escalate to NEVER)
- a fake `claude` binary's JSON output is parsed back into a
  structured result (success / is_error / raw / denied tools)
- a missing `claude` CLI degrades to a structured failure, never a crash
- the resolver routes agentic goals → `claude` while concrete commands
  (git status / run the tests / read a file) stay on native executors
- the CLI surface (`friday6 execute claude "<task>"`) accepts the type
- the capability registry exposes `executor:claude`

Everything is hermetic: the fake `claude` is a temp executable, the DB
is a tmp_path, no network, no real ~/.friday.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from friday_v6 import db
from friday_v6.execution import execute
from friday_v6.execution import executors as exec_mod

# ─────────────────────────────────────────────────────────────────────
# Fake claude binary — emits the same JSON shape as `claude -p`
# --output-format json` (result / is_error / terminal_reason /
# permission_denials).
# ─────────────────────────────────────────────────────────────────────

_FAKE_CLAUDE_SRC = """\
#!/usr/bin/env python3
import json, os, sys
mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")
task = ""
args = sys.argv[1:]
if "-p" in args:
    task = args[args.index("-p") + 1]
if mode == "error":
    print(json.dumps({"is_error": True, "result": "model exploded",
                      "terminal_reason": "api_error"}))
elif mode == "denied_tools":
    print(json.dumps({"is_error": False, "result": "did the thing",
                      "terminal_reason": "completed",
                      "permission_denials": [{"tool": "WebFetch"}]}))
elif mode == "naked":
    print("not json at all — a proxy warning", file=sys.stderr)
    print("raw stdout line")
elif mode == "hang":
    import time
    time.sleep(30)
else:
    print(json.dumps({"is_error": False, "result": f"answered: {task}",
                      "terminal_reason": "completed",
                      "permission_denials": []}))
"""


@pytest.fixture
def fake_claude(tmp_path: Path, monkeypatch, request) -> str:
    """Install a fake `claude` executable and point find_tool at it."""
    mode = getattr(request, "param", "ok")
    exe = tmp_path / "claude"
    exe.write_text(_FAKE_CLAUDE_SRC.replace('"ok"', f'"{mode}"'),
                   encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(exec_mod, "find_tool", lambda name: str(exe))
    return str(exe)


@pytest.fixture
def no_claude(monkeypatch):
    """Simulate a machine without the Claude Code CLI."""
    monkeypatch.setattr(exec_mod, "find_tool", lambda name: None)


def _conn(tmp_path):
    return db.connect(tmp_path / "v4.db")


# ─────────────────────────────────────────────────────────────────────
# Registration + gate
# ─────────────────────────────────────────────────────────────────────


class TestRegistration:
    def test_claude_registered(self):
        from friday_v6.execution.executors import _EXECUTORS, ClaudeCodeExecutor
        assert _EXECUTORS["claude"] is ClaudeCodeExecutor

    def test_gate_confirm_by_default(self):
        from friday_v6.execution.gate import PermissionGate, PermissionLevel
        gate = PermissionGate()
        assert gate.level_for("claude", "figure out why the build fails") \
            == PermissionLevel.CONFIRM

    def test_gate_destructive_task_escalates(self):
        from friday_v6.execution.executors import _CLAUDE_DANGEROUS_PHRASES
        from friday_v6.execution.gate import PermissionGate, PermissionLevel
        gate = PermissionGate(dangerous=_CLAUDE_DANGEROUS_PHRASES)
        assert gate.level_for("claude", "push my changes to origin") \
            == PermissionLevel.NEVER
        assert gate.level_for("claude", "deploy to production") \
            == PermissionLevel.NEVER
        # A *diagnostic* request mentioning the word must not escalate:
        assert gate.level_for("claude",
                              "investigate the deploy failure and tell me why") \
            == PermissionLevel.CONFIRM

    def test_capability_registry_exposes_claude(self):
        from friday_v6.capability import CapabilityRegistry
        reg = CapabilityRegistry()
        caps = reg.list()
        assert any(c.id == "executor:claude" for c in caps)


# ─────────────────────────────────────────────────────────────────────
# Execution — success / error / parse / missing CLI / gate
# ─────────────────────────────────────────────────────────────────────


class TestExecuteClaude:
    @pytest.mark.parametrize("fake_claude", ["ok"], indirect=True)
    def test_success_parses_result(self, tmp_path, fake_claude):
        conn = _conn(tmp_path)
        try:
            result = execute("claude", "figure out why the build fails",
                             cwd=str(tmp_path), conn=conn, force=True)
            assert result.status == "succeeded"
            assert result.action_type == "claude"
            assert "answered: figure out why the build fails" in result.output
            assert result.action_id  # audited
        finally:
            conn.close()

    @pytest.mark.parametrize("fake_claude", ["error"], indirect=True)
    def test_is_error_fails_structurally(self, tmp_path, fake_claude):
        conn = _conn(tmp_path)
        try:
            result = execute("claude", "do the thing", cwd=str(tmp_path),
                             conn=conn, force=True)
            assert result.status == "failed"
            assert "model exploded" in result.output
        finally:
            conn.close()

    @pytest.mark.parametrize("fake_claude", ["naked"], indirect=True)
    def test_non_json_output_surfaces_raw(self, tmp_path, fake_claude):
        conn = _conn(tmp_path)
        try:
            result = execute("claude", "do the thing", cwd=str(tmp_path),
                             conn=conn, force=True)
            # Fake exits 0 with non-JSON stdout → raw text surfaces.
            assert result.status == "succeeded"
            assert "raw stdout line" in result.output
        finally:
            conn.close()

    @pytest.mark.parametrize("fake_claude", ["denied_tools"], indirect=True)
    def test_denied_tools_noted_not_fatal(self, tmp_path, fake_claude):
        conn = _conn(tmp_path)
        try:
            result = execute("claude", "do the thing", cwd=str(tmp_path),
                             conn=conn, force=True)
            assert result.status == "succeeded"
            assert "permission rules" in result.output
        finally:
            conn.close()

    def test_missing_cli_degrades_gracefully(self, tmp_path, no_claude):
        conn = _conn(tmp_path)
        try:
            result = execute("claude", "do the thing", cwd=str(tmp_path),
                             conn=conn, force=True)
            assert result.status == "failed"
            assert "claude CLI not found" in result.output
            assert result.action_id  # still audited
        finally:
            conn.close()

    def test_denied_without_force_or_confirm(self, tmp_path, fake_claude):
        conn = _conn(tmp_path)
        try:
            result = execute("claude", "figure out why the build fails",
                             cwd=str(tmp_path), conn=conn)
            assert result.status == "denied"  # CONFIRM + no confirm_fn
            assert result.action_id  # denials are audited too
        finally:
            conn.close()

    def test_destructive_task_denied_even_with_confirm(self, tmp_path,
                                                       fake_claude):
        """A bare 'yes' never escalates a NEVER task — force is required."""
        conn = _conn(tmp_path)
        try:
            result = execute("claude", "push my changes to origin",
                             cwd=str(tmp_path), conn=conn,
                             confirm_fn=lambda _d: True)
            assert result.status == "denied"
            assert result.permission_level == "never"
        finally:
            conn.close()

    def test_confirm_fn_approves(self, tmp_path, fake_claude):
        conn = _conn(tmp_path)
        try:
            result = execute("claude", "figure out why the build fails",
                             cwd=str(tmp_path), conn=conn,
                             confirm_fn=lambda _d: True)
            assert result.status == "succeeded"
        finally:
            conn.close()

    def test_undo_payload_none(self):
        from friday_v6.execution.executors import ClaudeCodeExecutor
        assert ClaudeCodeExecutor()._undo_payload("anything") == {"op": "none"}

    def test_empty_task_fails_structurally(self, tmp_path, fake_claude):
        conn = _conn(tmp_path)
        try:
            result = execute("claude", "   ", cwd=str(tmp_path), conn=conn,
                             force=True)
            assert result.status == "failed"
        finally:
            conn.close()


# ─────────────────────────────────────────────────────────────────────
# Timeout — the fake hangs; the executor must time out, never hang
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("fake_claude", ["hang"], indirect=True)
def test_hang_times_out(tmp_path, fake_claude):
    from friday_v6.execution.executors import ClaudeCodeExecutor
    from friday_v6.execution.sandbox import Sandbox
    ex = ClaudeCodeExecutor(
        sandbox=Sandbox(allowed_roots=[tmp_path], timeout_seconds=60),
        timeout_seconds=1)
    res = ex.run("do the thing", cwd=str(tmp_path))
    assert res.timed_out is True


# ─────────────────────────────────────────────────────────────────────
# NLU routing — agentic goals → claude; concrete commands stay native
# ─────────────────────────────────────────────────────────────────────


class TestResolverRouting:
    def test_agentic_goals_route_to_claude(self):
        from friday_v6.nlu import resolve
        for phrase in ("figure out why the build fails and fix it",
                       "fix the failing test", "debug the memory leak",
                       "investigate the crash"):
            action = resolve(phrase)
            ex = action.to_execution()
            assert ex is not None, phrase
            assert ex["action_type"] == "claude", phrase
            assert ex["command"] == phrase  # the task text IS the command

    def test_concrete_commands_stay_native(self):
        from friday_v6.nlu import resolve
        assert resolve("git status").to_execution()["action_type"] == "git"
        assert resolve("run the tests").to_execution()["action_type"] \
            == "testing"
        assert resolve("read README.md").to_execution()["action_type"] \
            == "file"

    def test_llm_explicit_claude_threads_through(self):
        from friday_v6.nlu import resolve

        class FakeLLM:
            def parse_utterance(self, text):
                return {
                    "intent": "execute", "action_type": "claude",
                    "command": "", "target": None, "goal": None,
                    "entities": [], "needs_clarification": False,
                    "clarification": "", "confidence": 0.95,
                }

        action = resolve("figure out why the build fails and fix it",
                         llm=FakeLLM())
        ex = action.to_execution()
        assert ex["action_type"] == "claude"
        assert ex["command"] == "figure out why the build fails and fix it"

    def test_llm_agentic_none_action_type_still_delegates(self):
        """LLM returns execute/None/"" for an agentic goal → claude wins.

        Regression guard for the resolver ordering bug: assess() flags
        EXECUTE-without-action_type as needing clarification *before*
        the agentic reassignment routes it to claude — the reassignment
        must clear that stale clarification or the router would ask
        "What would you like me to run?" instead of delegating.
        """
        from friday_v6.nlu import resolve

        class FakeLLM:
            def parse_utterance(self, text):
                return {
                    "intent": "execute", "action_type": None,
                    "command": "", "target": None, "goal": None,
                    "entities": [], "needs_clarification": False,
                    "clarification": "", "confidence": 0.5,
                }

        action = resolve("figure out why the build fails and fix it",
                         llm=FakeLLM())
        assert action.needs_clarification is False
        assert action.can_execute is True
        ex = action.to_execution()
        assert ex["action_type"] == "claude"
        assert ex["command"] == "figure out why the build fails and fix it"

    def test_llm_requested_clarification_still_respected(self):
        """An LLM that explicitly asks a question keeps its clarification."""
        from friday_v6.nlu import resolve

        class FakeLLM:
            def parse_utterance(self, text):
                return {
                    "intent": "execute", "action_type": None,
                    "command": "", "target": None, "goal": None,
                    "entities": [], "needs_clarification": True,
                    "clarification": "the auth tests or the full suite?",
                    "confidence": 0.4,
                }

        action = resolve("figure out why the build fails and fix it",
                         llm=FakeLLM())
        assert action.needs_clarification is True
        assert action.clarification == "the auth tests or the full suite?"

    def test_ask_stays_ask(self):
        from friday_v6.nlu import resolve
        assert resolve("why is the build failing").intent.value == "ask"


# ─────────────────────────────────────────────────────────────────────
# End-to-end through the NL router (the MCU path)
# ─────────────────────────────────────────────────────────────────────


class TestNlRouterClaude:
    @pytest.mark.parametrize("fake_claude", ["ok"], indirect=True)
    def test_talk_delegates_agentic_goal(self, tmp_path, fake_claude):
        from friday_v6.nl_router import TextCommandHandler
        conn = _conn(tmp_path)
        try:
            handler = TextCommandHandler(conn, cwd=str(tmp_path))
            result = handler.handle(
                "figure out why the build fails and fix it", force=True)
            assert result.action == "executed"
            assert result.action_type == "claude"
            assert "answered" in result.response  # Claude's result surfaced
            assert result.status == "succeeded"
        finally:
            conn.close()

    @pytest.mark.parametrize("fake_claude", ["error"], indirect=True)
    def test_talk_failed_goal_is_honest(self, tmp_path, fake_claude):
        from friday_v6.nl_router import TextCommandHandler
        conn = _conn(tmp_path)
        try:
            handler = TextCommandHandler(conn, cwd=str(tmp_path))
            result = handler.handle("debug the memory leak", force=True)
            assert result.action == "failed"
            assert "model exploded" in result.response
            assert "Claude Code couldn't finish" in result.response
        finally:
            conn.close()


# ─────────────────────────────────────────────────────────────────────
# CLI surface — friday6 execute claude "<task>"
# ─────────────────────────────────────────────────────────────────────


class TestCliExecute:
    def test_claude_choice_parses(self, tmp_path, fake_claude):
        from friday_v6.cli_execute import main as execute_main
        code = execute_main([
            "execute", "claude", "figure out why the build fails",
            "--cwd", str(tmp_path), "--db", str(tmp_path / "v4.db"),
            "--force", "--json",
        ])
        assert code == 0

    def test_claude_missing_cli_exits_failed(self, tmp_path, no_claude):
        from friday_v6.cli_execute import main as execute_main
        code = execute_main([
            "execute", "claude", "do the thing",
            "--cwd", str(tmp_path), "--db", str(tmp_path / "v4.db"),
            "--force", "--json",
        ])
        assert code == 1

    def test_claude_denied_without_force_exits_two(self, tmp_path,
                                                   fake_claude):
        """A NEVER task without an explicit override exits 2 (denied)."""
        from friday_v6.cli_execute import main as execute_main
        code = execute_main([
            "execute", "claude", "push my changes to origin",
            "--cwd", str(tmp_path), "--db", str(tmp_path / "v4.db"),
            "--json",
        ])
        assert code == 2

    def test_claude_force_overrides_never(self, tmp_path, fake_claude):
        """--force is the explicit operator override that may bypass NEVER."""
        from friday_v6.cli_execute import main as execute_main
        code = execute_main([
            "execute", "claude", "push my changes to origin",
            "--cwd", str(tmp_path), "--db", str(tmp_path / "v4.db"),
            "--force", "--json",
        ])
        assert code == 0
