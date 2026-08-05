"""ScreenDemoRecorder — what Friday *sees* during a watch (Wave 23).

The watch-me loop ("teach me to do X") captures the operator's
audited commands; ``ScreenDemoRecorder`` adds the *screen* side: while
a watch is open, it periodically screenshots + OCRs the screen and
stores what it saw (``screen_events`` rows tagged with the watch id).
At skill formation, ``WatchRecorder`` turns those observations into
screen-context steps (informational — never auto-executed), so a
demonstration captures the on-screen flow, not just the commands.

Design laws:

- **Additive + never-crash.** No screen tools / no OCR → the recorder
  degrades to ``screen_unavailable`` and stores nothing. A sampling
  error is logged, never raised.
- **No spam.** Only *changed* screen states are recorded (identical
  OCR snapshots collapse), so a busy demo yields a few meaningful
  observations, not one per tick.
- **Hermetic.** The screen controller and DB path are injectable;
  tests fake the controller and use a tmp DB — no display needed.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

from .. import db as _db
from .controller import ScreenController

logger = logging.getLogger("friday_v6.screen.recorder")

#: OCR text is bounded so a busy screen never bloats a row.
_MAX_TEXT = 600


class ScreenDemoRecorder:
    """Background screen sampler bound to one watch (never raises)."""

    def __init__(self, watch_id: str, conn=None,
                 screen: Optional[ScreenController] = None,
                 interval: float = 3.0,
                 db_path=None) -> None:
        """
        Args:
            watch_id: the active watch this recorder feeds.
            conn: a DB connection (callers may pass None and use
                ``db_path``; the recorder opens its OWN connection so a
                background thread never shares the caller's).
            screen: injectable screen controller (default: real one).
            interval: seconds between samples.
            db_path: DB path for the recorder's own connection (tests
                pass a tmp path; default: product default).
        """
        self.watch_id = watch_id
        self._conn = conn
        self._db_path = db_path
        self._screen = screen or ScreenController()
        self._interval = max(interval, 1.0)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        #: The sampler's OWN DB connection (opened in ``start()``,
        #: closed in ``stop()``). A background thread must never share
        #: the caller's connection, and with no connection given it
        #: must never fall back to the real ~/.friday DB — hermetic
        #: tests pass a tmp ``db_path``.
        self._own_conn = None
        #: Whether the sampler actually ran. ``stop()`` only forces a
        #: final snapshot when it did — a recorder that never started
        #: (screen unavailable / opted out) records nothing at all, so
        #: an empty demonstration stays an empty skill.
        self._started = False
        self._last_text: Optional[str] = None
        self._last_error: Optional[str] = None
        self.observations = 0  #: how many changed states were recorded

    # ── capability ─────────────────────────────────────────────────

    @property
    def available(self) -> bool:
        """Whether the screen can actually be sampled here."""
        return self._screen.is_available()

    def last_error(self) -> str:
        return self._last_error or ""

    # ── lifecycle ──────────────────────────────────────────────────

    def start(self) -> bool:
        """Begin sampling in a background thread (daemon)."""
        if self._thread is not None and self._thread.is_alive():
            return True
        if not self.available:
            self._last_error = "screen capture/OCR tools unavailable"
            return False
        # The thread owns a dedicated connection (its own when none was
        # passed, otherwise a fresh one from the same path) so sampling
        # never shares the caller's connection and never touches a
        # default-path DB behind a hermetic test's back.
        #
        # ``check_same_thread=False`` is required: the connection is
        # opened HERE (main thread) but USED by the sampler thread — a
        # default True would silently drop every background write (the
        # row never lands while the observation counter still ticks).
        try:
            if self._conn is not None:
                self._own_conn = _db.connect(
                    path=self._db_path or _db.db_path_of(self._conn),
                    check_same_thread=False)
            else:
                self._own_conn = _db.connect(
                    path=self._db_path, check_same_thread=False)
        except Exception as exc:
            logger.debug(f"screen recorder db connect failed: {exc}")
            self._last_error = str(exc)
            return False
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run_loop, name="friday-screen-watch",
            daemon=True)
        self._thread.start()
        self._started = True
        return True

    def stop(self) -> int:
        """Stop sampling and record one final observation; returns count.

        Always safe to call twice (the final snapshot is recorded once,
        then the thread is joined and cleared). The final state is
        captured even when the sampler was started but produced nothing
        yet, so the formed skill ends on what the demo ended with.
        """
        self._stop.set()
        if self._thread is not None:
            if self._thread.is_alive():
                self._thread.join(timeout=self._interval + 2)
            self._thread = None
        # Final state: capture what the demo ended on — but ONLY when
        # the sampler actually ran (never started → nothing recorded,
        # so an empty demo stays an empty skill). Guarded so a second
        # ``stop()`` does not double-record.
        if self._started:
            try:
                self._sample_once(force=True)
            except Exception as exc:
                logger.debug(f"final screen sample skipped: {exc}")
        if self._own_conn is not None:
            try:
                self._own_conn.close()
            except Exception:
                pass
            self._own_conn = None
        return self.observations

    # ── sampling ───────────────────────────────────────────────────

    def _run_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._sample_once()
            except Exception as exc:
                logger.debug(f"screen watch sample failed: {exc}")
                self._last_error = str(exc)
            self._stop.wait(self._interval)

    def _sample_once(self, force: bool = False) -> None:
        """One snapshot: OCR the screen; store only *changed* states."""
        res = self._screen.ocr()
        if not res.ok:
            self._last_error = res.message
            return
        text = " ".join(w.text for w in (res.words or []))[:_MAX_TEXT]
        if text == self._last_text and not force:
            return  # unchanged — don't spam identical snapshots
        self._last_text = text
        event_type = "screen_change" if (self._last_text is not None
                                         and not force) else "screen_snapshot"
        try:
            conn = self._own_conn or self._conn
            if conn is None:
                # No connection available (start never succeeded):
                # degrade honestly — a background thread must never
                # open the real ~/.friday DB behind a hermetic test.
                self._last_error = "no database connection"
                return
            _db.record_screen_event(
                conn, text=text, event_type=event_type,
                watch_id=self.watch_id)
            self.observations += 1
        except Exception as exc:
            logger.debug(f"screen event store failed: {exc}")
            self._last_error = str(exc)

    def __repr__(self) -> str:
        return (f"<ScreenDemoRecorder watch={self.watch_id} "
                f"available={self.available} obs={self.observations}>")


__all__ = ["ScreenDemoRecorder"]
