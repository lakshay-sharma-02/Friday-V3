"""Mobile Companion — the phone app itself, push, quick status, NL talk.

Wave 15 closed the stub's transport; Wave 7 now ships the phone surface:

    - ``app/`` — the companion PWA (installable, phone-first). Chat
      through the one NLU brain, resume the shared one-presence thread,
      live SSE push feed with a durable replay cursor, one-time-code
      pairing, status. Served by ``friday6 mobile serve`` at ``/``.
    - The native Expo app lives in the repo at ``mobile/app/`` (typed
      API client, four screens, push registration — see its README for
      build + Apple/Google/EAS account steps; background push needs a
      dev build and a physical device).
    - ``PushNotificationService`` — a durable-queue consumer (rowid
      cursor, persisted) that replays ambient events to a transporter
      (paired devices' Expo tokens via ``fanout_transporter``; the
      daemon runs this on its own schedule).
    - ``MobileAPI`` / ``create_api_server`` — the stdlib server: the
      PWA + ``/api/status``, ``/api/conversation`` (the shared
      one-presence thread), ``POST /api/talk`` (the same ``nl_router``
      brain as talk/voice/web), pairing, and ``/api/events`` (SSE over
      the durable queue — push, replayable since a cursor).

Every accessor is guarded and never raises (the never-crash law), and
tests are hermetic via injectable ``db_path``.

**Status:** Wave 7 — SHIPPED (2026-08).
"""

from __future__ import annotations

from .push import (Notification, PushNotificationService, command_transporter,
                   file_transporter, expo_transporter, fanout_transporter,
                   EXPO_PUSH_URL)
from .api import MobileAPI, create_api_server
from .pairing import PairingService

_MOBILE_AVAILABLE = True


def is_available() -> bool:
    """Whether the mobile companion transport is implemented."""
    return _MOBILE_AVAILABLE


__all__ = [
    "MobileAPI",
    "create_api_server",
    "PushNotificationService",
    "Notification",
    "PairingService",
    "command_transporter",
    "file_transporter",
    "expo_transporter",
    "fanout_transporter",
    "EXPO_PUSH_URL",
    "is_available",
    "_MOBILE_AVAILABLE",
]
