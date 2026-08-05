"""macOS adapter for the desktop abstraction layer.

Controls macOS via AppleScript ``osascript`` (System Events / Accessibility
API). Per the Wave 2 roadmap, the macOS Accessibility API requires the
user to manually grant permission:
    System Settings → Privacy & Security → Accessibility
This CANNOT be automated — the adapter must detect the permission-denied
case gracefully and surface setup instructions instead of crashing.

Capabilities:
- List frontmost/focused window and open apps
- Focus an application (frontmost)
- Launch apps via ``open -a``
- Screenshots via ``screencapture``
- Desktop notifications via ``display notification``
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from .wm_abstraction import (
    DesktopAbstraction,
    MonitorInfo,
    WindowInfo,
    WorkspaceInfo,
)

logger = logging.getLogger("friday_v6.desktop.macos")

# Permission-denied markers from osascript's accessibility check
_PERMISSION_MARKERS = (
    "not allowed assistive access",
    "assistive access is disabled",
    "osascript is not allowed",
    "is not allowed to send Apple events",
    "not authorized to send Apple events",
    "-1743",
    "-25211",
)


class MacOSAdapter(DesktopAbstraction):
    """macOS backend via AppleScript System Events."""

    name = "macos"

    def __init__(self):
        self._has_osascript = shutil.which("osascript") is not None
        self._permission_granted: Optional[bool] = None  # None = unknown

    def is_available(self) -> bool:
        return sys.platform == "darwin" and self._has_osascript

    # ── Internal helpers ──────────────────────────────────────────

    def _osascript(self, script: str) -> Optional[str]:
        """Run an AppleScript and return stdout, or None on failure."""
        if not self._has_osascript:
            return None
        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                return result.stdout.strip()
            err = (result.stderr or "").lower()
            if any(m in err for m in _PERMISSION_MARKERS):
                self._permission_granted = False
                logger.warning(
                    "macOS Accessibility permission not granted. "
                    "Enable it at System Settings → Privacy & Security → "
                    "Accessibility for your terminal app."
                )
            else:
                logger.debug(f"osascript error: {result.stderr.strip()}")
        except Exception as exc:
            logger.debug(f"osascript failed: {exc}")
        return None

    # ── Read ──────────────────────────────────────────────────────

    def get_status(self) -> dict:
        windows = self.list_windows()
        active = self.get_active_window()
        return {
            "desktop": "macos",
            # None = unknown; True = granted; False = denied (setup needed)
            "accessibility_granted": self._permission_granted,
            "setup_instructions": self.setup_instructions(),
            "workspaces": [],
            "windows": [w.__dict__ for w in windows],
            "active_window": active.__dict__ if active else None,
            "window_count": len(windows),
        }

    def list_windows(self) -> list[WindowInfo]:
        """List frontmost windows of visible apps (System Events).

        AppleScript:
            tell application "System Events"
                repeat with p in (every process whose background only is false)
                    … name of p …
                end repeat
            end tell
        """
        # NOTE: build a plain string (not an AppleScript list) so the
        # "|" separator is preserved — osascript renders lists with a
        # paragraph separator (¶), which would contaminate app names.
        script = (
            'tell application "System Events"\n'
            'set output to ""\n'
            'repeat with p in (every process whose background only is false)\n'
            "  set output to output & (name of p) & \"|\"\n"
            "end repeat\n"
            "return output\n"
            "end tell"
        )
        out = self._osascript(script)
        if not out:
            return []

        # Frontmost app first (System Events returns ordered by frontmost)
        apps = [a for a in out.split("|") if a.strip()]
        frontmost = self._get_frontmost_app()
        if frontmost and frontmost in apps:
            apps.remove(frontmost)
            apps.insert(0, frontmost)

        windows = []
        for i, app in enumerate(apps):
            win = WindowInfo(
                window_id=str(i),
                app_class=app,
                title=self._get_window_title(app),
                is_active=(app == frontmost and i == 0),
            )
            windows.append(win)
        return windows

    def _get_frontmost_app(self) -> str:
        """Get the name of the frontmost application."""
        script = (
            'tell application "System Events" to get name of '
            "first application process whose frontmost is true"
        )
        out = self._osascript(script)
        return out or ""

    def _get_window_title(self, app: str) -> str:
        """Get the main window title of an application (best-effort)."""
        script = (
            'tell application "System Events" to tell process "' + app + '" to '
            "get name of first window"
        )
        # _osascript already returns None for errors (stderr), so only
        # stdout with returncode 0 reaches here.
        out = self._osascript(script)
        return out or ""

    def get_active_window(self) -> Optional[WindowInfo]:
        frontmost = self._get_frontmost_app()
        if not frontmost:
            return None
        return WindowInfo(
            window_id="0",
            app_class=frontmost,
            title=self._get_window_title(frontmost),
            is_active=True,
        )

    def list_workspaces(self) -> list[WorkspaceInfo]:
        # macOS Spaces are not scriptable via Accessibility API.
        return []

    def list_monitors(self) -> list[MonitorInfo]:
        script = (
            'tell application "System Events" to get size of every desktop'
        )
        out = self._osascript(script)
        if not out:
            return []
        # Output looks like "1366, 768"
        m = re.match(r"(\d+),\s*(\d+)", out)
        if m:
            return [MonitorInfo(
                name="Main Display",
                width=int(m.group(1)),
                height=int(m.group(2)),
                is_active=True,
            )]
        return []

    # ── Act ───────────────────────────────────────────────────────

    def focus(self, target: str, by: str = "class") -> bool:
        """Focus an application (frontmost). `target` is the app name."""
        script = (
            'tell application "System Events" to set frontmost of process "'
            + target
            + '" to true'
        )
        return self._osascript(script) is not None

    def switch_workspace(self, workspace_id_or_name: int | str) -> bool:
        # macOS Spaces can't be switched via Accessibility API.
        return False

    def launch_app(self, app: str, path: Optional[str] = None) -> bool:
        """Launch an app via `open -a` (or `open` a directory/project)."""
        try:
            if path:
                subprocess.Popen(["open", path], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
            else:
                subprocess.Popen(["open", "-a", app], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
            return True
        except Exception as exc:
            logger.warning(f"macOS launch failed: {exc}")
        return False

    def take_screenshot(self, output_path: Optional[str] = None) -> Optional[str]:
        """Take a screenshot via screencapture (works without Accessibility)."""
        if output_path is None:
            import datetime
            timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
            pictures = Path.home() / "Pictures"
            pictures.mkdir(exist_ok=True)
            output_path = str(pictures / f"friday_{timestamp}.png")

        try:
            subprocess.run(
                ["screencapture", "-x", output_path],
                capture_output=True, timeout=10,
            )
            if Path(output_path).exists():
                return output_path
        except Exception as exc:
            logger.warning(f"macOS screenshot failed: {exc}")
        return None

    def setup_instructions(self) -> str:
        """Return human-readable setup instructions for macOS permissions."""
        return (
            "macOS requires Accessibility permission for desktop control.\n"
            "1. Open System Settings → Privacy & Security → Accessibility\n"
            "2. Enable access for your terminal application\n"
            "3. Restart the terminal and try again"
        )
