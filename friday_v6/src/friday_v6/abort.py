"""abort.py — the kill switch (Wave 5, from V3's discipline).

``friday6 abort`` arms a *durable* kill switch that stops the agent
bridge mid-session: while armed, every Claude Code tool call is denied
immediately — the bridge's ``can_use_tool`` hook checks the switch
FIRST, before any permission ask is recorded — and new ``CLAUDE:``
prompts are refused until the switch is cleared. A runaway agent can
be stopped mid-turn from any surface.

Design laws:

- **Never-crash.** A missing or unreadable flag file reads as "not
  armed"; every accessor is guarded and degrades to an honest value.
- **Durable.** The switch is file-backed (``~/.friday/v6_abort.json``)
  so a daemon/bridge restart does not silently re-allow a runaway
  agent — the operator's override survives.
- **Hermetic.** The flag path is injectable; tests use tmp dirs and
  never touch the real ``~/.friday`` state.
- **Ambient.** Arming/disarming publishes KILL_SWITCH events on the
  ambient bus (best-effort, guarded) so the Live feed / mobile push
  surfaces see the override happen (V3's KILL_SWITCH_ACTIVATED
  pattern).

The exit criterion: ``friday6 abort`` stops a (mocked) bridge session
mid-turn — see ``tests/test_abort.py``.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger("friday_v6.abort")

#: Default flag file — lives with the rest of Friday's data.
DEFAULT_ABORT_FILE = Path.home() / ".friday" / "v6_abort.json"

#: Ambient event kinds (V3 naming, surfaced on the Live feed).
KILL_SWITCH_ACTIVATED = "kill_switch_activated"
KILL_SWITCH_DEACTIVATED = "kill_switch_deactivated"


class KillSwitch:
    """A durable operator kill switch for the agent bridge.

    File-backed JSON (``{armed, reason, at}``) so the flag survives
    restarts and is readable from any process. All accessors never
    raise — a missing/unreadable file is simply "not armed".
    """

    def __init__(self, path: Path | str | None = None,
                 db_path: Path | str | None = None) -> None:
        self._path = Path(path) if path else DEFAULT_ABORT_FILE
        #: State DB for the ambient KILL_SWITCH event. None → the product
        #: default (~/.friday/v4.db); hermetic tests pass a tmp path so
        #: arming never touches the real operator state.
        self._db_path = db_path
        self._lock = threading.Lock()

    # ── state ─────────────────────────────────────────────────────

    def _read(self) -> dict:
        try:
            if self._path.exists():
                return json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            logger.debug(f"abort state unreadable ({exc}) — not armed")
        return {}

    def _write(self, data: dict) -> bool:
        """Persist the flag file; returns True when the write succeeded.

        The kill switch's own success signal must be honest: if the
        flag can't be written, ``arm()`` reports False so the CLI says
        "failed" instead of "⛔ ABORT — armed" while the bridge stays
        live (fail-closed, never a lying success).
        """
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(data, indent=2), encoding="utf-8")
            return True
        except OSError as exc:
            logger.warning(f"abort state write failed: {exc}")
            return False

    # ── public API ────────────────────────────────────────────────

    def arm(self, reason: str = "") -> bool:
        """Arm the kill switch; returns True when newly armed.

        Idempotent: re-arming updates the reason but reports False (it
        was already armed) — the operator's override is not double-counted.
        """
        with self._lock:
            was_armed = self.is_armed()
            if not self._write({
                    "armed": True,
                    "reason": (reason or "").strip()[:200],
                    "at": datetime.now(UTC).isoformat(timespec="seconds"),
            }):
                return False  # couldn't persist — never claim success
            if not was_armed:
                self._publish(KILL_SWITCH_ACTIVATED, reason)
            return not was_armed

    def clear(self) -> bool:
        """Disarm the kill switch; returns True when it was armed."""
        with self._lock:
            was_armed = self.is_armed()
            if not self._write({
                    "armed": False,
                    "reason": "",
                    "at": datetime.now(UTC).isoformat(timespec="seconds"),
            }):
                return False  # couldn't persist — never claim success
            if was_armed:
                self._publish(KILL_SWITCH_DEACTIVATED, "")
            return was_armed

    def is_armed(self) -> bool:
        """Whether the switch is currently armed (never raises)."""
        return bool(self._read().get("armed", False))

    def status(self) -> dict:
        """Current switch state for CLIs / surfaces."""
        data = self._read()
        return {
            "armed": bool(data.get("armed", False)),
            "reason": data.get("reason", "") or "",
            "at": data.get("at", "") or "",
        }

    # ── ambient ───────────────────────────────────────────────────

    def _publish(self, kind: str, reason: str) -> None:
        """Best-effort KILL_SWITCH event on the ambient bus (guarded).

        Uses the default state DB (never injected) — surfaces like the
        Live feed read the durable queue from the same default path,
        so the override is visible even though the flag file is the
        source of truth. Never raises.
        """
        try:
            from . import db
            from .ambient import AmbientBus, Event, Priority
            conn = db.connect(path=self._db_path)
            try:
                detail = f" — {reason}" if reason.strip() else ""
                AmbientBus(conn).publish(Event(
                    topic="system",
                    payload=f"Kill switch {kind}{detail}",
                    priority=Priority.CRITICAL,
                    source="abort"))
            finally:
                conn.close()
        except Exception as exc:
            logger.debug(f"abort ambient publish skipped: {exc}")


#: Module singleton — the bridge hook and the CLI share one switch.
_switch: KillSwitch | None = None
_switch_lock = threading.Lock()


def kill_switch(path: Path | str | None = None,
                db_path: Path | str | None = None) -> KillSwitch:
    """The shared KillSwitch (lazily created and cached).

    ``path``/``db_path`` are for hermetic tests — a non-default path
    builds a FRESH switch (never the singleton) so tests never touch
    the real ~/.friday state.
    """
    global _switch
    with _switch_lock:
        if path is not None or db_path is not None:
            return KillSwitch(path=path, db_path=db_path)
        if _switch is None:
            _switch = KillSwitch()
        return _switch


def abort_now(reason: str = "", path: Path | str | None = None,
              db_path: Path | str | None = None) -> bool:
    """``friday6 abort`` — arm the shared switch + stop the bridge.

    Best-effort ends the shared Claude bridge session so an in-flight
    turn is cut short (the hook also denies any subsequent tool call).
    Returns True when the switch was newly armed. ``path``/``db_path``
    are for hermetic tests (never the real ~/.friday state).
    """
    newly = kill_switch(path=path, db_path=db_path).arm(reason)
    try:
        from .agent import get_bridge
        bridge = get_bridge()
        if bridge.available():
            bridge.end()
    except Exception as exc:
        logger.debug(f"abort bridge end skipped: {exc}")
    return newly


__all__ = [
    "DEFAULT_ABORT_FILE",
    "KILL_SWITCH_ACTIVATED",
    "KILL_SWITCH_DEACTIVATED",
    "KillSwitch",
    "abort_now",
    "kill_switch",
]
