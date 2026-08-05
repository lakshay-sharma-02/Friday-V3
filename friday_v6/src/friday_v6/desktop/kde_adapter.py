"""KDE adapter for the desktop abstraction layer.

Controls KDE Plasma via ``qdbus`` (KWin) and ``wmctrl`` / ``xdotool``
for X11 windows. KWin exposes desktop/window APIs over D-Bus.

Capabilities:
- List/focus windows (via wmctrl/xdotool on X11, KWin scripting best-effort)
- Switch workspaces via KWin's D-Bus interface
- Launch apps via ``kstart`` / ``gtk-launch``
"""

from __future__ import annotations

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

logger = logging.getLogger("friday_v6.desktop.kde")


class KDEAdapter(DesktopAbstraction):
    """KDE Plasma backend (qdbus/KWin + wmctrl/xdotool)."""

    name = "kde"

    def __init__(self):
        self._is_wayland = bool(os.environ.get("WAYLAND_DISPLAY"))
        self._has_qdbus = shutil.which("qdbus") is not None
        self._has_wmctrl = shutil.which("wmctrl") is not None
        self._has_xdotool = shutil.which("xdotool") is not None

    def is_available(self) -> bool:
        if self._has_wmctrl:
            return True
        if self._has_qdbus:
            de = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
            return "kde" in de or "plasma" in de
        return False

    # ── KWin D-Bus helpers ────────────────────────────────────────

    def _kwin_call(self, method: str, *args: str) -> Optional[str]:
        """Call a KWin D-Bus method and return stdout."""
        if not self._has_qdbus:
            return None
        try:
            out = subprocess.run(
                ["qdbus", "org.kde.KWin", "/KWin", method, *args],
                capture_output=True, text=True, timeout=5,
            )
            if out.returncode == 0:
                return out.stdout.strip()
        except Exception as exc:
            logger.debug(f"KWin D-Bus call failed ({method}): {exc}")
        return None

    # ── Read ──────────────────────────────────────────────────────

    def _wmctrl_windows(self) -> list[WindowInfo]:
        out = self._run(["wmctrl", "-lx"])
        if not out:
            return []
        windows = []
        for line in out.splitlines():
            parts = line.split(None, 4)
            if len(parts) < 4:
                continue
            win_id, desktop, _, app_class = parts[0], parts[1], parts[2], parts[3]
            title = parts[4] if len(parts) > 4 else ""
            windows.append(WindowInfo(
                window_id=win_id,
                title=title,
                app_class=app_class,
                workspace_id=int(desktop) + 1 if desktop != "-1" else 0,
            ))
        return windows

    def get_status(self) -> dict:
        windows = self.list_windows()
        workspaces = self.list_workspaces()
        active = self.get_active_window()
        return {
            "desktop": "kde",
            "session_type": "wayland" if self._is_wayland else "x11",
            "workspaces": [w.__dict__ for w in workspaces],
            "windows": [w.__dict__ for w in windows],
            "active_window": active.__dict__ if active else None,
            "window_count": len(windows),
        }

    def list_windows(self) -> list[WindowInfo]:
        if self._has_wmctrl:
            return self._wmctrl_windows()
        return []

    def get_active_window(self) -> Optional[WindowInfo]:
        if not self._is_wayland and self._has_xdotool:
            win_id = self._run(["xdotool", "getactivewindow"])
            if win_id:
                win_id = win_id.strip()
                name = self._run(["xdotool", "getwindowname", win_id])
                cls = self._run(["xdotool", "getwindowclassname", win_id])
                return WindowInfo(
                    window_id=win_id,
                    title=name.strip() if name else "",
                    app_class=cls.strip() if cls else "",
                    is_active=True,
                )
        return None

    def list_workspaces(self) -> list[WorkspaceInfo]:
        # KWin exposes current desktop via qdbus; total via viewport query
        current = self._kwin_call("currentDesktop")
        if current is None:
            # Fall back to wmctrl -d
            out = self._run(["wmctrl", "-d"])
            if not out:
                return []
            workspaces = []
            for line in out.splitlines():
                parts = line.split()
                if not parts:
                    continue
                try:
                    ws_id = int(parts[0])
                except ValueError:
                    continue
                workspaces.append(WorkspaceInfo(
                    id=ws_id + 1,
                    name=parts[-1] if parts else f"Workspace {ws_id + 1}",
                    is_active="*" in parts[1:3],
                ))
            return workspaces

        try:
            current_id = int(current)
        except ValueError:
            return []

        workspaces = []
        for i in range(1, max(current_id + 4, 5)):
            workspaces.append(WorkspaceInfo(
                id=i,
                name=f"Desktop {i}",
                is_active=(i == current_id),
            ))
        return workspaces

    def list_monitors(self) -> list[MonitorInfo]:
        # KWin monitors via qdbus: org.kde.KWin /KWin numberOfScreens + basic info
        count_raw = self._kwin_call("numberOfScreens")
        try:
            count = int(count_raw) if count_raw else 0
        except ValueError:
            count = 0
        monitors = []
        for i in range(count):
            monitors.append(MonitorInfo(
                name=f"Screen {i + 1}",
                is_active=(i == 0),
            ))
        return monitors

    # ── Act ───────────────────────────────────────────────────────

    def focus(self, target: str, by: str = "class") -> bool:
        if not self._is_wayland and self._has_xdotool:
            if by == "class":
                out = self._run(["xdotool", "search", "--class", target])
            elif by == "title":
                out = self._run(["xdotool", "search", "--name", target])
            else:
                out = self._run(["xdotool", "search", "--pid", target])
            if out:
                win_id = out.strip().split("\n")[0]
                self._run(["xdotool", "windowactivate", win_id])
                return True
            if self._has_wmctrl:
                self._run(["wmctrl", "-a", target])
                return True
        return False

    def switch_workspace(self, workspace_id_or_name: int | str) -> bool:
        """Switch to a workspace by 1-based index via KWin D-Bus."""
        if isinstance(workspace_id_or_name, int):
            self._kwin_call("setCurrentDesktop", str(workspace_id_or_name))
            return True
        # Named desktop: try wmctrl
        if self._has_wmctrl:
            self._run(["wmctrl", "-a", str(workspace_id_or_name)])
            return True
        return False

    def launch_app(self, app: str, path: Optional[str] = None) -> bool:
        try:
            if shutil.which("kstart"):
                cmd = ["kstart", app]
                if path:
                    cmd = ["sh", "-c",
                           f"cd {_shquote(path)} && kstart {app} &"]
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
                return True
            if shutil.which("gtk-launch"):
                subprocess.Popen(["gtk-launch", app],
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
                return True
            if path:
                subprocess.Popen(
                    ["sh", "-c", f"cd {_shquote(path)} && {app} &"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                return True
        except Exception as exc:
            logger.warning(f"KDE launch failed: {exc}")
        return False

    def take_screenshot(self, output_path: Optional[str] = None) -> Optional[str]:
        """Take a screenshot via spectacle or grim/import."""
        if output_path is None:
            import datetime
            timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
            pictures = Path.home() / "Pictures"
            pictures.mkdir(exist_ok=True)
            output_path = str(pictures / f"friday_{timestamp}.png")

        try:
            if shutil.which("spectacle"):
                subprocess.run(
                    ["spectacle", "-b", "-n", "-o", output_path],
                    capture_output=True, timeout=10,
                )
            elif shutil.which("grim"):
                subprocess.run(["grim", output_path], capture_output=True, timeout=10)
            elif shutil.which("import"):
                subprocess.run(["import", output_path], capture_output=True, timeout=10)
            if Path(output_path).exists():
                return output_path
        except Exception as exc:
            logger.warning(f"KDE screenshot failed: {exc}")
        return None

    def setup_instructions(self) -> str:
        """Return setup instructions for KDE desktop control."""
        missing = []
        if not self._has_qdbus:
            missing.append("qdbus (qt5-tools / qt6-tools)")
        if not self._is_wayland and not self._has_wmctrl:
            missing.append("wmctrl")
        if not self._is_wayland and not self._has_xdotool:
            missing.append("xdotool")
        if not missing:
            return "KDE desktop control is ready."
        return (
            "KDE desktop control needs: " + ", ".join(missing) + ".\n"
            "Install them with your package manager, then log out and back in."
        )
