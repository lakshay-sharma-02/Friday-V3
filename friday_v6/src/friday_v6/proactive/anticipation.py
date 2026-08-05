"""Anticipation Engine — FRIDAY's ability to anticipate your needs.

The crown jewel of proactive intelligence. Combines:
  - DeepContextEngine (what you're doing right now)
  - PatternLearner (what you typically do in this situation)
  - SessionStore (what you've done recently)
  - PriorityInference (is this worth interrupting for?)

To produce high-quality, contextual suggestions delivered at the right time.

Usage:
    engine = AnticipationEngine()
    suggestions = engine.get_suggestions()
    for s in suggestions:
        if s.should_speak:
            pipeline.speak(s.text)
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:  # pragma: no cover - import-time only
    pass

from .context_engine import DeepContextEngine
from .pattern_learner import PatternLearner
from .priority import PrioritizedItem, PriorityInference
from .session_memory import SessionStore

logger = logging.getLogger("friday_v6.proactive.anticipation")


class AnticipationEngine:
    """Generates proactive suggestions by combining context + patterns + priority.

    The main loop (called periodically or on relevant events):
      1. Get current context (desktop, time, git)
      2. Get learned patterns (action pairs, app transitions, timing)
      3. Generate suggestions from both sources
      4. Prioritize each suggestion based on focus level and fatigue
      5. Return: speak now, notify, queue, or suppress
    """

    def __init__(self, data_source=None, db_path=None):
        """``data_source``: optional V3DataSource-like object for enriching
        briefings with V3's workspace data. Built lazily when None (and
        only used if V3's DB is available).

        ``db_path``: optional V4 state DB path. When set, the desktop
        observer persists app-switch events into ``desktop_events`` so
        ``WatchRecorder`` ("watch me") can capture app opens as skill
        steps — the daemon passes its state DB so the always-on presence
        records everything itself (no CLI needed).
        """
        self.session_store = SessionStore()
        self.context_engine = DeepContextEngine(session_store=self.session_store)
        self.pattern_learner = PatternLearner()
        self.priority = PriorityInference()
        self._data_source = data_source
        self._db_path = db_path
        self._v3_digest: Optional[str] = None
        self._v3_digest_time: float = 0.0
        # Event-driven observer state (DesktopWatcher callbacks + heartbeat).
        self._observer_thread: Optional[threading.Thread] = None
        self._observer_stop: Optional[threading.Event] = None
        self._watcher: Optional = None  # DesktopWatcher (lazy import)
        self._last_app: Optional[str] = None
        self._last_action: Optional[str] = None
        self._last_repo: Optional[str] = None

    @property
    def data_source(self):
        """Lazily-built V3DataSource (V3 workspace data bridge)."""
        if self._data_source is None:
            try:
                from .v3source import V3DataSource
                self._data_source = V3DataSource()
            except Exception as exc:
                logger.debug(f"V3 data source init failed: {exc}")
                self._data_source = False
        return self._data_source

    def v3_digest(self, hours: float = 24.0) -> str:
        """Cached natural-language digest of V3's workspace view.

        Refreshed at most once per hour so briefings don't re-query V3's
        DB on every call. Returns '' when V3 is unavailable.
        """
        now = time.time()
        if self._v3_digest is not None and \
                now - self._v3_digest_time < 3600:
            return self._v3_digest
        digest = ""
        source = self.data_source
        if source:
            try:
                digest = source.workspace_digest(hours=hours) \
                    if hasattr(source, "workspace_digest") else ""
            except Exception as exc:
                logger.debug(f"V3 digest failed: {exc}")
        self._v3_digest = digest
        self._v3_digest_time = now
        return digest

    def get_suggestions(self, force: bool = False) -> list[PrioritizedItem]:
        """Get prioritized proactive suggestions for the current context.

        Args:
            force: If True, skip suppression and return all suggestions.
                   Used for `friday6 proactive suggest` command.

        Returns:
            List of PrioritizedItem objects sorted by priority.
        """
        all_items: list[PrioritizedItem] = []

        # 1. Get current context
        context = self.context_engine.get_context()

        # 2. Context-based suggestions
        for suggestion_text in self.context_engine.get_proactive_suggestions():
            item = PrioritizedItem(
                text=suggestion_text,
                category="suggestion",
                priority_score=50,
                urgency="soon",
                context=context.describe(),
                source="context",
            )
            all_items.append(item)

        # 3. Pattern-based suggestions (if we have learned patterns).
        #    Use the user's actual last observed action, not a hardcoded
        #    placeholder, so predictions reflect the real workflow.
        last_action = self.pattern_learner.get_last_action()
        pattern_suggestions = self.pattern_learner.get_suggestions({
            "active_app_class": context.active_app_class,
            "last_action": last_action or "",
            "active_repo": context.active_repo,
            "active_branch": context.active_branch,
        })
        for suggestion_text in pattern_suggestions:
            item = PrioritizedItem(
                text=suggestion_text,
                category="suggestion",
                priority_score=45,
                urgency="when_free",
                context=f"Pattern match for {context.active_app}",
                source="pattern",
            )
            all_items.append(item)

        # 4. Session-based suggestions
        if context.session_minutes > 30 and context.recent_commits_week > 0:
            all_items.append(PrioritizedItem(
                text=f"You've been working for {context.session_minutes} minutes. "
                     f"Would you like me to stage and commit your changes?",
                category="suggestion",
                priority_score=40,
                urgency="when_free",
                context=f"{context.session_minutes} min session, {context.dirty_repos} dirty repos",
                source="session",
            ))

        # 5. Evaluate and prioritize each item
        evaluated = []
        for item in all_items:
            evaluated_item = self.priority.evaluate(
                item,
                focus_level=context.focus_level,
                time_of_day=context.time_of_day,
            )
            evaluated.append(evaluated_item)

        # 6. Sort by priority score
        evaluated.sort(key=lambda x: x.priority_score, reverse=True)

        return evaluated

    def observe_activity(self, action_type: str, context: Optional[dict] = None):
        """Observe user activity to feed the pattern learner.

        Call this when:
          - User switches apps -> observe_activity("app_switch", {"app": "kitty"})
          - User edits a file -> observe_activity("edit_file", {"file": "main.py"})
          - User runs tests -> observe_activity("run_tests")
          - User commits -> observe_activity("git_commit")
        """
        if context is None:
            context = {}

        # Update session
        app_class = context.get("app", "")
        repo = context.get("repo", "")
        self.session_store.update_activity(app_class=app_class, repo=repo)

        # Learn pattern
        self.pattern_learner.observe_action(action_type, context)

        # Learn timing
        self.pattern_learner.observe_timing(action_type)

    def observe_app_switch(self, from_app: str, to_app: str,
                           repo: Optional[str] = None):
        """Observe app switches for sequence learning.

        ``repo`` optionally ties the transition to the project being worked
        on, so app-switch suggestions can be project-aware.
        """
        self.pattern_learner.observe_app_transition(from_app, to_app, repo=repo)

    def _record_desktop_event(self, app_class: str, repo: Optional[str] = None,
                              title: str = "") -> None:
        """Persist an app-switch/desktop event for the watch bridge.

        The always-on presence records what the operator opened so an
        active "watch me" capture can learn app opens (Brave → YouTube →
        VSCode) as skill steps. Never raises and never blocks: a missing
        or unwritable DB is skipped silently (the daemon law).
        """
        if not self._db_path:
            return
        try:
            from .. import db
            conn = db.connect(path=self._db_path)
            try:
                db.record_desktop_event(
                    conn, event_type="app_switch", app=app_class,
                    title=title, repo=repo or "")
            finally:
                conn.close()
        except Exception as exc:  # defensive — never crash
            logger.debug(f"desktop event record failed: {exc}")

    # ── Desktop observer ───────────────────────────────────────────────

    def start_observer(self, interval_seconds: float = 1.0,
                       heartbeat_seconds: float = 30.0,
                       wm: Optional[Any] = None) -> None:
        """Start an *event-driven* observer that feeds real desktop activity
        into the PatternLearner.

        Instead of polling the active window itself, the engine subscribes
        to a DesktopWatcher whose ``on_app_change`` callback fires the
        moment the user switches apps — learning reacts immediately, with
        no polling delay:
          - app switches → recorded as transitions + work-mode actions
          - a slow heartbeat keeps session_minutes live while the user
            stays in the same app (the watcher only fires on *changes*)

        Args:
            interval_seconds: Watcher change-detection poll interval — the
                upper bound on how quickly a switch is noticed (default 1s).
            heartbeat_seconds: How often to refresh the session store while
                the active app is unchanged (default 30s).
            wm: Optional WindowManager-like object to drive the watcher
                (defaults to auto-detection). Primarily for tests.

        Stop with stop_observer() or engine.cleanup().
        """
        if self._observer_thread and self._observer_thread.is_alive():
            return
        interval_seconds = max(interval_seconds, 0.1)
        # Floor prevents hot-looping session writes while staying testable.
        heartbeat_seconds = max(heartbeat_seconds, 0.5)

        self._observer_stop = threading.Event()
        self._last_app = None
        self._last_action = None
        self._last_repo = None

        # The watcher only fires on *changes*, so learn the app the user is
        # already in right now (also seeds last_app/last_action/last_repo).
        self._record_current_state()

        # Event-driven switch detection.
        from ..desktop.watcher import DesktopWatcher
        self._watcher = DesktopWatcher(
            wm=wm,
            poll_interval=interval_seconds,
            on_app_change=self._on_app_change,
        )
        self._watcher.start()

        # Slow heartbeat keeps the session warm between app changes.
        self._observer_thread = threading.Thread(
            target=self._heartbeat_loop,
            args=(heartbeat_seconds,),
            name="friday-proactive-observer",
            daemon=True,
        )
        self._observer_thread.start()

    def stop_observer(self) -> None:
        """Stop the observer — the DesktopWatcher and the heartbeat thread."""
        if self._observer_stop:
            self._observer_stop.set()
        if self._watcher is not None:
            try:
                self._watcher.stop()
            except Exception:
                pass
            self._watcher = None
        if self._observer_thread and self._observer_thread.is_alive():
            self._observer_thread.join(timeout=3)
        self._observer_thread = None
        self._last_app = None
        self._last_action = None
        self._last_repo = None

    @staticmethod
    def _action_for_app(app_class: str) -> str:
        """Map an app class to a coarse action label for pattern learning."""
        a = (app_class or "").lower()
        if any(e in a for e in ("code", "kitty", "vim", "nvim",
                                "jetbrains", "zcode", "sublime")):
            return "coding"
        if any(b in a for b in ("firefox", "chromium", "brave",
                                "google-chrome", "zen", "webkit")):
            return "browsing"
        if any(c in a for c in ("slack", "discord", "telegram", "whatsapp")):
            return "communicating"
        if any(n in a for n in ("obsidian", "logseq", "notion", "word")):
            return "writing"
        return "focus"

    def _record_current_state(self) -> None:
        """Learn the app the user is currently in (the watcher only fires
        on *changes*, so the initial state must be recorded explicitly)."""
        try:
            app_class, _title = self.context_engine.get_active_app()
            if not app_class:
                return
            action = self._action_for_app(app_class)
            repo, branch = self._probe_repo()
            self.observe_activity(
                action, {"app": app_class, "repo": repo, "branch": branch})
            self._last_app = app_class
            self._last_action = action
            self._last_repo = repo or ""
        except Exception as exc:
            logger.debug(f"Initial desktop sighting failed: {exc}")

    def _on_app_change(self, app_class: str) -> None:
        """DesktopWatcher callback — fired the moment the active app changes.

        Runs on the watcher's background thread; all learner/session calls
        are lock-protected, so this is safe cross-thread.
        """
        if self._observer_stop is None or self._observer_stop.is_set():
            return
        try:
            if self._last_app is not None and self._last_app != app_class:
                # Real desktop event: the user switched apps.
                self.observe_app_switch(self._last_app, app_class,
                                        repo=self._last_repo)
                # Persist the switch so "watch me" can capture app opens
                # as skill material (the desktop-observer bridge). Never
                # blocks or raises — the DB may be absent/unwritable.
                self._record_desktop_event(app_class, repo=self._last_repo)
            action = self._action_for_app(app_class)
            if action != self._last_action:
                # Only feed a *new* action so we don't learn meaningless
                # self-pairs (coding→coding).
                repo, branch = self._probe_repo()
                self.observe_activity(
                    action, {"app": app_class,
                             "from": self._last_app or "",
                             "repo": repo, "branch": branch})
                self._last_action = action
                self._last_repo = repo or self._last_repo or ""
            self._last_app = app_class
        except Exception as exc:
            logger.debug(f"App-change observation failed: {exc}")

    def _heartbeat_loop(self, heartbeat_seconds: float) -> None:
        """Refresh the session store while the user stays in the same app."""
        stop = self._observer_stop
        if stop is None:
            return
        while not stop.is_set():
            if stop.wait(heartbeat_seconds):
                return
            try:
                if self._last_app:
                    self.session_store.update_activity(
                        app_class=self._last_app, repo=self._last_repo or "")
            except Exception as exc:
                logger.debug(f"Session heartbeat failed: {exc}")

    def _probe_repo(self) -> tuple[str, str]:
        """Resolve the active window's repo/branch (best-effort).

        Uses the context engine's ``get_active_repo`` when available so
        pattern learning can tie suggestions to the current project.
        """
        probe = getattr(self.context_engine, "get_active_repo", None)
        if callable(probe):
            try:
                return probe()
            except Exception as exc:
                logger.debug(f"Repo probe failed: {exc}")
        return ("", "")

    def get_context_summary(self) -> str:
        """Get a spoken summary of what FRIDAY knows about your context."""
        ctx = self.context_engine.get_context()

        if not ctx.active_app:
            return "I can't see your desktop right now."

        parts = []
        if ctx.active_app:
            parts.append(f"You're in {ctx.active_app}")

        if ctx.work_mode:
            parts.append(f"in {ctx.work_mode} mode")

        if ctx.active_repo:
            parts.append(f"working on {ctx.active_repo}")

        if ctx.session_minutes > 0:
            parts.append(f"session: {ctx.session_minutes} minutes")

        # Add pattern insight if available
        stats = self.pattern_learner.get_stats()
        if stats["action_pairs_learned"] > 5:
            parts.append(f"I've learned {stats['action_pairs_learned']} patterns from your work")

        # Add V3 workspace digest when the V3 DB is present
        digest = self.v3_digest()
        if digest:
            parts.append(digest)

        return ". ".join(parts) + "."

    def get_learning_stats(self) -> dict:
        """Get statistics about what FRIDAY has learned."""
        return {
            "patterns": self.pattern_learner.get_stats(),
            "sessions_today": self.session_store.get_today_stats(),
            "sessions_this_week": self.session_store.get_weekly_stats(),
        }

    def cleanup(self):
        """End the current session and clean up resources."""
        self.stop_observer()
        self.session_store.end_session()
        self.context_engine.cleanup()
