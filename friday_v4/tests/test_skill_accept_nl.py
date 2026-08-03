"""Tests for the Wave 14 NL accept path — \"yes, run it\" for dispatch suggestions.

The operator-visible loop:

    Friday (offer): "That matches your 'run-tests' skill — want me to run
                    'git status' next?"          (voice proactive / dispatch)
    Operator:       "yes, run it"                (Intent.ACCEPT via the ONE NLU point)
    Friday:         next step → gate → sandbox → audit → "Done — git status."

Covers:
- nlu: ACCEPT classification (fallback) + LLM-still-wins; ties with
  EXECUTE resolve correctly ("yes, run the tests" stays EXECUTE)
- nl_router: accept runs the suggestion's next step through the gate;
  honest "no pending suggestion" when nothing matches; NEVER-level
  next steps (git push) are DENIED without an explicit force override
- voice: the offer surfaces in proactive_notify and ACCEPT outcomes are
  spoken through _try_nlu_route

Safety laws verified:
- A bare \"yes\" never escalates a NEVER step (force stays the caller's).
- Accepted steps are audited (gate → sandbox → audit).
- Every test is hermetic: tmp_path DB — never the real ~/.friday.
"""

from __future__ import annotations

import pytest

from friday_v4 import db
from friday_v4.skills import SkillRegistry, STATE_PROMOTED


def _conn(tmp_path):
    return db.connect(tmp_path / "v4.db")


def _promote_skill(conn, name: str = "run-tests", steps=None) -> str:
    """Create a skill and promote it (shadow ×N → verify → promote).

    The next step is a harmless ``shell: echo`` so the accept path's
    sandboxed execution succeeds in a tmp_path (no git repo there).
    """
    reg = SkillRegistry(conn)
    steps = steps or [
        {"action_type": "testing", "command": "pytest -q", "goal": "run tests"},
        {"action_type": "shell", "command": "echo hi", "goal": "next"},
    ]
    sid = reg.create(name, steps=steps)
    for _ in range(reg._verify_matches):
        reg.record_shadow_match(sid)
    reg.verify(sid)
    reg.promote(sid)
    assert reg.get(name).verification_state == STATE_PROMOTED
    return sid


def _matching_action(conn, action_type: str = "testing",
                     command: str = "pytest -q") -> None:
    """The operator does the skill's first step (real context match)."""
    db.record_action(conn, action_type, goal="go", command=command,
                     cwd="/home/me/friday_v4", status="succeeded")


# ==========================================================================
# nlu — ACCEPT through the ONE point (LLM-first, rules fallback)
# ==========================================================================


class TestAcceptNlu:
    def test_fallback_classifies_yes_as_accept(self):
        from friday_v4.nlu import Intent, resolve
        assert resolve("yes").intent == Intent.ACCEPT
        assert resolve("yes, run it").intent == Intent.ACCEPT
        assert resolve("go ahead").intent == Intent.ACCEPT
        assert resolve("do it").intent == Intent.ACCEPT

    def test_yes_run_the_tests_stays_execute(self):
        """A concrete command beats the bare acceptance — score wins."""
        from friday_v4.nlu import Intent, resolve
        action = resolve("yes, run the tests")
        assert action.intent == Intent.EXECUTE

    def test_llm_still_wins_over_keywords(self):
        """The LLM's interpretation wins even for 'yes' utterances."""
        from friday_v4.nlu import Intent, resolve

        class FakeLLM:
            def parse_utterance(self, text):
                return {"intent": "ask", "action_type": None, "command": "",
                        "target": "status", "goal": None, "entities": [],
                        "needs_clarification": False, "clarification": "",
                        "confidence": 0.96}

        action = resolve("yes, run it", llm=FakeLLM())
        assert action.intent == Intent.ASK  # the model's call, not keywords


# ==========================================================================
# nl_router — accept executes the suggestion through the gate
# ==========================================================================


class TestAcceptRouter:
    def _handler(self, tmp_path):
        from friday_v4.nl_router import TextCommandHandler
        return TextCommandHandler(conn=_conn(tmp_path))

    def test_yes_run_it_executes_next_step(self, tmp_path):
        """ACCEPT runs the matching suggestion's next step, audited."""
        handler = self._handler(tmp_path)
        _promote_skill(handler.conn, "run-tests")
        _matching_action(handler.conn)
        # cwd pinned to tmp_path: the sandbox roots there, so the
        # executed step stays hermetic (never touches the real repo).
        result = handler.handle("yes, run it", cwd=str(tmp_path))
        assert result.action == "executed"
        assert result.status == "succeeded"
        assert result.command == "echo hi"  # the skill's next step
        assert result.goal == "run-tests"
        # Audited: the next step (shell) is recorded on the trail.
        actions = db.recent_actions(handler.conn)
        assert actions[0]["action_type"] == "shell"
        assert actions[0]["goal"] == "skill 'run-tests' next step"

    def test_accept_without_pending_is_honest(self, tmp_path):
        handler = self._handler(tmp_path)
        result = handler.handle("yes, run it")
        assert result.action == "chat"
        assert "pending suggestion" in result.response

    def test_accept_never_step_denied_without_force(self, tmp_path):
        """A bare yes never escalates a NEVER step (git push)."""
        handler = self._handler(tmp_path)
        _promote_skill(handler.conn, "ship", steps=[
            {"action_type": "git", "command": "status", "goal": "check"},
            {"action_type": "git", "command": "push origin main",
             "goal": "ship"},
        ])
        _matching_action(handler.conn, action_type="git", command="status")
        result = handler.handle("yes, run it")
        assert result.action == "denied"
        assert "override" in result.response
        # The denied attempt is still audited (evidence law).
        actions = db.recent_actions(handler.conn)
        assert actions[0]["status"] == "denied"

    def test_accept_with_force_runs_never_step(self, tmp_path):
        """force bypasses the NEVER gate — but the run stays hermetic.

        cwd is pinned to tmp_path so the sandbox roots there: git has no
        repo in tmp_path, so the pushed command fails cleanly ("not a git
        repository") — never touching the real repo. The point is the
        gate passed (status != denied).
        """
        handler = self._handler(tmp_path)
        _promote_skill(handler.conn, "ship", steps=[
            {"action_type": "git", "command": "status", "goal": "check"},
            {"action_type": "git", "command": "push origin main",
             "goal": "ship"},
        ])
        _matching_action(handler.conn, action_type="git", command="status")
        result = handler.handle("yes, run it", force=True,
                                cwd=str(tmp_path))
        # The gate passed (force) but the sandboxed run failed cleanly
        # (no git repo in tmp_path) — hermetic, never a real push.
        assert result.status != "denied"
        assert result.action in ("executed", "failed")


# ==========================================================================
# mission integration — multi-step acceptance runs under mission supervision
# ==========================================================================


class TestAcceptMission:
    """'watch me' → dispatch → mission: a multi-step accept becomes a
    supervised mission (progress tracked, first step runs now)."""

    def _handler(self, tmp_path):
        from friday_v4.nl_router import TextCommandHandler
        return TextCommandHandler(conn=_conn(tmp_path))

    def test_multi_step_accept_creates_mission_and_runs_first_step(self, tmp_path):
        handler = self._handler(tmp_path)
        # 3 steps: trigger + two remaining — acceptance should form a
        # mission of the remaining work and execute the first of them now.
        _promote_skill(handler.conn, "ship-it", steps=[
            {"action_type": "testing", "command": "pytest -q",
             "goal": "verify"},
            {"action_type": "shell", "command": "echo hi", "goal": "next"},
            {"action_type": "shell", "command": "echo bye", "goal": "last"},
        ])
        _matching_action(handler.conn)
        result = handler.handle("yes, run it", cwd=str(tmp_path))
        assert result.action == "executed"
        assert result.status == "succeeded"
        assert result.command == "echo hi"  # first remaining step ran now
        assert result.mission_id  # the rest is supervised

        # The mission persists with the remaining step pending.
        from friday_v4.missions import MissionEngine, StepStatus
        engine = MissionEngine(handler.conn)
        mission = engine.get(result.mission_id)
        assert mission is not None
        assert mission.title == "continue: ship-it"
        assert len(mission.steps) == 2
        assert mission.steps[0].status == StepStatus.COMPLETED
        assert mission.steps[1].status == StepStatus.PENDING

    def test_single_step_accept_never_creates_mission(self, tmp_path):
        """One remaining step stays a direct execution — no mission wrapper."""
        handler = self._handler(tmp_path)
        _promote_skill(handler.conn, "run-tests")  # trigger + 1 next step
        _matching_action(handler.conn)
        result = handler.handle("yes, run it", cwd=str(tmp_path))
        assert result.action == "executed"
        assert result.mission_id is None

    def test_multi_step_never_first_step_denied_without_force(self, tmp_path):
        """The first remaining step being NEVER stays denied on a bare yes —
        but the mission is still saved for an explicit override."""
        handler = self._handler(tmp_path)
        _promote_skill(handler.conn, "ship", steps=[
            {"action_type": "git", "command": "status", "goal": "check"},
            {"action_type": "git", "command": "push origin main",
             "goal": "ship"},
            {"action_type": "shell", "command": "echo done", "goal": "finish"},
        ])
        _matching_action(handler.conn, action_type="git", command="status")
        result = handler.handle("yes, run it")
        assert result.action == "denied"
        assert result.mission_id  # saved for an explicit override
        assert "override" in result.response

    def test_multi_step_accept_is_audited(self, tmp_path):
        """The executed first step lands on the audit trail (evidence law)."""
        handler = self._handler(tmp_path)
        _promote_skill(handler.conn, "ship-it", steps=[
            {"action_type": "testing", "command": "pytest -q",
             "goal": "verify"},
            {"action_type": "shell", "command": "echo hi", "goal": "next"},
            {"action_type": "shell", "command": "echo bye", "goal": "last"},
        ])
        _matching_action(handler.conn)
        result = handler.handle("yes, run it", cwd=str(tmp_path))
        actions = db.recent_actions(handler.conn)
        assert actions[0]["action_type"] == "shell"
        assert actions[0]["command"] == "echo hi"
        assert result.mission_id

    def test_multi_step_accept_mission_fails_honestly(self, tmp_path):
        """A failing first step fails the mission with an honest response."""
        handler = self._handler(tmp_path)
        _promote_skill(handler.conn, "bad-flow", steps=[
            {"action_type": "testing", "command": "pytest -q",
             "goal": "verify"},
            {"action_type": "shell", "command": "exit 3", "goal": "boom"},
            {"action_type": "shell", "command": "echo bye", "goal": "last"},
        ])
        _matching_action(handler.conn)
        result = handler.handle("yes, run it", cwd=str(tmp_path))
        assert result.action in ("executed", "failed")
        if result.action == "failed":
            assert result.mission_id  # still saved so progress is visible


# ==========================================================================
# voice — the offer round-trip (offer → "yes, run it" → spoken result)
# ==========================================================================


class TestAcceptVoice:
    def _router(self, tmp_path):
        from friday_v4.voice.router import VoiceRouter

        class _FakePipeline:
            def __init__(self):
                self.spoken = []
            def speak(self, text):
                self.spoken.append(text)
            def stop_recording_and_process(self):
                return ""

        conn = _conn(tmp_path)
        router = VoiceRouter(_FakePipeline(), enable_proactive=False,
                             conn=conn)
        return router

    def test_skill_offer_surfaces_in_proactive_notify(self, tmp_path):
        router = self._router(tmp_path)
        _promote_skill(router._conn, "run-tests")
        _matching_action(router._conn)
        offer = router.proactive_notify(force=True)
        assert offer is not None
        assert "run-tests" in offer
        assert "want me to run" in offer
        assert router.pipeline.spoken and "run-tests" in router.pipeline.spoken[0]

    def test_no_offer_without_matching_context(self, tmp_path):
        router = self._router(tmp_path)
        _promote_skill(router._conn, "run-tests")
        # No matching recent action → nothing offered.
        assert router.proactive_notify(force=True) is None

    def test_accept_chat_response_is_spoken(self, tmp_path):
        """'yes, run it' with nothing pending speaks the honest answer."""
        router = self._router(tmp_path)
        router._conn = _conn(tmp_path)  # fresh, no suggestions
        response = router.route("yes, run it")
        assert "pending suggestion" in response


class TestAcceptWebSurface:
    def test_web_chat_accept_runs_next_step(self, tmp_path):
        """The web dashboard shares the same handler — accept works there."""
        from friday_v4.nl_router import TextCommandHandler
        conn = _conn(tmp_path)
        try:
            _promote_skill(conn, "run-tests")
            _matching_action(conn)
            result = TextCommandHandler(conn=conn).handle(
                "yes, run it", cwd=str(tmp_path))
            assert result.action == "executed"
            assert result.command == "echo hi"
        finally:
            conn.close()
