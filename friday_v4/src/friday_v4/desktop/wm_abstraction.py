"""Window Manager Abstraction for Friday V4.

A unified interface for desktop window management across desktop
environments. Follows the Wave 2 plan structure:

    DesktopAbstraction (base interface)  →  wm_abstraction.py
    HyprlandAdapter                      →  hyprland_adapter.py
    GNOMEAdapter                         →  gnome_adapter.py
    KDEAdapter                           →  kde_adapter.py
    MacOSAdapter                         →  macos_adapter.py
    WindowsAdapter                       →  windows_adapter.py

`WindowManager` is the auto-detecting facade used by the CLI, voice
router, and proactive engines. It picks the right adapter for the
current desktop environment and delegates to it, so callers can write
platform-independent code:

    wm = WindowManager()
    status = wm.get_status()
    windows = wm.list_windows()
    wm.focus_smart("code editor")
    wm.switch_workspace(2)
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from typing import ClassVar, Optional

logger = logging.getLogger("friday_v4.desktop.wm")


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------


@dataclass
class WindowInfo:
    """Information about a single window on the desktop."""

    window_id: str = ""
    title: str = ""
    app_class: str = ""       # Window class (e.g., "kitty", "firefox")
    workspace_id: int = 0
    workspace_name: str = ""
    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0
    monitor: int = 0
    pid: int = 0
    floating: bool = False
    fullscreen: bool = False
    is_active: bool = False

    @property
    def app_name(self) -> str:
        """Human-readable app name from class."""
        name_map = {
            "kitty": "Code Editor",
            "alacritty": "Terminal",
            "foot": "Terminal",
            "wezterm": "Terminal",
            "Code": "VS Code",
            "code-oss": "VS Code",
            "firefox": "Browser",
            "firefoxdeveloperedition": "Browser",
            "chromium": "Browser",
            "chromium-browser": "Browser",
            "google-chrome": "Browser",
            "brave-browser": "Browser",
            "zen": "Browser",
            "thunar": "File Manager",
            "nautilus": "File Manager",
            "dolphin": "File Manager",
            "slack": "Slack",
            "discord": "Discord",
            "spotify": "Spotify",
            "zcode": "ZCode",
            "obsidian": "Obsidian",
        }
        return name_map.get(self.app_class, self.app_class.title())


@dataclass
class WorkspaceInfo:
    """Information about a workspace."""

    id: int = 0
    name: str = ""
    monitor: str = ""
    window_count: int = 0
    is_active: bool = False
    last_window_title: str = ""


@dataclass
class MonitorInfo:
    """Information about a monitor/display."""

    name: str = ""
    width: int = 0
    height: int = 0
    refresh_rate: float = 0.0
    is_active: bool = False
    active_workspace: int = 0
    scale: float = 1.0
    make: str = ""
    model: str = ""


# ---------------------------------------------------------------------------
# Smart Window Resolver — Maps Natural Names to Actual Apps
# ---------------------------------------------------------------------------


class SmartWindowResolver:
    """Maps natural language app names to actual window classes/titles.

    This is what enables saying "Friday, focus code editor" and having
    Friday understand you mean kitty or VS Code.

    Resolution strategy:
      1. Direct match — "kitty" → class:kitty
      2. Semantic match — "code editor" → ["kitty", "code", "alacritty"]
      3. Fuzzy match — "browser" → ["firefox", "chromium", "brave"]
      4. Best guess — pick the most likely window from open windows
    """

    # Natural name → possible window classes
    SEMANTIC_MAP: ClassVar[dict[str, list[str]]] = {
        "code editor": ["kitty", "Code", "code-oss", "alacritty",
                        "wezterm", "foot", "zcode"],
        "terminal": ["kitty", "alacritty", "wezterm", "foot",
                     "gnome-terminal", "konsole"],
        "editor": ["Code", "code-oss", "vim", "nvim", "neovim",
                   "sublime_text", "jetbrains"],
        "browser": ["firefox", "firefoxdeveloperedition",
                    "chromium", "chromium-browser",
                    "google-chrome", "brave-browser", "zen"],
        "files": ["thunar", "nautilus", "dolphin", "nemo"],
        "file manager": ["thunar", "nautilus", "dolphin", "nemo"],
        "chat": ["slack", "discord", "telegram", "whatsapp"],
        "slack": ["slack"],
        "discord": ["discord"],
        "music": ["spotify"],
        "spotify": ["spotify"],
        "vs code": ["Code", "code-oss"],
        "vscode": ["Code", "code-oss"],
        "zcode": ["zcode"],
        "obsidian": ["obsidian"],
        "notes": ["obsidian", "logseq"],
        "settings": ["gnome-control-center", "systemsettings",
                     "xfce4-settings-manager"],
    }

    # Common words to strip from natural language queries
    _STOP_WORDS: ClassVar[set[str]] = {"the", "a", "an", "my", "to",
                                        "please", "could", "would", "can",
                                        "switch", "focus", "open", "go"}

    @classmethod
    def resolve(cls, query: str, open_windows: list[WindowInfo]) -> Optional[str]:
        """Resolve a natural language query to a window class.

        Args:
            query: Natural language (e.g., "focus code editor", "switch to browser")
            open_windows: List of currently open windows

        Returns:
            Window class to focus, or None if no match found
        """
        query_lower = query.lower().strip()

        # Strip stop words and commands
        words = [w for w in query_lower.split()
                 if w not in cls._STOP_WORDS]

        # Try each word/phrase as a semantic lookup
        for phrase in [query_lower, *words]:
            # Check semantic map
            if phrase in cls.SEMANTIC_MAP:
                candidates = cls.SEMANTIC_MAP[phrase]
                # Find the first open window matching any candidate
                for w in open_windows:
                    if w.app_class.lower() in [c.lower() for c in candidates]:
                        logger.info(f"Resolved '{phrase}' → {w.app_class}")
                        return w.app_class

            # Check direct class match
            for w in open_windows:
                if phrase == w.app_class.lower():
                    return w.app_class
                # Check if phrase is contained in title
                if phrase in w.title.lower():
                    return w.app_class

        # If nothing matched but we have open windows, return None
        return None

    @classmethod
    def suggest_for_window(cls, window: WindowInfo) -> list[str]:
        """Suggest natural language names for a window.

        Used to help users discover what they can say.
        """
        suggestions = []
        for name, classes in cls.SEMANTIC_MAP.items():
            if window.app_class.lower() in [c.lower() for c in classes]:
                suggestions.append(name)
        return suggestions


# ---------------------------------------------------------------------------
# Natural-language desktop command router (CLI/web surface)
# ---------------------------------------------------------------------------

#: Verbs that resolve to desktop actions, mapped to the WM operation.
_DESKTOP_TEXT_ACTIONS: dict[str, str] = {
    "focus": "focus", "show": "focus", "switch": "workspace",
    "go to": "workspace", "open": "open", "launch": "launch",
    "screenshot": "screenshot", "capture": "screenshot",
    "snapshot": "screenshot", "take": "screenshot",
}


def _has_word(text: str, word: str) -> bool:
    import re
    return bool(re.search(rf"\b{re.escape(word)}\b", text))


def desktop_text_command(text: str) -> str:
    """Route one natural-language desktop command to the window manager.

    The §2 Wave-2 hardening entry point for the text surfaces (``friday4
    talk`` and the web dashboard chat) — desktop control is no longer
    voice-only. Same verbs as the voice router: focus / switch workspace /
    open / launch / screenshot / status. Never raises: an unavailable
    desktop degrades to an honest message (never a crash).

    Returns the response string, or "" when nothing matched (callers fall
    through to the normal chat fallback).
    """
    raw = (text or "").strip()
    if not raw:
        return ""
    lower = raw.lower()
    try:
        wm = WindowManager()
    except Exception as exc:
        logger.debug(f"desktop_text_command: no WM ({exc})")
        return "Desktop control isn't available on this system."
    if not wm.is_available:
        return "Desktop control isn't available on this system."

    # Read-queries take precedence over action words.
    if ("what am i working on" in lower or "what's on my screen" in lower
            or "what is open" in lower or "what's open" in lower
            or "what windows" in lower or "whats open" in lower
            or "show desktop" in lower):
        return _desktop_status_text(wm)

    for action, op in _DESKTOP_TEXT_ACTIONS.items():
        if not _has_word(lower, action):
            continue
        idx = lower.index(action) + len(action)
        target = raw[idx:].strip()
        for prefix in ("to", "the", "me"):
            target = target.removeprefix(prefix).strip()
        if op == "focus":
            if not target:
                return "What would you like me to focus?"
            resolved = wm.focus_smart(target)
            return f"Focused {resolved}." if resolved else \
                f"I couldn't find '{target}'."
        if op == "open":
            return _desktop_open_or_launch(wm, target, "open")
        if op == "launch":
            return _desktop_open_or_launch(wm, target, "launch")
        if op == "workspace":
            return _desktop_workspace(wm, target)
        if op == "screenshot":
            return "Screenshot saved." if wm.take_screenshot() \
                else "Sorry, I couldn't take a screenshot."
    return ""


def _desktop_open_or_launch(wm, target: str, verb: str) -> str:
    if not target:
        return f"What would you like me to {verb}?"
    resolved = wm.focus_smart(target)
    if resolved:
        return f"Focused {resolved}."
    if wm.launch_app(target):
        return f"Launching {target}."
    return f"I couldn't {verb} '{target}'."


def _desktop_workspace(wm, target: str) -> str:
    target = target.strip()
    for prefix in ("workspace", "to workspace", "desktop", "to desktop"):
        if target.lower().startswith(prefix):
            target = target[len(prefix):].strip()
    nums = [int(n) for n in target.split() if n.isdigit()]
    if nums:
        if wm.switch_workspace(nums[0]):
            return f"Switched to workspace {nums[0]}."
    if target:
        try:
            for ws in wm.list_workspaces():
                if target.lower() in ws.name.lower():
                    if wm.switch_workspace(ws.id):
                        return f"Switching to workspace {ws.id}."
        except Exception:
            pass
        windows = wm.list_windows()
        resolved = SmartWindowResolver.resolve(target, windows)
        if resolved:
            for w in windows:
                if w.app_class.lower() == resolved.lower():
                    if wm.switch_workspace(w.workspace_id):
                        return (f"Switching to workspace {w.workspace_id} "
                                f"where {resolved} is open.")
    return f"I couldn't find workspace '{target}'."


def _desktop_status_text(wm) -> str:
    try:
        active = wm.get_active_window()
        windows = wm.list_windows()
        workspaces = wm.list_workspaces()
        parts = []
        if active:
            parts.append(f"You're in {active.app_name} on "
                         f"workspace {active.workspace_id}")
            if active.title and "friday" not in active.title.lower():
                parts.append(f"Working on {active.title[:40]}")
        parts.append(f"{len(windows)} windows open across "
                     f"{len(workspaces)} workspaces")
        return ". ".join(parts) + "."
    except Exception:
        return "I couldn't check your desktop status right now."


# ---------------------------------------------------------------------------
# Desktop Environment Detection
# ---------------------------------------------------------------------------


def _shquote(s: str) -> str:
    """Minimal POSIX single-quote escaping for shell interpolation."""
    return "'" + s.replace("'", "'\\''") + "'"


def detect_desktop_environment() -> str:
    """Detect the current desktop environment / window manager.

    Returns one of: "hyprland", "gnome", "kde", "sway", "i3",
    "macos", "windows", "unknown"
    """
    if os.name == "nt":
        return "windows"

    if sys.platform == "darwin":
        return "macos"

    # Hyprland
    if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
        return "hyprland"

    # Wayland compositors
    if os.environ.get("WAYLAND_DISPLAY"):
        de = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
        if "gnome" in de:
            return "gnome"
        if "kde" in de or "plasma" in de:
            return "kde"
        if "sway" in de:
            return "sway"
        return "wayland"

    # X11
    if os.environ.get("DISPLAY"):
        de = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
        if "gnome" in de:
            return "gnome"
        if "kde" in de or "plasma" in de:
            return "kde"
        if "i3" in de:
            return "i3"
        return "x11"

    return "unknown"


# ---------------------------------------------------------------------------
# Generic desktop notification helpers (used by the base adapter)
# ---------------------------------------------------------------------------


def _notify_linux(title: str, message: str, urgency: str = "normal",
                  timeout_ms: Optional[int] = None) -> bool:
    """Send a desktop notification via notify-send (Linux).

    Args:
        timeout_ms: Auto-dismiss timeout in milliseconds. Passed as
            ``notify-send -t`` so the banner fades on daemons that honor
            it (dunst, mako, KDE). GNOME ignores this only for
            ``critical`` urgency, which is why auto-dismissable
            notifications must not use ``critical``. When None the server
            default applies.
    """
    try:
        cmd = ["notify-send", "-a", "Friday", "-u", urgency]
        if timeout_ms is not None:
            cmd += ["-t", str(int(timeout_ms))]
        subprocess.run(
            cmd + [title, message],
            capture_output=True, timeout=3,
        )
        return True
    except Exception:
        return False


def _notify_macos(title: str, message: str) -> bool:
    """Send a desktop notification via osascript (macOS)."""
    script = (
        f'display notification "{message}" with title "Friday: {title}"'
    )
    try:
        subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, timeout=5,
        )
        return True
    except Exception:
        return False


def _notify_windows(title: str, message: str) -> bool:
    """Send a desktop notification via PowerShell balloon tip (Windows)."""
    script = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "$n = New-Object System.Windows.Forms.NotifyIcon; "
        "$n.Icon = [System.Drawing.SystemIcons]::Information; "
        "$n.Visible = $true; "
        f"$n.ShowBalloonTip(3000, '{title}', '{message}', "
        "[System.Windows.Forms.ToolTipIcon]::Info); "
        "Start-Sleep -Milliseconds 3500; $n.Dispose()"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True, timeout=10,
        )
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# DesktopAbstraction — Base Interface
# ---------------------------------------------------------------------------


class DesktopAbstraction:
    """Base interface for a desktop environment adapter.

    Platform adapters (Hyprland, GNOME, KDE, macOS, Windows) subclass this
    and implement the operations that make sense for their environment.
    Operations that don't apply return safe defaults (empty lists, False,
    None) so callers never crash on an unsupported platform.

    The interface is deliberately small and covers the Wave 2 scope:
      - Read:   status, windows, active window, workspaces, monitors
      - Act:    focus window, switch workspace, launch app, screenshot
      - Signal: desktop notifications
    """

    name: str = "unknown"

    # ── Availability ──────────────────────────────────────────────

    def is_available(self) -> bool:
        """Whether this adapter can talk to the current desktop."""
        return False

    @property
    def desktop_environment(self) -> str:
        return self.name

    # ── Read ──────────────────────────────────────────────────────

    def get_status(self) -> dict:
        """Full desktop status — workspaces, active window, monitors."""
        if not self.is_available():
            return {"error": f"Desktop environment '{self.name}' not supported yet"}
        return {"desktop": self.name}

    def list_windows(self) -> list[WindowInfo]:
        """List all open windows across all workspaces."""
        return []

    def get_active_window(self) -> Optional[WindowInfo]:
        """Get the currently focused window."""
        return None

    def list_workspaces(self) -> list[WorkspaceInfo]:
        """List all workspaces."""
        return []

    def get_active_workspace(self) -> Optional[WorkspaceInfo]:
        """Get the currently active workspace."""
        for ws in self.list_workspaces():
            if ws.is_active:
                return ws
        return None

    def list_monitors(self) -> list[MonitorInfo]:
        """List connected monitors/displays."""
        return []

    # ── Act ───────────────────────────────────────────────────────

    def focus(self, target: str, by: str = "class") -> bool:
        """Focus a window by class, title, or pid."""
        return False

    def focus_smart(self, query: str) -> Optional[str]:
        """Focus a window using natural language.

        Uses SmartWindowResolver to figure out what the user means.
        Returns the focused window class, or None if nothing matched.
        """
        windows = self.list_windows()
        resolved = SmartWindowResolver.resolve(query, windows)
        if resolved:
            if self.focus(resolved, "class"):
                return resolved
        return None

    def switch_workspace(self, workspace_id_or_name: int | str) -> bool:
        """Switch to a workspace by ID or name."""
        return False

    def launch_app(self, app: str, path: Optional[str] = None) -> bool:
        """Launch an application, optionally in a directory.

        Args:
            app: Application name, command, or executable path
            path: Optional working directory / project to open
        """
        return False

    def take_screenshot(self, output_path: Optional[str] = None) -> Optional[str]:
        """Take a screenshot of the current workspace.

        Returns the path to the saved screenshot, or None on failure.
        """
        return None

    # ── Notifications ─────────────────────────────────────────────

    def setup_instructions(self) -> str:
        """Return human-readable setup instructions for this platform.

        Subclasses override this when the desktop environment needs tools,
        permissions, or configuration before the adapter can work. The CLI
        surfaces this via ``friday4 desktop platforms`` when unavailable.
        """
        return (
            f"Desktop integration for '{self.name}' is not available on "
            "this machine. Install the required tools and try again."
        )

    @staticmethod
    def notify(title: str, message: str, urgency: str = "normal",
               timeout_ms: Optional[int] = None) -> bool:
        """Send a desktop notification on the current platform.

        Args:
            title: Notification title
            message: Notification body
            urgency: "low", "normal", or "critical" (Linux only)
            timeout_ms: Auto-dismiss timeout in milliseconds (Linux only).
                Defaults to the server default when None. Prefer an
                explicit timeout over ``urgency="critical"`` for anything
                that should fade on its own — critical banners are
                persistent on GNOME and several other desktops.
        """
        if os.name == "nt":
            return _notify_windows(title, message)
        if sys.platform == "darwin":
            return _notify_macos(title, message)
        return _notify_linux(title, message, urgency, timeout_ms)

    # ── Helpers ───────────────────────────────────────────────────

    def _run(self, cmd: list[str], timeout: int = 5) -> Optional[str]:
        """Run a subprocess and return stdout, or None on failure."""
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            logger.warning(f"[{self.name}] command failed ({cmd[0]}): {exc}")
        return None

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} available={self.is_available()}>"


# ---------------------------------------------------------------------------
# Adapter Registry
# ---------------------------------------------------------------------------

#: Names of every platform the abstraction layer supports (for `--help`).
SUPPORTED_PLATFORMS = [
    "hyprland",
    "gnome",
    "kde",
    "macos",
    "windows",
]


def create_adapter(de: str | None = None) -> DesktopAbstraction:
    """Create the adapter for a desktop environment name.

    Args:
        de: Desktop environment name (e.g. "hyprland", "gnome"). If None,
            auto-detects the current environment.

    Returns:
        A concrete adapter instance, or a bare ``DesktopAbstraction``
        (unavailable) if the environment is unknown/unsupported.
    """
    key = (de or detect_desktop_environment()).lower()

    # sway uses Hyprland-like wlroots IPC — best-effort via the
    # Hyprland adapter's hyprctl-style commands.
    if key in ("hyprland", "sway"):
        from .hyprland_adapter import HyprlandAdapter
        return HyprlandAdapter()
    if key == "gnome":
        from .gnome_adapter import GNOMEAdapter
        return GNOMEAdapter()
    if key in ("kde", "plasma"):
        from .kde_adapter import KDEAdapter
        return KDEAdapter()
    if key in ("macos", "darwin"):
        from .macos_adapter import MacOSAdapter
        return MacOSAdapter()
    if key in ("windows", "win32", "nt"):
        from .windows_adapter import WindowsAdapter
        return WindowsAdapter()

    # Generic X11/Wayland/i3: GNOMEAdapter's wmctrl/xdotool fallbacks work
    # on most X11 sessions and Shell Eval on GNOME Wayland.
    if key in ("wayland", "x11", "i3"):
        from .gnome_adapter import GNOMEAdapter
        return GNOMEAdapter()

    logger.debug(f"No adapter for '{key}', using base DesktopAbstraction")
    return DesktopAbstraction()


# ---------------------------------------------------------------------------
# WindowManager — Facade / Main Desktop Interface
# ---------------------------------------------------------------------------


class WindowManager:
    """Auto-detecting facade over the platform adapters.

    This is the class callers should use. It detects the current desktop
    environment, instantiates the matching adapter, and delegates to it.
    All public methods of ``DesktopAbstraction`` are available.

    Usage:
        wm = WindowManager()
        status = wm.get_status()
        windows = wm.list_windows()
        wm.focus_smart("code editor")
        wm.switch_workspace(2)
    """

    def __init__(self, de: str | None = None):
        self._de = de or detect_desktop_environment()
        self._adapter = create_adapter(self._de)
        logger.info(
            f"Desktop environment: {self._de} "
            f"({'available' if self.is_available else 'unavailable'})"
        )

    # ── Delegation ────────────────────────────────────────────────

    def __getattr__(self, name: str):
        """Delegate interface methods to the active adapter."""
        adapter = self.__dict__.get("_adapter")
        if adapter is None:
            raise AttributeError(name)
        # Only delegate public interface methods (avoids masking attributes)
        if name.startswith("_") or name in ("is_available", "desktop_environment"):
            raise AttributeError(name)
        attr = getattr(adapter, name)
        if attr is None:
            raise AttributeError(name)
        return attr

    @property
    def is_available(self) -> bool:
        return self._adapter.is_available()

    @property
    def desktop_environment(self) -> str:
        return self._de

    # Send a desktop notification on the current platform.
    notify = staticmethod(DesktopAbstraction.notify)

    def __repr__(self) -> str:
        return (f"<WindowManager de={self._de} "
                f"adapter={self._adapter.__class__.__name__} "
                f"available={self.is_available}>")
