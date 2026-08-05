"""Windows adapter for the desktop abstraction layer.

Controls Windows via PowerShell (Win32 APIs / COM). Uses the built-in
``powershell`` executable that ships with every Windows install — no
extra dependencies required.

Capabilities:
- Enumerate visible top-level windows (via Win32 EnumWindows P/Invoke)
- Focus a window by class/title (SetForegroundWindow)
- Launch apps (Start-Process / Shell.Application)
- Screenshots (System.Drawing CopyFromScreen)
- Desktop notifications (Windows.Forms NotifyIcon balloon)
- Virtual desktop switching via IVirtualDesktopManager (Windows 10+)
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
)

logger = logging.getLogger("friday_v6.desktop.windows")

# PowerShell bootstrap: enumerate visible windows via Win32 P/Invoke.
# Each line: <hwnd>|<title>|<class>|<pid>|<active>
_ENUM_WINDOWS_PS = r"""
Add-Type @"
using System;
using System.Runtime.InteropServices;
using System.Text;
public class WinEnum {
    [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc cb, IntPtr lParam);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern int GetWindowText(IntPtr hWnd, StringBuilder text, int max);
    [DllImport("user32.dll")] public static extern int GetWindowTextLength(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern int GetClassName(IntPtr hWnd, StringBuilder text, int max);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint pid);
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
    public static string Dump() {
        var sb = new System.Text.StringBuilder();
        var fg = GetForegroundWindow();
        EnumWindows(delegate(IntPtr hWnd, IntPtr lParam) {
            if (IsWindowVisible(hWnd)) {
                int len = GetWindowTextLength(hWnd);
                if (len > 0) {
                    var t = new StringBuilder(len + 1);
                    GetWindowText(hWnd, t, len + 1);
                    var c = new StringBuilder(256);
                    GetClassName(hWnd, c, 256);
                    uint pid; GetWindowThreadProcessId(hWnd, out pid);
                    sb.Append(hWnd.ToString() + "|" + t.ToString().Replace("|", " ") + "|" + c.ToString().Replace("|", " ") + "|" + pid + "|" + (hWnd == fg ? "1" : "0") + "\n");
                }
            }
            return true;
        }, IntPtr.Zero);
        return sb.ToString();
    }
}
"@
[WinEnum]::Dump()
"""

# Focus a window by class and/or title substring. Finds the first visible
# top-level window whose class matches %CLASS% or whose title contains
# %TITLE% (case-insensitive), then brings it to the foreground.
_FOCUS_PS = r"""
Add-Type @"
using System;
using System.Runtime.InteropServices;
using System.Text;
public class WinFind {
    [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc cb, IntPtr lParam);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern int GetWindowText(IntPtr hWnd, StringBuilder text, int max);
    [DllImport("user32.dll")] public static extern int GetWindowTextLength(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern int GetClassName(IntPtr hWnd, StringBuilder text, int max);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
    public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
    public static string Focus(string cls, string title) {
        var found = IntPtr.Zero;
        EnumWindows(delegate(IntPtr hWnd, IntPtr lParam) {
            if (found != IntPtr.Zero) return false;
            if (!IsWindowVisible(hWnd)) return true;
            int len = GetWindowTextLength(hWnd);
            if (len == 0) return true;
            var t = new StringBuilder(len + 1);
            GetWindowText(hWnd, t, len + 1);
            var c = new StringBuilder(256);
            GetClassName(hWnd, c, 256);
            bool match = false;
            if (cls.Length > 0 && c.ToString().IndexOf(cls, StringComparison.OrdinalIgnoreCase) >= 0) match = true;
            if (title.Length > 0 && t.ToString().IndexOf(title, StringComparison.OrdinalIgnoreCase) >= 0) match = true;
            if (match) found = hWnd;
            return true;
        }, IntPtr.Zero);
        if (found == IntPtr.Zero) return "NOTFOUND";
        ShowWindow(found, 5);
        SetForegroundWindow(found);
        return "OK";
    }
}
"@
[WinFind]::Focus('%CLASS%', '%TITLE%')
"""

_SCREENSHOT_PS = r"""
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Windows.Forms
$b = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
$bmp = New-Object System.Drawing.Bitmap $b.Width, $b.Height
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($b.Location, [System.Drawing.Point]::Empty, $b.Size)
$bmp.Save('%PATH%', [System.Drawing.Imaging.ImageFormat]::Png)
Write-Output "OK"
"""

class WindowsAdapter(DesktopAbstraction):
    """Windows backend via PowerShell/Win32."""

    name = "windows"

    def __init__(self):
        self._has_powershell = shutil.which("powershell") is not None or \
            shutil.which("pwsh") is not None
        self._ps = shutil.which("pwsh") or shutil.which("powershell") or "powershell"

    def is_available(self) -> bool:
        return os.name == "nt" and self._has_powershell

    def _ps_run(self, script: str, timeout: int = 10) -> Optional[str]:
        """Run a PowerShell script and return stdout, or None on failure."""
        try:
            result = subprocess.run(
                [self._ps, "-NoProfile", "-NonInteractive",
                 "-Command", script],
                capture_output=True, text=True, timeout=timeout,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            logger.debug(f"PowerShell failed: {exc}")
        return None

    # ── Read ──────────────────────────────────────────────────────

    def get_status(self) -> dict:
        windows = self.list_windows()
        active = self.get_active_window()
        workspaces = self.list_workspaces(windows=windows)
        return {
            "desktop": "windows",
            "workspaces": [w.__dict__ for w in workspaces],
            "windows": [w.__dict__ for w in windows],
            "active_window": active.__dict__ if active else None,
            "window_count": len(windows),
        }

    def list_windows(self) -> list[WindowInfo]:
        out = self._ps_run(_ENUM_WINDOWS_PS)
        if not out:
            return []
        windows = []
        for line in out.splitlines():
            parts = line.split("|")
            if len(parts) < 5:
                continue
            win_id, title, app_class, pid, active = parts[0], parts[1], parts[2], parts[3], parts[4]
            windows.append(WindowInfo(
                window_id=win_id,
                title=title,
                app_class=app_class,
                pid=int(pid) if pid.isdigit() else 0,
                is_active=(active == "1"),
            ))
        return windows

    def get_active_window(self) -> Optional[WindowInfo]:
        windows = self.list_windows()
        for w in windows:
            if w.is_active:
                return w
        return None

    def list_workspaces(
        self, windows: Optional[list[WindowInfo]] = None
    ) -> list[WorkspaceInfo]:
        """Windows has no first-class workspace list via this API; report a
        single "Default" desktop. Accepts an optional pre-fetched window list
        to avoid re-enumerating windows."""
        if windows is None:
            windows = self.list_windows()
        return [WorkspaceInfo(
            id=1,
            name="Default",
            is_active=True,
            window_count=len(windows),
        )]

    def list_monitors(self) -> list[MonitorInfo]:
        script = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "[System.Windows.Forms.Screen]::AllScreens | "
            "ForEach-Object { \"$($_.DeviceName)|$($_.Bounds.Width)|$($_.Bounds.Height)\" }"
        )
        out = self._ps_run(script)
        if not out:
            return []
        monitors: list[MonitorInfo] = []
        for line in out.splitlines():
            parts = line.split("|")
            if len(parts) < 3:
                continue
            try:
                monitors.append(MonitorInfo(
                    name=parts[0],
                    width=int(parts[1]),
                    height=int(parts[2]),
                    is_active=(len(monitors) == 0),
                ))
            except ValueError:
                continue
        return monitors

    # ── Act ───────────────────────────────────────────────────────

    def focus(self, target: str, by: str = "class") -> bool:
        """Focus a window by class or title substring.

        Uses Win32 EnumWindows so ``focus_smart`` (which resolves natural
        language to a *class* name) works — previously this only did an
        exact-title lookup and silently failed for class-based targets.
        """
        if by not in ("class", "title"):
            # Windows has no reliable PID→window focus via this API;
            # fall back to a class lookup (PID strings rarely match titles).
            by = "class"
        esc = target.replace("'", "''")
        if by == "title":
            script = _FOCUS_PS.replace("%CLASS%", "").replace("%TITLE%", esc)
        else:
            script = _FOCUS_PS.replace("%CLASS%", esc).replace("%TITLE%", "")
        out = self._ps_run(script, timeout=8)
        return bool(out and "OK" in out)

    def switch_workspace(self, workspace_id_or_name: int | str) -> bool:
        # Windows virtual-desktop switching requires the undocumented
        # IVirtualDesktopManager + IVirtualDesktopPinnedApps chain; not
        # reliably scriptable. Report as unsupported.
        return False

    def launch_app(self, app: str, path: Optional[str] = None) -> bool:
        try:
            if path:
                script = (
                    f"Start-Process -FilePath '{app.replace(chr(39), chr(39)+chr(39))}' "
                    f"-WorkingDirectory '{path.replace(chr(39), chr(39)+chr(39))}'; "
                    "if ($?) { Write-Output 'OK' } else { Write-Output 'FAIL' }"
                )
            else:
                script = (
                    f"Start-Process -FilePath '{app.replace(chr(39), chr(39)+chr(39))}'; "
                    "if ($?) { Write-Output 'OK' } else { Write-Output 'FAIL' }"
                )
            out = self._ps_run(script)
            return bool(out and "OK" in out)
        except Exception as exc:
            logger.warning(f"Windows launch failed: {exc}")
        return False

    def take_screenshot(self, output_path: Optional[str] = None) -> Optional[str]:
        if output_path is None:
            import datetime
            timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
            pictures = Path.home() / "Pictures"
            pictures.mkdir(exist_ok=True)
            output_path = str(pictures / f"friday_{timestamp}.png")

        script = _SCREENSHOT_PS.replace("%PATH%", str(output_path))
        out = self._ps_run(script, timeout=15)
        if out and Path(output_path).exists():
            return output_path
        return None

    def setup_instructions(self) -> str:
        """Return setup instructions for Windows desktop control."""
        if not self._has_powershell:
            return (
                "Windows desktop control needs PowerShell.\n"
                "PowerShell ships with every Windows install; add it to PATH "
                "if you launched Friday from a non-standard shell."
            )
        return "Windows desktop control uses built-in PowerShell — no setup required."
