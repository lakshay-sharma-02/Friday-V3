"""Hyprland adapter for the desktop abstraction layer.

Ports Friday V3's Hyprland window-manager integration (via ``hyprctl``)
into a Wave-2 platform adapter. Hyprland is the primary platform —
a Wayland compositor controlled through the ``hyprctl`` JSON IPC.

Capabilities:
- List/focus windows (by class, title, pid, or natural language)
- Switch workspaces, list workspace/monitor state
- Screenshots via ``grim`` (Wayland-native)
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from .wm_abstraction import (
    DesktopAbstraction,
    MonitorInfo,
    WindowInfo,
    WorkspaceInfo,
    _shquote,
)

logger = logging.getLogger("friday_v4.desktop.hyprland")

# Find hyprctl wherever it is in PATH
_HYPRCTL: str = shutil.which("hyprctl") or "/usr/bin/hyprctl"


class HyprlandAdapter(DesktopAbstraction):
    """Hyprland window manager backend (via hyprctl)."""

    name = "hyprland"

    def __init__(self):
        self._session = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")

    def is_available(self) -> bool:
        return bool(self._session) and Path(_HYPRCTL).exists()

    # ── Internal helpers ──────────────────────────────────────────

    def _run_hyprctl(self, args: list[str]) -> Optional[str]:
        """Run hyprctl with JSON output."""
        try:
            result = subprocess.run(
                [_HYPRCTL, *args, "-j"],  # JSON output
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            logger.warning(f"hyprctl failed: {exc}")
        return None

    def _hyprland_active_window(self) -> Optional[WindowInfo]:
        """Get active window info via hyprctl activewindow."""
        raw = self._run_hyprctl(["activewindow"])
        if not raw:
            return None

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None

        if not data or data.get("window_id") is None:
            if "class" not in data and "title" not in data:
                return None

        return WindowInfo(
            window_id=str(data.get("address", "")),
            title=data.get("title", ""),
            app_class=data.get("class", ""),
            workspace_id=data.get("workspace", {}).get("id", 0)
                if isinstance(data.get("workspace"), dict) else data.get("workspace", 0),
            workspace_name=str(data.get("workspace", {}).get("name", "")
                if isinstance(data.get("workspace"), dict) else ""),
            x=data.get("at", [0, 0])[0] if data.get("at") else 0,
            y=data.get("at", [0, 0])[1] if data.get("at") else 0,
            width=data.get("size", [0, 0])[0] if data.get("size") else 0,
            height=data.get("size", [0, 0])[1] if data.get("size") else 0,
            monitor=data.get("monitor", 0),
            pid=data.get("pid", 0),
            floating=data.get("floating", False),
            is_active=True,
        )

    def _hyprland_workspaces(self) -> list[WorkspaceInfo]:
        """Get all workspaces via hyprctl workspaces."""
        raw = self._run_hyprctl(["workspaces"])
        if not raw:
            return []

        active_raw = self._run_hyprctl(["activeworkspace"])
        active_id = None
        if active_raw:
            try:
                active_data = json.loads(active_raw)
                active_id = active_data.get("id")
            except json.JSONDecodeError:
                pass

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []

        workspaces = []
        for entry in data:
            ws = WorkspaceInfo(
                id=entry.get("id", 0),
                name=entry.get("name", str(entry.get("id", 0))),
                monitor=entry.get("monitor", ""),
                window_count=entry.get("windows", 0),
                is_active=entry.get("id") == active_id,
                last_window_title=entry.get("lastwindowtitle", ""),
            )
            workspaces.append(ws)
        return workspaces

    def _hyprland_monitors(self) -> list[MonitorInfo]:
        """Get monitor info via hyprctl monitors."""
        raw = self._run_hyprctl(["monitors"])
        if not raw:
            return []

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []

        monitors = []
        for entry in data:
            mon = MonitorInfo(
                name=entry.get("name", ""),
                width=entry.get("width", 0),
                height=entry.get("height", 0),
                refresh_rate=entry.get("refreshRate", 0.0),
                is_active=entry.get("focused", False),
                active_workspace=entry.get("activeWorkspace", {}).get("id", 0)
                    if isinstance(entry.get("activeWorkspace"), dict)
                    else entry.get("activeWorkspace", 0),
                scale=entry.get("scale", 1.0),
                make=entry.get("make", ""),
                model=entry.get("model", ""),
            )
            monitors.append(mon)
        return monitors

    # ── Read ──────────────────────────────────────────────────────

    def get_status(self) -> dict:
        """Get comprehensive Hyprland desktop status."""
        monitors = self._hyprland_monitors()
        workspaces = self._hyprland_workspaces()
        windows = self._hyprland_windows()
        active = self._hyprland_active_window()

        return {
            "desktop": "hyprland",
            "monitors": [m.__dict__ for m in monitors],
            "workspaces": [w.__dict__ for w in workspaces],
            "windows": [w.__dict__ for w in windows],
            "active_window": active.__dict__ if active else None,
            "window_count": len(windows),
        }

    def _hyprland_windows(self) -> list[WindowInfo]:
        """Get all windows via hyprctl clients."""
        raw = self._run_hyprctl(["clients"])
        if not raw:
            return []

        active_window = self._hyprland_active_window()
        active_id = active_window.window_id if active_window else ""

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []

        windows = []
        for entry in data:
            win = WindowInfo(
                window_id=entry.get("address", ""),
                title=entry.get("title", ""),
                app_class=entry.get("class", ""),
                workspace_id=entry.get("workspace", {}).get("id", 0)
                    if isinstance(entry.get("workspace"), dict)
                    else entry.get("workspace", 0),
                workspace_name=str(entry.get("workspace", {}).get("name", "")
                    if isinstance(entry.get("workspace"), dict)
                    else entry.get("workspace", "")),
                x=entry.get("at", [0, 0])[0],
                y=entry.get("at", [0, 0])[1],
                width=entry.get("size", [0, 0])[0],
                height=entry.get("size", [0, 0])[1],
                monitor=entry.get("monitor", 0),
                pid=entry.get("pid", 0),
                floating=entry.get("floating", False),
                fullscreen=entry.get("fullscreen", 0) != 0,
                is_active=entry.get("address", "") == active_id,
            )
            windows.append(win)
        return windows

    def list_windows(self) -> list[WindowInfo]:
        return self._hyprland_windows()

    def get_active_window(self) -> Optional[WindowInfo]:
        return self._hyprland_active_window()

    def list_workspaces(self) -> list[WorkspaceInfo]:
        return self._hyprland_workspaces()

    def list_monitors(self) -> list[MonitorInfo]:
        return self._hyprland_monitors()

    # ── Act ───────────────────────────────────────────────────────

    def focus(self, target: str, by: str = "class") -> bool:
        """Focus a window by class, title, or PID."""
        try:
            if by == "class":
                subprocess.run(
                    [_HYPRCTL, "dispatch", "focuswindow", f"class:{target}"],
                    capture_output=True, timeout=3,
                )
                return True
            elif by == "title":
                subprocess.run(
                    [_HYPRCTL, "dispatch", "focuswindow", f"title:{target}"],
                    capture_output=True, timeout=3,
                )
                return True
            elif by == "pid":
                subprocess.run(
                    [_HYPRCTL, "dispatch", "focuswindow", f"pid:{target}"],
                    capture_output=True, timeout=3,
                )
                return True
        except Exception as exc:
            logger.warning(f"Focus failed: {exc}")
        return False

    def switch_workspace(self, workspace_id_or_name: int | str) -> bool:
        """Switch to a workspace by ID or name."""
        try:
            subprocess.run(
                [_HYPRCTL, "dispatch", "workspace", str(workspace_id_or_name)],
                capture_output=True, timeout=3,
            )
            return True
        except Exception as exc:
            logger.warning(f"Workspace switch failed: {exc}")
            return False

    def launch_app(self, app: str, path: Optional[str] = None) -> bool:
        """Launch an app, optionally in a directory (via sh -c)."""
        try:
            cmd = f"cd {_shquote(path)} && {app}" if path else app
            subprocess.Popen(
                ["sh", "-c", f"{cmd} &"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        except Exception as exc:
            logger.warning(f"Launch failed: {exc}")
            return False

    def take_screenshot(self, output_path: Optional[str] = None) -> Optional[str]:
        """Take a screenshot of the current workspace via grim."""
        if output_path is None:
            import datetime
            timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
            pictures = Path.home() / "Pictures"
            pictures.mkdir(exist_ok=True)
            output_path = str(pictures / f"friday_{timestamp}.png")

        try:
            subprocess.run(
                ["grim", output_path],
                capture_output=True, timeout=10,
            )
            if Path(output_path).exists():
                logger.info(f"Screenshot saved to {output_path}")
                return output_path
        except Exception as exc:
            logger.warning(f"Screenshot failed: {exc}")

        return None

    def setup_instructions(self) -> str:
        """Return setup instructions for Hyprland desktop control."""
        if not self._session:
            return (
                "Hyprland desktop control needs an active Hyprland session.\n"
                "Log into a Hyprland session and run Friday from within it "
                "(HYPRLAND_INSTANCE_SIGNATURE must be set)."
            )
        return (
            f"hyprctl not found at {_HYPRCTL}.\n"
            "Install it with your package manager, e.g. "
            "`sudo pacman -S hyprland` or `apt install hyprland`."
        )
