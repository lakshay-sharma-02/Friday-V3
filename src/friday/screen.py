"""Screen/Workspace Awareness — Friday sees what you're working on.

Detects the active window, running applications, clipboard content,
and can capture screen content for OCR. This is the bridge between
Friday and your real-world workspace — what MCU FRIDAY does naturally.

Data sources (tried in order of reliability):
  - ``hyprctl activewindow`` (Wayland/Hyprland — primary)
  - ``xdotool getactivewindow`` (X11 fallback)
  - ``wl-paste`` / ``xclip`` (clipboard)
  - ``import`` + ``tesseract`` (screenshot + OCR — opt-in)
  - ``psutil`` (process enumeration)

All sources are best-effort: failures return empty defaults.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from typing import Optional


# ── Known browser patterns for URL extraction ──

_BROWSER_TITLE_PATTERNS: list[tuple[str, str, str]] = [
    # (window_title_regex, browser_name, url_extract_group)
    (r"^(.*) — Brave$", "Brave", 1),
    (r"^(.*) - Brave$", "Brave", 1),
    (r"^(.*) — Google Chrome$", "Chrome", 1),
    (r"^(.*) - Google Chrome$", "Chrome", 1),
    (r"^(.*) — Firefox$", "Firefox", 1),
    (r"^(.*) - Firefox$", "Firefox", 1),
    (r"^(.*) — Vivaldi$", "Vivaldi", 1),
    (r"^(.*) — Opera$", "Opera", 1),
    (r"^(.*) — Edge$", "Edge", 1),
    (r"^(.*) - Edge$", "Edge", 1),
    # Chromium-based
    (r"^(.*) — Chromium$", "Chromium", 1),
    (r"^(.*) - Chromium$", "Chromium", 1),
]


@dataclass
class ScreenContext:
    """Snapshot of what's happening on the user's workspace.

    Collected each daemon cycle (or on demand via ``friday screen``).
    All fields are Optional — no field is guaranteed.
    """

    # Active window
    active_window_title: str = ""
    active_window_class: str = ""
    active_window_pid: int = 0
    active_window_process: str = ""

    # Desktop environment
    desktop_environment: str = ""  # "hyprland" | "wayland" | "x11" | "unknown"
    workspace_id: int = 0
    workspace_count: int = 0

    # Running context
    browser_url: str = ""  # extracted from browser window title
    browser_name: str = ""  # which browser
    clipboard_text: str = ""  # current clipboard content (truncated)
    clipboard_source: str = ""  # how clipboard was read (wl-paste | xclip | none)

    # Screen capture (opt-in, requires tesseract)
    screen_text: str = ""  # OCR result from screenshot
    ocr_available: bool = False

    # Process overview
    running_processes: int = 0
    top_processes: list[dict] = field(default_factory=list)

    # Timestamp
    collected_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "active_window_title": self.active_window_title,
            "active_window_class": self.active_window_class,
            "active_window_pid": self.active_window_pid,
            "active_window_process": self.active_window_process,
            "desktop_environment": self.desktop_environment,
            "workspace_id": self.workspace_id,
            "browser_url": self.browser_url,
            "browser_name": self.browser_name,
            "clipboard_text": self.clipboard_text[:500] if self.clipboard_text else "",
            "clipboard_source": self.clipboard_source,
            "screen_text": self.screen_text[:500] if self.screen_text else "",
            "ocr_available": self.ocr_available,
            "running_processes": self.running_processes,
            "top_processes": self.top_processes[:10] if self.top_processes else [],
            "collected_at": self.collected_at,
        }

    def format_brief(self) -> str:
        """One-line summary of what you're doing."""
        parts = []
        if self.active_window_process:
            parts.append(f"App: {self.active_window_process}")
        if self.active_window_title:
            title_short = self.active_window_title[:40]
            parts.append(f"Window: {title_short}")
        if self.browser_url:
            parts.append(f"URL: {self.browser_url[:50]}")
        if self.clipboard_text:
            parts.append("Clipboard: yes")
        return "  ".join(parts) if parts else "No screen context available"

    def format_block(self) -> str:
        """Multi-line formatted screen context."""
        lines: list[str] = []
        lines.append(f"  Desktop:    {self.desktop_environment}")
        lines.append(f"  Active app: {self.active_window_process}")
        lines.append(f"  Window:     {self.active_window_title[:80]}")
        if self.browser_url:
            lines.append(f"  Browser:    {self.browser_name} — {self.browser_url[:80]}")
        if self.clipboard_text:
            ct = self.clipboard_text[:100].replace("\n", "\\n")
            lines.append(f"  Clipboard:  {ct}")
        if self.ocr_available:
            lines.append("  OCR:        available")
        if self.screen_text:
            st = self.screen_text[:120].replace("\n", " ")
            lines.append(f"  Screen:     {st}")
        lines.append(f"  Procs:      {self.running_processes}")
        return "\n".join(lines)


# ── Desktop environment detection ──


def _detect_desktop() -> str:
    """Detect the desktop environment."""
    # Check Hyprland first (WAYLAND_DISPLAY is set, hyprctl exists)
    if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
        return "hyprland"
    # Check Wayland
    if os.environ.get("WAYLAND_DISPLAY"):
        return "wayland"
    # Check X11
    if os.environ.get("DISPLAY"):
        return "x11"
    return "unknown"


def _get_hyprland_active() -> dict:
    """Get active window info via hyprctl (Hyprland)."""
    try:
        result = subprocess.run(
            ["hyprctl", "activewindow", "-j"],
            capture_output=True, text=True, timeout=3,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return {}
        data = json.loads(result.stdout)
        return {
            "title": data.get("title", ""),
            "class": data.get("class", ""),
            "pid": data.get("pid", 0),
            "workspace_id": data.get("workspace", {}).get("id", 0) if isinstance(data.get("workspace"), dict) else 0,
        }
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return {}


def _get_x11_active() -> dict:
    """Get active window info via xdotool + xprop (X11)."""
    try:
        win_id = subprocess.run(
            ["xdotool", "getactivewindow"],
            capture_output=True, text=True, timeout=2,
        )
        if win_id.returncode != 0 or not win_id.stdout.strip():
            return {}
        wid = win_id.stdout.strip()

        # Get window name
        name = subprocess.run(
            ["xdotool", "getwindowname", wid],
            capture_output=True, text=True, timeout=2,
        )
        title = name.stdout.strip() if name.returncode == 0 else ""

        # Get window class
        cls = subprocess.run(
            ["xprop", "-id", wid, "WM_CLASS"],
            capture_output=True, text=True, timeout=2,
        )
        window_class = ""
        if cls.returncode == 0 and cls.stdout.strip():
            # Parse format: WM_CLASS(STRING) = "WM_CLASS", "WM_CLASS"
            parts = cls.stdout.strip().split("=")
            if len(parts) > 1:
                raw = parts[1].strip().strip('"')
                classes = [c.strip().strip('"') for c in raw.split(",")]
                if classes:
                    window_class = classes[-1]  # Last class is most specific

        return {
            "title": title,
            "class": window_class,
            "pid": 0,  # xdotool doesn't easily give PID
            "workspace_id": 0,
        }
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return {}


def _get_active_window() -> dict:
    """Get active window info, trying hyprctl first then xdotool."""
    de = _detect_desktop()
    if de == "hyprland":
        result = _get_hyprland_active()
        if result:
            return result
    if de in ("x11", "unknown"):
        result = _get_x11_active()
        if result:
            return result
    return {}


# ── Browser URL extraction ──


def _extract_browser_url(title: str, window_class: str) -> tuple[str, str]:
    """Extract browser URL and name from window title/class.

    Returns (url, browser_name). Empty strings if not a browser window.
    """
    if not title:
        return ("", "")

    # Try title-based patterns first.
    for pattern, browser_name, group_idx in _BROWSER_TITLE_PATTERNS:
        m = re.match(pattern, title)
        if m:
            page_title = m.group(group_idx)
            # If page title looks like a URL, use it directly.
            if page_title.startswith(("http://", "https://", "localhost:")):
                return (page_title, browser_name)
            # Otherwise it's the page title, not the URL.
            # For now, return the title as context.
            return (page_title, browser_name)

    # Try class-based detection.
    if window_class:
        wc_lower = window_class.lower()
        if "brave" in wc_lower:
            return (title, "Brave")
        if "chrom" in wc_lower:
            return (title, "Chrome/Chromium")
        if "firefox" in wc_lower:
            return (title, "Firefox")
        if "edge" in wc_lower:
            return (title, "Edge")

    return ("", "")


# ── Clipboard ──


def _read_clipboard() -> tuple[str, str]:
    """Read clipboard content.

    Returns (content, source).
    Tries wl-paste (Wayland) first, then xclip (X11).
    """
    # Wayland
    try:
        result = subprocess.run(
            ["wl-paste"], capture_output=True, text=True, timeout=2,
        )
        if result.returncode == 0 and result.stdout.strip():
            return (result.stdout.strip()[:2000], "wl-paste")
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass

    # X11 fallback
    try:
        result = subprocess.run(
            ["xclip", "-o", "-selection", "clipboard"],
            capture_output=True, text=True, timeout=2,
        )
        if result.returncode == 0 and result.stdout.strip():
            return (result.stdout.strip()[:2000], "xclip")
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass

    return ("", "")


# ── Screenshot + OCR (opt-in) ──


def _ocr_available() -> bool:
    """Check if tesseract is available on PATH."""
    try:
        result = subprocess.run(
            ["which", "tesseract"], capture_output=True, text=True, timeout=2,
        )
        return result.returncode == 0
    except (FileNotFoundError, OSError):
        return False


def _capture_screen_text() -> str:
    """Take a screenshot and run OCR. Returns extracted text.

    Uses ``import`` (ImageMagick) for screenshot and ``tesseract`` for OCR.
    Returns empty string if either tool is unavailable or fails.
    Best-effort — never raises.
    """
    if not _ocr_available():
        return ""

    tmp_path = f"/tmp/friday_screen_{int(time.time())}.png"
    try:
        # Screenshot via ImageMagick
        subprocess.run(
            ["import", "-window", "root", "-silent", tmp_path],
            capture_output=True, text=True, timeout=10,
        )
        if not os.path.exists(tmp_path):
            return ""

        # OCR via tesseract
        result = subprocess.run(
            ["tesseract", tmp_path, "stdout", "--psm", "6"],
            capture_output=True, text=True, timeout=15,
        )
        text = result.stdout.strip() if result.returncode == 0 else ""
        return text[:2000]  # Truncate to avoid huge strings

    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return ""
    finally:
        # Cleanup temp file
        try:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except OSError:
            pass


# ── Process overview ──


def _get_process_overview() -> tuple[int, list[dict]]:
    """Get process count and top CPU-consuming processes.

    Returns (count, top_processes) where each process is a dict
    with name, cpu_percent, memory_percent.
    """
    try:
        import psutil
        count = len(psutil.pids())
        top = []
        for proc in sorted(psutil.process_iter(["name", "cpu_percent", "memory_percent"]),
                          key=lambda p: p.info.get("cpu_percent", 0) or 0,
                          reverse=True)[:10]:
            try:
                info = proc.info
                if info.get("cpu_percent", 0) or info.get("memory_percent", 0):
                    top.append({
                        "name": info.get("name", "?")[:30],
                        "cpu_percent": round(info.get("cpu_percent", 0) or 0, 1),
                        "memory_percent": round(info.get("memory_percent", 0) or 0, 1),
                    })
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return (count, top)
    except ImportError:
        return (0, [])


# ── Change detection ──


@dataclass
class ScreenChange:
    """A detected change between two consecutive screen snapshots."""
    change_type: str  # "app_switch" | "url_change" | "clipboard_change" | "window_title_change"
    old_value: str = ""
    new_value: str = ""
    detail: str = ""


def detect_screen_changes(
    prev: ScreenContext,
    curr: ScreenContext,
) -> list[ScreenChange]:
    """Detect changes between two consecutive screen snapshots.

    Args:
        prev: Previous screen context.
        curr: Current screen context.

    Returns:
        List of ScreenChange objects describing what changed.
        Empty list if nothing significant changed.
    """
    changes: list[ScreenChange] = []

    # App switch — process/class changed
    old_app = prev.active_window_process or ""
    new_app = curr.active_window_process or ""
    if new_app and old_app and old_app.lower() != new_app.lower():
        changes.append(ScreenChange(
            change_type="app_switch",
            old_value=old_app,
            new_value=new_app,
            detail=f"Switched from {old_app} to {new_app}",
        ))
    elif new_app and not old_app:
        # First time detecting an active app
        changes.append(ScreenChange(
            change_type="app_switch",
            old_value="",
            new_value=new_app,
            detail=f"Now using {new_app}",
        ))

    # URL change
    old_url = prev.browser_url or ""
    new_url = curr.browser_url or ""
    if new_url and old_url and old_url != new_url:
        changes.append(ScreenChange(
            change_type="url_change",
            old_value=old_url,
            new_value=new_url,
            detail=f"Browser URL changed in {curr.browser_name}",
        ))
    elif new_url and not old_url:
        changes.append(ScreenChange(
            change_type="url_change",
            old_value="",
            new_value=new_url,
            detail=f"Browsing {new_url[:60]}",
        ))

    # Clipboard change
    old_clip = prev.clipboard_text or ""
    new_clip = curr.clipboard_text or ""
    if new_clip and old_clip and old_clip != new_clip:
        # Only report if the clipboard content is meaningfully different
        # (ignore trivial whitespace changes)
        old_stripped = old_clip.strip()[:100]
        new_stripped = new_clip.strip()[:100]
        if old_stripped != new_stripped:
            changes.append(ScreenChange(
                change_type="clipboard_change",
                old_value=old_clip[:100],
                new_value=new_clip[:100],
                detail=f"New clipboard content: {new_clip[:80]}",
            ))

    return changes


# ── Main collection function ──


def collect_screen_context(
    include_ocr: bool = False,
    include_clipboard: bool = True,
) -> ScreenContext:
    """Collect a snapshot of what's happening on the user's workspace.

    Args:
        include_ocr: If True, take a screenshot and run OCR (requires
            ImageMagick ``import`` + ``tesseract``).
        include_clipboard: If True, read clipboard content.

    Returns:
        A ``ScreenContext`` with whatever data could be collected.
        Never raises.
    """
    ctx = ScreenContext(collected_at=time.time())

    try:
        ctx.desktop_environment = _detect_desktop()
    except Exception:
        pass

    # Active window
    try:
        win = _get_active_window()
        ctx.active_window_title = win.get("title", "")
        ctx.active_window_class = win.get("class", "")
        ctx.active_window_pid = win.get("pid", 0)

        # Derive process name from window class or PID
        if ctx.active_window_class:
            ctx.active_window_process = ctx.active_window_class
        elif ctx.active_window_pid:
            try:
                import psutil
                proc = psutil.Process(ctx.active_window_pid)
                ctx.active_window_process = proc.name() or ""
            except Exception:
                pass

        ctx.workspace_id = win.get("workspace_id", 0)
    except Exception:
        pass

    # Browser URL extraction
    try:
        url, browser = _extract_browser_url(
            ctx.active_window_title, ctx.active_window_class)
        ctx.browser_url = url
        ctx.browser_name = browser
    except Exception:
        pass

    # Clipboard
    if include_clipboard:
        try:
            text, source = _read_clipboard()
            ctx.clipboard_text = text
            ctx.clipboard_source = source
        except Exception:
            pass

    # OCR
    try:
        ctx.ocr_available = _ocr_available()
        if include_ocr and ctx.ocr_available:
            ctx.screen_text = _capture_screen_text()
    except Exception:
        pass

    # Processes
    try:
        count, top = _get_process_overview()
        ctx.running_processes = count
        ctx.top_processes = top
    except Exception:
        pass

    return ctx
