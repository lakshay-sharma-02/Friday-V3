"""Desktop Awareness & Control for Friday V4.

Gives Friday eyes and hands on your desktop:
- See what windows you have open
- Know which workspace you're on
- Focus any window by name
- Switch workspaces
- Launch applications
- Take screenshots
- Send desktop notifications
- System tray presence + global hotkeys (Wave 2)
- Ambient feed → desktop notification channel (Wave 2)

Platform support (progressive):
  1. Hyprland (primary — Wayland, daily driver)     ✅
  2. GNOME (Wayland/X11)                            ✅
  3. KDE (X11/Wayland)                              ✅
  4. macOS (Accessibility API)                      ✅
  5. Windows (Win32/PowerShell)                     ✅

All platforms share the ``DesktopAbstraction`` interface; use
``WindowManager`` to auto-detect and control the current environment.
"""

from __future__ import annotations

from .wm_abstraction import (
    DesktopAbstraction,
    MonitorInfo,
    SUPPORTED_PLATFORMS,
    SmartWindowResolver,
    WindowInfo,
    WindowManager,
    WorkspaceInfo,
    create_adapter,
    detect_desktop_environment,
)
from .tray import SystemTray
from .hotkeys import GlobalHotkeys
from .notifier import DesktopNotificationChannel
from .watcher import DesktopWatcher

__all__ = [
    "DesktopAbstraction",
    "WindowManager",
    "WindowInfo",
    "WorkspaceInfo",
    "MonitorInfo",
    "SmartWindowResolver",
    "SystemTray",
    "GlobalHotkeys",
    "DesktopNotificationChannel",
    "DesktopWatcher",
    "SUPPORTED_PLATFORMS",
    "create_adapter",
    "detect_desktop_environment",
]
