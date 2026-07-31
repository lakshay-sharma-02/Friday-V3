"""Global hotkeys for Friday V4 (Wave 2).

Registers system-wide hotkeys so Friday can be summoned from anywhere:
- Push-to-talk (default ``ctrl+shift+space``) triggers a voice session
- ``ctrl+shift+f`` shows desktop status

Uses the ``keyboard`` library (optional dependency; may need root on
some Linux setups). If unavailable, ``GlobalHotkeys.available`` is False
and callers should degrade gracefully.

Usage:
    hotkeys = GlobalHotkeys(
        push_to_talk="ctrl+shift+space",
        on_push_to_talk=start_voice_session,
        on_status=show_desktop_status,
    )
    hotkeys.start()     # register hotkeys
    hotkeys.stop()      # unregister
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

logger = logging.getLogger("friday_v4.desktop.hotkeys")

_DEFAULT_PUSH_TO_TALK = "ctrl+shift+space"
_DEFAULT_STATUS = "ctrl+shift+f"


class GlobalHotkeys:
    """Global keyboard shortcut registration.

    Attributes:
        available: True if the ``keyboard`` library is usable.
        registered: Names of currently registered hotkeys.
    """

    def __init__(
        self,
        push_to_talk: str = _DEFAULT_PUSH_TO_TALK,
        status: str = _DEFAULT_STATUS,
        on_push_to_talk: Optional[Callable[[], None]] = None,
        on_status: Optional[Callable[[], None]] = None,
    ):
        self.push_to_talk = push_to_talk
        self.status_hotkey = status
        self._on_push_to_talk = on_push_to_talk
        self._on_status = on_status
        self._keyboard = None
        self.registered: list[str] = []
        self.available = self._check_available()

    def _check_available(self) -> bool:
        try:
            import keyboard  # noqa: F401
            self._keyboard = keyboard
            return True
        except ImportError:
            logger.info("keyboard library not installed — global hotkeys unavailable")
            return False
        except Exception as exc:
            # e.g. no display / missing privileges
            logger.info(f"keyboard library unavailable: {exc}")
            return False

    def start(self) -> bool:
        """Register all hotkeys.

        Returns:
            True if hotkeys were registered (or are already registered).
        """
        if not self.available or self._keyboard is None:
            return False

        hotkeys: list[tuple[str, Optional[Callable[[], None]]]] = [
            (self.push_to_talk, self._on_push_to_talk),
            (self.status_hotkey, self._on_status),
        ]
        for hotkey, callback in hotkeys:
            if not hotkey or not callback:
                continue
            try:
                self._keyboard.add_hotkey(hotkey, callback)
                self.registered.append(hotkey)
                logger.info(f"Registered hotkey: {hotkey}")
            except Exception as exc:
                logger.warning(f"Failed to register hotkey '{hotkey}': {exc}")

        return bool(self.registered)

    def stop(self) -> None:
        """Unregister all hotkeys."""
        if not self.available or self._keyboard is None:
            self.registered = []
            return
        for hotkey in self.registered:
            try:
                self._keyboard.remove_hotkey(hotkey)
            except Exception as exc:
                logger.debug(f"Failed to remove hotkey '{hotkey}': {exc}")
        self.registered = []

    def __repr__(self) -> str:
        return (f"<GlobalHotkeys available={self.available} "
                f"registered={self.registered}>")
