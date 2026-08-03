"""Mobile Companion — push transport, quick status, NL talk from your phone.

Wave 15 closes the stub: the phone becomes another surface of the SAME
Friday. The transport is pure-stdlib and local:

    - ``PushNotificationService`` — a durable-queue consumer (rowid
      cursor, persisted) that replays ambient events to a transporter
      (a companion app plugs a real FCM/APNS/webhook endpoint in).
    - ``MobileAPI`` / ``create_api_server`` — a stdlib HTTP server the
      companion talks to: ``/api/status``, ``/api/conversation`` (the
      shared one-presence thread), ``POST /api/talk`` (the same
      ``nl_router`` brain as talk/voice/web), and ``/api/events`` (SSE
      over the durable queue — push, replayable since a cursor).

The React Native app (``app/``) communicates with this local API over
the network. Every accessor is guarded and never raises (the never-crash
law), and tests are hermetic via injectable ``db_path``.

**Status:** Wave 15 — built (2026-08).
"""

from __future__ import annotations

from .push import (Notification, PushNotificationService, command_transporter,
                   file_transporter)
from .api import MobileAPI, create_api_server

_MOBILE_AVAILABLE = True


def is_available() -> bool:
    """Whether the mobile companion transport is implemented."""
    return _MOBILE_AVAILABLE


__all__ = [
    "MobileAPI",
    "create_api_server",
    "PushNotificationService",
    "Notification",
    "command_transporter",
    "file_transporter",
    "is_available",
    "_MOBILE_AVAILABLE",
]
