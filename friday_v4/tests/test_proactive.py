"""Proactive engine tests — session memory, pattern learner, priority.

Unit-level tests that avoid touching the real ~/.friday/ directory
(monkeypatched to a tmp dir). Covers the SessionStore JSON round-trip
bug where repos_active is a set in memory but a list after reload.
"""

from __future__ import annotations

import time as _time
from unittest.mock import MagicMock, patch


class TestSessionStore:
    def _make_store(self, tmp_path, monkeypatch):
        """Build a SessionStore pointed at a tmp dir."""
        from friday_v4.proactive import session_memory as sm

        session_dir = tmp_path / "sessions"
        session_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(sm, "_SESSION_DIR", session_dir)
        monkeypatch.setattr(sm, "_SESSION_FILE", session_dir / "current.json")
        monkeypatch.setattr(sm, "_HISTORY_FILE", session_dir / "history.jsonl")
        return sm

    def test_update_activity_after_json_roundtrip(self, tmp_path, monkeypatch):
        """Regression: after a session is persisted and reloaded, repos_active
        is a list (JSON round-trip) — update_activity must not crash with
        'list' object has no attribute 'add'."""
        sm = self._make_store(tmp_path, monkeypatch)

        store = sm.SessionStore()
        store.start_session("kitty")
        store.update_activity(app_class="kitty", repo="repo1")
        store.end_session()  # archives + removes current.json

        # Fresh session, then reload from disk (list round-trip)
        store = sm.SessionStore()
        store.start_session("kitty")
        store.update_activity(app_class="kitty", repo="repo1")
        assert sm._SESSION_FILE.exists()

        # Reload the persisted session
        store2 = sm.SessionStore()
        assert store2._current is not None
        # Reloaded repos_active is normalized back to a set
        assert isinstance(store2._current["repos_active"], set)

        # The crash: adding a repo to a list-backed session
        store2.update_activity(app_class="kitty", repo="repo2")
        assert "repo2" in store2._current["repos_active"]
        assert "repo1" in store2._current["repos_active"]

    def test_end_session_with_list_repos_archives(self, tmp_path, monkeypatch):
        """end_session must serialize a loaded (list) repos_active fine."""
        sm = self._make_store(tmp_path, monkeypatch)

        store = sm.SessionStore()
        store.start_session("kitty")
        store.update_activity(app_class="kitty", repo="repo1")
        store.end_session()

        # Load the archived session back and end it again — no crash
        store2 = sm.SessionStore()
        store2.start_session("kitty")
        store2.end_session()
        assert sm._HISTORY_FILE.exists()

    def test_session_stats_survive_roundtrip(self, tmp_path, monkeypatch):
        """get_today_stats / get_weekly_stats work with archived sessions."""
        sm = self._make_store(tmp_path, monkeypatch)

        store = sm.SessionStore()
        store.start_session("kitty")
        store.update_activity(app_class="kitty", repo="repo1")
        store.end_session()

        store2 = sm.SessionStore()
        today = store2.get_today_stats()
        assert today["session_count"] >= 1
        week = store2.get_weekly_stats()
        assert week["total_sessions"] >= 1


class TestPatternLearner:
    def _make_learner(self, tmp_path, monkeypatch):
        """Build a PatternLearner pointed at a tmp dir."""
        from friday_v4.proactive import pattern_learner as pl

        patterns_dir = tmp_path / "patterns"
        patterns_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(pl, "_PATTERNS_DIR", patterns_dir)
        monkeypatch.setattr(pl, "_ACTION_PAIRS_FILE", patterns_dir / "action_pairs.json")
        monkeypatch.setattr(pl, "_APP_SEQUENCES_FILE", patterns_dir / "app_sequences.json")
        monkeypatch.setattr(pl, "_TIMING_PATTERNS_FILE", patterns_dir / "timing_patterns.json")
        return pl

    def test_get_last_action_returns_most_recent(self, tmp_path, monkeypatch):
        """get_last_action returns the newest observed action."""
        pl = self._make_learner(tmp_path, monkeypatch)
        learner = pl.PatternLearner()
        learner._recent_actions = [
            {"action": "edit_file", "context": {}, "timestamp": "t1"},
            {"action": "run_tests", "context": {}, "timestamp": "t2"},
        ]
        assert learner.get_last_action() == "run_tests"

    def test_get_last_action_empty_returns_none(self, tmp_path, monkeypatch):
        pl = self._make_learner(tmp_path, monkeypatch)
        learner = pl.PatternLearner()
        assert learner.get_last_action() is None


class TestRepoAwarePatterns:
    """Patterns must be learnable per-repo so suggestions can tie to
    projects ("in repo X you usually run tests after editing")."""

    def _make_learner(self, tmp_path, monkeypatch):
        from friday_v4.proactive import pattern_learner as pl

        patterns_dir = tmp_path / "patterns"
        patterns_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(pl, "_PATTERNS_DIR", patterns_dir)
        monkeypatch.setattr(pl, "_ACTION_PAIRS_FILE", patterns_dir / "action_pairs.json")
        monkeypatch.setattr(pl, "_APP_SEQUENCES_FILE", patterns_dir / "app_sequences.json")
        monkeypatch.setattr(pl, "_TIMING_PATTERNS_FILE", patterns_dir / "timing_patterns.json")
        return pl

    def test_per_repo_action_pairs_learned(self, tmp_path, monkeypatch):
        """edit_file → run_tests in repoA, but edit_file → git_commit in
        repoB — repo-scoped prediction must win per project."""
        pl = self._make_learner(tmp_path, monkeypatch)
        learner = pl.PatternLearner()

        learner.observe_action("edit_file", {"repo": "repoA"})
        learner.observe_action("run_tests", {"repo": "repoA"})
        learner.observe_action("edit_file", {"repo": "repoA"})
        learner.observe_action("run_tests", {"repo": "repoA"})

        learner.observe_action("edit_file", {"repo": "repoB"})
        learner.observe_action("git_commit", {"repo": "repoB"})

        assert learner.get_most_likely_next_action("edit_file", repo="repoA") == "run_tests"
        assert learner.get_most_likely_next_action("edit_file", repo="repoB") == "git_commit"
        # global fallback still works when no repo is given
        assert learner.get_most_likely_next_action("edit_file") == "run_tests"

    def test_per_repo_app_transitions(self, tmp_path, monkeypatch):
        """App transitions are also tracked per repo."""
        pl = self._make_learner(tmp_path, monkeypatch)
        learner = pl.PatternLearner()

        learner.observe_app_transition("code", "firefox", repo="repoA")
        learner.observe_app_transition("code", "firefox", repo="repoA")
        learner.observe_app_transition("code", "slack", repo="repoB")

        assert learner.get_most_likely_next_app("code", repo="repoA") == "firefox"
        assert learner.get_most_likely_next_app("code", repo="repoB") == "slack"

    def test_get_suggestions_repo_tied(self, tmp_path, monkeypatch):
        """Suggestions reference the project when repo context is given."""
        pl = self._make_learner(tmp_path, monkeypatch)
        learner = pl.PatternLearner()
        learner.observe_action("edit_file", {"repo": "repoA", "app": "code"})
        learner.observe_action("run_tests", {"repo": "repoA", "app": "code"})

        suggestions = learner.get_suggestions({
            "active_app_class": "code",
            "last_action": "edit_file",
            "active_repo": "repoA",
            "active_branch": "main",
        })
        joined = " ".join(suggestions).lower()
        assert "repoa" in joined  # suggestion is tied to the project
        assert "check test report" in joined  # next-action label rendered

    def test_get_suggestions_global_when_no_repo(self, tmp_path, monkeypatch):
        """Without repo context, suggestions stay global (no ' in X' clause)."""
        pl = self._make_learner(tmp_path, monkeypatch)
        learner = pl.PatternLearner()
        learner.observe_action("edit_file", {"repo": "repoA"})
        learner.observe_action("run_tests", {"repo": "repoA"})

        suggestions = learner.get_suggestions({
            "active_app_class": "code",
            "last_action": "edit_file",
        })
        joined = " ".join(suggestions).lower()
        assert "repoa" not in joined
        assert "check test report" in joined

    def test_stats_include_repos(self, tmp_path, monkeypatch):
        pl = self._make_learner(tmp_path, monkeypatch)
        learner = pl.PatternLearner()
        learner.observe_action("edit_file", {"repo": "repoA"})
        learner.observe_action("run_tests", {"repo": "repoA"})
        learner.observe_app_transition("code", "firefox", repo="repoB")

        stats = learner.get_stats()
        assert stats["repos_observed"] >= 2
        assert stats["repo_transitions_tracked"] >= 1


class TestAnticipationLastAction:
    def test_anticipation_passes_real_last_action(self, tmp_path, monkeypatch):
        """Regression: AnticipationEngine must pass the user's actual last
        observed action to the PatternLearner, not a hardcoded 'edit_file'."""
        from friday_v4.proactive import pattern_learner as pl
        from friday_v4.proactive import session_memory as sm
        from friday_v4.proactive.anticipation import AnticipationEngine

        patterns_dir = tmp_path / "patterns"
        patterns_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(pl, "_PATTERNS_DIR", patterns_dir)
        monkeypatch.setattr(pl, "_ACTION_PAIRS_FILE", patterns_dir / "action_pairs.json")
        monkeypatch.setattr(pl, "_APP_SEQUENCES_FILE", patterns_dir / "app_sequences.json")
        monkeypatch.setattr(pl, "_TIMING_PATTERNS_FILE", patterns_dir / "timing_patterns.json")

        session_dir = tmp_path / "sessions"
        session_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(sm, "_SESSION_DIR", session_dir)
        monkeypatch.setattr(sm, "_SESSION_FILE", session_dir / "current.json")
        monkeypatch.setattr(sm, "_HISTORY_FILE", session_dir / "history.jsonl")

        engine = AnticipationEngine()
        engine.observe_activity("edit_file", {"app": "kitty"})
        engine.observe_activity("run_tests", {"app": "kitty"})

        # Keep the test hermetic: stub the context engine so no real
        # desktop/git subprocesses run during get_suggestions().
        from friday_v4.proactive.context_engine import WorkContext
        engine.context_engine.get_context = lambda: WorkContext(
            active_app="Code", active_app_class="Code",
            session_minutes=0, recent_commits_week=0, dirty_repos=0,
        )
        engine.context_engine.get_proactive_suggestions = lambda: []

        captured = {}

        def spy(context):
            captured.update(context)
            return []

        with patch.object(engine.pattern_learner, "get_suggestions",
                          side_effect=spy):
            engine.get_suggestions(force=True)

        assert captured.get("last_action") == "run_tests"


class TestDesktopObserver:
    """The background observer must feed *real* desktop activity (app
    switches + focus actions) into the PatternLearner and session store."""

    def _isolate(self, tmp_path, monkeypatch):
        """Point pattern learner + session store at tmp dirs (hermetic)."""
        from friday_v4.proactive import pattern_learner as pl
        from friday_v4.proactive import session_memory as sm

        patterns_dir = tmp_path / "patterns"
        patterns_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(pl, "_PATTERNS_DIR", patterns_dir)
        monkeypatch.setattr(pl, "_ACTION_PAIRS_FILE", patterns_dir / "action_pairs.json")
        monkeypatch.setattr(pl, "_APP_SEQUENCES_FILE", patterns_dir / "app_sequences.json")
        monkeypatch.setattr(pl, "_TIMING_PATTERNS_FILE", patterns_dir / "timing_patterns.json")

        session_dir = tmp_path / "sessions"
        session_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(sm, "_SESSION_DIR", session_dir)
        monkeypatch.setattr(sm, "_SESSION_FILE", session_dir / "current.json")
        monkeypatch.setattr(sm, "_HISTORY_FILE", session_dir / "history.jsonl")

    def _make_fake_wm(self, apps):
        """A fake WindowManager-like object feeding a scripted app sequence
        to the DesktopWatcher (None once the sequence is exhausted)."""
        from friday_v4.desktop.wm_abstraction import WindowInfo

        state = {"i": 0}

        class FakeWM:
            is_available = True

            def get_active_window(self):
                i = state["i"]
                state["i"] += 1
                if i < len(apps):
                    return WindowInfo(window_id=str(i), app_class=apps[i],
                                      title="t")
                return None

            def get_active_workspace(self):
                return None

        return FakeWM()

    def test_observer_learns_app_transition(self, tmp_path, monkeypatch):
        """On a real app switch (kitty → firefox) the DesktopWatcher's
        on_app_change must drive transition learning instantly."""
        from friday_v4.proactive.anticipation import AnticipationEngine

        self._isolate(tmp_path, monkeypatch)
        engine = AnticipationEngine()

        class FakeContextEngine:
            def get_active_app(self):
                return ("kitty", "main.py")

            def get_active_repo(self):
                return ("", "")

            def cleanup(self):
                pass

        engine.context_engine = FakeContextEngine()

        # Watcher sequence: kitty (initial, primed) → firefox (switch event)
        wm = self._make_fake_wm(["kitty", "firefox"])
        engine.start_observer(interval_seconds=0.1, heartbeat_seconds=30.0,
                              wm=wm)
        _time.sleep(0.4)
        engine.stop_observer()

        stats = engine.pattern_learner.get_stats()
        assert stats["app_transitions_learned"] >= 1
        assert engine.pattern_learner.get_last_action() == "browsing"
        assert engine.session_store.get_today_stats()["session_count"] >= 1

    def test_observer_same_app_does_not_pollute_patterns(self, tmp_path, monkeypatch):
        """Staying in one app must not learn self-transitions or spam
        self-pairs — the watcher never fires and only the heartbeat runs."""
        from friday_v4.proactive.anticipation import AnticipationEngine

        self._isolate(tmp_path, monkeypatch)
        engine = AnticipationEngine()

        class FakeContextEngine:
            def get_active_app(self):
                return ("kitty", "main.py")

            def get_active_repo(self):
                return ("", "")

            def cleanup(self):
                pass

        engine.context_engine = FakeContextEngine()

        # The app never changes — the watcher must not fabricate events.
        wm = self._make_fake_wm(["kitty", "kitty", "kitty"])
        engine.start_observer(interval_seconds=0.1, heartbeat_seconds=0.5,
                              wm=wm)
        _time.sleep(0.7)  # several polls + one heartbeat
        engine.stop_observer()

        stats = engine.pattern_learner.get_stats()
        assert stats["app_transitions_learned"] == 0
        assert stats["action_pairs_learned"] == 0  # no coding→coding self-pairs
        assert engine.session_store.get_today_stats()["session_count"] >= 1

    def test_observer_records_repo_context(self, tmp_path, monkeypatch):
        """The initial sighting must attach the active repo/branch to the
        observed action and session."""
        from friday_v4.proactive.anticipation import AnticipationEngine

        self._isolate(tmp_path, monkeypatch)
        engine = AnticipationEngine()

        class FakeContextEngine:
            def get_active_app(self):
                return ("kitty", "main.py")

            def get_active_repo(self):
                return ("repoA", "main")

            def cleanup(self):
                pass

        engine.context_engine = FakeContextEngine()

        wm = self._make_fake_wm(["kitty"])
        engine.start_observer(interval_seconds=0.1, heartbeat_seconds=30.0,
                              wm=wm)
        _time.sleep(0.3)
        engine.stop_observer()

        # The first sighting recorded the repo + branch in the observation
        recent = engine.pattern_learner._recent_actions
        assert recent and recent[0]["context"].get("repo") == "repoA"
        assert recent[0]["context"].get("branch") == "main"
        # ...and the per-repo recent buffer was seeded
        assert "repoA" in engine.pattern_learner._recent_actions_by_repo
        # The session store also recorded the repo
        assert "repoA" in engine.session_store._current["repos_active"]

    def test_observer_is_event_driven_via_desktop_watcher(self, tmp_path, monkeypatch):
        """The engine must subscribe to a DesktopWatcher (not self-poll),
        and tear it down on stop."""
        from friday_v4.proactive.anticipation import AnticipationEngine

        self._isolate(tmp_path, monkeypatch)
        engine = AnticipationEngine()

        class FakeContextEngine:
            def get_active_app(self):
                return ("", "")

            def get_active_repo(self):
                return ("", "")

            def cleanup(self):
                pass

        engine.context_engine = FakeContextEngine()

        wm = self._make_fake_wm(["kitty"])
        engine.start_observer(interval_seconds=0.1, wm=wm)
        try:
            assert engine._watcher is not None
            # the switch callback is wired to the engine
            assert engine._watcher.on_app_change == engine._on_app_change
        finally:
            engine.stop_observer()
        assert engine._watcher is None
        assert engine._observer_thread is None

    def test_observer_initial_sighting_recorded_once(self, tmp_path, monkeypatch):
        """The initial app must be learned exactly once — _record_current_state
        records it, and the watcher's prime must NOT re-fire on_app_change for
        the same app (no double-record, no self-pair)."""
        from friday_v4.proactive.anticipation import AnticipationEngine

        self._isolate(tmp_path, monkeypatch)
        engine = AnticipationEngine()

        class FakeContextEngine:
            def get_active_app(self):
                return ("kitty", "main.py")

            def get_active_repo(self):
                return ("repoA", "main")

            def cleanup(self):
                pass

        engine.context_engine = FakeContextEngine()

        # Single app, never changes — only the initial sighting should learn.
        wm = self._make_fake_wm(["kitty"])
        engine.start_observer(interval_seconds=0.1, heartbeat_seconds=30.0,
                              wm=wm)
        _time.sleep(0.4)
        engine.stop_observer()

        assert len(engine.pattern_learner._recent_actions) == 1
        assert engine.pattern_learner._recent_actions[0]["action"] == "coding"
        assert engine.pattern_learner.get_stats()["app_transitions_learned"] == 0
        assert engine.pattern_learner.get_stats()["action_pairs_learned"] == 0

    def test_observer_restart_and_double_start(self, tmp_path, monkeypatch):
        """start→stop→start must work cleanly (state fully reset), and a
        second start while running must be a no-op (idempotent)."""
        from friday_v4.proactive.anticipation import AnticipationEngine

        self._isolate(tmp_path, monkeypatch)
        engine = AnticipationEngine()

        class FakeContextEngine:
            def get_active_app(self):
                return ("kitty", "main.py")

            def get_active_repo(self):
                return ("repoA", "main")

            def cleanup(self):
                pass

        engine.context_engine = FakeContextEngine()

        # First lifecycle: kitty → firefox transition is learned.
        wm1 = self._make_fake_wm(["kitty", "firefox"])
        engine.start_observer(interval_seconds=0.1, heartbeat_seconds=30.0,
                              wm=wm1)
        # Double-start while running must not spin up a second watcher.
        watcher1 = engine._watcher
        engine.start_observer(interval_seconds=0.1, wm=wm1)
        assert engine._watcher is watcher1
        _time.sleep(0.4)
        engine.stop_observer()
        assert engine._watcher is None
        assert engine._observer_thread is None
        assert engine.pattern_learner.get_stats()["app_transitions_learned"] >= 1

        # Second lifecycle after stop: a fresh transition is learned again,
        # proving _last_app/_last_repo were fully reset.
        wm2 = self._make_fake_wm(["kitty", "slack"])
        engine.start_observer(interval_seconds=0.1, heartbeat_seconds=30.0,
                              wm=wm2)
        _time.sleep(0.4)
        engine.stop_observer()
        stats = engine.pattern_learner.get_stats()
        assert stats["app_transitions_learned"] >= 2  # firefox + slack
        # repo context flows through both lifecycles (not just state survival)
        assert stats["repos_observed"] >= 1

    def test_get_active_repo_no_desktop(self):
        """get_active_repo returns ('', '') without a WindowManager."""
        from friday_v4.proactive.context_engine import DeepContextEngine

        engine = DeepContextEngine()
        with patch.object(type(engine), "_window_manager",
                          property(lambda self: None)):
            assert engine.get_active_repo() == ("", "")

    def test_get_active_repo_resolves_via_pid_and_caches(self):
        """get_active_repo uses the window PID's CWD and serves repeat calls
        from the per-PID cache (no re-resolution within the TTL)."""
        from friday_v4.proactive.context_engine import DeepContextEngine

        engine = DeepContextEngine()
        wm = MagicMock()
        wm.is_available = True
        active = MagicMock()
        active.pid = 12345
        wm.get_active_window.return_value = active

        wm_prop = property(lambda self: wm)
        with patch.object(type(engine), "_window_manager", wm_prop), \
             patch.object(type(engine), "_cwd_for_window",
                          staticmethod(lambda pid: "/proc/12345/cwd")), \
             patch.object(type(engine), "_resolve_repo",
                          staticmethod(lambda cwd: ("repoA", "main"))):
            assert engine.get_active_repo() == ("repoA", "main")

            # Second call within the TTL must hit the cache and NOT
            # re-resolve (patched _resolve_repo would return WRONG).
            with patch.object(type(engine), "_resolve_repo",
                              staticmethod(lambda cwd: ("WRONG", ""))):
                assert engine.get_active_repo() == ("repoA", "main")

    def test_get_active_app_no_desktop(self):
        """Without a WindowManager, get_active_app returns ('', '') and never
        spawns git subprocesses (desktop-only probe)."""
        from friday_v4.proactive.context_engine import DeepContextEngine

        engine = DeepContextEngine()
        with patch.object(type(engine), "_window_manager",
                          property(lambda self: None)):
            assert engine.get_active_app() == ("", "")

    def test_get_active_app_returns_window(self):
        """With a WindowManager, get_active_app returns (app_class, title)."""
        from friday_v4.proactive.context_engine import DeepContextEngine

        engine = DeepContextEngine()
        wm = MagicMock()
        wm.is_available = True
        active = MagicMock()
        active.app_class = "kitty"
        active.title = "main.py"
        wm.get_active_window.return_value = active
        with patch.object(type(engine), "_window_manager",
                          property(lambda self: wm)):
            assert engine.get_active_app() == ("kitty", "main.py")


class TestProactiveWatchSuggestion:
    """`friday4 proactive watch` must learn AND suggest standalone — the
    ProactiveSuggestionChannel shares the observer engine and is torn down
    on exit."""

    def test_watch_parser_has_suggestion_poll(self):
        """The watch subcommand exposes --suggestion-poll (default 120s) so
        the suggestion notification cadence is configurable standalone."""
        import argparse

        from friday_v4.cli_proactive import build_proactive_parser

        parser = argparse.ArgumentParser(prog="friday4")
        subparsers = parser.add_subparsers(dest="command")
        build_proactive_parser(subparsers)
        args = parser.parse_args(["proactive", "watch", "--suggestion-poll", "30",
                                  "--interval", "2"])
        assert args.suggestion_poll == 30.0
        assert args.interval == 2.0

    def test_watch_parser_default(self):
        import argparse

        from friday_v4.cli_proactive import build_proactive_parser

        parser = argparse.ArgumentParser(prog="friday4")
        subparsers = parser.add_subparsers(dest="command")
        build_proactive_parser(subparsers)
        args = parser.parse_args(["proactive", "watch"])
        assert args.suggestion_poll == 120.0
        assert args.interval == 1.0

    def test_cmd_watch_wires_suggestion_channel(self):
        """cmd_proactive_watch starts a ProactiveSuggestionChannel sharing the
        observer engine (caller-owned — channel must NOT clean it up), starts
        it daemon=True, and stops both on exit."""
        from friday_v4.cli_proactive import cmd_proactive_watch

        engine = MagicMock()
        channel = MagicMock()

        class Args:
            interval = 1.0
            suggestion_poll = 45.0

        # NOTE: cmd_proactive_watch does `from .proactive import AnticipationEngine`,
        # which resolves the attribute bound on the package by proactive/__init__.py
        # (`from .anticipation import AnticipationEngine`) — so the patch target is
        # the package attribute, not the submodule one.
        with patch("friday_v4.proactive.AnticipationEngine",
                   return_value=engine) as mock_engine_cls, \
             patch("friday_v4.desktop.notifier.ProactiveSuggestionChannel",
                   return_value=channel) as mock_channel_cls, \
             patch("friday_v4.cli_proactive.time.sleep",
                   side_effect=KeyboardInterrupt):
            result = cmd_proactive_watch(Args())

        assert result == 0
        mock_engine_cls.assert_called_once_with()
        engine.start_observer.assert_called_once_with(interval_seconds=1.0)
        # The channel shares the caller's engine and uses the configured poll.
        mock_channel_cls.assert_called_once_with(
            engine=engine, poll_interval=45.0)
        channel.start.assert_called_once_with(daemon=True)
        # Clean shutdown: channel first, then observer + session.
        channel.stop.assert_called_once()
        engine.stop_observer.assert_called_once()
        engine.cleanup.assert_called_once()
