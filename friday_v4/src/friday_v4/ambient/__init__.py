"""Ambient Layer — in-process event bus + durable queue + channels (Wave 11).

The architectural fix the whole product has been waiting for: **push
replaces polling**. Components publish typed events; channels (voice
speak, desktop notify, web SSE) subscribe; disconnected surfaces replay
the durable queue on reconnect.

```
publisher ──► bus.publish(event) ──► durable queue (sqlite, v4.db)
                  │  └─► in-process subscribers (voice/desktop/web)
                  ▼
             replays for late subscribers
```

Design laws (Wave 11 doc §3.4):
- Components publish typed events; channels subscribe — no direct
  coupling.
- Events are queued durably (V4 DB) so a disconnected surface replays
  on reconnect.
- Priority-aware: critical events interrupt; routine events queue for
  the next briefing.

**Status:** Wave 11 — built (2026-08). Pure stdlib (threading +
sqlite3), hermetic tests, never-crash (importing this package never
breaks the rest of Friday V4).

Usage:
    from friday_v4.ambient import AmbientBus
    bus = AmbientBus(conn)          # conn is the V4 DB (queue table)
    bus.publish("security", "2 high-sev vulns in MindWell", priority=1)
    token = bus.subscribe("security", fn)   # in-process callback
"""

from __future__ import annotations

try:
    from .bus import AmbientBus, Event, EventPriority, Priority
    from .channels import ChannelRegistry, notify_channel
    _AMBIENT_AVAILABLE = True
except ImportError:  # pragma: no cover - defensive stub
    AmbientBus = None  # type: ignore
    Event = None  # type: ignore
    EventPriority = None  # type: ignore
    Priority = None  # type: ignore
    ChannelRegistry = None  # type: ignore
    notify_channel = None  # type: ignore
    _AMBIENT_AVAILABLE = False


def is_available() -> bool:
    """Whether the ambient layer is implemented yet."""
    return _AMBIENT_AVAILABLE


__all__ = [
    "AmbientBus",
    "Event",
    "EventPriority",
    "Priority",
    "ChannelRegistry",
    "notify_channel",
    "is_available",
    "_AMBIENT_AVAILABLE",
]
