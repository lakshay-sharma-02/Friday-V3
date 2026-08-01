"""Desktop state watcher — fires callbacks on window / workspace changes.

Completes the Wave 2 plan's desktop API:

    desktop.on_window_change(callback)
    desktop.on_workspace_change(callback)

Real desktop events (active-window, active-app, or workspace changes) are
detected by polling the WindowManager facade at a small interval and diffing
the previous state — no native event subscriptions required, which keeps it
cross-platform across every adapter.

Usage:
    watcher = DesktopWatcher(
        wm=WindowManager(),
        on_window_change=lambda win: print(f"Switched to {win.app_class}"),
        on_app_change=lambda app: print(f"App: {app}"),
        on_workspace_change=lambda ws: print(f"Workspace: {ws.id}"),
    )
    watcher.start()        # background thread
    ...
    watcher.stop()

Callbacks run on the watcher's background thread, so they should return
quickly (or hand off to a queue).
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

from .wm_abstraction import WindowInfo, WindowManager, WorkspaceInfo

logger = logging.getLogger("friday_v4.desktop.watcher")


class DesktopWatcher:
    """Polls desktop state and invokes callbacks on changes.

    Attributes:
        wm: The WindowManager (or any object exposing get_active_window()
            and get_active_workspace()).
        poll_interval: Seconds between state samples.
        on_window_change: Called with the new WindowInfo when the active
            window changes (window id or title differs).
        on_app_change: Called with the new app class string when the active
            application changes.
        on_workspace_change: Called with the new WorkspaceInfo when the
            active workspace changes.
        running: Whether the polling loop is active.
    """

    def __init__(
        self,
        wm: Optional[WindowManager] = None,
        poll_interval: float = 1.0,
        on_window_change: Optional[Callable[[WindowInfo], None]] = None,
        on_app_change: Optional[Callable[[str], None]] = None,
        on_workspace_change: Optional[Callable[[WorkspaceInfo], None]] = None,
    ):
        self.wm = wm or WindowManager()
        self.poll_interval = max(poll_interval, 0.1)
        self.on_window_change = on_window_change
        self.on_app_change = on_app_change
        self.on_workspace_change = on_workspace_change
        self.running = False
        self._thread: Optional[threading.Thread] = None

        # Last-seen state (for change detection)
        self._last_window_id: str = ""
        self._last_app: str = ""
        self._last_workspace_id: int = -1

    @property
    def available(self) -> bool:
        """Whether a usable desktop backend is present."""
        try:
            return bool(self.wm.is_available)
        except Exception:
            return False

    # ── Lifecycle ─────────────────────────────────────────────────

    def start(self, daemon: bool = True) -> bool:
        """Start the polling loop.

        Args:
            daemon: If True, run in a background thread and return. If
                False, block (call stop() from another thread).

        Returns:
            True if the watcher started (even with no desktop backend —
            the loop then simply polls a no-op state).
        """
        if self.running:
            return True
        self.running = True
        # Prime the last-seen state so the first poll doesn't fire a
        # spurious change event for the window that's already active.
        try:
            self._capture_state()
        except Exception:
            pass
        if daemon:
            self._thread = threading.Thread(
                target=self._run_loop, name="friday-desktop-watcher",
                daemon=True,
            )
            self._thread.start()
        else:
            self._run_loop()
        return True

    def stop(self) -> None:
        """Stop the polling loop."""
        self.running = False
        if self._thread is not None:
            self._thread.join(timeout=max(self.poll_interval + 1, 3))
            self._thread = None

    # ── Polling ───────────────────────────────────────────────────

    def _run_loop(self) -> None:
        while self.running:
            try:
                self.poll_once()
            except Exception as exc:
                logger.debug(f"Desktop watcher poll error: {exc}")
            # Event-driven sleep: wake early on stop() via the flag check.
            deadline = threading.Event()
            for _ in range(int(self.poll_interval / 0.05)):
                if not self.running:
                    return
                deadline.wait(0.05)

    def poll_once(self) -> None:
        """Sample desktop state once and fire callbacks for changes."""
        if not self.running:
            return
        active = None
        workspace = None
        try:
            if self.wm.is_available:
                active = self.wm.get_active_window()
                workspace = self.wm.get_active_workspace()
        except Exception as exc:
            logger.debug(f"Desktop state sample failed: {exc}")
            return

        self._fire_window_change(active)
        self._fire_workspace_change(workspace)

    # ── Change detection ──────────────────────────────────────────

    def _capture_state(self) -> None:
        """Record current state without firing callbacks (used on start)."""
        if not self.wm.is_available:
            return
        active = self.wm.get_active_window()
        if active:
            self._last_window_id = active.window_id or active.title
            self._last_app = active.app_class or ""
        ws = self.wm.get_active_workspace()
        if ws:
            self._last_workspace_id = ws.id

    def _fire_window_change(self, active: Optional[WindowInfo]) -> None:
        window_id = (active.window_id or active.title) if active else ""
        if window_id and window_id != self._last_window_id:
            self._last_window_id = window_id
            if self.on_window_change and active is not None:
                try:
                    self.on_window_change(active)
                except Exception as exc:
                    logger.debug(f"on_window_change callback failed: {exc}")

        app = active.app_class or "" if active else ""
        if app and app != self._last_app:
            self._last_app = app
            if self.on_app_change:
                try:
                    self.on_app_change(app)
                except Exception as exc:
                    logger.debug(f"on_app_change callback failed: {exc}")

    def _fire_workspace_change(self, workspace: Optional[WorkspaceInfo]) -> None:
        if workspace is None:
            return
        if workspace.id != self._last_workspace_id:
            self._last_workspace_id = workspace.id
            if self.on_workspace_change:
                try:
                    self.on_workspace_change(workspace)
                except Exception as exc:
                    logger.debug(f"on_workspace_change callback failed: {exc}")

    def __repr__(self) -> str:
        return (f"<DesktopWatcher running={self.running} "
                f"window='{self._last_app}' ws={self._last_workspace_id}>")
