"""Mobile Companion — Push notifications, quick status, mobile voice input.

Bridge between Friday's desktop daemon and your phone. Provides a REST/WS API
for mobile clients, push notification transport, and voice input from mobile.

The mobile app (React Native) lives in `app/` and communicates with the
desktop daemon via the local network API.

Capabilities:
    - Push notification transport (APNS / FCM)
    - REST API for status queries
    - WebSocket for real-time updates
    - Voice input relay (phone mic → desktop STT)
    - Quick action dispatch

**Status:** Wave 7 — not implemented yet. The imports below are guarded so
importing this package never crashes the rest of Friday V4.
"""

from __future__ import annotations

try:
    from .api import MobileAPI, create_api_server
    from .push import PushNotificationService, Notification
    _MOBILE_AVAILABLE = True
except ImportError:  # pragma: no cover - Wave 7 stub
    MobileAPI = None  # type: ignore
    create_api_server = None  # type: ignore
    PushNotificationService = None  # type: ignore
    Notification = None  # type: ignore
    _MOBILE_AVAILABLE = False


def is_available() -> bool:
    """Whether the mobile companion is implemented yet."""
    return _MOBILE_AVAILABLE


__all__ = [
    "MobileAPI",
    "create_api_server",
    "PushNotificationService",
    "Notification",
    "is_available",
    "_MOBILE_AVAILABLE",
]
