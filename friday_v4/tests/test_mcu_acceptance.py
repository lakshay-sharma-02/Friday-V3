"""MCU acceptance harness — the five proof moments (Wave 19, slice 1).

The exit condition of Wave 19 is *"the five MCU acceptance tests, all
passing through natural language only"* (MCU_FRIDAY_STANDARD.md §3).
This suite is that instrument: each proof moment is driven through the
**same NL brain every surface uses** (``nl_router.TextCommandHandler`` —
talk / voice / web / mobile all route through it), with no flags, no
CLI syntax, no hardcoded answers:

1. **Mission shepherding** — "ship the auth refactor by Friday" →
   mission created → replan reports "plan changed because…" (and with
   ``FRIDAY_V4_CLAUDE_PLANNER`` the decomposition happens through
   Claude Code).
2. **Workflow copying** — "watch me do this" → captured → "learn this"
   → shadow skill → verified by the sweep → promoted (operator
   approval) → auto-dispatched → "yes, run it" runs it through the gate.
3. **Deep reasoning** — "what's the deal between X and Y" → a
   researched, evidence-cited, ranged estimate (seeded hermetic repos).
4. **Adaptive identity** — "be more casual, Tony" → tone shifts now,
   persists, and "why do you talk that way" explains it.
5. **Capability composition** — "figure out why the build fails and fix
   it" → decomposed onto the Claude Code CLI through the gate →
   audited, result surfaced.

Everything is hermetic: tmp_path DBs, fake ``claude`` binaries, seeded
repos, no network, no real ~/.friday.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from friday_v4 import db

# ─────────────────────────────────────────────────────────────────────
# Fake claude — serves BOTH the executor (mode=ok) and the planner
# (mode=plan_ok) surfaces, matching the real CLI's JSON shape.
# ─────────────────────────────────────────────────────────────────────

_FAKE_CLAUDE_SRC = """\
#!/usr/bin/env python3
import json, os, sys
mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")
args = sys.argv[1:]
task = ""
if "-p" in args:
    task = args[args.index("-p") + 1]
if mode == "error":
    print(json.dumps({"is_error": True, "result": "model exploded",
                      "terminal_reason": "api_error"}))
elif mode == "plan_ok":
    plan = [
        {"title": "Run the test suite", "action_type": "testing",
         "command": "tests/"},
        {"title": "Fix the failing tests", "action_type": "claude",
         "command": "fix the failing tests"},
        {"title": "Ask the operator to review", "action_type": None,
         "command": ""},
    ]
    print(json.dumps({"is_error": False,
                      "result": json.dumps({"plan": plan}),
                      "terminal_reason": "completed",
                      "permission_denials": []}))
else:
    print(json.dumps({"is_error": False, "result": f"answered: {task}",
                      "terminal_reason": "completed",
                      "permission_denials": []}))
"""


@pytest.fixture
def fake_claude(tmp_path: Path, monkeypatch, request) -> str:
    """Install a fake `claude` executable and point both consumers at it."""
    mode = getattr(request, "param", "ok")
    exe = tmp_path / "claude"
    exe.write_text(_FAKE_CLAUDE_SRC.replace('"ok"', f'"{mode}"'),
                   encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    from friday_v4.execution import executors as exec_mod
    from friday_v4.missions import claude_planner as cp_mod
    monkeypatch.setattr(exec_mod, "find_tool", lambda name: str(exe))
    monkeypatch.setattr(cp_mod, "find_tool", lambda name: str(exe))
    return str(exe)


@pytest.fixture
def no_claude(monkeypatch):
    """Simulate a machine without the Claude Code CLI."""
    from friday_v4.execution import executors as exec_mod
    from friday_v4.missions import claude_planner as cp_mod
    monkeypatch.setattr(exec_mod, "find_tool", lambda name: None)
    monkeypatch.setattr(cp_mod, "find_tool", lambda name: None)


def _conn(tmp_path):
    return db.connect(tmp_path / "v4.db")


def _handler(tmp_path, conn=None, cwd=None):
    from friday_v4.nl_router import TextCommandHandler
    return TextCommandHandler(conn=conn or _conn(tmp_path), cwd=cwd or str(tmp_path))


def _seed_repos(tmp_path: Path) -> tuple[Path, Path]:
    """Two small hermetic repos sharing auth.py + fastapi (research M3)."""
    a = tmp_path / "vivaha"
    (a / "src").mkdir(parents=True)
    (a / "src" / "auth.py").write_text("from fastapi import FastAPI\n")
    (a / "README.md").write_text("# vivaha\nShared auth for the family.\n")
    b = tmp_path / "mindwell"
    (b / "src").mkdir(parents=True)
    (b / "src" / "auth.py").write_text("from fastapi import FastAPI\n")
    (b / "README.md").write_text("# MindWell\nMindfulness tracking.\n")
    return a, b


# ═════════════════════════════════════════════════════════════════════
# Moment 1 — Mission shepherding (MCU test #1)
# ═════════════════════════════════════════════════════════════════════


class TestMoment1MissionShepherding:
    def test_ship_the_auth_refactor_creates_and_shepherds(self, tmp_path,
                                                          no_claude):
        """'ship the auth refactor by Friday' → mission created with a
        plan → the operator completes the manual step → shepherded."""
        from friday_v4.missions import MissionEngine
        handler = _handler(tmp_path)

        created = handler.handle("ship the auth refactor by Friday")
        assert created.action == "mission_created"
        assert created.mission_id
        assert "step(s)" in created.response          # proposes next steps
        mission = MissionEngine(handler.conn).get(created.mission_id)
        assert mission is not None and mission.steps

        # Shepherding: the (manual) step is completed by the operator —
        # Friday never invents a result.
        done = handler.handle_manual(created.mission_id, "reviewed the plan")
        assert done.action == "manual_completed"
        completed = MissionEngine(handler.conn).get(created.mission_id)
        assert completed.status.value == "completed"

    def test_how_is_it_going_reports_next_step(self, tmp_path, no_claude):
        """Shepherding (MCU test #1): 'how's it going' names the next
        step — Friday proposes what's next, it doesn't just say a
        percentage. Asked with no target, the ACTIVE mission is the one
        being shepherded."""
        from friday_v4.reasoning import answer
        conn = _conn(tmp_path)
        try:
            handler = _handler(tmp_path, conn=conn)
            created = handler.handle("ship the auth refactor by Friday")
            assert created.action == "mission_created"

            result = handler.handle("how's it going")
            assert result.action == "chat"
            assert "auth refactor" in result.response
            assert "next" in result.response

            # The reasoning path cites real mission state (evidence).
            ans = answer("how's it going", conn=conn)
            assert ans.known
            assert any(e.source == "v4.missions" for e in ans.evidence)
        finally:
            conn.close()

    def test_how_is_auth_refactor_going_targets_the_mission(self, tmp_path,
                                                            no_claude):
        """A named target narrows to that mission."""
        handler = _handler(tmp_path)
        handler.handle("ship the auth refactor by Friday")
        result = handler.handle("how's the auth refactor going")
        assert result.action == "chat"
        assert "auth refactor" in result.response

    def test_mission_blocker_is_reported(self, tmp_path):
        """A failed step is surfaced as the blocker — never hidden."""
        from friday_v4.reasoning import answer
        conn = _conn(tmp_path)
        try:
            from friday_v4.missions import MissionEngine
            engine = MissionEngine(conn, cwd=str(tmp_path))
            mission = engine.create("ship the auth refactor by Friday")
            steps = db.list_mission_steps(conn, mission.id)
            db.update_mission_step(conn, steps[0]["id"],
                                   status="failed", result="lint broke")
            db.update_mission(conn, mission.id, status="failed")

            ans = answer("how's it going", conn=conn)
            assert ans.known
            assert "blocked" in ans.text or "failed" in ans.text
        finally:
            conn.close()

    @pytest.mark.parametrize("fake_claude", ["plan_ok"], indirect=True)
    def test_replan_reports_change_through_claude_code(self, tmp_path,
                                                       fake_claude,
                                                       monkeypatch):
        """'replan this mission' re-decomposes through Claude Code and
        reports 'plan changed because …' — the adaptation contract."""
        from friday_v4.missions import MissionEngine
        handler = _handler(tmp_path)

        # Deterministic creation first (env off → 1 manual step; claude
        # is never consulted even though the fixture installed it).
        created = handler.handle("ship the auth refactor by Friday")
        assert created.action == "mission_created"
        assert len(MissionEngine(handler.conn).get(
            created.mission_id).steps) == 1

        # Reality changes → the operator asks to replan; Claude Code
        # decomposes the goal into a richer plan.
        monkeypatch.setenv("FRIDAY_V4_CLAUDE_PLANNER", "1")
        replanned = handler.handle("replan this mission")
        assert replanned.action == "mission_replanned"
        assert "plan changed because" in replanned.response
        assert "step(s)" in replanned.response
        reloaded = MissionEngine(handler.conn).get(created.mission_id)
        assert len(reloaded.steps) == 3            # Claude's decomposition
        assert reloaded.steps[0].action_type == "testing"
        assert reloaded.steps[1].action_type == "claude"
        assert reloaded.steps[2].action_type is None   # manual step
        # The delegation was audited as a first-class action.
        rows = db.recent_actions(handler.conn, action_type="claude_plan")
        assert rows and rows[0]["status"] == "succeeded"

    def test_mission_survives_without_claude(self, tmp_path, no_claude,
                                             monkeypatch):
        """Opt-in set, no claude CLI → deterministic floor, never a crash."""
        monkeypatch.setenv("FRIDAY_V4_CLAUDE_PLANNER", "1")
        handler = _handler(tmp_path)
        created = handler.handle("ship the auth refactor by Friday")
        assert created.action == "mission_created"
        assert created.mission_id


# ═════════════════════════════════════════════════════════════════════
# Moment 2 — Workflow copying (MCU test #2)
# ═════════════════════════════════════════════════════════════════════


class TestMoment2WorkflowCopying:
    def _demo(self, conn, cwd: str, rounds: int = 3) -> None:
        """'echo hello' → 'echo world' in ``cwd`` (safe, hermetic steps)."""
        for _ in range(rounds):
            db.record_action(conn, "shell", command="echo hello", cwd=cwd,
                             status="succeeded")
            db.record_action(conn, "shell", command="echo world", cwd=cwd,
                             status="succeeded")

    def test_watch_learn_promote_dispatch_run(self, tmp_path):
        """The full loop: watch me → shadow skill → sweep verifies →
        operator approves promotion → dispatch offers → 'yes, run it'
        runs it through the gate."""
        from friday_v4.skills import (ShadowExecutor, SkillDispatcher,
                                      SkillRegistry, STATE_SHADOW)
        conn = _conn(tmp_path)
        cwd = str(tmp_path)
        try:
            handler = _handler(tmp_path, conn=conn)

            started = handler.handle("watch me do this")
            assert started.action == "watching"
            self._demo(conn, cwd, rounds=1)   # exactly 2 steps

            formed = handler.handle("learn this")
            assert formed.action == "skill_formed"
            assert "shadow" in formed.response
            skill = SkillRegistry(conn).list()[0]
            assert skill.verification_state == STATE_SHADOW
            assert [s["command"] for s in skill.steps] == ["echo hello",
                                                           "echo world"]
            # The skill's first step must match the LATEST audited action
            # for the daemon sweep to record shadow matches.
            db.record_action(conn, "shell", command="echo hello", cwd=cwd,
                             status="succeeded")

            # The daemon's sweep verifies at the match threshold (pure
            # read — nothing executes); the operator then approves.
            assert SkillRegistry(conn).list()[0].is_shadow
            for _ in range(4):  # ≥ DEFAULT_VERIFY_MATCHES shadow matches
                ShadowExecutor(conn).sweep()
            assert SkillRegistry(conn).list()[0].verification_state \
                == "verified"
            assert SkillRegistry(conn).promote(skill.id)   # approval
            assert SkillRegistry(conn).list()[0].is_promoted

            # Dispatch: the matching context raises an offer for the
            # next step — nothing executes from the offer itself.
            recent = [{"action_type": "shell", "command": "echo hello",
                       "cwd": cwd}]
            suggestion = SkillDispatcher(conn).suggest(recent=recent)[0]
            assert suggestion["skill_name"] == skill.name
            assert suggestion["next_steps"][0]["command"] == "echo world"
            assert suggestion["pending_approval"] is True

            # The operator accepts through the NL surface → the gate
            # runs the (safe) next step.
            accepted = handler.handle("yes, run it")
            assert accepted.action == "executed"
            assert accepted.status == "succeeded"
            assert accepted.command == "echo world"
        finally:
            conn.close()

    def test_what_did_you_learn_cites_real_skill(self, tmp_path):
        """ASK cites the formed skill — evidence, not a hardcoded string."""
        conn = _conn(tmp_path)
        try:
            handler = _handler(tmp_path, conn=conn)
            handler.handle("watch me do this")
            db.record_action(conn, "git", command="git status",
                             cwd=str(tmp_path), status="succeeded")
            handler.handle("learn this")
            result = handler.handle("what did you learn")
            assert result.action == "chat"
            assert "learned" in result.response
        finally:
            conn.close()


# ═════════════════════════════════════════════════════════════════════
# Moment 3 — Deep reasoning (MCU test #3)
# ═════════════════════════════════════════════════════════════════════


class TestMoment3DeepReasoning:
    def test_whats_the_deal_between_researches_with_evidence(self, tmp_path):
        """'what's the deal between X and Y' → a researched, cited,
        ranged estimate — through the plain NL surface, no flags."""
        a, b = _seed_repos(tmp_path)
        handler = _handler(tmp_path)
        result = handler.handle(f"what's the deal between {a} and {b}")
        assert result.intent == "research"
        assert result.action == "chat"
        assert "auth.py" in result.response          # evidence
        assert "estimate" in result.response.lower()  # ranged
        assert "confidence" in result.response.lower()

    def test_analyze_vs_still_works(self, tmp_path):
        a, b = _seed_repos(tmp_path)
        handler = _handler(tmp_path)
        result = handler.handle(f"analyze {a} vs {b}")
        assert result.action == "chat"
        assert "auth.py" in result.response

    def test_whats_the_deal_with_pair_researches(self, tmp_path):
        """The "with" variant of the MCU sentence ('what's the deal
        with X and Y') researches too — Wave 19 slice 2 sweep caught it
        classifying ASK and answering "I don't know yet" instead."""
        a, b = _seed_repos(tmp_path)
        handler = _handler(tmp_path)
        result = handler.handle(f"what's the deal with {a} and {b}")
        assert result.intent == "research"
        assert result.action == "chat"
        assert "auth.py" in result.response          # evidence
        assert "estimate" in result.response.lower()  # ranged

    def test_research_lead_does_not_hijack_stronger_intents(self):
        """A bare research lead (no pair) must not hijack a stronger
        intent: 'what's the deal with my security scan' scans, it does
        not fall into the single-repo research path."""
        from friday_v4.nlu import resolve
        assert resolve("what's the deal with my security scan").intent \
            == "security"


# ═════════════════════════════════════════════════════════════════════
# Moment 4 — Adaptive identity (MCU test #4)
# ═════════════════════════════════════════════════════════════════════


class TestMoment4AdaptiveIdentity:
    def test_be_more_casual_persists_and_explains(self, tmp_path):
        from friday_v4.relationship import RelationshipEngine
        conn = _conn(tmp_path)
        try:
            handler = _handler(tmp_path, conn=conn)
            shifted = handler.handle("be more casual, Tony")
            assert shifted.action == "style"
            assert "casual" in shifted.response

            # Explains why — the exact MCU exchange.
            why = _handler(tmp_path, conn=conn).handle(
                "why do you talk that way")
            assert why.action == "chat"
            assert "because you asked me" in why.response

            # Persists across sessions (new connection, same DB).
            again = RelationshipEngine(_conn(tmp_path)).direction()
            assert again is not None and again.tone == "casual"
        finally:
            conn.close()

    def test_reset_restores_default(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            handler = _handler(tmp_path, conn=conn)
            handler.handle("be more formal")
            reset = handler.handle("be yourself again")
            assert reset.action == "style"
            from friday_v4.relationship import RelationshipEngine
            assert RelationshipEngine(conn).direction() is None
        finally:
            conn.close()


# ═════════════════════════════════════════════════════════════════════
# Moment 5 — Capability composition (MCU test #5)
# ═════════════════════════════════════════════════════════════════════


class TestMoment5CapabilityComposition:
    @pytest.mark.parametrize("fake_claude", ["ok"], indirect=True)
    def test_figure_out_and_fix_delegates_through_gate(self, tmp_path,
                                                       fake_claude):
        """'figure out why the build fails and fix it' → decomposed onto
        the Claude Code CLI → audited, result surfaced."""
        conn = _conn(tmp_path)
        try:
            handler = _handler(tmp_path, conn=conn)
            result = handler.handle("figure out why the build fails and "
                                    "fix it", force=True)
            assert result.action == "executed"
            assert result.action_type == "claude"
            assert result.status == "succeeded"
            assert "answered" in result.response    # the agent's result
            assert result.action_id                # audited
        finally:
            conn.close()

    def test_composition_without_claude_is_honest(self, tmp_path, no_claude):
        conn = _conn(tmp_path)
        try:
            handler = _handler(tmp_path, conn=conn)
            result = handler.handle("figure out why the build fails and "
                                    "fix it", force=True)
            assert result.action == "failed"
            assert "Claude Code" in result.response   # honest, not a lie
        finally:
            conn.close()
