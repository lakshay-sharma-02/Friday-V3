"""Hermetic tests for the shared natural-language router (Wave 9).

Covers the TextCommandHandler — the single entry point that turns an
utterance into an action (execute / mission / desktop / chat) that both
``friday6 talk`` and the voice router use:

- execute: "run the tests" flows through the real execution layer,
  results are spoken naturally, denied stays denied
- plan: "ship the auth refactor" creates a persistent mission
- desktop: delegated to an injected handler (never guessed by us)
- clarification: ambiguous/unknown requests ask, never guess
- manual steps: only completed via an explicit operator result
- never-crash: garbage input returns a graceful response

Every test is hermetic: tmp_path DB — never the real ~/.friday.
"""

from __future__ import annotations

import pytest

from friday_v6 import db
from friday_v6.nl_router import TextCommandHandler, voice_confirm


def _real_find_tool(name):
    """The real tool finder (saved so tests can patch just 'claude')."""
    from friday_v6.security.tooling import find_tool
    return find_tool(name)


def _handler(tmp_path, **kw):
    # cwd is pinned to tmp_path so a 'testing' step runs pytest on a
    # controlled directory (fast) instead of the whole repo (minutes).
    kw.setdefault("cwd", str(tmp_path))
    return TextCommandHandler(db.connect(tmp_path / "v4.db"), **kw)


def _seed_repo(tmp_path) -> None:
    """Give the executor a realistic environment: one passing test file
    and a git repo, so `testing` (pytest → exit 0) and `git` (status →
    exit 0) actually succeed instead of failing on empty/hostile dirs.
    """
    (tmp_path / "test_sample.py").write_text(
        "def test_ok():\n    assert 1 + 1 == 2\n", encoding="utf-8")
    try:
        from friday_v6.execution import execute
        execute("shell", "git init -q", cwd=str(tmp_path), force=True,
                goal="seed repo")
    except Exception:
        pass


# ==========================================================================
# Execute intent — the MCU path
# ==========================================================================


class TestExecute:
    def test_run_the_tests_executes(self, tmp_path):
        _seed_repo(tmp_path)
        handler = _handler(tmp_path)
        result = handler.handle("run the tests", force=True)
        assert result.action == "executed"
        assert result.intent == "execute"
        assert result.action_type == "testing"
        assert result.status == "succeeded"
        assert result.action_id  # audited
        assert "Done" in result.response

    def test_git_status_executes(self, tmp_path):
        _seed_repo(tmp_path)
        handler = _handler(tmp_path)
        result = handler.handle("git status", force=True)
        assert result.action == "executed"
        assert result.action_type == "git"

    def test_denied_action_says_no(self, tmp_path):
        _seed_repo(tmp_path)
        handler = _handler(tmp_path)
        # CLI contract (no durable_ask): CONFIRM + no force + no
        # confirm_fn → fails closed with a plain denial.
        result = handler.handle("run the tests")
        assert result.action == "denied"
        assert "won't do that" in result.response

    def test_non_interactive_confirm_becomes_durable_ask(self, tmp_path):
        """Phone/web/daemon path (durable_ask=True): a CONFIRM action
        that can't prompt interactively becomes a DURABLE permission
        ask the operator answers from any surface ("yes, run it"),
        never a dead-end denial."""
        _seed_repo(tmp_path)
        handler = _handler(tmp_path)
        result = handler.handle("run the tests", durable_ask=True)
        assert result.action == "asked"
        assert "yes, run it" in result.response
        assert result.request_id
        assert result.status == "asked"
        # The ask is durable + pending in the DB.
        from friday_v6 import db
        conn = db.connect(tmp_path / "v4.db")
        try:
            pending = db.pending_permission_requests(conn, limit=10)
            assert any(p["id"] == result.request_id
                       for p in pending)
        finally:
            conn.close()

    def test_failed_execution_surfaces_why(self, tmp_path):
        """Wave 19 slice 2: a real command failure says *why* (the
        actual stderr/error), not a bare 'That didn't work — failed.'
        ``git status`` in a non-repo dir fails with git's own message
        — deterministic and hermetic (tmp_path has no .git)."""
        handler = _handler(tmp_path)
        result = handler.handle("git status", force=True)
        assert result.action == "failed"
        assert "didn't work" in result.response
        assert "not a git repository" in result.response  # the reason

    def test_confirm_fn_gates_execution(self, tmp_path):
        _seed_repo(tmp_path)
        handler = _handler(tmp_path)
        yes = handler.handle("run the tests", confirm_fn=lambda _d: True)
        assert yes.action == "executed"
        no = handler.handle("run the tests", confirm_fn=lambda _d: False)
        assert no.action == "denied"  # interactive deny stays denied

    def test_clone_url_routes_to_claude(self, tmp_path, monkeypatch):
        """'clone it <url>' → claude executor, never a dead 'git' guess.

        The URL IS the concrete command (Wave 20): the claude executor
        handles clone/setup. The task stays a confirmable ask without a
        confirm_fn; with one, the executor is claude. The claude CLI is
        patched away so the test never clones a real repo (hermetic).
        """
        import friday_v6.execution.executors as ex
        monkeypatch.setattr(ex, "find_tool",
                            lambda name: None if name == "claude"
                            else _real_find_tool(name))
        url = "https://github.com/example/awesome-repo.git"
        handler = _handler(tmp_path)
        # CLI path (no durable_ask): CONFIRM + no confirm_fn → denied,
        # but still correctly routed to the claude executor.
        result = handler.handle(f"clone it {url}")
        assert result.action == "denied"
        assert result.action_type == "claude"
        assert result.command == f"clone it {url}"
        # Phone path: same utterance becomes a durable ask.
        asked = handler.handle(f"clone it {url}", durable_ask=True)
        assert asked.action == "asked"
        assert asked.action_type == "claude"
        assert asked.request_id
        # With confirmation → claude task runs (claude CLI absent →
        # 'failed' with an honest reason, never a misroute).
        yes = handler.handle(f"clone it {url}",
                             confirm_fn=lambda _d: True)
        assert yes.action_type == "claude"
        assert yes.action == "failed"
        assert "claude" in yes.response.lower()

    def test_plain_url_routes_to_claude(self, tmp_path):
        """A bare URL (no verb) is still a claude task, not a guess."""
        url = "git@github.com:acme/widget.git"
        handler = _handler(tmp_path)
        result = handler.handle(url)
        assert result.action_type == "claude"
        assert result.command == url

    def test_execute_without_action_type_clarifies(self, tmp_path):
        handler = _handler(tmp_path)
        result = handler.handle("run")
        assert result.action == "clarification"
        assert "run" in result.response.lower()


# ==========================================================================
# Plan intent — missions
# ==========================================================================


class TestPlan:
    def test_plan_creates_mission(self, tmp_path):
        handler = _handler(tmp_path)
        result = handler.handle("ship the auth refactor")
        assert result.action == "mission_created"
        assert result.mission_id
        assert "step" in result.response.lower()
        from friday_v6.missions import MissionEngine
        mission = MissionEngine(db.connect(tmp_path / "v4.db")).get(
            result.mission_id)
        assert mission is not None
        assert mission.status.value == "planned"

    def test_plan_persists_across_reconnect(self, tmp_path):
        path = tmp_path / "v4.db"
        result = TextCommandHandler(db.connect(path), cwd=str(tmp_path)).handle(
            "set up the CI pipeline")
        assert result.mission_id
        from friday_v6.missions import MissionEngine
        mission = MissionEngine(db.connect(path)).get(result.mission_id)
        assert mission.title == "set up the CI pipeline"


# ==========================================================================
# Desktop intent — delegated, never guessed
# ==========================================================================


class TestDesktop:
    def test_delegates_to_handler(self, tmp_path):
        calls = []

        def fake_desktop(text: str) -> str:
            calls.append(text)
            return "Focused the editor."

        handler = _handler(tmp_path, desktop_handler=fake_desktop)
        result = handler.handle("focus the editor")
        assert result.action == "desktop"
        assert calls  # delegated
        assert "Focused the editor." == result.response

    def test_no_handler_tells_truth(self, tmp_path):
        handler = _handler(tmp_path)
        result = handler.handle("focus the editor")
        assert result.action == "chat"
        assert "isn't wired" in result.response

    def test_desktop_handler_failure_never_crashes(self, tmp_path):
        def boom(_text: str) -> str:
            raise RuntimeError("wm gone")

        handler = _handler(tmp_path, desktop_handler=boom)
        result = handler.handle("switch workspace 2")
        assert result.action == "failed"
        assert "wm gone" in result.response


# ==========================================================================
# Chat / clarification / safety
# ==========================================================================


class TestChat:
    def test_greeting(self, tmp_path):
        result = _handler(tmp_path).handle("hello")
        assert result.action == "chat"
        assert "Friday" in result.response

    def test_help(self, tmp_path):
        result = _handler(tmp_path).handle("what can you do")
        assert result.action == "chat"
        assert "run the tests" in result.response

    def test_garbage_never_crashes(self, tmp_path):
        for text in ("", "   ", "purple monkey dishwasher",
                     "asdf qwer zxcv 12345"):
            result = _handler(tmp_path).handle(text)
            assert result.response  # always a graceful reply

    def test_empty_text(self, tmp_path):
        result = _handler(tmp_path).handle("")
        assert result.response == "I'm listening."

    def test_ask_degrades_gracefully(self, tmp_path):
        result = _handler(tmp_path).handle("what's the status of my projects")
        assert result.action == "chat"
        assert result.response


# ==========================================================================
# Manual mission steps
# ==========================================================================


class TestManual:
    def test_manual_step_only_completed_by_operator(self, tmp_path):
        _seed_repo(tmp_path)
        handler = _handler(tmp_path)
        result = handler.handle("improve the parser architecture")
        # planner makes this a manual step (no executor) → no auto-done
        assert result.action == "mission_created"
        from friday_v6.missions import MissionEngine
        engine = MissionEngine(db.connect(tmp_path / "v4.db"))
        mission = engine.get(result.mission_id)
        assert not mission.steps[0].is_executable
        # The operator completes it explicitly (mission is auto-started
        # by handle_manual since a planned mission can't advance).
        done = handler.handle_manual(result.mission_id, "wrote the design")
        assert done.action == "manual_completed"
        assert "wrote the design" in done.response
        assert engine.get(result.mission_id).steps[0].status.value == \
            "completed"

    def test_manual_unknown_mission(self, tmp_path):
        result = _handler(tmp_path).handle_manual("m_nope", "done")
        assert result.action == "failed"


# ==========================================================================
# voice_confirm helper
# ==========================================================================


class TestVoiceConfirm:
    def test_yes_words_approve(self):
        confirm = voice_confirm(lambda _q: "yes go ahead")
        assert confirm("run tests?") is True

    def test_no_words_deny(self):
        confirm = voice_confirm(lambda _q: "no wait")
        assert confirm("run tests?") is False

    def test_failure_denies_safely(self):
        def broken(_q):
            raise RuntimeError("stt down")
        confirm = voice_confirm(broken)
        assert confirm("run tests?") is False

    def test_empty_reply_denies(self):
        confirm = voice_confirm(lambda _q: "")
        assert confirm("run tests?") is False
