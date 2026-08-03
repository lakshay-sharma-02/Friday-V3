"""GNOME adapter for the desktop abstraction layer.

Controls GNOME Shell via D-Bus (``gdbus``), with ``wmctrl`` / ``xdotool``
fallbacks for X11 sessions.

GNOME quirks handled here:
- **Wayland vs X11:** window/workspace enumeration differs. On X11 we use
  ``wmctrl`` (works reliably). On Wayland we use GNOME Shell's D-Bus
  ``Eval`` interface (best-effort; some GNOME versions restrict it).
- **Version fragmentation:** we detect the GNOME version via ``gdbus``
  and prefer APIs that exist across 42/43/44.
- **Dynamic workspaces:** GNOME workspaces are virtual/dynamic; IDs here
  are 1-based workspace indices (matching ``wmctrl -s`` / Shell indices).
"""

from __future__ import annotations

import json
import logging
import os
import re
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

logger = logging.getLogger("friday_v4.desktop.gnome")


class GNOMEAdapter(DesktopAbstraction):
    """GNOME Shell backend (D-Bus / wmctrl / xdotool)."""

    name = "gnome"

    def __init__(self):
        self._is_wayland = bool(os.environ.get("WAYLAND_DISPLAY"))
        self._has_gdbus = shutil.which("gdbus") is not None
        self._has_wmctrl = shutil.which("wmctrl") is not None
        self._has_xdotool = shutil.which("xdotool") is not None
        # Lazy: version detection spawns gdbus, only run when first needed.
        self._version: Optional[str] = None

    # ── Detection ─────────────────────────────────────────────────

    @property
    def version(self) -> str:
        """GNOME Shell version (e.g. '44.3'), detected lazily."""
        if self._version is None:
            self._version = self._detect_version()
        return self._version

    def _detect_version(self) -> str:
        """Detect GNOME Shell version (e.g. '44.3')."""
        if not self._has_gdbus:
            return ""
        try:
            out = subprocess.run(
                ["gdbus", "call", "--session", "--dest", "org.gnome.Shell",
                 "--object-path", "/org/gnome/Shell", "--method",
                 "org.gnome.Shell.Eval", "global.version"],
                capture_output=True, text=True, timeout=5,
            )
            m = re.search(r"'([\d.]+)'", out.stdout or "")
            if m:
                return m.group(1)
        except Exception as exc:
            logger.debug(f"GNOME version detection failed: {exc}")
        return ""

    def is_available(self) -> bool:
        if not (self._has_gdbus or self._has_wmctrl):
            return False
        de = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
        if "gnome" in de:
            return True
        # wmctrl is X11-only — don't claim availability on Wayland
        # unless GNOME is the actual desktop.
        return self._has_wmctrl and not self._is_wayland

    # ── Internal helpers ──────────────────────────────────────────

    def _wmctrl_windows(self) -> list[WindowInfo]:
        """List windows on X11 via wmctrl -lx.

        Output lines:  <hex-id> <desktop> <host> <class> <title...>
        """
        out = self._run(["wmctrl", "-lx"])
        if not out:
            return []

        active_raw = self._wmctrl_active_window_id()
        try:
            active_id: Optional[int] = int(active_raw) if active_raw else None
        except ValueError:
            active_id = None

        windows = []
        for line in out.splitlines():
            parts = line.split(None, 4)
            if len(parts) < 4:
                continue
            win_id, desktop, _host, app_class = parts[0], parts[1], parts[2], parts[3]
            title = parts[4] if len(parts) > 4 else ""
            try:
                is_active = active_id is not None and int(win_id, 16) == active_id
            except ValueError:
                is_active = False
            windows.append(WindowInfo(
                window_id=win_id,
                title=title,
                app_class=app_class,
                workspace_id=int(desktop) + 1 if desktop != "-1" else 0,
                is_active=is_active,
            ))
        return windows

    def _wmctrl_active_window_id(self) -> str:
        """Get the active window's decimal id via xdotool.

        xdotool returns a decimal id; wmctrl reports hex ids. The caller
        compares numerically so padding differences don't matter.
        """
        if self._has_xdotool:
            out = self._run(["xdotool", "getactivewindow"])
            if out:
                return out.strip()
        return ""

    def _wmctrl_workspaces(self) -> list[WorkspaceInfo]:
        """List workspaces on X11 via wmctrl -d.

        Output lines:  <id> <desktop-count> * <host> <size> <viewport> <name>
        """
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
            is_active = "*" in parts[1:3]
            # Name is everything after the viewport (usually last token)
            name = parts[-1] if parts else f"Workspace {ws_id + 1}"
            workspaces.append(WorkspaceInfo(
                id=ws_id + 1,
                name=name,
                is_active=is_active,
            ))
        return workspaces

    def _shell_eval(self, js: str) -> Optional[str]:
        """Evaluate JS inside GNOME Shell via D-Bus (best-effort).

        Returns the evaluated string, or None if Eval is restricted/unavailable.
        """
        if not self._has_gdbus:
            return None
        try:
            out = subprocess.run(
                ["gdbus", "call", "--session", "--dest", "org.gnome.Shell",
                 "--object-path", "/org/gnome/Shell", "--method",
                 "org.gnome.Shell.Eval", js],
                capture_output=True, text=True, timeout=5,
            )
            # Response is (true, '...') or (false, '...')
            m = re.search(r"^\((true|false),\s*'(.*)'\)\s*$", out.stdout or "", re.DOTALL)
            if m and m.group(1) == "true":
                return m.group(2)
        except Exception as exc:
            logger.debug(f"GNOME Shell Eval failed: {exc}")
        return None

    # ── Read ──────────────────────────────────────────────────────

    def get_status(self) -> dict:
        windows = self.list_windows()
        workspaces = self.list_workspaces()
        active = self.get_active_window()
        return {
            "desktop": "gnome",
            "gnome_version": self.version,
            "session_type": "wayland" if self._is_wayland else "x11",
            "workspaces": [w.__dict__ for w in workspaces],
            "windows": [w.__dict__ for w in windows],
            "active_window": active.__dict__ if active else None,
            "window_count": len(windows),
        }

    def list_windows(self) -> list[WindowInfo]:
        # X11: wmctrl is reliable and fast
        if not self._is_wayland and self._has_wmctrl:
            return self._wmctrl_windows()

        # Wayland: try GNOME Shell Eval (best-effort)
        if self._is_wayland:
            js = (
                "const wins = global.get_window_actors().map(a => {"
                "  const w = a.meta_window;"
                "  return {id: String(w.get_id()), title: w.get_title(), "
                "          cls: w.get_wm_class() || '', "
                "          ws: w.get_workspace() ? w.get_workspace().index() : 0, "
                "          active: w.has_focus()};"
                "});"
                "JSON.stringify(wins)"
            )
            raw = self._shell_eval(js)
            if raw:
                try:
                    data = json.loads(raw)
                    return [WindowInfo(
                        window_id=str(e.get("id", "")),
                        title=e.get("title", ""),
                        app_class=e.get("cls", ""),
                        workspace_id=int(e.get("ws", 0)) + 1,
                        is_active=bool(e.get("active")),
                    ) for e in data]
                except json.JSONDecodeError:
                    pass
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

        if self._is_wayland:
            js = (
                "const a = global.get_window_actors().find(x => x.meta_window.has_focus());"
                "a ? JSON.stringify({id: String(a.meta_window.get_id()), "
                "title: a.meta_window.get_title(), "
                "cls: a.meta_window.get_wm_class() || '', "
                "ws: a.meta_window.get_workspace() ? a.meta_window.get_workspace().index() : 0}) "
                ": 'null'"
            )
            raw = self._shell_eval(js)
            if raw and raw != "null":
                try:
                    e = json.loads(raw)
                    return WindowInfo(
                        window_id=str(e.get("id", "")),
                        title=e.get("title", ""),
                        app_class=e.get("cls", ""),
                        workspace_id=int(e.get("ws", 0)) + 1,
                        is_active=True,
                    )
                except json.JSONDecodeError:
                    pass
        return None

    def list_workspaces(self) -> list[WorkspaceInfo]:
        if not self._is_wayland and self._has_wmctrl:
            return self._wmctrl_workspaces()

        if self._is_wayland:
            js = (
                "const wm = global.workspace_manager;"
                "JSON.stringify([...Array(wm.n_workspaces)].map((_, i) => {"
                "  const ws = wm.get_workspace_by_index(i);"
                "  return {id: i + 1, active: ws === wm.get_active_workspace(),"
                "          windows: ws.list_windows().length};"
                "}))"
            )
            raw = self._shell_eval(js)
            if raw:
                try:
                    data = json.loads(raw)
                    return [WorkspaceInfo(
                        id=int(e.get("id", 0)),
                        name=f"Workspace {e.get('id', 0)}",
                        is_active=bool(e.get("active")),
                        window_count=int(e.get("windows", 0)),
                    ) for e in data]
                except json.JSONDecodeError:
                    pass
        return []

    def list_monitors(self) -> list[MonitorInfo]:
        js = (
            "JSON.stringify(global.display.get_monitors().map(m => ({"
            "  name: m.connector || '', width: m.width, height: m.height,"
            "  is_active: m === global.display.get_current_monitor(),"
            "  refresh_rate: m.refresh_rate || 0}))"
            ")"
        )
        raw = self._shell_eval(js)
        if not raw:
            return []
        try:
            data = json.loads(raw)
            return [MonitorInfo(
                name=e.get("name", ""),
                width=int(e.get("width", 0)),
                height=int(e.get("height", 0)),
                refresh_rate=float(e.get("refresh_rate", 0.0)),
                is_active=bool(e.get("is_active")),
            ) for e in data]
        except (json.JSONDecodeError, TypeError, ValueError):
            return []

    # ── Act ───────────────────────────────────────────────────────

    def focus(self, target: str, by: str = "class") -> bool:
        """Focus a window by class, title, or pid (X11: xdotool)."""
        if self._is_wayland:
            # Best-effort via Shell Eval: activate the first matching window
            js = (
                "const a = global.get_window_actors().find(x => {"
                "  const w = x.meta_window;"
                f"  return w.get_wm_class() && w.get_wm_class().toLowerCase() "
                f"=== '{target.lower()}';"
                "});"
                "if (a) { a.meta_window.activate(global.get_current_time()); 'ok' } else { 'miss' }"
            )
            return self._shell_eval(js) == "ok"

        if self._has_xdotool:
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
                # Fallback: activate by title substring
                self._run(["wmctrl", "-a", target])
                return True
        return False

    def switch_workspace(self, workspace_id_or_name: int | str) -> bool:
        """Switch to a workspace by 1-based index or name."""
        if not self._is_wayland and self._has_wmctrl:
            # wmctrl is 0-indexed
            if isinstance(workspace_id_or_name, int):
                target = max(0, workspace_id_or_name - 1)
                self._run(["wmctrl", "-s", str(target)])
                return True
            self._run(["wmctrl", "-a", str(workspace_id_or_name)])
            return True

        if self._is_wayland:
            if isinstance(workspace_id_or_name, int):
                idx = max(0, workspace_id_or_name - 1)
                js = (
                    "const wm = global.workspace_manager;"
                    f"wm.get_workspace_by_index({idx}).activate(global.get_current_time());"
                    "'ok'"
                )
                return self._shell_eval(js) == "ok"
        return False

    def launch_app(self, app: str, path: Optional[str] = None) -> bool:
        """Launch an app via gio/gio-launch, optionally in a directory."""
        try:
            # Prefer gio launch (GNOME desktop entries) when it's an app id
            if shutil.which("gtk-launch"):
                subprocess.Popen(
                    ["gtk-launch", app],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                return True
            if shutil.which("gio"):
                cmd = ["gio", "launch", app]
                if path:
                    cmd = ["sh", "-c",
                           f"cd {_shquote(path)} && gio launch {app} &"]
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
                return True
        except Exception as exc:
            logger.warning(f"GNOME launch failed: {exc}")
        return False

    def take_screenshot(self, output_path: Optional[str] = None) -> Optional[str]:
        """Take a screenshot via gnome-screenshot (or grim on Wayland)."""
        if output_path is None:
            import datetime
            timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
            pictures = Path.home() / "Pictures"
            pictures.mkdir(exist_ok=True)
            output_path = str(pictures / f"friday_{timestamp}.png")

        try:
            if self._is_wayland and shutil.which("grim"):
                subprocess.run(["grim", output_path], capture_output=True, timeout=10)
            elif shutil.which("gnome-screenshot"):
                subprocess.run(
                    ["gnome-screenshot", "-f", output_path],
                    capture_output=True, timeout=10,
                )
            elif shutil.which("import"):
                subprocess.run(["import", output_path], capture_output=True, timeout=10)
            if Path(output_path).exists():
                return output_path
        except Exception as exc:
            logger.warning(f"GNOME screenshot failed: {exc}")
        return None

    def setup_instructions(self) -> str:
        """Return setup instructions for GNOME desktop control."""
        missing = []
        if not self._has_gdbus:
            missing.append("gdbus (libglib2.0-bin)")
        if not self._is_wayland and not self._has_wmctrl:
            missing.append("wmctrl")
        if not self._is_wayland and not self._has_xdotool:
            missing.append("xdotool")
        if not missing:
            return "GNOME desktop control is ready."
        return (
            "GNOME desktop control needs: " + ", ".join(missing) + ".\n"
            "Install them, e.g. `sudo apt install "
            + " ".join(m.split(" (")[0] for m in missing)
            + "`."
        )
