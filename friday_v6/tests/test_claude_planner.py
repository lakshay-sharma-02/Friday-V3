"""Hermetic tests for Claude Code as the mission planner (Wave 18 close-out).

``ClaudePlanner`` (``missions/claude_planner.py``) connects the
``Planner.enhancer`` hook to the local Claude Code CLI: agentic goals
("ship the auth refactor by Friday") decompose through ``claude -p``
(read-only tools, gated, sandboxed, audited) into :class:`StepPlan`
objects that map onto Friday's executor vocabulary.

This suite verifies:

- a fake ``claude`` binary's plan JSON parses into StepPlans
  (executable + manual steps), fenced or bare
- missing CLI / is_error / non-JSON / empty plan / timeout → ``None``
  (the deterministic planner is always the floor)
- unknown ``action_type`` values become manual steps (never invented)
- destructive plan goals are refused by the gate (NEVER) — even with a
  working ``claude`` available
- the delegation is audited (``action_type = "claude_plan"``) with its
  gate level and outcome
- the CLI arg shape (``-p`` / ``--model`` / ``--allowedTools`` read-only)
- the NL router end-to-end: ``friday6 talk "ship the auth refactor"``
  creates a mission from Claude's plan — and still works without claude

Everything is hermetic: the fake ``claude`` is a temp executable, the DB
is a tmp_path, no network, no real ~/.friday.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from friday_v6 import db
from friday_v6.missions import Planner

# ─────────────────────────────────────────────────────────────────────
# Fake claude binary — emits the same JSON shape as `claude -p`
# --output-format json`; the `result` field carries the assistant's
# answer (the plan JSON object) like the real CLI.
# ─────────────────────────────────────────────────────────────────────

_FAKE_CLAUDE_SRC = """\
#!/usr/bin/env python3
import json, os, sys
mode = os.environ.get("FAKE_CLAUDE_MODE", "plan_ok")
args = sys.argv[1:]
if mode == "plan_manual":
    plan = [
        {"title": "Do something exotic", "action_type": "banana",
         "command": "peel"},
        {"title": "Manual review", "action_type": None, "command": ""},
    ]
    print(json.dumps({"is_error": False, "result": json.dumps({"plan": plan}),
                      "terminal_reason": "completed",
                      "permission_denials": []}))
elif mode == "plan_error":
    print(json.dumps({"is_error": True, "result": "model exploded",
                      "terminal_reason": "api_error"}))
elif mode == "plan_fenced":
    plan = [{"title": "Lint the code", "action_type": "shell",
             "command": "lint"}]
    fenced = "```json\\n" + json.dumps({"plan": plan}) + "\\n```"
    print(json.dumps({"is_error": False, "result": fenced,
                      "terminal_reason": "completed",
                      "permission_denials": []}))
elif mode == "plan_empty":
    print(json.dumps({"is_error": False, "result": json.dumps({"plan": []}),
                      "terminal_reason": "completed",
                      "permission_denials": []}))
elif mode == "plan_naked":
    print("not json at all", file=sys.stderr)
    print("raw noise")
elif mode == "plan_hang":
    import time
    time.sleep(30)
elif mode == "plan_args":
    with open(os.environ.get("FAKE_ARGS_FILE", "/dev/null"), "w") as fh:
        fh.write(json.dumps(args))
    plan = [{"title": "Inspect the repo", "action_type": "shell",
             "command": "echo inspected"}]
    print(json.dumps({"is_error": False, "result": json.dumps({"plan": plan}),
                      "terminal_reason": "completed",
                      "permission_denials": []}))
else:
    # default plan_ok
    plan = [
        {"title": "Run the test suite", "action_type": "testing",
         "command": "tests/"},
        {"title": "Fix the failing tests", "action_type": "claude",
         "command": "fix the failing tests"},
        {"title": "Ask the operator to review", "action_type": None,
         "command": ""},
    ]
    print(json.dumps({"is_error": False, "result": json.dumps({"plan": plan}),
                      "terminal_reason": "completed",
                      "permission_denials": []}))
"""


@pytest.fixture
def fake_claude(tmp_path: Path, monkeypatch, request) -> str:
    """Install a fake `claude` executable and point find_tool at it."""
    from friday_v6.missions import claude_planner as cp_mod

    mode = getattr(request, "param", "plan_ok")
    exe = tmp_path / "claude"
    exe.write_text(_FAKE_CLAUDE_SRC.replace('"plan_ok"', f'"{mode}"'),
                   encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(cp_mod, "find_tool", lambda name: str(exe))
    return str(exe)


@pytest.fixture
def no_claude(monkeypatch):
    """Simulate a machine without the Claude Code CLI."""
    from friday_v6.missions import claude_planner as cp_mod

    monkeypatch.setattr(cp_mod, "find_tool", lambda name: None)


def _conn(tmp_path):
    return db.connect(tmp_path / "v4.db")


def _cp(tmp_path, conn=None, **kw):
    """A ClaudePlanner pinned to the fake binary (never env-dependent)."""
    from friday_v6.missions import ClaudePlanner

    kw.setdefault("timeout_seconds", 2)
    return ClaudePlanner(cwd=str(tmp_path), conn=conn,
                         model="test-model", **kw)


# ─────────────────────────────────────────────────────────────────────
# Parsing — the plan JSON → StepPlans
# ─────────────────────────────────────────────────────────────────────


class TestParse:
    @pytest.mark.parametrize("fake_claude", ["plan_ok"], indirect=True)
    def test_plan_parses_into_steps(self, tmp_path, fake_claude):
        steps = _cp(tmp_path)("ship the auth refactor")
        assert steps is not None
        assert len(steps) == 3
        assert steps[0].action_type == "testing"
        assert steps[0].command == "tests/"
        assert steps[1].action_type == "claude"          # delegates later
        assert steps[2].action_type is None              # manual step
        assert steps[0].title == "Run the test suite"

    @pytest.mark.parametrize("fake_claude", ["plan_ok"], indirect=True)
    def test_planner_uses_claude_enhancer(self, tmp_path, fake_claude):
        """Planner(enhancer=ClaudePlanner(...)) returns Claude's steps
        with the mission cwd attached (the _with_cwd contract)."""
        plan = Planner(enhancer=_cp(tmp_path)).plan(
            "ship the auth refactor", cwd=str(tmp_path))
        assert len(plan) == 3
        assert plan[0].action_type == "testing"
        assert plan[0].cwd == str(tmp_path)

    @pytest.mark.parametrize("fake_claude", ["plan_fenced"], indirect=True)
    def test_fenced_json_parsed(self, tmp_path, fake_claude):
        steps = _cp(tmp_path)("ship the auth refactor")
        assert steps is not None
        assert steps[0].action_type == "shell"
        assert steps[0].command == "lint"

    @pytest.mark.parametrize("fake_claude", ["plan_manual"], indirect=True)
    def test_unknown_action_type_becomes_manual(self, tmp_path,
                                                fake_claude):
        """A plan step Friday has no executor for is a manual step —
        never invented, never skipped."""
        steps = _cp(tmp_path)("anything")
        assert steps is not None
        assert steps[0].action_type is None      # "banana" → manual
        assert steps[1].action_type is None      # null → manual

    @pytest.mark.parametrize("fake_claude", ["plan_args"], indirect=True)
    def test_cli_arg_shape(self, tmp_path, fake_claude, monkeypatch):
        """-p print mode, --model, and the read-only --allowedTools
        allowlist reach the CLI (verified via the fake's argv dump)."""
        args_file = tmp_path / "args.json"
        monkeypatch.setenv("FAKE_ARGS_FILE", str(args_file))
        steps = _cp(tmp_path)("anything")
        assert steps is not None
        argv = json.loads(args_file.read_text(encoding="utf-8"))
        assert argv[0] == "-p"
        assert "--model" in argv
        assert argv[argv.index("--model") + 1] == "test-model"
        assert "--allowedTools" in argv
        assert argv[argv.index("--allowedTools") + 1] == "Read Glob Grep"
        # Planning is read-only: no Bash / Edit / Write tools ever.
        assert all(t not in argv for t in ("Bash", "Edit", "Write"))


# ─────────────────────────────────────────────────────────────────────
# Degradation — every failure keeps the deterministic floor
# ─────────────────────────────────────────────────────────────────────


class TestDegradation:
    def test_missing_cli_returns_none(self, tmp_path, no_claude):
        from friday_v6.missions import ClaudePlanner

        cp = ClaudePlanner(cwd=str(tmp_path), model="test-model",
                           timeout_seconds=2)
        assert cp("ship the auth refactor") is None
        # The planner still works — deterministic manual fallback.
        plan = Planner(enhancer=cp).plan("improve the parser architecture")
        assert plan and plan[0].action_type is None

    @pytest.mark.parametrize("fake_claude", ["plan_error"], indirect=True)
    def test_is_error_returns_none(self, tmp_path, fake_claude):
        assert _cp(tmp_path)("ship the auth refactor") is None

    @pytest.mark.parametrize("fake_claude", ["plan_naked"], indirect=True)
    def test_non_json_returns_none(self, tmp_path, fake_claude):
        assert _cp(tmp_path)("ship the auth refactor") is None

    @pytest.mark.parametrize("fake_claude", ["plan_empty"], indirect=True)
    def test_empty_plan_returns_none(self, tmp_path, fake_claude):
        assert _cp(tmp_path)("ship the auth refactor") is None

    @pytest.mark.parametrize("fake_claude", ["plan_hang"], indirect=True)
    def test_hang_times_out_returns_none(self, tmp_path, fake_claude):
        assert _cp(tmp_path, timeout_seconds=1)("ship the auth refactor") \
            is None

    def test_empty_goal_returns_none(self, tmp_path):
        assert _cp(tmp_path)("   ") is None

    def test_never_raises_on_garbage_goal(self, tmp_path, no_claude):
        assert _cp(tmp_path)("\x00\x01bad") is None


# ─────────────────────────────────────────────────────────────────────
# Gate — destructive plan goals are refused even with claude present
# ─────────────────────────────────────────────────────────────────────


class TestGate:
    @pytest.mark.parametrize("fake_claude", ["plan_ok"], indirect=True)
    def test_destructive_goal_refused(self, tmp_path, fake_claude):
        """'deploy to production by Friday' never reaches claude — the
        gate refuses it and the deterministic planner makes it manual."""
        from friday_v6.missions import ClaudePlanner

        cp = ClaudePlanner(cwd=str(tmp_path), model="test-model",
                           timeout_seconds=2)
        assert cp("deploy to production by Friday") is None
        plan = Planner(enhancer=cp).plan("deploy to production by Friday")
        assert plan and plan[0].action_type is None  # nothing invented

    @pytest.mark.parametrize("fake_claude", ["plan_ok"], indirect=True)
    def test_diagnostic_goal_not_refused(self, tmp_path, fake_claude):
        """A diagnostic plan goal mentioning the word stays plan-able."""
        steps = _cp(tmp_path)("figure out why the deploy fails")
        assert steps is not None


# ─────────────────────────────────────────────────────────────────────
# Audit — the delegation is a first-class action
# ─────────────────────────────────────────────────────────────────────


class TestAudit:
    @pytest.mark.parametrize("fake_claude", ["plan_ok"], indirect=True)
    def test_plan_recorded_succeeded(self, tmp_path, fake_claude):
        conn = _conn(tmp_path)
        try:
            steps = _cp(tmp_path, conn=conn)("ship the auth refactor")
            assert steps is not None
            rows = db.recent_actions(conn, action_type="claude_plan")
            assert len(rows) == 1
            assert rows[0]["status"] == "succeeded"
            assert "planned 3 step(s)" in rows[0]["output"]
        finally:
            conn.close()

    @pytest.mark.parametrize("fake_claude", ["plan_ok"], indirect=True)
    def test_refused_goal_audited_denied(self, tmp_path, fake_claude):
        conn = _conn(tmp_path)
        try:
            from friday_v6.missions import ClaudePlanner

            cp = ClaudePlanner(cwd=str(tmp_path), conn=conn,
                               model="test-model", timeout_seconds=2)
            assert cp("deploy to production by Friday") is None
            rows = db.recent_actions(conn, action_type="claude_plan")
            assert len(rows) == 1
            assert rows[0]["status"] == "denied"
            assert rows[0]["permission_level"] == "never"
        finally:
            conn.close()

    @pytest.mark.parametrize("fake_claude", ["plan_naked"], indirect=True)
    def test_failed_plan_audited(self, tmp_path, fake_claude):
        conn = _conn(tmp_path)
        try:
            assert _cp(tmp_path, conn=conn)("ship the auth refactor") is None
            rows = db.recent_actions(conn, action_type="claude_plan")
            assert len(rows) == 1
            assert rows[0]["status"] == "failed"
        finally:
            conn.close()


# ─────────────────────────────────────────────────────────────────────
# NL router end-to-end — the MCU path (talk / voice / web share it)
# ─────────────────────────────────────────────────────────────────────


class TestNlRouterMission:
    @pytest.mark.parametrize("fake_claude", ["plan_ok"], indirect=True)
    def test_talk_plans_mission_through_claude(self, tmp_path, fake_claude,
                                               monkeypatch):
        """With FRIDAY_V4_CLAUDE_PLANNER=1 the NL mission path delegates
        goal decomposition to Claude Code (the shared talk/voice/web
        surface)."""
        monkeypatch.setenv("FRIDAY_V4_CLAUDE_PLANNER", "1")
        from friday_v6.nl_router import TextCommandHandler

        conn = _conn(tmp_path)
        try:
            handler = TextCommandHandler(conn, cwd=str(tmp_path))
            result = handler.handle("ship the auth refactor by Friday")
            assert result.action == "mission_created"
            assert result.mission_id

            from friday_v6.missions import MissionEngine
            mission = MissionEngine(conn).get(result.mission_id)
            assert mission is not None
            assert len(mission.steps) == 3          # Claude's plan
            assert mission.steps[0].action_type == "testing"
            assert mission.steps[1].action_type == "claude"
            assert mission.steps[2].action_type is None
            assert "step(s)" in result.response
        finally:
            conn.close()

    def test_talk_plans_mission_without_claude(self, tmp_path, no_claude,
                                               monkeypatch):
        """Opt-in set but no claude CLI → deterministic fallback;
        mission still created, never a crash."""
        monkeypatch.setenv("FRIDAY_V4_CLAUDE_PLANNER", "1")
        from friday_v6.nl_router import TextCommandHandler

        conn = _conn(tmp_path)
        try:
            handler = TextCommandHandler(conn, cwd=str(tmp_path))
            result = handler.handle("ship the auth refactor by Friday")
            assert result.action == "mission_created"
            assert result.mission_id

            from friday_v6.missions import MissionEngine
            mission = MissionEngine(conn).get(result.mission_id)
            assert mission is not None and mission.steps
        finally:
            conn.close()

    def test_talk_mission_hermetic_without_optin(self, tmp_path,
                                                 monkeypatch):
        """Without FRIDAY_V4_CLAUDE_PLANNER, mission planning is pure
        deterministic — claude is never consulted (hermetic by default,
        even when a claude CLI exists on the machine)."""
        from friday_v6.missions import claude_planner as cp_mod
        from friday_v6.nl_router import TextCommandHandler

        called: list[str] = []
        monkeypatch.setattr(
            cp_mod, "find_tool",
            lambda name: called.append(name) or "/fake/claude")
        conn = _conn(tmp_path)
        try:
            handler = TextCommandHandler(conn, cwd=str(tmp_path))
            result = handler.handle("ship the auth refactor by Friday")
            assert result.action == "mission_created"
            assert called == []  # claude was never consulted
        finally:
            conn.close()

    @pytest.mark.parametrize("fake_claude", ["plan_error"], indirect=True)
    def test_talk_mission_survives_claude_failure(self, tmp_path,
                                                  fake_claude, monkeypatch):
        """Claude errors mid-plan → deterministic fallback, never a
        crash, mission still created."""
        monkeypatch.setenv("FRIDAY_V4_CLAUDE_PLANNER", "1")
        from friday_v6.nl_router import TextCommandHandler

        conn = _conn(tmp_path)
        try:
            handler = TextCommandHandler(conn, cwd=str(tmp_path))
            result = handler.handle("ship the auth refactor by Friday")
            assert result.action == "mission_created"
        finally:
            conn.close()


# ─────────────────────────────────────────────────────────────────────
# Replan — 'replan this mission' re-decomposes through Claude (opt-in)
# ─────────────────────────────────────────────────────────────────────


class TestReplan:
    def test_make_planner_env_off_deterministic(self, tmp_path):
        """make_planner is the single construction point: env off →
        deterministic planner (manual fallback for unknown goals)."""
        from friday_v6.missions import make_planner

        plan = make_planner(cwd=str(tmp_path)).plan(
            "improve the parser architecture")
        assert plan and plan[0].action_type is None  # manual fallback

    @pytest.mark.parametrize("fake_claude", ["plan_ok"], indirect=True)
    def test_make_planner_env_on_uses_claude(self, tmp_path, fake_claude,
                                             monkeypatch):
        from friday_v6.missions import make_planner

        monkeypatch.setenv("FRIDAY_V4_CLAUDE_PLANNER", "1")
        plan = make_planner(cwd=str(tmp_path)).plan("ship the auth refactor")
        assert len(plan) == 3
        assert plan[0].action_type == "testing"  # Claude's plan

    @pytest.mark.parametrize("fake_claude", ["plan_ok"], indirect=True)
    def test_engine_replan_uses_claude(self, tmp_path, fake_claude,
                                       monkeypatch):
        """MissionEngine's default planner honors the opt-in, so
        ``replan`` (and ``create``) decompose through Claude Code."""
        from friday_v6.missions import MissionEngine, MissionStatus

        conn = _conn(tmp_path)
        try:
            # Deterministic creation first (env off).
            engine = MissionEngine(conn, cwd=str(tmp_path))
            mission = engine.create("ship the auth refactor by Friday")
            assert mission is not None
            assert len(mission.steps) == 1  # manual fallback, no claude

            # Env on → the SAME engine type replans through claude.
            monkeypatch.setenv("FRIDAY_V4_CLAUDE_PLANNER", "1")
            engine2 = MissionEngine(conn, cwd=str(tmp_path))
            report_res = engine2.replan(
                mission.id, "ship the auth refactor by Friday",
                reason="operator asked", cwd=str(tmp_path))
            assert report_res.changed is True
            reloaded = engine2.get(mission.id)
            assert len(reloaded.steps) == 3      # Claude's plan
            assert reloaded.steps[0].action_type == "testing"
            assert reloaded.status == MissionStatus.ACTIVE
        finally:
            conn.close()

    def test_replan_hermetic_without_optin(self, tmp_path, monkeypatch):
        """Env off → replan is pure deterministic; claude never
        consulted (hermetic by default even with claude installed)."""
        from friday_v6.missions import claude_planner as cp_mod
        from friday_v6.missions import MissionEngine
        from friday_v6.nl_router import TextCommandHandler

        called: list[str] = []
        monkeypatch.setattr(
            cp_mod, "find_tool",
            lambda name: called.append(name) or "/fake/claude")
        conn = _conn(tmp_path)
        try:
            handler = TextCommandHandler(conn, cwd=str(tmp_path))
            assert handler.handle(
                "ship the auth refactor by Friday").action \
                == "mission_created"
            result = handler.handle("replan this mission")
            assert result.action == "mission_replanned"
            assert called == []  # claude never consulted
            mission = MissionEngine(conn).get(result.mission_id)
            assert mission is not None and mission.steps
        finally:
            conn.close()

    @pytest.mark.parametrize("fake_claude", ["plan_ok"], indirect=True)
    def test_nl_replan_this_mission_through_claude(self, tmp_path,
                                                   fake_claude, monkeypatch):
        """'replan this mission' re-decomposes the latest mission's
        goal through Claude Code and reports 'plan changed because…'."""
        from friday_v6.missions import MissionEngine
        from friday_v6.nl_router import TextCommandHandler

        conn = _conn(tmp_path)
        try:
            handler = TextCommandHandler(conn, cwd=str(tmp_path))
            created = handler.handle("ship the auth refactor by Friday")
            mid = created.mission_id
            assert mid

            monkeypatch.setenv("FRIDAY_V4_CLAUDE_PLANNER", "1")
            result = handler.handle("replan this mission")
            assert result.action == "mission_replanned"
            assert result.mission_id == mid
            assert "plan changed because" in result.response
            assert "step(s)" in result.response

            reloaded = MissionEngine(conn).get(mid)
            assert len(reloaded.steps) == 3      # Claude's plan
            assert reloaded.steps[0].action_type == "testing"
        finally:
            conn.close()

    @pytest.mark.parametrize("fake_claude", ["plan_error"], indirect=True)
    def test_nl_replan_survives_claude_failure(self, tmp_path, fake_claude,
                                               monkeypatch):
        """Claude errors mid-replan → deterministic fallback; the
        mission still replans, never a crash."""
        from friday_v6.nl_router import TextCommandHandler

        conn = _conn(tmp_path)
        try:
            handler = TextCommandHandler(conn, cwd=str(tmp_path))
            mid = handler.handle("ship the auth refactor by Friday").mission_id
            monkeypatch.setenv("FRIDAY_V4_CLAUDE_PLANNER", "1")
            result = handler.handle("replan this mission")
            assert result.action == "mission_replanned"
            assert result.mission_id == mid
        finally:
            conn.close()

    def test_nl_replan_no_mission(self, tmp_path, monkeypatch):
        """No missions yet → honest answer, never a crash."""
        from friday_v6.nl_router import TextCommandHandler

        conn = _conn(tmp_path)
        try:
            monkeypatch.setenv("FRIDAY_V4_CLAUDE_PLANNER", "1")
            handler = TextCommandHandler(conn, cwd=str(tmp_path))
            result = handler.handle("replan this mission")
            assert result.action == "chat"
            assert "don't have a mission" in result.response
        finally:
            conn.close()

    def test_replan_request_detection(self):
        """Replan detection is conservative: replan phrases match,
        mission *creation* never does."""
        from friday_v6.nl_router import _is_replan_request

        for yes in ("replan this mission", "re-plan the plan",
                    "change the plan", "revise my plan",
                    "update the plan", "adapt the mission plan"):
            assert _is_replan_request(yes), yes
        for no in ("create a plan", "ship the auth refactor by Friday",
                   "plan the migration", "run the tests"):
            assert not _is_replan_request(no), no
