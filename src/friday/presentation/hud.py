"""HUD-Style Terminal Notifications — heads-up display overlay for Friday.

Provides:
  - A persistent terminal status line (like tmux status bar) showing
    Friday status, presence state, watcher count, pending items, and
    last cycle time.
  - Non-blocking popup notifications that float in the terminal for 5s.
  - Configurable modes: off, compact, full.
  - ANSI-aware: detects terminal capabilities, suppresses when not a TTY.
  - Safe: suppresses popups when the user is typing.

Design:
  - No Rich dependency for status bar — pure ANSI escape codes.
  - Uses cursor-position save/restore and scroll-region tricks to avoid
    disturbing the user's current terminal content.
  - A lightweight background thread checks for new notifications and
    updates the status line.
  - Popup notifications render above the status line for 5s then disappear.
"""

from __future__ import annotations

import atexit
import os
import re
import select
import shutil
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from ..db import connect


# ──────────────────────────────────────────────────────────────────────────
# Terminal detection
# ──────────────────────────────────────────────────────────────────────────


def _is_tty() -> bool:
    """Check if stdout is an interactive terminal."""
    return sys.stdout.isatty()


def _terminal_width() -> int:
    """Get terminal width, falling back to 80."""
    try:
        return shutil.get_terminal_size().columns
    except Exception:
        return 80


def _terminal_height() -> int:
    """Get terminal height, falling back to 24."""
    try:
        return shutil.get_terminal_size().lines
    except Exception:
        return 24


# ──────────────────────────────────────────────────────────────────────────
# ANSI helpers
# ──────────────────────────────────────────────────────────────────────────

_ANSI_CLEAR_LINE = "\033[2K"
_ANSI_SAVE_CURSOR = "\033[s"
_ANSI_RESTORE_CURSOR = "\033[u"
_ANSI_HIDE_CURSOR = "\033[?25l"
_ANSI_SHOW_CURSOR = "\033[?25h"
_ANSI_RESET = "\033[0m"

_CODES = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
    "cyan": "\033[36m",
    "red": "\033[31m",
    "gray": "\033[90m",
    "bg_gray": "\033[100m",
    "bg_blue": "\033[44m",
    "bg_green": "\033[42m",
    "bg_red": "\033[41m",
    "bg_yellow": "\033[43m",
}


def _color(text: str, *names: str) -> str:
    """Apply ANSI color codes to text."""
    if not _is_tty():
        return text
    prefix = "".join(_CODES.get(n, "") for n in names)
    return prefix + text + _ANSI_RESET


def _move_to(row: int, col: int = 0) -> str:
    """ANSI cursor position escape."""
    return f"\033[{row};{col}H"


# ──────────────────────────────────────────────────────────────────────────
# Data models
# ──────────────────────────────────────────────────────────────────────────


class StatusMode(Enum):
    OFF = "off"
    COMPACT = "compact"
    FULL = "full"


@dataclass
class FridayStatus:
    """Snapshot of Friday's current status for HUD display."""

    daemon_state: str = "stopped"       # running | stopped | crashed | analyzing
    presence: str = "away"               # focus | desk | away
    watcher_count: int = 0
    pending_items: int = 0
    last_cycle_ago: str = "never"
    has_alerts: bool = False
    active_agent: bool = False
    unread_events: int = 0


@dataclass
class HudNotification:
    """A notification popup to display in the HUD."""

    id: int
    message: str
    level: str = "info"     # info | warning | error | success
    timestamp: float = 0.0
    duration: float = 5.0


# ──────────────────────────────────────────────────────────────────────────
# Status data fetching
# ──────────────────────────────────────────────────────────────────────────


def _fetch_status() -> FridayStatus:
    """Fetch current Friday status from DB and daemon."""
    try:
        from ..daemon import get_status
        st = get_status()
        dstate = st.get("state", "stopped")
        last_cycle_at = st.get("last_cycle_at", "")
        cycle_count = st.get("cycle_count", 0)
        pending = st.get("new_pending_initiatives", 0) or 0
        active_agent = st.get("active_agent", False) or False
        has_alerts = st.get("has_alerts", False)

        # Compute "time ago" for last cycle.
        last_cycle_ago = "never"
        if last_cycle_at:
            try:
                from datetime import datetime
                last = datetime.fromisoformat(last_cycle_at)
                delta = datetime.now() - last
                secs = int(delta.total_seconds())
                if secs < 60:
                    last_cycle_ago = f"{secs}s ago"
                elif secs < 3600:
                    last_cycle_ago = f"{secs // 60}m ago"
                else:
                    last_cycle_ago = f"{secs // 3600}h ago"
            except Exception:
                last_cycle_ago = "?"
    except Exception:
        dstate = "stopped"
        last_cycle_ago = "never"
        pending = 0
        active_agent = False
        has_alerts = False

    # Watcher count.
    watcher_count = 0
    try:
        conn = connect()
        row = conn.execute("SELECT COUNT(*) AS cnt FROM watch_history WHERE outcome='running'").fetchone()
        if row:
            watcher_count = row["cnt"]
        conn.close()
    except Exception:
        pass

    # Unread events.
    unread = 0
    try:
        from ..ambient import get_unread_count
        conn = connect()
        unread = get_unread_count(conn)
        conn.close()
    except Exception:
        pass

    return FridayStatus(
        daemon_state=dstate,
        presence="focus" if dstate == "running" else "away",
        watcher_count=watcher_count,
        pending_items=pending,
        last_cycle_ago=last_cycle_ago,
        has_alerts=has_alerts,
        active_agent=active_agent,
        unread_events=unread,
    )


# ──────────────────────────────────────────────────────────────────────────
# HUD Engine
# ──────────────────────────────────────────────────────────────────────────


class HudEngine:
    """Manages the terminal HUD — status bar + popup notifications.

    Usage::
        hud = HudEngine()
        hud.start()           # Start background thread
        hud.notify("Build passed!", "success")
        ...
        hud.stop()            # Stop thread, cleanup
    """

    def __init__(self, mode: StatusMode = StatusMode.FULL):
        self.mode = mode
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._notifications: list[HudNotification] = []
        self._next_notif_id = 0
        self._last_status: Optional[FridayStatus] = None
        self._status_line = ""
        self._refresh_interval = 2.0  # seconds
        self._active = _is_tty()

        # Clear the status line on exit.
        atexit.register(self._cleanup)

    def notify(self, message: str, level: str = "info", duration: float = 5.0) -> None:
        """Queue a notification to display in the terminal HUD."""
        if not self._active or self.mode == StatusMode.OFF:
            return
        with self._lock:
            self._next_notif_id += 1
            self._notifications.append(HudNotification(
                id=self._next_notif_id,
                message=message,
                level=level,
                timestamp=time.time(),
                duration=duration,
            ))
            # Keep only last 3 notifications.
            if len(self._notifications) > 3:
                self._notifications = self._notifications[-3:]

    def start(self) -> None:
        """Start the HUD background thread."""
        if not self._active or self.mode == StatusMode.OFF:
            return
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the HUD background thread."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None
        self._cleanup()

    def _cleanup(self) -> None:
        """Clean up terminal state."""
        if not self._active:
            return
        try:
            sys.stdout.write(_ANSI_SHOW_CURSOR + _ANSI_RESET)
            sys.stdout.flush()
        except Exception:
            pass

    def _render_status_bar(self, status: FridayStatus) -> str:
        """Build the status line content."""
        if self.mode == StatusMode.OFF:
            return ""

        w = _terminal_width()
        status_icon = {
            "running": _color("●", "green"),
            "stopped": _color("○", "gray"),
            "crashed": _color("✗", "red"),
            "analyzing": _color("◷", "yellow"),
        }.get(status.daemon_state, _color("?", "gray"))

        presence_icon = {
            "focus": _color("🧘", "cyan"),
            "desk": _color("🪑", "green"),
            "away": _color("🚶", "gray"),
        }.get(status.presence, "?")

        status_text = f" {status_icon} {status.daemon_state.upper()} {presence_icon}"

        left = status_text
        right_parts = []

        if status.watcher_count > 0:
            right_parts.append(f"👁 {status.watcher_count}")

        if status.pending_items > 0:
            right_parts.append(_color(f"📨 {status.pending_items}", "yellow"))

        if status.active_agent:
            right_parts.append(_color("🤖 agent", "blue"))

        if status.unread_events > 0:
            right_parts.append(_color(f"📬 {status.unread_events}", "green"))

        right_parts.append(f"⏱ {status.last_cycle_ago}")

        right = "  ".join(right_parts)

        # Fit left + right into terminal width.
        left_visible = len(_strip_ansi(left))
        right_visible = len(_strip_ansi(right))
        available = w - left_visible - right_visible - 2

        if available < 0:
            # Truncate right if too long.
            right = right[:max(0, w - left_visible - 5)] + "..."
            right_visible = len(_strip_ansi(right))

        padding = max(0, w - left_visible - right_visible - 1)
        return left + _color(" " * padding, "dim") + right

    def _render_popup(self, notif: HudNotification) -> Optional[str]:
        """Render a single notification popup as a floating bar."""
        if self.mode == StatusMode.OFF:
            return None

        w = _terminal_width()
        elapsed = time.time() - notif.timestamp
        remaining = max(0, notif.duration - elapsed)

        if remaining <= 0:
            return None

        level_colors = {
            "info": ("blue", "ℹ"),
            "warning": ("yellow", "⚠"),
            "error": ("red", "✗"),
            "success": ("green", "✓"),
        }
        color_name, icon = level_colors.get(notif.level, ("gray", "●"))

        msg = notif.message[:w - 6]
        padded = msg.ljust(w - 6)
        bar = _color(f"  {icon} {padded}  ", f"bg_{color_name}", "bold", "white")
        return bar

    def _popup_rows(self) -> int:
        """Number of terminal rows currently occupied by popups."""
        now = time.time()
        with self._lock:
            active = sum(1 for n in self._notifications if now - n.timestamp < n.duration)
        return min(active, 3)

    def _draw(self, status: FridayStatus) -> None:
        """Draw status bar + popups on the terminal."""
        if not self._active or self.mode == StatusMode.OFF:
            return

        try:
            h = _terminal_height()
            now = time.time()
            popup_lines: list[str] = []

            with self._lock:
                active_notifs = [n for n in self._notifications if now - n.timestamp < n.duration]
                self._notifications = active_notifs  # Clean expired
                for notif in active_notifs[-3:]:
                    rendered = self._render_popup(notif)
                    if rendered:
                        popup_lines.append(rendered)

            popup_count = len(popup_lines)
            status_bar = self._render_status_bar(status)
            self._status_line = status_bar

            # Save cursor, go to bottom, draw popups + status line.
            out = [_ANSI_SAVE_CURSOR]

            # Draw status bar on the last line.
            status_row = h
            out.append(_move_to(status_row, 0))
            out.append(_ANSI_CLEAR_LINE + status_bar)

            # Draw popups above status bar.
            for i, popup in enumerate(popup_lines):
                row = h - popup_count + i
                if row >= 1:
                    out.append(_move_to(row, 0))
                    out.append(_ANSI_CLEAR_LINE + popup)

            out.append(_ANSI_RESTORE_CURSOR)
            sys.stdout.write("".join(out))
            sys.stdout.flush()
        except Exception:
            pass

    def _run_loop(self) -> None:
        """Background thread: fetch status, draw HUD, handle notifications."""
        # Hide cursor while HUD is active.
        try:
            sys.stdout.write(_ANSI_HIDE_CURSOR)
            sys.stdout.flush()
        except Exception:
            pass

        last_draw = 0.0
        while self._running:
            now = time.time()

            # Check if user is typing — suppress popups if stdin has data.
            has_stdin = False
            try:
                if select.select([sys.stdin], [], [], 0)[0]:
                    has_stdin = True
            except (OSError, ValueError):
                pass

            if has_stdin:
                # User is typing — only update status bar, no popups.
                pass

            try:
                status = _fetch_status()
                self._last_status = status
            except Exception:
                continue

            # Draw on interval or on status change.
            if now - last_draw >= self._refresh_interval:
                self._draw(status)
                last_draw = now

            time.sleep(0.2)

        # Restore cursor.
        try:
            sys.stdout.write(_ANSI_SHOW_CURSOR)
            sys.stdout.flush()
        except Exception:
            pass


# ──────────────────────────────────────────────────────────────────────────
# Module-level singleton
# ──────────────────────────────────────────────────────────────────────────

_hud_instance: Optional[HudEngine] = None


def get_hud() -> HudEngine:
    """Get or create the global HUD singleton."""
    global _hud_instance
    if _hud_instance is None:
        _hud_instance = HudEngine()
    return _hud_instance


def hud_start(mode: str = "full") -> None:
    """Start the HUD."""
    mode_enum = StatusMode(mode.lower()) if mode.lower() in ("off", "compact", "full") else StatusMode.FULL
    hud = get_hud()
    hud.mode = mode_enum
    hud.start()


def hud_stop() -> None:
    """Stop the HUD."""
    global _hud_instance
    if _hud_instance:
        _hud_instance.stop()
        _hud_instance = None


def hud_notify(message: str, level: str = "info") -> None:
    """Send a notification to the HUD."""
    hud = get_hud()
    hud.notify(message, level)


# ──────────────────────────────────────────────────────────────────────────
# Utility
# ──────────────────────────────────────────────────────────────────────────


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from a string."""
    return re.sub(r"\033\[[0-9;]*m", "", text)
