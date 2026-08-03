"""System tray integration for Friday V4 (Wave 2).

Provides a persistent tray icon showing Friday's status (daemon state,
ambient feed count) with a context menu for quick actions.

Uses ``pystray`` (optional dependency). If pystray isn't installed the
class still constructs but ``start()`` reports it as unavailable —
callers should degrade gracefully.

Usage:
    tray = SystemTray(feed_count=3, on_voice=lambda: ...)
    tray.start()          # blocks (or run in a thread)
    tray.update_feed_count(7)
    tray.stop()
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

logger = logging.getLogger("friday_v4.desktop.tray")


class SystemTray:
    """A system tray icon for Friday.

    Attributes:
        available: True if pystray is installed and usable.
        title: The tooltip / icon title.
    """

    def __init__(
        self,
        title: str = "Friday V4",
        feed_count: int = 0,
        daemon_state: str = "idle",
        on_voice: Optional[Callable[[], None]] = None,
        on_status: Optional[Callable[[], None]] = None,
        on_quit: Optional[Callable[[], None]] = None,
    ):
        self.title = title
        self.feed_count = feed_count
        self.daemon_state = daemon_state
        self._on_voice = on_voice
        self._on_status = on_status
        self._on_quit = on_quit
        self._icon = None
        self._thread: Optional[threading.Thread] = None
        self.available = self._check_available()

    def _check_available(self) -> bool:
        try:
            import PIL  # noqa: F401
            import pystray  # noqa: F401
            return True
        except ImportError:
            logger.info("pystray/PIL not installed — system tray unavailable")
            return False

    def _build_icon(self):
        """Create the pystray Icon (lazy — needs pystray/PIL present)."""
        import pystray
        from PIL import Image, ImageDraw

        # Simple FRIDAY diamond glyph on a rounded square
        size = 64
        image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle([2, 2, size - 2, size - 2], radius=14, fill=(0, 120, 212, 255))
        draw.polygon(
            [(size // 2, 10), (size - 12, size // 2), (size // 2, size - 10), (12, size // 2)],
            fill=(255, 255, 255, 255),
        )

        def on_voice(icon, item):
            if self._on_voice:
                self._on_voice()

        def on_status(icon, item):
            if self._on_status:
                self._on_status()

        def on_quit(icon, item):
            icon.stop()
            if self._on_quit:
                self._on_quit()

        menu = pystray.Menu(
            pystray.MenuItem("🎙  Start voice", on_voice, default=True),
            pystray.MenuItem("📊  Desktop status", on_status),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", on_quit),
        )
        tooltip = f"{self.title} — {self.daemon_state}"
        if self.feed_count:
            tooltip += f" ({self.feed_count} feed events)"
        return pystray.Icon("friday", image, tooltip, menu)

    def update_feed_count(self, count: int) -> None:
        """Update the tray tooltip with the latest ambient feed count."""
        self.feed_count = count
        if self._icon is not None:
            try:
                self._icon.title = (
                    f"{self.title} — {self.daemon_state}"
                    + (f" ({count} feed events)" if count else "")
                )
            except Exception:
                pass

    def update_daemon_state(self, state: str) -> None:
        """Update the daemon state shown in the tooltip."""
        self.daemon_state = state
        self.update_feed_count(self.feed_count)

    def start(self, daemon: bool = True) -> bool:
        """Show the tray icon.

        Args:
            daemon: If True, run the icon loop in a background thread and
                return immediately. If False, block until stop() is called.

        Returns:
            True if the tray started successfully.
        """
        if not self.available:
            return False
        try:
            self._icon = self._build_icon()
        except Exception as exc:
            logger.warning(f"Failed to build tray icon: {exc}")
            return False

        icon = self._icon
        if icon is None:
            return False
        if daemon:
            self._thread = threading.Thread(
                target=icon.run, name="friday-tray", daemon=True,
            )
            self._thread.start()
            logger.info("System tray started (background)")
        else:
            icon.run()
        return True

    def stop(self) -> None:
        """Stop the tray icon."""
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                pass
            self._icon = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def __repr__(self) -> str:
        return (f"<SystemTray available={self.available} "
                f"feed_count={self.feed_count} state={self.daemon_state}>")
