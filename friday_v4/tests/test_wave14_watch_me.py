"""Hermetic tests for Wave 14 — Watch Me (demonstration capture).

Covers:
- db.py: watches table (migration v5) + desktop_events table (migration
  v6), start/end/get/list/active, the actions_between window query,
  single-active-watch enforcement, and the desktop-observer bridge
- skills/watcher.py: watch → work → stop forms a generalized shadow
  skill (repo context, collapsed duplicates, chronological merge of
  audited actions + observed app opens); no capture → no skill
- skills/noticer.py: repeated patterns → offers; known skills skipped
- nlu: SKILL intent (fallback), resolver target threading
- nl_router: watch me / learn this / what did you learn, all surfaces
- reasoning: skills_provider cites real skills (Wiring Law)
- cli_skills: watch / watch-stop / noticed commands
- daemon: SkillLearner.sweep_once surfaces 'noticed' offers

Safety laws verified:
- Everything formed starts in shadow mode, never executes.
- Offers are pure reads — nothing is formed until the operator accepts.
- Every test is hermetic: tmp_path DB — never the real ~/.friday.
"""

from __future__ import annotations

from friday_v4 import db
from friday_v4.skills import (
    RepetitionNoticer,
    SkillRegistry,
    STATE_SHADOW,
    WatchRecorder,
)


def _conn(tmp_path):
    return db.connect(tmp_path / "v4.db")


def _record_demo(conn, repo: str = "friday_v4", rounds: int = 2) -> None:
    """A 'testing → git' demonstration in ``repo``, oldest first."""
    cwd = f"/home/me/{repo}"
    for r in range(rounds):
        db.record_action(conn, "testing", goal=f"run tests {r}",
                         command="pytest -q", cwd=cwd, status="succeeded")
        db.record_action(conn, "git", goal=f"check state {r}",
                         command="git status", cwd=cwd, status="succeeded")


# ==========================================================================
# db.py — watches table + helpers
# ==========================================================================


class TestWatchesDb:
    def test_schema_v7_includes_watches_desktop_and_autonomy(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            assert db.schema_version(conn) == 8
            tables = {r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'").fetchall()}
            assert "watches" in tables
            assert "desktop_events" in tables
            assert "permission_requests" in tables
            assert "operator_overrides" in tables
        finally:
            conn.close()

    def test_start_get_end_lifecycle(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            wid = db.start_watch(conn, name="deploy routine", context="repo-x")
            assert wid
            watch = db.get_watch(conn, wid)
            assert watch["status"] == "active"
            assert watch["name"] == "deploy routine"
            assert watch["context"] == "repo-x"
            assert db.active_watch(conn)["id"] == wid

            assert db.end_watch(conn, wid)
            assert db.get_watch(conn, wid)["status"] == "stopped"
            assert db.active_watch(conn) is None
        finally:
            conn.close()

    def test_only_one_active_watch(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            w1 = db.start_watch(conn)
            w2 = db.start_watch(conn)  # closes w1
            assert db.get_watch(conn, w1)["status"] == "stopped"
            assert db.get_watch(conn, w2)["status"] == "active"
            assert len(db.list_watches(conn, status="active")) == 1
        finally:
            conn.close()

    def test_actions_between_window(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            wid = db.start_watch(conn)
            watch = db.get_watch(conn, wid)
            db.record_action(conn, "testing", goal="g1", command="pytest -q",
                             status="succeeded")
            db.record_action(conn, "git", goal="g2", command="git status",
                             status="succeeded")
            captured = db.actions_between(conn, watch["started_at"],
                                          db.now_iso())
            assert [a["action_type"] for a in captured] == ["testing", "git"]
        finally:
            conn.close()

    def test_end_watch_links_skill(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            wid = db.start_watch(conn)
            sid = "skill-abc"
            assert db.end_watch(conn, wid, skill_id=sid)
            watch = db.get_watch(conn, wid)
            assert watch["status"] == "formed"
            assert watch["skill_id"] == sid
        finally:
            conn.close()


# ==========================================================================
# skills/watcher.py — WatchRecorder (explicit capture)
# ==========================================================================


class TestWatchRecorder:
    def test_watch_stop_forms_generalized_shadow_skill(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            watcher = WatchRecorder(conn)
            wid = watcher.start(name="run-tests")
            _record_demo(conn, repo="friday_v4", rounds=1)  # one workflow
            formed = watcher.stop(wid)
            assert formed, "a captured demonstration should form a skill"
            skill = formed["skill"]
            assert skill.verification_state == STATE_SHADOW
            assert skill.confidence == 0.0  # shadow-first, never executes
            assert skill.name == "run-tests"
            assert len(skill.steps) == 2
            # Generalization: steps carry the repo they ran in.
            assert skill.steps[0]["repo"] == "friday_v4"
            assert skill.steps[0]["action_type"] == "testing"
            # The watch is closed + linked.
            assert db.get_watch(conn, wid)["status"] == "formed"
        finally:
            conn.close()

    def test_consecutive_duplicates_collapsed(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            watcher = WatchRecorder(conn)
            wid = watcher.start()
            cwd = "/home/me/app"
            for _ in range(3):
                db.record_action(conn, "git", command="git status", cwd=cwd)
            formed = watcher.stop(wid)
            skill = formed["skill"]
            assert len(skill.steps) == 1  # 3× git status → 1 step
        finally:
            conn.close()

    def test_stop_without_watch_returns_none(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            assert WatchRecorder(conn).stop() is None
        finally:
            conn.close()

    def test_stop_with_no_actions_forms_nothing(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            watcher = WatchRecorder(conn)
            wid = watcher.start()
            assert watcher.stop(wid) is None  # nothing watched → no skill
            assert db.get_watch(conn, wid)["status"] == "stopped"
        finally:
            conn.close()

    def test_mismatched_same_name_demo_is_not_discarded(self, tmp_path):
        """A same-name skill with DIFFERENT steps must not swallow the
        fresh demonstration — the new skill gets a versioned name."""
        conn = _conn(tmp_path)
        try:
            reg = SkillRegistry(conn)
            reg.create("deploy", steps=[{"action_type": "shell",
                                         "command": "deploy.sh"}])
            watcher = WatchRecorder(conn, registry=reg)
            wid = watcher.start(name="deploy")
            _record_demo(conn, rounds=1)  # testing→git, NOT the shell step
            formed = watcher.stop(wid)
            skill = formed["skill"]
            assert skill.name == "deploy-2"  # fresh demo preserved
            assert skill.steps[0]["action_type"] == "testing"
            assert len(reg.list()) == 2
        finally:
            conn.close()

    def test_matching_same_name_demo_is_reused(self, tmp_path):
        """A same-name skill whose first step matches the demo is reused
        (no duplicate, no loss — the demonstration is the same workflow)."""
        conn = _conn(tmp_path)
        try:
            reg = SkillRegistry(conn)
            reg.create("run-tests", steps=[{"action_type": "testing",
                                            "command": "pytest -q"}])
            watcher = WatchRecorder(conn, registry=reg)
            wid = watcher.start(name="run-tests")
            _record_demo(conn, rounds=1)
            formed = watcher.stop(wid)
            assert formed["skill"].id == reg.get("run-tests").id
            assert len(reg.list()) == 1
        finally:
            conn.close()

    def test_capture_returns_watched_actions(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            watcher = WatchRecorder(conn)
            wid = watcher.start()
            db.record_action(conn, "testing", command="pytest -q")
            actions = watcher.capture(wid)
            assert len(actions) == 1
        finally:
            conn.close()


# ==========================================================================
# Desktop-observer bridge (Wave 14 close-out) — app opens are capturable
# ==========================================================================


class TestWatchCapturesDesktop:
    """The always-on presence records app switches into ``desktop_events``;
    ``WatchRecorder.stop`` merges them with audited actions so "watch me"
    → open Brave → open VSCode → "learn this" forms a real skill whose
    steps are open:app actions."""

    def test_watch_captures_app_opens(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            watcher = WatchRecorder(conn)
            wid = watcher.start(name="morning-flow")
            db.record_desktop_event(conn, app="brave", title="youtube")
            db.record_desktop_event(conn, app="code", repo="friday_v4")
            formed = watcher.stop(wid)
            assert formed
            skill = formed["skill"]
            steps = skill.steps
            assert len(steps) == 2
            # App opens become action_type=app_switch steps, command=app.
            assert steps[0]["action_type"] == "app_switch"
            assert steps[0]["command"] == "brave"
            assert steps[1]["action_type"] == "app_switch"
            assert steps[1]["command"] == "code"
            # Repo context generalizes like audited actions.
            assert steps[1]["repo"] == "friday_v4"
        finally:
            conn.close()

    def test_desktop_and_audited_actions_merge_in_order(self, tmp_path):
        """Desktop events and audited actions interleave into one ordered
        step list (events are captured by created_at, oldest first).

        Note: this relies on two consecutive ``now_iso()`` calls differing
        (microsecond precision) so the event's created_at is strictly
        earlier than the action's — if that assumption ever changes, the
        merge helper needs explicit timestamp injection instead."""
        conn = _conn(tmp_path)
        try:
            watcher = WatchRecorder(conn)
            wid = watcher.start()
            db.record_desktop_event(conn, app="code")
            db.record_action(conn, "git", command="git status")
            formed = watcher.stop(wid)
            steps = formed["skill"].steps
            assert [s["command"] for s in steps] == ["code", "git status"]
        finally:
            conn.close()

    def test_consecutive_same_app_events_collapse(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            watcher = WatchRecorder(conn)
            wid = watcher.start()
            for _ in range(3):
                db.record_desktop_event(conn, app="code")
            formed = watcher.stop(wid)
            assert len(formed["skill"].steps) == 1
        finally:
            conn.close()

    def test_events_without_app_are_skipped(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            watcher = WatchRecorder(conn)
            wid = watcher.start()
            db.record_desktop_event(conn, app="", title="ignored")
            db.record_action(conn, "git", command="git status")
            formed = watcher.stop(wid)
            steps = formed["skill"].steps
            assert len(steps) == 1
            assert steps[0]["action_type"] == "git"
        finally:
            conn.close()

    def test_engine_observer_records_desktop_events(self, tmp_path):
        """The AnticipationEngine's app-change callback persists events
        when given a db_path (daemon = the always-on presence)."""
        import threading
        from friday_v4.proactive.anticipation import AnticipationEngine
        path = tmp_path / "v4.db"
        engine = AnticipationEngine(db_path=path)
        # The callback guard requires the observer to be "started" (the
        # daemon's always-on state); simulate that without threads.
        engine._observer_stop = threading.Event()
        engine._last_app = "kitty"
        engine._last_repo = "friday_v4"
        engine._on_app_change("brave")
        conn = db.connect(path)
        try:
            events = db.recent_desktop_events(conn)
            assert len(events) == 1
            assert events[0]["app"] == "brave"
            assert events[0]["repo"] == "friday_v4"
            # End-to-end: watch → recorded event → skill step.
            watcher = WatchRecorder(conn)
            wid = watcher.start(name="browse-flow")
            engine._on_app_change("code")
            formed = watcher.stop(wid)
            assert formed and formed["skill"].steps
        finally:
            conn.close()

    def test_desktop_events_helpers_guarded(self, tmp_path):
        """Missing table / bad rows degrade to [] — never crash."""
        conn = _conn(tmp_path)
        try:
            assert db.desktop_events_between(conn, "a", "b") == []
            assert db.recent_desktop_events(conn) == []
        finally:
            conn.close()

    def test_desktop_events_helpers_degrade_on_missing_table(self, tmp_path):
        """A dropped table must degrade gracefully — reads → [], write →
        None, never a crash (matches the missions guard pattern)."""
        conn = _conn(tmp_path)
        try:
            conn.execute("DROP TABLE IF EXISTS desktop_events")
            conn.commit()
            assert db.desktop_events_between(conn, "a", "b") == []
            assert db.recent_desktop_events(conn) == []
            assert db.record_desktop_event(conn, app="brave") is None
        finally:
            conn.close()


# ==========================================================================
# skills/noticer.py — 'I noticed you do this every time'
# ==========================================================================


class TestRepetitionNoticer:
    def test_notices_repeated_pattern(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            _record_demo(conn, repo="friday_v4", rounds=3)
            offers = RepetitionNoticer(conn, min_occurrences=2).notice()
            assert offers, "a repeated pattern should be noticed"
            offer = offers[0]
            assert offer["count"] >= 2
            assert "every time" in offer["offer"]
            assert offer["context"] == "friday_v4"  # repo generalization
        finally:
            conn.close()

    def test_skips_patterns_already_learned(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            # Form the skill first (explicitly), then the noticer must
            # not re-offer the same pattern.
            watcher = WatchRecorder(conn)
            wid = watcher.start(name="run-tests")
            _record_demo(conn)
            watcher.stop(wid)
            assert RepetitionNoticer(conn, min_occurrences=2).notice() == []
        finally:
            conn.close()

    def test_no_repeat_no_offer(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            db.record_action(conn, "testing", command="pytest -q")
            assert RepetitionNoticer(conn, min_occurrences=2).notice() == []
        finally:
            conn.close()

    def test_notice_is_pure_read(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            _record_demo(conn, rounds=3)
            assert db.list_skills(conn) == []  # nothing formed by noticing
            RepetitionNoticer(conn, min_occurrences=2).notice()
            assert db.list_skills(conn) == []  # still nothing formed
        finally:
            conn.close()


# ==========================================================================
# nlu — SKILL intent through the ONE point (LLM-first, rules fallback)
# ==========================================================================


class TestSkillNlu:
    def test_fallback_classifies_watch_as_skill(self):
        from friday_v4.nlu import Intent, resolve
        action = resolve("watch me do this")  # no LLM → deterministic fallback
        assert action.intent == Intent.SKILL
        assert action.target == "start"

    def test_fallback_classifies_learn_as_skill(self):
        from friday_v4.nlu import Intent, resolve
        assert resolve("learn this").intent == Intent.SKILL
        assert resolve("stop watching").intent == Intent.SKILL
        assert resolve("stop watching").target == "stop"

    def test_llm_still_wins_over_keywords(self):
        """The LLM's interpretation wins even when keywords are present."""
        from friday_v4.nlu import Intent, resolve

        class FakeLLM:
            def parse_utterance(self, text):
                return {"intent": "ask", "action_type": None, "command": "",
                        "target": "vivaha", "goal": None, "entities": [],
                        "needs_clarification": False, "clarification": "",
                        "confidence": 0.97}

        action = resolve("watch me do this", llm=FakeLLM())
        assert action.intent == Intent.ASK  # the model's call, not keywords


# ==========================================================================
# nl_router — watch me / learn this / what did you learn
# ==========================================================================


class TestSkillRouter:
    def _handler(self, tmp_path):
        from friday_v4.nl_router import TextCommandHandler
        return TextCommandHandler(conn=_conn(tmp_path))

    def test_watch_then_learn_forms_skill(self, tmp_path):
        handler = self._handler(tmp_path)
        start = handler.handle("watch me do this")
        assert start.action == "watching"
        assert "watching" in start.response
        _record_demo(tmp_path and _conn(tmp_path)) if False else None
        # Record actions directly against the handler's conn.
        conn = handler.conn
        _record_demo(conn, repo="friday_v4")
        stop = handler.handle("learn this")
        assert stop.action == "skill_formed"
        assert "skill" in stop.response and "shadow" in stop.response
        # Skill exists and is in shadow.
        from friday_v4.skills import SkillRegistry
        skills = SkillRegistry(conn).list()
        assert skills and skills[0].verification_state == STATE_SHADOW

    def test_stop_without_watch_is_honest(self, tmp_path):
        handler = self._handler(tmp_path)
        result = handler.handle("learn this")
        assert "wasn't watching" in result.response

    def test_what_did_you_learn_summarizes(self, tmp_path):
        """A question routes ASK → the reasoning skills_provider (Wiring
        Law) and cites the real skill — no 'I don't know'."""
        handler = self._handler(tmp_path)
        handler.handle("watch me do this")
        _record_demo(handler.conn, rounds=1)
        handler.handle("learn this")
        result = handler.handle("what did you learn")
        assert result.action == "chat"
        assert "learned" in result.response
        assert "shadow" in result.response

    def test_skill_name_hint_parses_utterance(self):
        from friday_v4.nl_router import _skill_name_hint
        assert _skill_name_hint("watch me do deploy routine") == "deploy routine"
        assert _skill_name_hint("watch me") == ""
        assert _skill_name_hint("learn how to lint") == "lint"


# ==========================================================================
# reasoning — skills_provider (Wiring Law: ASK cites real skills)
# ==========================================================================


class TestSkillsProvider:
    def test_asks_cite_real_skills(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            reg = SkillRegistry(conn)
            reg.create("run-tests", steps=[{"action_type": "testing"}])
            from friday_v4.reasoning import answer
            ans = answer("what did you learn?", conn=conn)
            assert ans.known
            assert "run-tests" in ans.text
            assert any(e.source == "v4.skills" for e in ans.evidence)
        finally:
            conn.close()

    def test_no_skills_answers_dont_know(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            from friday_v4.reasoning import answer
            ans = answer("what did you learn?", conn=conn)
            assert not ans.known  # no evidence → honest "I don't know yet"
        finally:
            conn.close()


# ==========================================================================
# cli_skills — watch / watch-stop / noticed
# ==========================================================================


class TestSkillsCli:
    def _args(self, tmp_path, **kw):
        from types import SimpleNamespace
        base = {"db": tmp_path / "v4.db", "json": False, "name": "",
                "note": ""}
        base.update(kw)
        return SimpleNamespace(**base)

    def test_watch_and_watch_stop(self, tmp_path, capsys):
        from friday_v4.cli_skills import cmd_skills_watch, cmd_skills_watch_stop
        assert cmd_skills_watch(self._args(tmp_path, name="deploy")) == 0
        _record_demo(_conn(tmp_path))
        assert cmd_skills_watch_stop(self._args(tmp_path, name="deploy")) == 0
        out = capsys.readouterr().out
        assert "Learned from" in out and "deploy" in out

    def test_watch_stop_without_watch(self, tmp_path, capsys):
        from friday_v4.cli_skills import cmd_skills_watch_stop
        assert cmd_skills_watch_stop(self._args(tmp_path)) == 0
        assert "wasn't watching" in capsys.readouterr().out

    def test_noticed_lists_offers(self, tmp_path, capsys):
        from friday_v4.cli_skills import cmd_skills_noticed
        _record_demo(_conn(tmp_path), rounds=3)
        assert cmd_skills_noticed(self._args(tmp_path)) == 0
        assert "every time" in capsys.readouterr().out

    def test_noticed_empty_message(self, tmp_path, capsys):
        from friday_v4.cli_skills import cmd_skills_noticed
        assert cmd_skills_noticed(self._args(tmp_path)) == 0
        assert "Nothing new" in capsys.readouterr().out


# ==========================================================================
# daemon — SkillLearner surfaces 'noticed' offers
# ==========================================================================


class TestSkillLearnerOffers:
    def test_sweep_once_reports_offers(self, tmp_path):
        from friday_v4.daemon import SkillLearner
        learner = SkillLearner(interval=3600.0,
                               db_path=tmp_path / "v4.db")
        _record_demo(_conn(tmp_path), rounds=3)
        learner.sweep_once()
        assert learner.last_report is not None
        assert "offers" in learner.last_report
        assert learner.last_report["offers"] >= 1
        assert learner.last_report["offer_lines"]

    def test_sweep_never_executes(self, tmp_path):
        from friday_v4.daemon import SkillLearner
        learner = SkillLearner(interval=3600.0,
                               db_path=tmp_path / "v4.db")
        _record_demo(_conn(tmp_path), rounds=3)
        before = db.list_skills(_conn(tmp_path))
        learner.sweep_once()
        after = db.list_skills(_conn(tmp_path))
        # Learning forms shadow skills (read-only on the world), and the
        # noticer offers do NOT form anything beyond the learned ones —
        # no executions ever happen.
        assert all(s["verification_state"] == STATE_SHADOW
                   for s in after)
        assert len(after) >= len(before)


# ==========================================================================
# Package-level — the layer exports the new Wave 14 pieces
# ==========================================================================


class TestWave14Exports:
    def test_skills_layer_exports_watch_me(self):
        from friday_v4.skills import (
            RepetitionNoticer,
            WatchRecorder,
            is_available,
        )
        assert is_available() is True
        assert WatchRecorder is not None
        assert RepetitionNoticer is not None

    def test_shadow_matches_repo_context(self, tmp_path):
        """Generalization: a repo-scoped step only matches in that repo."""
        from friday_v4.skills.shadow import _step_matches
        step = {"action_type": "testing", "command": "pytest -q",
                "repo": "friday_v4"}
        assert _step_matches(step, {"action_type": "testing",
                                    "command": "pytest -q",
                                    "cwd": "/home/me/friday_v4"})
        assert not _step_matches(step, {"action_type": "testing",
                                        "command": "pytest -q",
                                        "cwd": "/home/me/other_repo"})
        # Back-compat: steps without repo match anywhere.
        bare = {"action_type": "testing", "command": "pytest -q"}
        assert _step_matches(bare, {"action_type": "testing",
                                    "command": "pytest -q",
                                    "cwd": "/home/me/other_repo"})
