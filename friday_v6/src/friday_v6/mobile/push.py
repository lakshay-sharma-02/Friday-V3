"""Mobile push transport — durable-queue consumer for the companion (Wave 15).

The phone becomes another surface of the *same* Friday by consuming the
durable ambient queue (the table every publisher writes to through the
Wave 11 ``AmbientBus``). ``PushNotificationService`` replays events it
hasn't delivered yet (a rowid cursor, persisted across restarts) and
hands each one to a transporter — a pluggable ``(Notification) -> None``
that a companion app plugs a real push endpoint into (FCM/APNS/webhook).
Default transporter is a no-op logger; a file transporter writes a JSONL
outbox for testing/offline inspection.

Design laws (Wave 15 + daemon law):
- Pure stdlib; ``db_path`` injectable so tests stay hermetic.
- Never raises: a missing/corrupt DB, state file, or transporter yields
  a skipped poll (count 0), never a crash.
- Cursor is the durable queue's ``rowid`` (insert order) so a restart
  delivers exactly what was missed — the same replay contract as the
  web SSE stream and the mobile API's ``/api/events``.
- Priority-aware: ``min_priority`` filters what's *pushed* (CRITICAL
  and IMPORTANT interrupt; ROUTINE stays in the queue for briefing).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger("friday_v6.mobile.push")

#: Default state file (cursor persistence). Override in tests.
_DEFAULT_STATE = Path.home() / ".friday" / "v4_mobile_push.json"


@dataclass(frozen=True)
class Notification:
    """One event delivered to a phone surface."""

    topic: str
    payload: str
    priority: int = 0          # 0 routine | 1 important | 2 critical
    source: str = "system"
    created_at: str = ""

    def to_dict(self) -> dict:
        return {
            "topic": self.topic,
            "payload": self.payload,
            "priority": self.priority,
            "source": self.source,
            "created_at": self.created_at,
        }


#: Transporter: receives one delivered notification. Must never raise
#: (the service guards it — a dead transport must not break the queue).
Transporter = Callable[[Notification], None]


def log_transporter(notification: Notification) -> None:
    """Default transporter: log at INFO (visible in `friday6 mobile serve`)."""
    logger.info("mobile push [%s] %s", notification.topic, notification.payload[:120])


def file_transporter(outbox: Path) -> Transporter:
    """Transporter that appends each notification as a JSONL line.

    The hermetic-test + offline-inspection transporter: the companion
    (or a test) can read the outbox file to see what was pushed.
    """

    def _transporter(notification: Notification) -> None:
        try:
            outbox.parent.mkdir(parents=True, exist_ok=True)
            with outbox.open("a") as fh:
                fh.write(json.dumps(notification.to_dict(), default=str) + "\n")
        except OSError as exc:
            logger.debug(f"mobile outbox write failed: {exc}")

    return _transporter


# ── Expo push (Wave 7) — the real phone destination ──────────────────

#: The Expo push service endpoint (https://docs.expo.dev/push-notifications).
EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"


def _post(url: str, payload: dict, timeout_seconds: float = 15.0) -> Optional[str]:
    """POST JSON to ``url``; returns the response text or None (never raises)."""
    import json as _json
    import urllib.error
    import urllib.request

    body = _json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        logger.debug(f"expo push failed: {exc}")
        return None


def expo_transporter(token: str, timeout_seconds: float = 15.0,
                     post: Optional[callable] = None) -> Transporter:
    """Transporter that delivers notifications to one Expo push token.

    The real phone destination (Wave 7): a paired device's Expo push
    token receives the ambient event as a system notification. Pure
    stdlib (``urllib``), bounded timeout, and never raises — a failed
    send logs and the poll continues (the daemon law).
    """
    token = (token or "").strip()
    if not token:
        return lambda notification: None
    sender = post or _post

    def _transporter(notification: Notification) -> None:
        payload = {
            "to": token,
            "title": f"Friday · {notification.topic}",
            "body": (notification.payload or "")[:200],
            "sound": "default",
            "data": notification.to_dict(),
        }
        sender(EXPO_PUSH_URL, payload, timeout_seconds)

    return _transporter


def fanout_transporter(db_path=None, *, timeout_seconds: float = 15.0,
                       post: Optional[callable] = None) -> Transporter:
    """Transporter that pushes to every paired device (Wave 7).

    The daemon's default mobile destination once the operator has
    paired a phone: each poll reads the device registry fresh (a newly
    paired phone starts receiving immediately; an unpairing stops
    delivery on the next pass). Never raises — a bad token fails one
    device, never the queue.
    """
    sender = post or _post

    def _transporter(notification: Notification) -> None:
        try:
            from .. import db
            conn = db.connect(path=db_path)
            try:
                devices = db.list_devices(conn)
            finally:
                conn.close()
        except Exception as exc:
            logger.debug(f"fanout device lookup failed: {exc}")
            return
        for device in devices:
            token = (device.get("token") or "").strip()
            if not token:
                continue
            if not token.startswith("ExponentPushToken["):
                # A PWA/other client — it receives events over its SSE
                # stream, not Expo push. Skip so we never POST a bogus
                # token to exp.host (Wave 7: the PWA pairs for identity,
                # the native app pairs an Expo token for background push).
                logger.debug(
                    f"fanout skip {device.get('id')}: non-Expo token "
                    f"({device.get('platform') or 'unknown'})")
                continue
            try:
                expo_transporter(token, timeout_seconds=timeout_seconds,
                                 post=sender)(notification)
            except Exception as exc:
                logger.debug(f"fanout device {device.get('id')} failed: {exc}")

    return _transporter


def command_transporter(command: str, timeout_seconds: float = 20.0) -> Transporter:
    """Transporter that pipes each notification's JSON to a shell command.

    The operator-configurable hook (Wave 15): ``command`` is a shell
    pipeline that receives one notification per invocation on **stdin**
    — e.g. ``curl -s -X POST -d @- https://ntfy.sh/friday`` for a
    real push endpoint, or ``cat >> ~/friday-push.log`` for a custom
    outbox. The operator authors the command themselves (their own
    config/CLI flag); it is a *transport*, never an execution path.

    Never raises: a missing binary, nonzero exit, or timeout logs and
    the poll continues — a dead hook never wedges the durable queue
    (daemon law).
    """
    import subprocess

    if not (command or "").strip():
        # No hook configured — a no-op transporter (never a per-event
        # subprocess of the empty string).
        return lambda notification: None

    def _transporter(notification: Notification) -> None:
        try:
            payload = json.dumps(notification.to_dict(), default=str) + "\n"
            subprocess.run(command, input=payload, shell=True,
                           capture_output=True, text=True,
                           timeout=timeout_seconds)
        except Exception as exc:
            logger.debug(f"mobile command hook failed: {exc}")

    return _transporter


class PushNotificationService:
    """Replays the durable ambient queue to a transporter (cursor-based).

    Usage:
        service = PushNotificationService(db_path=v4_db, transporter=fn)
        delivered = service.poll_once()   # deliver events since the cursor
        delivered = service.poll_once()   # nothing new → 0
        cursor = service.cursor           # last delivered rowid

    The cursor persists to ``state_file`` after each poll so a daemon
    restart delivers only what the phone missed — never a re-delivery
    storm, never a lost event.
    """

    def __init__(self, db_path=None, state_file: Optional[Path] = None,
                 transporter: Optional[Transporter] = None,
                 min_priority: int = 0,
                 limit: int = 100) -> None:
        self._db_path = db_path
        self._state_file = Path(state_file) if state_file else _DEFAULT_STATE
        self._transporter = transporter or log_transporter
        self.min_priority = min_priority
        self.limit = limit
        self.cursor: int = self._load_cursor()
        self.delivered_total = 0
        self.last_error: Optional[str] = None

    # ── cursor persistence ──────────────────────────────────────────

    def _load_cursor(self) -> int:
        try:
            if self._state_file.exists():
                data = json.loads(self._state_file.read_text())
                return int(data.get("cursor", 0) or 0)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            logger.debug(f"mobile cursor unreadable: {exc}")
        return 0

    def _save_cursor(self) -> None:
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            self._state_file.write_text(json.dumps({"cursor": self.cursor}))
        except OSError as exc:
            logger.debug(f"mobile cursor save failed: {exc}")

    # ── poll ────────────────────────────────────────────────────────

    def poll_once(self) -> int:
        """Deliver events since the cursor; returns count delivered.

        Reads ``ambient_events_since`` (oldest first, bounded), filters
        by priority, transports each, and advances the cursor. Never
        raises — a missing DB/table/transport degrades to 0.
        """
        try:
            from .. import db
            conn = db.connect(path=self._db_path)
            try:
                events = db.ambient_events_since(conn, self.cursor,
                                                 limit=self.limit)
            finally:
                conn.close()
        except Exception as exc:
            logger.debug(f"mobile poll failed: {exc}")
            self.last_error = str(exc)
            return 0

        delivered = 0
        for ev in events:
            rowid = int(ev.get("rowid") or 0)
            if rowid <= self.cursor:
                continue
            priority = int(ev.get("priority") or 0)
            if priority < self.min_priority:
                # Still advance the cursor — the event was *seen*, it
                # just isn't push-worthy. ROUTINE events queue for the
                # next briefing, never a re-delivery loop.
                self.cursor = rowid
                continue
            note = Notification(
                topic=ev.get("topic") or "system",
                payload=ev.get("payload") or "",
                priority=priority,
                source=ev.get("source") or "system",
                created_at=ev.get("created_at") or "",
            )
            try:
                self._transporter(note)
            except Exception as exc:
                logger.debug(f"mobile transporter failed: {exc}")
                # Keep the cursor advanced so one dead notification
                # doesn't wedge the whole queue (daemon law).
            self.cursor = rowid
            self.delivered_total += 1
            delivered += 1

        if delivered or self.cursor:
            self._save_cursor()
        self.last_error = None
        return delivered


__all__ = ["Notification", "PushNotificationService", "Transporter",
           "log_transporter", "file_transporter", "command_transporter",
           "expo_transporter", "fanout_transporter", "EXPO_PUSH_URL"]
