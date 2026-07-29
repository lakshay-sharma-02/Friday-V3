"""Persistent Watchers — monitor conditions across daemon cycles and notify when met.

A **Persistent Watcher** is a user-defined condition that Friday checks on
each daemon cycle. When the condition transitions from unmet → met, Friday
pushes an ambient feed event and sends a notification.

Examples::

    friday wait create "tests pass" --type shell_exit_code --command "pytest"
    friday wait create "server up" --type http_status --url "http://localhost:8080/health"
    friday wait create "file changed" --type file_modified --path "README.md"

When a watcher fires (condition becomes true after being false), it:
  1. Records the result in the persistent_watchers table
  2. Pushes a ``watcher_fired`` event to the ambient feed
  3. The notification engine routes it through the operator's preferred channel

Condition types:
  - ``shell_exit_code`` — runs a shell command; fires when exit code == 0
  - ``file_modified`` — checks file modification time; fires on change
  - ``http_status`` — checks HTTP response; fires when status < 400
  - ``process_running`` — checks if a process name is running; fires when found
"""

from __future__ import annotations

import json
import os
import subprocess
import time as _time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from .db import now_iso


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class Watcher:
    """One persistent condition to check on each daemon cycle.

    Attributes:
        id:                  DB auto-increment ID.
        name:                Human-readable name (e.g. "tests pass").
        condition_type:      One of ``shell_exit_code``, ``file_modified``,
                             ``http_status``, ``process_running``.
        condition_params:    JSON string with type-specific params.
        check_interval_seconds: Minimum seconds between checks (default 300).
        repeat:              If True, auto-re-arm after firing (recurring).
        last_checked_at:     ISO timestamp of last check.
        last_result:         True if the condition was met on last check.
        last_error:          Error message from last check, if any.
        notified:            True if the operator was notified about current state.
        created_at:          ISO timestamp.
        updated_at:          ISO timestamp.
    """
    id: int = 0
    name: str = ""
    condition_type: str = "shell_exit_code"
    condition_params: str = "{}"
    check_interval_seconds: int = 300
    repeat: bool = False
    last_checked_at: Optional[str] = None
    last_result: Optional[bool] = None
    last_error: Optional[str] = None
    notified: bool = False
    created_at: str = ""
    updated_at: str = ""


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

_WATCHER_TABLE = """
    CREATE TABLE IF NOT EXISTS persistent_watchers (
        id                      INTEGER PRIMARY KEY AUTOINCREMENT,
        name                    TEXT NOT NULL UNIQUE,
        condition_type          TEXT NOT NULL,
        condition_params        TEXT NOT NULL DEFAULT '{}',
        check_interval_seconds  INTEGER NOT NULL DEFAULT 300,
        repeat                  INTEGER NOT NULL DEFAULT 0,
        last_checked_at         TEXT,
        last_result             INTEGER,
        last_error              TEXT,
        notified                INTEGER NOT NULL DEFAULT 0,
        created_at              TEXT NOT NULL,
        updated_at              TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_persistent_watchers_type
        ON persistent_watchers(condition_type);
"""

_ADD_REPEAT_COLUMN = """
    ALTER TABLE persistent_watchers ADD COLUMN repeat INTEGER NOT NULL DEFAULT 0;
"""


def _ensure_table(conn) -> None:
    try:
        conn.executescript(_WATCHER_TABLE)
        conn.commit()
    except Exception:
        conn.rollback()
    # Migrate existing tables: add repeat column if missing.
    try:
        conn.execute(_ADD_REPEAT_COLUMN)
        conn.commit()
    except Exception:
        conn.rollback()


# ---------------------------------------------------------------------------
# Condition checkers
# ---------------------------------------------------------------------------


def _check_shell_exit_code(params: dict) -> tuple[bool, Optional[str]]:
    """Run a shell command; return (True, None) if exit code == 0."""
    command = params.get("command", "")
    if not command:
        return False, "No command specified"
    try:
        result = subprocess.run(
            command, shell=True, timeout=params.get("timeout", 60),
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            return True, None
        stderr = (result.stderr or "").strip()[:200]
        stdout = (result.stdout or "").strip()[:200]
        detail = f"exit code {result.returncode}"
        if stderr:
            detail += f": {stderr}"
        elif stdout:
            detail += f": {stdout}"
        return False, detail
    except subprocess.TimeoutExpired:
        return False, "Command timed out"
    except FileNotFoundError:
        return False, "Command not found"
    except Exception as exc:
        return False, str(exc)[:200]


def _check_file_modified(params: dict, prev_result: Optional[bool]) -> tuple[bool, Optional[str]]:
    """Check if a file was modified since last check.

    Returns True if the file exists AND has been modified since the last
    check (or on first check, if the file exists).

    Uses ``last_checked_at`` (passed via ``params``) rather than an embedded
    mtime so that the tracking survives daemon restarts.
    """
    path = params.get("path", "")
    last_checked_at = params.get("last_checked_at")
    if not path:
        return False, "No file path specified"
    try:
        if not os.path.exists(path):
            return False, f"File not found: {path}"
        mtime = os.path.getmtime(path)
        # First check: file exists, that's the trigger.
        if prev_result is None:
            return True, None
        # Subsequent checks: fire if mtime is newer than last check time.
        if last_checked_at:
            try:
                last_time = datetime.fromisoformat(last_checked_at).timestamp()
                if mtime <= last_time:
                    return False, None
            except (ValueError, TypeError):
                pass  # fall through to fire
        return True, None
    except Exception as exc:
        return False, str(exc)[:200]


def _check_http_status(params: dict) -> tuple[bool, Optional[str]]:
    """Check an HTTP endpoint; returns True if status < 400."""
    url = params.get("url", "")
    if not url:
        return False, "No URL specified"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=params.get("timeout", 30)) as resp:
            if resp.status < 400:
                return True, None
            return False, f"HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        if exc.code < 400:
            return True, None
        return False, f"HTTP {exc.code}"
    except Exception as exc:
        return False, str(exc)[:200]


def _check_git_dirty(params: dict) -> tuple[bool, Optional[str]]:
    """Check if a git repository has uncommitted changes."""
    path = params.get("path", "")
    if not path:
        return False, "No path specified"
    try:
        result = subprocess.run(
            ["git", "-C", path, "status", "--porcelain"],
            timeout=10, capture_output=True, text=True,
        )
        if result.stdout.strip():
            return True, None
        return False, "No uncommitted changes"
    except subprocess.TimeoutExpired:
        return False, "Git status timed out"
    except FileNotFoundError:
        return False, "Git not found"
    except Exception as exc:
        return False, str(exc)[:200]


def _check_process_running(params: dict) -> tuple[bool, Optional[str]]:
    """Check if a process name is running via pgrep."""
    process_name = params.get("process", "") or params.get("name", "")
    if not process_name:
        return False, "No process name specified"
    try:
        result = subprocess.run(
            ["pgrep", "-f", process_name],
            timeout=10, capture_output=True, text=True,
        )
        if result.returncode == 0:
            return True, None
        return False, f"Process '{process_name}' not found"
    except FileNotFoundError:
        # pgrep not available
        try:
            result = subprocess.run(
                ["ps", "aux"], timeout=10, capture_output=True, text=True,
            )
            if process_name in result.stdout:
                return True, None
            return False, f"Process '{process_name}' not found"
        except Exception as exc:
            return False, str(exc)[:200]
    except Exception as exc:
        return False, str(exc)[:200]

def _check_active_app(params: dict, prev_result: Optional[bool]) -> tuple[bool, Optional[str]]:
    """Check if the active application matches a pattern.

    Fires when the currently focused application's process name or
    window class matches the ``app`` parameter. Uses screen.py's
    ``collect_screen_context()`` to detect the active app on each check.

    Params:
        app: Application name/class to match (e.g. ``"code"``, ``"chromium"``,
             ``"Alacritty"``). Case-insensitive substring match.
        window_title: Optional regex to match against the window title.
    """
    app_name = params.get("app", "") or params.get("process", "")
    title_pattern = params.get("window_title", "")

    if not app_name and not title_pattern:
        return False, "No app or window_title specified"

    try:
        from .screen import collect_screen_context

        ctx = collect_screen_context(include_clipboard=False, include_ocr=False)
        if not ctx.active_window_process and not ctx.active_window_title:
            return False, "Could not detect active window"

        # Check app name (substring match, case-insensitive).
        if app_name:
            app_lower = app_name.lower()
            proc_lower = (ctx.active_window_process or "").lower()
            class_lower = (ctx.active_window_class or "").lower()
            if app_lower in proc_lower or app_lower in class_lower:
                return True, None

        # Check window title (regex match).
        if title_pattern:
            import re
            title = ctx.active_window_title or ""
            try:
                if re.search(title_pattern, title, re.IGNORECASE):
                    return True, None
                return False, f"Window title '{title[:60]}' did not match '{title_pattern}'"
            except re.error as exc:
                return False, f"Invalid regex: {exc}"

        return False, f"App '{app_name}' not active (current: {ctx.active_window_process or '?'})"
    except Exception as exc:
        return False, str(exc)[:200]


def _check_clipboard_content(params: dict, prev_result: Optional[bool]) -> tuple[bool, Optional[str]]:
    """Check if clipboard content matches a pattern.

    Fires when the clipboard contains (or doesn't contain) matching text.
    Useful for "tell me when I copy a URL" or "notify when I copy an error".

    Params:
        contains: Text to search for in clipboard (substring match).
        regex: Optional regex pattern to match clipboard content.
        min_length: Minimum clipboard text length to consider (default 1).
    """
    contains = params.get("contains", "")
    pattern = params.get("regex", "")
    min_length = int(params.get("min_length", 1))

    if not contains and not pattern:
        return False, "No 'contains' or 'regex' specified"

    try:
        from .screen import _read_clipboard

        text, source = _read_clipboard()
        if not text or len(text.strip()) < min_length:
            return False, "Clipboard is empty or too short"

        text_to_check = text.strip()

        # Check contains (substring match, case-insensitive).
        if contains:
            if contains.lower() in text_to_check.lower():
                return True, None
            return False, f"Clipboard did not contain '{contains}'"

        # Check regex.
        if pattern:
            import re
            try:
                if re.search(pattern, text_to_check):
                    return True, None
                return False, f"Clipboard did not match regex '{pattern}'"
            except re.error as exc:
                return False, f"Invalid regex: {exc}"

        return False, "No match condition specified"
    except Exception as exc:
        return False, str(exc)[:200]


def _check_window_title(params: dict, prev_result: Optional[bool]) -> tuple[bool, Optional[str]]:
    """Check if the active window title matches a regex pattern.

    Fires when the currently focused window's title matches the given
    regex or substring. This is useful for "tell me when I open X".

    Params:
        title: Regex pattern to match against window title.
        contains: Substring to find in window title (case-insensitive).
    """
    title_pattern = params.get("title", "")
    contains = params.get("contains", "")

    if not title_pattern and not contains:
        return False, "No 'title' or 'contains' specified"

    try:
        from .screen import collect_screen_context

        ctx = collect_screen_context(include_clipboard=False, include_ocr=False)
        title = ctx.active_window_title or ""

        if not title:
            return False, "Could not detect window title"

        # Check substring match.
        if contains:
            if contains.lower() in title.lower():
                return True, None
            return False, f"Window title '{title[:60]}' did not contain '{contains}'"

        # Check regex.
        if title_pattern:
            import re
            try:
                if re.search(title_pattern, title, re.IGNORECASE):
                    return True, None
                return False, f"Window title '{title[:60]}' did not match '{title_pattern}'"
            except re.error as exc:
                return False, f"Invalid regex: {exc}"

        return False, "No match condition specified"
    except Exception as exc:
        return False, str(exc)[:200]


# ---------------------------------------------------------------------------
# WatcherEngine
# ---------------------------------------------------------------------------


class WatcherEngine:
    """Create, read, update, delete, and check persistent watchers.

    Usage::

        eng = WatcherEngine(conn)
        eng.create("tests pass", "shell_exit_code", {"command": "pytest"})
        eng.check_all()
    """

    def __init__(self, conn) -> None:
        self._conn = conn
        _ensure_table(conn)

    # ── CRUD ──────────────────────────────────────────────────────────

    def create(
        self,
        name: str,
        condition_type: str,
        condition_params: Optional[dict] = None,
        check_interval_seconds: int = 300,
        repeat: bool = False,
    ) -> Watcher:
        """Create a new persistent watcher.

        Args:
            name: Human-readable name.
            condition_type: One of ``shell_exit_code``, ``file_modified``,
                ``http_status``, ``process_running``.
            condition_params: Type-specific parameters.
            check_interval_seconds: Min seconds between checks.
            repeat: If True, auto-re-arm after firing (recurring).

        Returns:
            The persisted Watcher.

        Raises:
            ValueError: If a watcher with this name already exists or the
                condition type is invalid.
        """
        if condition_type not in _CHECKERS:
            raise ValueError(
                f"Invalid condition type: {condition_type}. "
                f"Valid: {', '.join(_CHECKERS.keys())}"
            )

        condition_params = condition_params or {}
        now = now_iso()
        params_json = json.dumps(condition_params)

        try:
            self._conn.execute(
                """INSERT INTO persistent_watchers
                   (name, condition_type, condition_params, check_interval_seconds,
                    repeat, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (name, condition_type, params_json, check_interval_seconds,
                 int(repeat), now, now),
            )
            self._conn.commit()
            watcher_id = self._conn.execute(
                "SELECT id FROM persistent_watchers WHERE name = ?", (name,)
            ).fetchone()["id"]
        except Exception as exc:
            self._conn.rollback()
            if "UNIQUE" in str(exc):
                raise ValueError(f"Watcher '{name}' already exists") from exc
            raise

        return self._row_to_watcher({
            "id": watcher_id, "name": name, "condition_type": condition_type,
            "condition_params": params_json, "check_interval_seconds": check_interval_seconds,
            "repeat": int(repeat),
            "last_checked_at": None, "last_result": None, "last_error": None,
            "notified": 0, "created_at": now, "updated_at": now,
        })

    def get(self, name: str) -> Optional[Watcher]:
        """Look up a watcher by name."""
        row = self._conn.execute(
            "SELECT * FROM persistent_watchers WHERE name = ?", (name,)
        ).fetchone()
        return self._row_to_watcher(row) if row else None

    def list_all(self) -> list[Watcher]:
        """Return all watchers, ordered by name."""
        rows = self._conn.execute(
            "SELECT * FROM persistent_watchers ORDER BY name"
        ).fetchall()
        return [self._row_to_watcher(r) for r in rows]

    def delete(self, name: str) -> bool:
        """Delete a watcher by name. Returns True if deleted."""
        row = self._conn.execute(
            "SELECT id FROM persistent_watchers WHERE name = ?", (name,)
        ).fetchone()
        if row is None:
            return False
        self._conn.execute(
            "DELETE FROM persistent_watchers WHERE id = ?", (row["id"],)
        )
        self._conn.commit()
        return True

    def _row_to_watcher(self, row) -> Watcher:
        return Watcher(
            id=row["id"],
            name=row["name"],
            condition_type=row["condition_type"],
            condition_params=row["condition_params"],
            check_interval_seconds=row["check_interval_seconds"],
            repeat=bool(row["repeat"]) if "repeat" in row else False,
            last_checked_at=row["last_checked_at"],
            last_result=bool(row["last_result"]) if row["last_result"] is not None else None,
            last_error=row["last_error"],
            notified=bool(row["notified"]) if row["notified"] is not None else False,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    # ── Checking ──────────────────────────────────────────────────────

    def check_one(self, watcher: Watcher) -> dict[str, Any]:
        """Run a single watcher's condition check.

        Args:
            watcher: The Watcher to check.

        Returns:
            A result dict:
              - ``triggered``: True if the condition just became met (was unmet)
              - ``met``: True if the condition IS met now
              - ``error``: Error message, or None
              - ``watcher_name``: The watcher name
              - ``condition_type``: The type of check performed
        """
        now = now_iso()

        # Parse condition_params JSON.
        params_raw = watcher.condition_params
        try:
            params = json.loads(params_raw) if isinstance(params_raw, str) else params_raw
        except (json.JSONDecodeError, TypeError):
            params = {}

        checker = _CHECKERS.get(watcher.condition_type)
        if checker is None:
            self._update_result(watcher.id, False, f"Unknown type: {watcher.condition_type}", now)
            return {
                "triggered": False, "met": False,
                "error": f"Unknown condition type: {watcher.condition_type}",
                "watcher_name": watcher.name,
                "condition_type": watcher.condition_type,
            }

        # Inject watcher metadata into params for checkers that need it.
        params["last_checked_at"] = watcher.last_checked_at

        # Run the checker.
        met, error = checker(params, watcher.last_result)

        # Determine if this is a new trigger (was unmet, now met).
        was_met = watcher.last_result
        triggered = met and not was_met and was_met is not None
        # First successful check also counts as triggered.
        if met and was_met is None:
            triggered = True

        self._update_result(watcher.id, met, error, now)

        return {
            "triggered": triggered,
            "met": met,
            "error": error,
            "watcher_name": watcher.name,
            "condition_type": watcher.condition_type,
        }

    def check_all(self) -> list[dict[str, Any]]:
        """Run every watcher that is due for a check (based on interval).

        Returns:
            List of result dicts. Only watchers that were due for a check
            are included.
        """
        results: list[dict[str, Any]] = []
        for watcher in self.list_all():
            # Check if this watcher is due.
            if not self._is_due(watcher):
                continue
            result = self.check_one(watcher)
            results.append(result)

            # If triggered, push to ambient feed.
            if result.get("triggered"):
                self._push_trigger_event(result)

        return results

    def _is_due(self, watcher: Watcher) -> bool:
        """Check if enough time has passed since the last check."""
        if watcher.last_checked_at is None:
            return True
        try:
            last = datetime.fromisoformat(watcher.last_checked_at)
            elapsed = (datetime.now(timezone.utc) - last).total_seconds()
            return elapsed >= watcher.check_interval_seconds
        except (ValueError, TypeError):
            return True

    def _update_result(
        self, watcher_id: int, met: bool, error: Optional[str], now: str
    ) -> None:
        """Persist the latest check result."""
        self._conn.execute(
            "UPDATE persistent_watchers SET last_checked_at=?, last_result=?, "
            "last_error=?, updated_at=? WHERE id=?",
            (now, 1 if met else 0, error, now, watcher_id),
        )
        self._conn.commit()

    def create_auto_watcher(
        self,
        name: str,
        condition_type: str,
        condition_params: Optional[dict] = None,
        ttl_minutes: int = 30,
    ) -> Optional[Watcher]:
        """Create a temporary auto-watcher from screen context changes.

        These are auto-expiring watchers created when Friday detects
        an app switch, URL change, or clipboard change. They have
        a short TTL (default 30 min) and the ``auto_watcher`` prefix
        so they can be identified and pruned later.

        Args:
            name: Human-readable name (auto-prefixed with "[auto] ").
            condition_type: One of the valid condition types.
            condition_params: Type-specific parameters.
            ttl_minutes: Minutes until this auto-watcher expires.

        Returns:
            The created Watcher, or None if creation failed (e.g.
            already exists or invalid type).

        Usage (typically from daemon::

            eng.create_auto_watcher(
                "browser switched",
                "active_app",
                {"app": "chromium"},
                ttl_minutes=30,
            )
        """
        auto_name = f"[auto] {name}"

        # Don't create if one already exists with a similar name.
        existing = self.get(auto_name)
        if existing:
            # Reset its TTL by re-creating (touch).
            self.delete(auto_name)

        try:
            return self.create(
                name=auto_name,
                condition_type=condition_type,
                condition_params=condition_params or {},
                check_interval_seconds=min(ttl_minutes * 60, 300),
                repeat=True,
            )
        except (ValueError, Exception):
            return None

    def prune_auto_watchers(self) -> int:
        """Delete stale auto-watchers (prefix [auto]) that are no longer useful.

        Three categories of auto-watchers are pruned:
          1. Never fired and older than 1 hour (stale).
          2. Fired and acknowledged — cleanup complete, watcher served its purpose.
          3. Fired but not acknowledged after 6 hours — stale notification.

        Returns the number of deleted auto-watchers.
        """
        deleted = 0
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        for w in self.list_all():
            if not w.name.startswith("[auto] "):
                continue

            if not w.last_checked_at:
                continue

            try:
                last = datetime.fromisoformat(w.last_checked_at)
                age = (now - last).total_seconds()
            except (ValueError, TypeError):
                continue

            should_delete = False

            # 1. Never fired and stale (> 1 hour).
            if w.last_result is None and age > 3600:
                should_delete = True

            # 2. Fired and acknowledged — job done.
            if w.last_result and w.notified and age > 60:  # 1 min grace for ack
                should_delete = True

            # 3. Fired but never acknowledged after 6 hours.
            if w.last_result and not w.notified and age > 21600:
                should_delete = True

            if should_delete:
                if self.delete(w.name):
                    deleted += 1

        return deleted

    def acknowledge(self, name: str) -> bool:
        """Acknowledge a fired watcher — reset notified flag.

        Returns True if the watcher was found and acknowledged.
        """
        import sqlite3
        try:
            row = self._conn.execute(
                "SELECT id, repeat FROM persistent_watchers WHERE name = ?", (name,)
            ).fetchone()
        except sqlite3.OperationalError:
            # repeat column may not exist in older DBs.
            row = self._conn.execute(
                "SELECT id FROM persistent_watchers WHERE name = ?", (name,)
            ).fetchone()
        if row is None:
            return False
        # If repeat=True, reset last_result so it can fire again.
        is_repeat = bool(row["repeat"]) if "repeat" in row else False
        if is_repeat:
            self._conn.execute(
                "UPDATE persistent_watchers SET notified=0, last_result=NULL, "
                "updated_at=? WHERE id=?",
                (now_iso(), row["id"]),
            )
        else:
            self._conn.execute(
                "UPDATE persistent_watchers SET notified=1, "
                "updated_at=? WHERE id=?",
                (now_iso(), row["id"]),
            )
        self._conn.commit()
        return True

    def _push_trigger_event(self, result: dict) -> None:
        """Push a ``watcher_fired`` event to the ambient feed.

        For auto-watchers (name starting with ``[auto]``), also sends
        a desktop notification via notify-send so Friday proactively
        tells you what it noticed.
        """
        try:
            from .ambient import AmbientEvent, push_event

            name = result.get("watcher_name", "?")
            ctype = result.get("condition_type", "?")
            is_auto = name.startswith("[auto]")

            ev = AmbientEvent(
                timestamp=now_iso(),
                event_type="watcher_fired",
                title=f"Watcher '{name}' triggered",
                detail=f"Condition '{name}' ({ctype}) is now met.",
                source="watcher",
                priority=2,
                category="intelligence",
                actionable=True,
                action_command=f"friday wait ack {name}",
            )
            push_event(self._conn, ev)

            # Desktop notification for auto-watchers (proactive alert).
            if is_auto:
                display_name = name.replace("[auto] ", "").strip()[:60]
                title_text = f"🔔 Friday — {display_name}"
                message_text = f"Auto-watcher '{display_name}' triggered: condition '{ctype}' met."

                if sys.platform == "linux":
                    subprocess.run(
                        ["notify-send", title_text, message_text],
                        timeout=5, capture_output=True,
                    )
                elif sys.platform == "darwin":
                    subprocess.run(
                        ["osascript", "-e",
                         f'display notification "{message_text}" with title "{title_text}"'],
                        timeout=5, capture_output=True,
                    )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Checker registry
# ---------------------------------------------------------------------------

_CHECKERS: dict[str, callable] = {
    "shell_exit_code": lambda params, prev: _check_shell_exit_code(params),
    "file_modified": _check_file_modified,
    "http_status": lambda params, prev: _check_http_status(params),
    "process_running": lambda params, prev: _check_process_running(params),
    "git_dirty": _check_git_dirty,
    "active_app": _check_active_app,
    "clipboard_content": _check_clipboard_content,
    "window_title": _check_window_title,
}


# ---------------------------------------------------------------------------
# Auto-watcher tuning config — which apps/contexts create auto-watchers
# ---------------------------------------------------------------------------

# Preference key prefix stored in ``operator_preferences``.
_TUNE_PREFIX = "auto_watcher_tune_"

# Global mode key — when enabled, bypasses all tuning and creates auto-watchers
# for EVERY detectable app change. Stored in ``operator_preferences``.
_GLOBAL_KEY = "auto_watcher_global"

# Default categories and their keywords (fallback when no tuning rule exists).
DEFAULT_BROWSER_KEYWORDS = ("chromium", "chrome", "brave", "firefox",
                              "edge", "opera", "vivaldi", "thorium")
DEFAULT_IDE_KEYWORDS = ("code", "cursor", "vim", "neovim", "nvim",
                        "idea", "webstorm", "pycharm", "intellij",
                        "emacs", "zed", "sublime")
DEFAULT_TERMINAL_KEYWORDS = ("alacritty", "kitty", "gnome-terminal",
                             "konsole", "terminator", "wezterm",
                             "foot", "st", "rxvt", "tmux")


def _tune_key(app_pattern: str) -> str:
    """Build the operator_preferences key for an app tuning rule."""
    safe = app_pattern.strip().lower().replace(" ", "_")
    return f"{_TUNE_PREFIX}{safe}"


def is_global_mode(conn) -> bool:
    """Check if global auto-watcher mode is enabled.

    When global mode is on, ``should_create_auto_watcher()`` returns True
    for EVERY detectable app change, bypassing all tuning rules and
    category detection. Power user mode: creates watchers for literally
    every app you switch to.
    """
    try:
        row = conn.execute(
            "SELECT value FROM operator_preferences WHERE key = ?",
            (_GLOBAL_KEY,),
        ).fetchone()
        return row is not None and row["value"].strip().lower() == "true"
    except Exception:
        return False


def set_global_mode(conn, enabled: bool) -> None:
    """Enable or disable global auto-watcher mode.

    Args:
        enabled: True to watch every app, False to use tuned/category rules.
    """
    val = "true" if enabled else "false"
    try:
        from .db import set_operator_preference
        set_operator_preference(conn, key=_GLOBAL_KEY, value=val, source="explicit")
    except Exception as exc:
        raise ValueError(f"Failed to set global mode: {exc}")


def get_tuning_rules(conn) -> list[dict]:
    """Get all auto-watcher tuning rules from operator_preferences.

    Returns a list of dicts:
      - app: the app pattern (e.g. "brave", "code")
      - action: "watch" or "ignore"
      - source: how the rule was set

    Rules are ordered: most recently set first.
    """
    rules: list[dict] = []
    try:
        rows = conn.execute(
            "SELECT key, value, set_at, source FROM operator_preferences "
            "WHERE key LIKE ? ORDER BY set_at DESC",
            (f"{_TUNE_PREFIX}%",),
        ).fetchall()
        for r in rows:
            app_pattern = r["key"][len(_TUNE_PREFIX):].replace("_", " ")
            rules.append({
                "app": app_pattern,
                "action": r["value"],
                "source": r["source"],
                "set_at": r["set_at"],
            })
    except Exception:
        pass
    return rules


def set_tuning_rule(conn, app_pattern: str, action: str) -> None:
    """Set a tuning rule for an app pattern.

    Args:
        app_pattern: App name/pattern to tune (e.g. "brave", "code", "alacritty").
        action: "watch" to create auto-watchers, "ignore" to suppress them.

    Raises:
        ValueError: If action is not "watch" or "ignore".
    """
    action = action.strip().lower()
    if action not in ("watch", "ignore"):
        raise ValueError(f"Invalid action '{action}'. Must be 'watch' or 'ignore'.")

    key = _tune_key(app_pattern)
    try:
        from .db import set_operator_preference
        set_operator_preference(conn, key=key, value=action, source="explicit")
    except Exception as exc:
        raise ValueError(f"Failed to set tuning rule: {exc}")


def remove_tuning_rule(conn, app_pattern: str) -> bool:
    """Remove a tuning rule for an app pattern. Returns True if removed."""
    key = _tune_key(app_pattern)
    try:
        cur = conn.execute(
            "DELETE FROM operator_preferences WHERE key = ?", (key,)
        )
        conn.commit()
        return cur.rowcount > 0
    except Exception:
        return False


def reset_tuning_defaults(conn) -> int:
    """Remove ALL auto-watcher tuning rules, restoring defaults.

    Returns the number of rules removed.
    """
    try:
        cur = conn.execute(
            "DELETE FROM operator_preferences WHERE key LIKE ?",
            (f"{_TUNE_PREFIX}%",),
        )
        conn.commit()
        return cur.rowcount
    except Exception:
        return 0


def should_create_auto_watcher(conn, app_name: str) -> bool:
    """Check if an auto-watcher should be created for the given app.

    Priority (highest first):
      1. **Global mode** — if enabled via ``friday wait context --global``,
         returns True for EVERY app. Bypasses all tuning and category rules.
      2. **Explicit tuning rules** — per-app watch/ignore rules set via
         ``friday wait context --tune add``.
      3. **Default category detection** — browser, IDE, and terminal keywords.
      4. **Unknown app** — no auto-watcher by default.

    Args:
        conn: Database connection.
        app_name: The app name/process to check.

    Returns:
        True if an auto-watcher should be created, False if suppressed.
    """
    if not app_name:
        return False

    app_lower = app_name.strip().lower()

    # 0. Global mode bypass — highest priority.
    try:
        if is_global_mode(conn):
            return True
    except Exception:
        pass

    # 1. Check explicit tuning rules.
    try:
        rules = get_tuning_rules(conn)
        for rule in rules:
            rule_app = rule["app"].strip().lower()
            if rule_app in app_lower or app_lower in rule_app:
                return rule["action"] == "watch"
    except Exception:
        pass

    # 2. Fall back to default category detection.
    if any(k in app_lower for k in DEFAULT_BROWSER_KEYWORDS):
        return True
    if any(k in app_lower for k in DEFAULT_IDE_KEYWORDS):
        return True
    if any(k in app_lower for k in DEFAULT_TERMINAL_KEYWORDS):
        return True

    # 3. Unknown app — no auto-watcher by default.
    return False


# ---------------------------------------------------------------------------
# Stats — auto-watcher usage statistics
# ---------------------------------------------------------------------------


def get_auto_watcher_stats(conn) -> dict:
    """Gather statistics about auto-watcher usage.

    Returns a dict with:
      - total_auto_watchers: total [auto] watchers ever created
      - active_auto_watchers: currently existing [auto] watchers
      - triggered_count: how many have fired (last_result = True)
      - by_app: list of {app, total, triggered, last_seen} per app
      - ignored_apps: list of app patterns tuned to "ignore"
      - global_mode: {enabled, enabled_since, duration_hours}
      - total_traditional: count of non-auto watchers
    """
    stats = {
        "total_auto_watchers": 0,
        "active_auto_watchers": 0,
        "triggered_count": 0,
        "by_app": [],
        "ignored_apps": [],
        "global_mode": {"enabled": False, "enabled_since": None, "duration_hours": 0},
        "total_traditional": 0,
    }

    # Ensure the persistent_watchers table exists so the query below works
    # even when called on a fresh DB without a WatcherEngine.
    _ensure_table(conn)

    # ── Section 1: Auto-watcher counts from persistent_watchers ──
    try:
        rows = conn.execute(
            "SELECT name, condition_type, condition_params, "
            "last_result, last_checked_at, created_at, updated_at "
            "FROM persistent_watchers ORDER BY name"
        ).fetchall()
    except Exception:
        rows = []

    auto_rows = [r for r in rows if r["name"].startswith("[auto] ")]
    trad_rows = [r for r in rows if not r["name"].startswith("[auto] ")]

    stats["active_auto_watchers"] = len(auto_rows)
    stats["total_auto_watchers"] = len(auto_rows)
    stats["total_traditional"] = len(trad_rows)

    # Parse auto-watchers by app.
    app_map: dict[str, dict] = {}
    for r in auto_rows:
        triggered = bool(r["last_result"]) if r["last_result"] is not None else False
        if triggered:
            stats["triggered_count"] += 1

        app_name = _extract_app_name(r)
        if app_name not in app_map:
            app_map[app_name] = {"app": app_name, "total": 0, "triggered": 0, "last_seen": r["updated_at"] or r["created_at"]}
        app_map[app_name]["total"] += 1
        if triggered:
            app_map[app_name]["triggered"] += 1
        seen = r["updated_at"] or r["created_at"]
        if seen and seen > app_map[app_name]["last_seen"]:
            app_map[app_name]["last_seen"] = seen

    stats["by_app"] = sorted(app_map.values(), key=lambda x: (-x["triggered"], -x["total"]))

    # ── Section 2: Tuning rules (ignored apps) ──
    try:
        all_rules = get_tuning_rules(conn)
        stats["ignored_apps"] = [r["app"] for r in all_rules if r["action"] == "ignore"]
    except Exception:
        pass

    # ── Section 3: Global mode timing ──
    try:
        row = conn.execute(
            "SELECT value, set_at FROM operator_preferences WHERE key = ?",
            (_GLOBAL_KEY,),
        ).fetchone()
        if row is not None:
            enabled = row["value"].strip().lower() == "true"
            stats["global_mode"]["enabled"] = enabled
            if row["set_at"]:
                stats["global_mode"]["enabled_since"] = row["set_at"]
                try:
                    from datetime import datetime, timezone
                    set_time = datetime.fromisoformat(row["set_at"])
                    duration = (datetime.now(timezone.utc) - set_time).total_seconds()
                    stats["global_mode"]["duration_hours"] = round(duration / 3600, 1)
                except Exception:
                    pass
    except Exception:
        pass

    return stats


def _extract_app_name(row) -> str:
    """Extract the app/context name from a persistent_watchers row.

    Tries to parse from condition_params first, then falls back to
    parsing the watcher name after the ``[auto] `` prefix.

    Returns a cleaned app name (e.g. "Brave", "Code", "github.com").
    """
    name = row["name"]
    params_raw = row["condition_params"]

    # Try to extract from condition_params first (most accurate).
    if params_raw:
        try:
            params = json.loads(params_raw) if isinstance(params_raw, str) else params_raw
            app_val = params.get("app") or params.get("process") or params.get("contains") or params.get("title") or ""
            if app_val:
                return app_val.strip()[:40]
        except (json.JSONDecodeError, TypeError):
            pass

    # Fall back to parsing the [auto] name.
    raw = name.replace("[auto] ", "", 1).strip()

    # Handle common patterns: "browser: Brave active" -> "Brave"
    # "url: https://github.com..." -> "github.com"
    # "clipboard: URL detected" -> "clipboard"
    for prefix in ("browser: ", "ide: ", "app: ", "url: ", "clipboard: "):
        if raw.startswith(prefix):
            raw = raw[len(prefix):]
            break

    # Clean trailing words like "active", "detected", "..."
    for suffix in (" active", " detected", "...", " ..."):
        if raw.endswith(suffix):
            raw = raw[:-len(suffix)]
            break

    return raw.strip()[:40] or "unknown"


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def format_watcher(w: Watcher, verbose: bool = False) -> str:
    """Render a watcher as human-readable text."""
    status = "✅ FIRED" if w.notified and w.last_result else (
        "🔔 TRIGGERED" if w.last_result else "⏳ Waiting"
    )
    lines = [
        f"Watcher: {w.name}",
        f"  Type:    {w.condition_type}",
        f"  Status:  {status}",
        f"  Interval: {w.check_interval_seconds}s",
    ]
    if w.repeat:
        lines.append(f"  Repeat:  auto-re-arm enabled")
    if w.last_checked_at:
        lines.append(f"  Last check: {w.last_checked_at[:19]}")
        if w.last_result is not None:
            lines.append(f"  Last result: {'met' if w.last_result else 'unmet'}")
        if w.last_error:
            lines.append(f"  Last error: {w.last_error[:120]}")
    if verbose:
        try:
            params = json.loads(w.condition_params)
            for k, v in params.items():
                if k.startswith("_"):
                    continue
                lines.append(f"  {k}: {v}")
        except (json.JSONDecodeError, TypeError):
            lines.append(f"  Params: {w.condition_params[:100]}")
    return "\n".join(lines)


def format_watchers(watchers: list[Watcher]) -> str:
    """Render a list of watchers as a table."""
    if not watchers:
        return "  No persistent watchers defined."
    lines = [f"{'Name':<25} {'Type':<20} {'Status':<15} {'Interval':<10}", "-" * 75]
    for w in watchers:
        status = "Triggered" if w.last_result else (
            "Notified" if w.notified else "Waiting"
        )
        lines.append(
            f"{w.name:<25} {w.condition_type:<20} {status:<15} {w.check_interval_seconds}s"
        )
    return "\n".join(lines)
