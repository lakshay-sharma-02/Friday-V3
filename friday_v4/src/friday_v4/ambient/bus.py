"""AmbientBus — in-process event bus with a durable sqlite queue (Wave 11).

Publishers emit typed events; subscribers get them in-process; the
durable queue (a table in the V4 DB) means a late subscriber (a web tab
that opened after the event, a mobile client reconnecting) can replay
what it missed. Priority-aware: high-priority events interrupt
(critical), routine events queue for the next briefing.

The queue table lives in the V4 DB so it's covered by the same
migrations and hermetic-tested with ``tmp_path`` connections.

Never raises on publish/subscribe — the ambient layer must never break
a publisher (daemon law).
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from enum import IntEnum
from typing import Callable, Optional

from .. import db

logger = logging.getLogger("friday_v4.ambient.bus")


class Priority(IntEnum):
    """Event priority — drives interruption vs briefing-queue behavior."""

    ROUTINE = 0   # queue for the next briefing
    IMPORTANT = 1  # surface when convenient (desktop notify)
    CRITICAL = 2   # interrupt now (voice speak, urgent notification)


#: Backward-compatible alias used by callers of Wave 11's spec.
EventPriority = Priority


@dataclass(frozen=True)
class Event:
    """One typed, citable event on the bus."""

    topic: str            # security | mission | suggestion | research | system
    payload: str          # human-readable summary
    priority: Priority = Priority.ROUTINE
    source: str = "system"  # daemon | cli | web | voice | collab

    def to_dict(self) -> dict:
        return {
            "topic": self.topic,
            "payload": self.payload,
            "priority": self.priority.value,
            "source": self.source,
        }


#: Subscriber callback: (Event) -> None. Must never raise (the bus
#: guards it) — the daemon law applies to channels too.
Subscriber = Callable[[Event], None]


class AmbientBus:
    """Thread-safe in-process bus + durable queue.

    Usage:
        bus = AmbientBus(conn)
        bus.publish(Event("security", "2 high-sev vulns", Priority.IMPORTANT))
        token = bus.subscribe("security", on_security_event)
        bus.unsubscribe(token)
        bus.replay(topic="security")   # durable events missed by a surface
    """

    def __init__(self, conn=None) -> None:
        self._conn = conn
        self._lock = threading.Lock()
        self._subscribers: dict[str, list[tuple[int, Subscriber]]] = {}
        self._next_token = 0

    # ── publish ──────────────────────────────────────────────────────

    def publish(self, event: Event) -> int:
        """Deliver to in-process subscribers + persist to the durable queue.

        Returns the number of events replayed/persisted for tests.
        Never raises — failures degrade to logging.
        """
        if not isinstance(event, Event):
            event = Event(topic="system", payload=str(event))
        # Persist first (durable), then fan out in-process.
        self._persist(event)
        with self._lock:
            # Topic-specific subscribers, then the wildcard "*" surface
            # subscribers (Wave 15: surfaces that want EVERY event — the
            # mobile push channel, voice/desktop channels — subscribe to
            # "*" instead of enumerating topics).
            subs = list(self._subscribers.get(event.topic, ())) + \
                   list(self._subscribers.get("*", ()))
        for _token, fn in subs:
            try:
                fn(event)
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug(f"subscriber {event.topic} failed: {exc}")
        return 1

    def _persist(self, event: Event) -> None:
        if self._conn is None:
            return
        try:
            self._conn.execute(
                "INSERT INTO ambient_events (id, topic, payload, priority, "
                "source, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (db._new_id(), event.topic, event.payload,
                 event.priority.value, event.source, db.now_iso()))
            self._conn.commit()
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug(f"ambient persist failed: {exc}")

    # ── subscribe ────────────────────────────────────────────────────

    def subscribe(self, topic: str, fn: Subscriber) -> int:
        """Subscribe ``fn`` to ``topic``; returns an unsubscribe token.

        ``topic="*"`` subscribes to EVERY event (Wave 15 — surfaces
        that push on any topic, like the mobile/voice/desktop channels).
        """
        with self._lock:
            self._next_token += 1
            self._subscribers.setdefault(topic, []).append(
                (self._next_token, fn))
            return self._next_token

    def unsubscribe(self, token: int) -> bool:
        with self._lock:
            for topic, subs in list(self._subscribers.items()):
                before = len(subs)
                self._subscribers[topic] = [s for s in subs
                                            if s[0] != token]
                if len(self._subscribers[topic]) != before:
                    return True
        return False

    # ── replay (late/durable consumers) ──────────────────────────────

    def replay(self, topic: Optional[str] = None,
               limit: int = 50) -> list[Event]:
        """Replay durable events (optionally per topic) — never raises."""
        if self._conn is None:
            return []
        try:
            if topic:
                rows = self._conn.execute(
                    "SELECT topic, payload, priority, source FROM "
                    "ambient_events WHERE topic = ? ORDER BY rowid DESC "
                    "LIMIT ?", (topic, limit)).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT topic, payload, priority, source FROM "
                    "ambient_events ORDER BY rowid DESC LIMIT ?",
                    (limit,)).fetchall()
            return [
                Event(r["topic"], r["payload"], Priority(r["priority"]),
                      r["source"])
                for r in rows
            ]
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug(f"ambient replay failed: {exc}")
            return []


__all__ = ["AmbientBus", "Event", "EventPriority", "Priority", "Subscriber"]
