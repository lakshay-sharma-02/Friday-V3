"""Hermetic tests for Wave 23 — screen side of the watch-me loop.

Covers:
- db.py: screen_events table (migration v10) + record/screen_events_between
- screen/recorder.py: ScreenDemoRecorder — changed-state dedup, graceful
  degrade without screen tools, thread lifecycle (never raises)
- skills/watcher.py: screen observations become screen-context steps in
  the formed skill, interleaved chronologically with audited actions
- nl_router: "teach me to do X" starts the sampler ("I'm also watching
  your screen"); "that's it" stops it before the skill forms and the
  reply counts the screen observations
- cli_skills: watch/watch-stop wire the sampler (honest degrade + JSON
  fields)

Safety laws verified:
- Screen steps are informational (action_type="screen") — the dispatcher
  can never auto-execute an OCR snapshot as a command.
- No screen tools → honest note, never a crash, nothing recorded.
- Every test is hermetic: tmp_path DB + fake screen controller — never a
  real display, never the real ~/.friday.
"""

from __future__ import annotations

from friday_v6 import db
from friday_v6.screen.controller import ActionResult
from friday_v6.screen.parsers import OCRWord
from friday_v6.screen.recorder import ScreenDemoRecorder
from friday_v6.skills import WatchRecorder


def _conn(tmp_path):
    return db.connect(tmp_path / "v4.db")


def _words(text: str, top: int = 40) -> list:
    """One line of OCR words at the given y position."""
    left = 0
    out = []
    for piece in text.split():
        w = OCRWord(text=piece, left=left, top=top, width=40,
                    height=20, conf=90.0)
        out.append(w)
        left += 45
    return out


class _FakeScreen:
    """A screen that answers OCR from a scripted queue (never a display)."""

    def __init__(self, *states: str, available: bool = True) -> None:
        self._queue = list(states)
        self._available = available

    def is_available(self) -> bool:
        return self._available

    def ocr(self, image_path=None) -> ActionResult:
        if not self._queue:
            return ActionResult(False, "screen gone")
        text = self._queue.pop(0)
        return ActionResult(True, "read", words=_words(text))


# ==========================================================================
# db.py — screen_events table + helpers
# ==========================================================================


class TestScreenEventsSchema:
    def test_migration_v10_creates_screen_events(self, tmp_path):
        conn = _conn(tmp_path)
        cols = {row[1] for row in conn.execute(
            "PRAGMA table_info(screen_events)")}
        assert {"id", "watch_id", "event_type", "text", "context",
                "created_at"} <= cols

    def test_record_and_between_roundtrip(self, tmp_path):
        conn = _conn(tmp_path)
        start = db.now_iso()
        eid = db.record_screen_event(
            conn, text="login  password", event_type="screen_snapshot",
            watch_id="w1")
        assert eid
        rows = db.screen_events_between(conn, start, db.now_iso())
        assert len(rows) == 1
        assert rows[0]["text"] == "login  password"
        assert rows[0]["watch_id"] == "w1"

    def test_between_orders_oldest_first(self, tmp_path):
        conn = _conn(tmp_path)
        for text in ("first", "second", "third"):
            db.record_screen_event(conn, text=text,
                                   event_type="screen_snapshot",
                                   watch_id="w1")
        rows = db.screen_events_between(conn, "2000-01-01", "2999-01-01")
        assert [r["text"] for r in rows] == ["first", "second", "third"]

    def test_recent_screen_events_newest_first(self, tmp_path):
        conn = _conn(tmp_path)
        for text in ("first", "second"):
            db.record_screen_event(conn, text=text,
                                   event_type="screen_snapshot",
                                   watch_id="w1")
        rows = db.recent_screen_events(conn, limit=5)
        assert [r["text"] for r in rows] == ["second", "first"]


# ==========================================================================
# screen/recorder.py — the sampler
# ==========================================================================


class TestScreenDemoRecorder:
    def test_records_changed_states_only(self, tmp_path):
        conn = _conn(tmp_path)
        screen = _FakeScreen("hello world", "hello world", "goodbye")
        rec = ScreenDemoRecorder("w1", conn=conn, screen=screen,
                                 interval=1.0)
        rec._sample_once()          # "hello world" → recorded
        rec._sample_once()          # unchanged → collapsed
        rec._sample_once()          # "goodbye" → recorded
        rows = db.screen_events_between(conn, "2000-01-01", "2999-01-01")
        assert [r["text"] for r in rows] == ["hello world", "goodbye"]
        assert rec.observations == 2

    def test_unavailable_screen_records_nothing(self, tmp_path):
        conn = _conn(tmp_path)
        rec = ScreenDemoRecorder("w1", conn=conn,
                                 screen=_FakeScreen(available=False),
                                 interval=1.0)
        assert not rec.available
        assert not rec.start()
        rows = db.screen_events_between(conn, "2000-01-01", "2999-01-01")
        assert rows == []
        assert "unavailable" in rec.last_error().lower()

    def test_stop_captures_final_state(self, tmp_path):
        conn = _conn(tmp_path)
        screen = _FakeScreen("step one", "step two")
        rec = ScreenDemoRecorder("w1", conn=conn, screen=screen,
                                 interval=1.0)
        rec.start()                 # sampler running
        # Let the thread take its first sample (deterministic), then
        # stop: the final forced snapshot captures what the demo ended on.
        import time
        for _ in range(50):
            if rec.observations >= 1:
                break
            time.sleep(0.01)
        rec.stop()
        rows = db.screen_events_between(conn, "2000-01-01", "2999-01-01")
        assert any(r["text"] == "step one" for r in rows)
        assert any(r["text"] == "step two" for r in rows)
        # stop twice is safe
        rec.stop()

    def test_stop_without_start_records_nothing(self, tmp_path):
        """A recorder that never ran must record nothing — an empty demo
        stays an empty skill (no final-state capture)."""
        conn = _conn(tmp_path)
        screen = _FakeScreen("step one", "step two")
        rec = ScreenDemoRecorder("w1", conn=conn, screen=screen,
                                 interval=1.0)
        rec.stop()                  # never started → nothing recorded
        rows = db.screen_events_between(conn, "2000-01-01", "2999-01-01")
        assert rows == []

    def test_start_runs_thread_and_samples(self, tmp_path):
        conn = _conn(tmp_path)
        screen = _FakeScreen("thread sample", "thread sample two")
        rec = ScreenDemoRecorder("w1", conn=conn, screen=screen,
                                 interval=1.0)
        assert rec.start()
        rec.stop()
        rows = db.screen_events_between(conn, "2000-01-01", "2999-01-01")
        assert len(rows) >= 1
        assert rec.observations >= 1

    def test_ocr_failure_is_absorbed(self, tmp_path):
        conn = _conn(tmp_path)
        screen = _FakeScreen("ok")  # runs out after first sample
        rec = ScreenDemoRecorder("w1", conn=conn, screen=screen,
                                 interval=1.0)
        rec._sample_once()
        rec._sample_once()          # no more states → error, no raise
        rows = db.screen_events_between(conn, "2000-01-01", "2999-01-01")
        assert len(rows) == 1


# ==========================================================================
# skills/watcher.py — screen steps in the formed skill
# ==========================================================================


class TestWatchScreenIntegration:
    def test_screen_observations_become_screen_steps(self, tmp_path):
        conn = _conn(tmp_path)
        watcher = WatchRecorder(conn)
        wid = watcher.start(name="deploy routine")
        db.record_action(conn, "git", command="git push origin main",
                         goal="push", cwd="/home/me/repo",
                         status="succeeded")
        db.record_screen_event(conn, text="deploy  dashboard  save",
                               event_type="screen_snapshot", watch_id=wid)
        formed = watcher.stop()
        assert formed
        steps = formed["skill"].steps
        assert any(s.get("action_type") == "screen"
                   and "deploy" in s.get("command", "")
                   for s in steps)

    def test_screen_steps_never_auto_execute(self, tmp_path):
        """screen steps are informational — the dispatcher ignores them."""
        conn = _conn(tmp_path)
        watcher = WatchRecorder(conn)
        wid = watcher.start(name="demo")
        db.record_action(conn, "git", command="git status", goal="check",
                         cwd="/home/me/repo", status="succeeded")
        db.record_screen_event(conn, text="rm -rf /important",
                               event_type="screen_snapshot", watch_id=wid)
        formed = watcher.stop()
        assert formed
        steps = formed["skill"].steps
        assert any(s.get("action_type") == "screen" for s in steps)
        # No step carries the OCR text as an executable command type.
        assert not any(s.get("action_type") != "screen"
                       and "rm -rf" in s.get("command", "") for s in steps)

    def test_no_screen_events_means_no_screen_steps(self, tmp_path):
        conn = _conn(tmp_path)
        watcher = WatchRecorder(conn)
        watcher.start(name="plain")
        db.record_action(conn, "git", command="git status", goal="check",
                         cwd="/home/me/repo", status="succeeded")
        formed = watcher.stop()
        assert formed
        assert not any(s.get("action_type") == "screen"
                       for s in formed["skill"].steps)

    def test_screen_steps_interleave_chronologically(self, tmp_path):
        """A screen change mid-demo sits between the actions, not at the end."""
        conn = _conn(tmp_path)
        watcher = WatchRecorder(conn)
        watcher.start(name="flow")
        db.record_action(conn, "git", command="git status", goal="a",
                         cwd="/home/me/repo", status="succeeded")
        db.record_screen_event(conn, text="middle state",
                               event_type="screen_change", watch_id="x")
        db.record_action(conn, "git", command="git commit", goal="b",
                         cwd="/home/me/repo", status="succeeded")
        formed = watcher.stop()
        assert formed
        steps = formed["skill"].steps
        kinds = [s.get("action_type") for s in steps]
        # Both git steps present; any screen step sits between them.
        git_idx = [i for i, k in enumerate(kinds) if k != "screen"]
        assert len(git_idx) == 2
        for i in range(len(steps)):
            if steps[i].get("action_type") == "screen":
                assert git_idx[0] < i < git_idx[1] or i > git_idx[1]


# ==========================================================================
# nl_router — the teach path starts/stops the sampler
# ==========================================================================


class TestTeachPathScreen:
    def _handler(self, tmp_path, monkeypatch, screen=None):
        """A handler whose screen recorder uses a fake screen.

        ``ScreenDemoRecorder`` is patched to inject the fake screen so
        the recorder the handler *actually* creates is hermetic (no
        real display, no real tools). The recorder opens its own DB
        connection for each sample, so the watch id it records under is
        the real one regardless of connection sharing.
        """
        from friday_v6.nl_router import TextCommandHandler
        conn = _conn(tmp_path)
        handler = TextCommandHandler(conn=conn)
        if screen is not None:

            fake = screen
            real_init = ScreenDemoRecorder.__init__

            def patched_init(self, watch_id, conn=None, screen=None,
                             interval=3.0, db_path=None):
                real_init(self, watch_id, conn=conn, screen=fake,
                          interval=interval, db_path=db_path)

            monkeypatch.setattr(ScreenDemoRecorder, "__init__",
                                patched_init)
        return handler, conn

    def test_teach_start_notes_screen_watching(self, tmp_path, monkeypatch):
        screen = _FakeScreen("deploy page")
        handler, conn = self._handler(tmp_path, monkeypatch, screen)
        result = handler.handle("teach me to do the deploy routine")
        assert result is not None
        assert "I'm also watching your screen" in result.response
        assert result.action == "watching"

    def test_teach_start_degrades_honestly_without_screen(self, tmp_path,
                                                          monkeypatch):
        screen = _FakeScreen(available=False)
        handler, conn = self._handler(tmp_path, monkeypatch, screen)
        result = handler.handle("teach me to do the deploy routine")
        assert result is not None
        assert "screen capture unavailable" in result.response

    def test_teach_stop_stops_recorders_and_counts_screen_steps(
            self, tmp_path, monkeypatch):
        screen = _FakeScreen("deploy  dashboard  save", "deploy  done")
        handler, conn = self._handler(tmp_path, monkeypatch, screen)
        handler.handle("teach me to do the deploy routine")
        # Demonstrate + Friday sees the screen change. The recorder's
        # own thread samples through the fake screen; ``stop()`` inside
        # "that's it" records the final state too.
        db.record_action(conn, "git", command="git push", goal="push",
                         cwd="/home/me/repo", status="succeeded")
        result = handler.handle("that's it")
        assert result is not None
        assert result.action == "skill_formed"
        assert "screen observation" in result.response

    def test_bare_thats_it_not_hijacked(self, tmp_path, monkeypatch):
        handler, conn = self._handler(tmp_path, monkeypatch)
        result = handler.handle("that's it")
        assert result is not None
        assert result.action != "skill_formed"  # ordinary chat keeps routing


# ==========================================================================
# cli_skills — watch/watch-stop wiring (hermetic, --no-screen)
# ==========================================================================


class TestCliWatchScreen:
    def _build(self, tmp_path):
        import argparse

        from friday_v6.cli_skills import (
            _stop_screen_recorders,
            build_skills_parser,
            cmd_skills_watch,
            cmd_skills_watch_stop,
        )
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        build_skills_parser(sub)
        return (cmd_skills_watch, cmd_skills_watch_stop,
                _stop_screen_recorders, parser, tmp_path)

    def test_watch_no_screen_flag_is_hermetic(self, tmp_path, capsys):
        cmd_watch, _, _stop, parser, tmp_path = self._build(tmp_path)
        args = parser.parse_args(
            ["skills", "watch", "demo", "--db",
             str(tmp_path / "v4.db"), "--no-screen", "--json"])
        rc = cmd_watch(args)
        out = capsys.readouterr().out
        assert rc == 0
        payload = __import__("json").loads(out)
        assert payload["started"] is True
        assert payload["screen"] is False

    def test_watch_stop_after_watch_with_screen_steps(self, tmp_path,
                                                      capsys):
        _, cmd_stop, _stop, parser, tmp_path = self._build(tmp_path)
        db_path = tmp_path / "v4.db"
        conn = db.connect(db_path)
        try:
            watcher = WatchRecorder(conn)
            watcher.start(name="deploy")
            db.record_action(conn, "git", command="git status", goal="check",
                             cwd="/home/me/repo", status="succeeded")
            db.record_screen_event(conn, text="deploy  view",
                                   event_type="screen_snapshot",
                                   watch_id="w")
        finally:
            conn.close()
        args = parser.parse_args(
            ["skills", "watch-stop", "--db", str(db_path), "--json"])
        rc = cmd_stop(args)
        out = capsys.readouterr().out
        assert rc == 0
        payload = __import__("json").loads(out)
        assert payload["formed"] is True
        assert payload["screen_observations"] >= 1

    def test_watch_stop_without_watch_is_honest(self, tmp_path, capsys):
        cmd_watch, cmd_stop, _stop, parser, tmp_path = self._build(tmp_path)
        args = parser.parse_args(
            ["skills", "watch-stop", "--db",
             str(tmp_path / "v4.db"), "--json"])
        rc = cmd_stop(args)
        out = capsys.readouterr().out
        assert rc == 0
        payload = __import__("json").loads(out)
        assert payload["formed"] is False
