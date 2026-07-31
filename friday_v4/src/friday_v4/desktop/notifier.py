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

Usage:
    channel = DesktopNotificationChannel(min_priority=1, poll_interval=5.0)
    channel.start()        # background thread
    ...
    channel.stop()

CLI: ``friday desktop watch [--min-priority N] [--poll N]``
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Optional

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
        self._v3: Optional = None  # friday.ambient module (lazy)

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
        """Raise a desktop notification for an ambient event."""
        urgency = "critical" if (event.priority or 0) >= 3 else (
            "normal" if (event.priority or 0) >= 2 else "low"
        )
        title = event.title or event.event_type
        detail = event.detail or ""
        message = detail[:200] if detail else title[:200]
        if event.project:
            title = f"{event.project}: {title}"

        DesktopAbstraction.notify(title, message, urgency=urgency)
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
            time.sleep(self.poll_interval)
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
