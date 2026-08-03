"""Ambient channels — fan-out from the bus to concrete surfaces (Wave 11).

The Wave 11 doc's ``channels.py``: voice (speak), desktop (notify), web
(SSE). Each channel is a thin adapter registered by name so publishers
never depend on a surface directly — the bus stays the only coupling.

A channel is a ``(Event) -> None`` subscriber; the registry maps names
to channel builders so the daemon can wire what's available (voice
pipeline, desktop notifier, web server) and degrade when a surface is
missing (daemon law).
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from .bus import Event, Priority

logger = logging.getLogger("friday_v4.ambient.channels")

#: Channel builder: (event) -> None. Surfaces wrap their own adapters.
Channel = Callable[[Event], None]


class ChannelRegistry:
    """Name → channel; publish fan-out with graceful degradation."""

    def __init__(self) -> None:
        self._channels: dict[str, Channel] = {}

    def register(self, name: str, channel: Channel) -> None:
        self._channels[name] = channel

    def unregister(self, name: str) -> None:
        self._channels.pop(name, None)

    def fanout(self, event: Event) -> None:
        """Deliver to every registered channel — never raises."""
        for name, channel in list(self._channels.items()):
            try:
                channel(event)
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug(f"channel {name} failed on {event.topic}: {exc}")

    def names(self) -> list[str]:
        return sorted(self._channels)


#: The shared registry (daemon registers its live surfaces here).
registry = ChannelRegistry()


def notify_channel(event: Event) -> None:
    """Default channel builder used by publishers when they just want
    the registry fan-out (daemon, web SSE, voice)."""
    registry.fanout(event)


#: Convenience: does any registered channel claim high priority?
def would_interrupt(event: Event) -> bool:
    """Whether this event is critical enough to interrupt (voice/SSE)."""
    return event.priority >= Priority.IMPORTANT


# ── Surface channel builders (Wave 15 — push reaches every surface) ──


def speak_channel(speak_fn: Callable[[str], bool],
                  min_priority: Priority = Priority.CRITICAL) -> Channel:
    """A voice channel: speaks events at/above ``min_priority``.

    ``speak_fn(text)`` is the pipeline's TTS entry (returns success).
    The daemon registers this when the voice pipeline is running, so a
    CRITICAL ambient event (security, mission) is *spoken*, not just
    queued. Guarded: a failing TTS never breaks the fan-out.
    """

    def _channel(event: Event) -> None:
        if event.priority < min_priority:
            return
        try:
            speak_fn(event.payload[:200])
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug(f"voice channel failed on {event.topic}: {exc}")

    return _channel


def desktop_channel(notify_fn: Callable[..., bool],
                    min_priority: Priority = Priority.IMPORTANT) -> Channel:
    """A desktop channel: notifies events at/above ``min_priority``.

    ``notify_fn(title, message, **kw)`` matches the
    ``DesktopAbstraction.notify`` signature. IMPORTANT+ events pop a
    banner; ROUTINE events queue for the next briefing. Guarded.
    """

    def _channel(event: Event) -> None:
        if event.priority < min_priority:
            return
        try:
            notify_fn(f"Friday · {event.topic.capitalize()}",
                      event.payload[:200], urgency="normal", timeout_ms=10000)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug(f"desktop channel failed on {event.topic}: {exc}")

    return _channel


__all__ = ["ChannelRegistry", "Channel", "registry", "notify_channel",
           "would_interrupt", "speak_channel", "desktop_channel"]
