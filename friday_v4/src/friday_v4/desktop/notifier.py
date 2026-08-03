"""Desktop notification channel — V3 ambient feed → desktop overlay.

This is the Wave 2 "Desktop notification channel (V3 ambient → desktop
overlay)" deliverable from PLAN.md Phase 2. It bridges Friday V3's
structured ambient event feed (``friday.ambient``) into desktop
notifications so Friday surfaces what matters *proactively*.

Design:
- Polls the V3 ambient feed for new events above a priority threshold.
- Remembers the last-seen event id so each event is notified exactly once.
- Leaves the feed itself untouched (dismissal is left to the operator).
- Degrades gracefully if V3 isn't installed/importable.

Also provides :class:`ProactiveSuggestionChannel`, which polls the V4
proactive engine (``AnticipationEngine.get_suggestions``) and raises a
desktop notification for each suggestion that passed priority filtering —
so learned patterns surface as notifications while the daemon runs.

Usage:
    channel = DesktopNotificationChannel(min_priority=1, poll_interval=5.0)
    channel.start()        # background thread
    ...
    channel.stop()

CLI: ``friday4 desktop watch [--min-priority N] [--poll N]``
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from .wm_abstraction import DesktopAbstraction

logger = logging.getLogger("friday_v4.desktop.notifier")

_STATE_DIR = Path.home() / ".friday"
_STATE_FILE = _STATE_DIR / "v4_notifier_state.json"

# Event types that should never be pushed as desktop notifications
_SILENT_TYPES = {
    "cycle_complete",
    "cycle_failed",  # noisy; surfaced elsewhere
}


class DesktopNotificationChannel:
    """Polls the V3 ambient feed and raises desktop notifications.

    Attributes:
        min_priority: Only notify events with priority >= this (0-3).
        poll_interval: Seconds between feed polls.
        last_event_id: The most recently seen ambient event id.
        running: Whether the polling loop is active.
    """

    def __init__(
        self,
        min_priority: int = 1,
        poll_interval: float = 10.0,
        state_file: Optional[Path] = None,
    ):
        self.min_priority = min_priority
        self.poll_interval = poll_interval
        self._state_file = state_file or _STATE_FILE
        self.last_event_id: int = self._load_last_id()
        self.running = False
        self._thread: Optional[threading.Thread] = None
        self._v3: Optional[dict] = None  # friday.ambient module (lazy)

    # ── State persistence ─────────────────────────────────────────

    def _load_last_id(self) -> int:
        try:
            if self._state_file.exists():
                data = json.loads(self._state_file.read_text())
                return int(data.get("last_event_id", 0) or 0)
        except (json.JSONDecodeError, OSError, ValueError):
            pass
        return 0

    def _save_last_id(self) -> None:
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            self._state_file.write_text(
                json.dumps({"last_event_id": self.last_event_id})
            )
        except OSError:
            pass

    # ── V3 connectivity ───────────────────────────────────────────

    def _load_v3(self):
        """Lazily import V3 ambient + db modules."""
        if self._v3 is not None:
            return self._v3
        try:
            import friday.ambient as ambient  # type: ignore
            from friday.db import connect  # type: ignore
            self._v3 = {"ambient": ambient, "connect": connect}
        except ImportError:
            logger.info("V3 not installed — ambient notification channel inactive")
            self._v3 = False
        return self._v3

    # ── Polling ───────────────────────────────────────────────────

    def poll_once(self) -> int:
        """Poll the feed once and notify for new events.

        Returns:
            Number of notifications raised.
        """
        v3 = self._load_v3()
        if not v3:
            return 0

        try:
            conn = v3["connect"]()
        except Exception as exc:
            logger.debug(f"Could not open V3 DB: {exc}")
            return 0

        notified = 0
        try:
            feed = v3["ambient"].get_feed(
                conn, limit=50, include_dismissed=False
            )
            # get_feed returns newest-first; iterate oldest-first
            new_events = [e for e in reversed(feed)
                          if e.id > self.last_event_id
                          and e.event_type not in _SILENT_TYPES
                          and int(e.priority or 0) >= self.min_priority]

            for event in new_events:
                self._notify_event(event)
                notified += 1

            if feed:
                newest_id = max(e.id for e in feed)
                if newest_id > self.last_event_id:
                    self.last_event_id = newest_id
                    self._save_last_id()
        except Exception as exc:
            logger.debug(f"Ambient poll failed: {exc}")
        finally:
            try:
                conn.close()
            except Exception:
                pass

        return notified

    def _notify_event(self, event) -> None:
        """Raise a desktop notification for an ambient event.

        Uses normal urgency with a priority-scaled timeout so banners
        auto-dismiss — critical urgency is persistent on GNOME and other
        desktops, which made notifications stick around indefinitely.
        """
        priority = int(event.priority or 0)
        # Higher-priority events stay on screen a little longer, but every
        # event still fades on its own.
        timeout_ms = {3: 12000, 2: 8000}.get(priority, 5000)
        title = event.title or event.event_type
        detail = event.detail or ""
        message = detail[:200] if detail else title[:200]
        if event.project:
            title = f"{event.project}: {title}"

        DesktopAbstraction.notify(title, message, urgency="normal",
                                  timeout_ms=timeout_ms)
        logger.info(f"Desktop notification: {title}")

    # ── Lifecycle ─────────────────────────────────────────────────

    def start(self, daemon: bool = True) -> bool:
        """Start the polling loop.

        Args:
            daemon: If True, run in a background thread and return.
                If False, block (call stop() from another thread).

        Returns:
            True if the loop started.
        """
        if self.running:
            return True
        self.running = True
        if daemon:
            self._thread = threading.Thread(
                target=self._run_loop, name="friday-notifier", daemon=True,
            )
            self._thread.start()
        else:
            self._run_loop()
        return True

    def _run_loop(self) -> None:
        """The background polling loop."""
        while self.running:
            try:
                self.poll_once()
            except Exception:
                logger.debug("Notifier poll error", exc_info=True)
            # Short waits with running-check keep stop() responsive even
            # for long poll intervals.
            for _ in range(max(int(self.poll_interval / 0.5), 1)):
                if not self.running:
                    return
                time.sleep(0.5)
        logger.info("Desktop notification channel stopped")

    def stop(self) -> None:
        """Stop the polling loop."""
        self.running = False
        if self._thread is not None:
            self._thread.join(timeout=max(self.poll_interval + 1, 3))
            self._thread = None

    def __repr__(self) -> str:
        return (f"<DesktopNotificationChannel min_priority={self.min_priority} "
                f"last_event_id={self.last_event_id} running={self.running}>")


class ProactiveSuggestionChannel:
    """Polls the V4 proactive engine and raises desktop notifications.

    Bridges ``AnticipationEngine.get_suggestions()`` into the desktop
    notification channel, so pattern-learned suggestions ("Whenever
    you're in Code you switch to Firefox") pop up as notifications while
    the daemon runs.

    Design:
    - Polls the engine for suggestions that passed priority filtering
      (``should_notify`` set by :class:`PriorityInference`).
    - Remembers each notified suggestion text so a suggestion is raised
      at most once per ``cooldown_seconds`` — no notification spam.
    - Accepts an optional injected engine (the daemon shares the same
      AnticipationEngine as the proactive observer so patterns are warm).
    - Degrades gracefully if the engine isn't importable/available.

    Usage:
        channel = ProactiveSuggestionChannel(engine=engine, poll_interval=120.0)
        channel.start()        # background thread
        ...
        channel.stop()
    """

    def __init__(
        self,
        engine: Optional[Any] = None,
        poll_interval: float = 120.0,
        cooldown_seconds: float = 3600.0,
        notify: Optional[Callable[..., bool]] = None,
        bus=None,
    ):
        self.engine = engine  # AnticipationEngine (lazy if None)
        self.poll_interval = poll_interval
        self.cooldown_seconds = cooldown_seconds
        self._notify = notify or DesktopAbstraction.notify
        self._bus = bus  # shared Wave 11 AmbientBus (optional)
        self.running = False
        self._thread: Optional[threading.Thread] = None
        # Only clean up engines we built ourselves — an injected engine
        # (e.g. the daemon's shared observer) stays owned by its caller.
        self._owns_engine = engine is None
        # suggestion text -> epoch of last notification (cooldown dedup)
        self._notified: dict[str, float] = {}

    # ── Engine ─────────────────────────────────────────────────────

    def _get_engine(self):
        """Lazily build an AnticipationEngine if none was injected."""
        if self.engine is None:
            try:
                from ..proactive.anticipation import AnticipationEngine
                self.engine = AnticipationEngine()
                self._owns_engine = True
            except Exception as exc:
                logger.debug(f"Proactive engine unavailable: {exc}")
                self.engine = False
        return self.engine

    def _cleanup_engine(self) -> None:
        """End the engine's session on stop (only if we created it).

        An injected engine belongs to its caller (e.g. the daemon's shared
        observer), so it is never cleaned up here — otherwise the daemon's
        own shutdown would end the session twice.
        """
        if (self._owns_engine and self.engine
                and self.engine is not False
                and hasattr(self.engine, "cleanup")):
            try:
                self.engine.cleanup()
            except Exception:
                pass

    # ── Polling ───────────────────────────────────────────────────

    def poll_once(self) -> int:
        """Poll for suggestions once and notify anything new above threshold.

        Returns:
            Number of notifications raised.
        """
        engine = self._get_engine()
        if not engine:
            return 0

        try:
            suggestions = engine.get_suggestions()
        except Exception as exc:
            logger.debug(f"Proactive suggestion poll failed: {exc}")
            return 0

        notified = 0
        now = time.time()
        for item in suggestions:
            # Only surface items the priority engine flagged as notify-worthy.
            if not getattr(item, "should_notify", False):
                continue
            text = (getattr(item, "text", "") or "").strip()
            if not text:
                continue
            # Cooldown: don't re-notify the same suggestion too often.
            last = self._notified.get(text)
            if last is not None and (now - last) < self.cooldown_seconds:
                continue
            self._raise_notification(item, text)
            self._notified[text] = now
            notified += 1

        self._prune_notified(now)
        return notified

    def _raise_notification(self, item, text: str) -> None:
        """Raise a desktop notification for a suggestion item.

        Always normal urgency with a bounded timeout so the banner fades
        (critical banners are persistent on GNOME and never auto-dismiss).
        Speak-worthy items stay up a little longer. When a shared Wave 11
        bus is wired, the suggestion is ALSO published durably so the web
        dashboard / voice / briefing surfaces get it (push, not just the
        desktop banner).
        """
        timeout_ms = 12000 if getattr(item, "should_speak", False) else 10000
        title = "Friday · Suggestion"
        if getattr(item, "source", None):
            title = f"Friday · {str(item.source).capitalize()}"
        message = text[:200]
        try:
            self._notify(title, message, urgency="normal",
                         timeout_ms=timeout_ms)
            logger.info(f"Suggestion notification: {title} — {text[:60]}")
        except Exception as exc:
            logger.debug(f"Suggestion notification failed: {exc}")
        if self._bus is not None:
            try:
                from ..ambient import Event, Priority
                prio = Priority.IMPORTANT \
                    if getattr(item, "should_speak", False) else Priority.ROUTINE
                self._bus.publish(Event(
                    topic="suggestion",
                    payload=text[:200],
                    priority=prio,
                    source="daemon.suggestions"))
            except Exception as exc:
                logger.debug(f"Suggestion bus publish failed: {exc}")

    def _prune_notified(self, now: float) -> None:
        """Drop cooldown entries older than the window (bounds memory)."""
        stale = [t for t, ts in self._notified.items()
                 if (now - ts) >= self.cooldown_seconds]
        for t in stale:
            self._notified.pop(t, None)

    # ── Lifecycle ─────────────────────────────────────────────────

    def start(self, daemon: bool = True) -> bool:
        """Start the polling loop (background thread unless ``daemon=False``)."""
        if self.running:
            return True
        self.running = True
        if daemon:
            self._thread = threading.Thread(
                target=self._run_loop, name="friday-suggestions", daemon=True,
            )
            self._thread.start()
        else:
            self._run_loop()
        return True

    def _run_loop(self) -> None:
        """The background polling loop."""
        while self.running:
            try:
                self.poll_once()
            except Exception:
                logger.debug("Suggestion poll error", exc_info=True)
            # Short waits with running-check keep stop() responsive even
            # for long poll intervals.
            for _ in range(max(int(self.poll_interval / 0.5), 1)):
                if not self.running:
                    return
                time.sleep(0.5)
        logger.info("Proactive suggestion channel stopped")

    def stop(self) -> None:
        """Stop the polling loop and clean up any engine we created."""
        self.running = False
        if self._thread is not None:
            self._thread.join(timeout=max(self.poll_interval + 1, 3))
            self._thread = None
        self._cleanup_engine()

    def __repr__(self) -> str:
        return (f"<ProactiveSuggestionChannel engine={'set' if self.engine else 'none'} "
                f"poll_interval={self.poll_interval} running={self.running}>")
