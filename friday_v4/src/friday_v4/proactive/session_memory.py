"""Session Memory — remembers work sessions across system restarts.

Lightweight, file-based storage (JSON) that tracks:
  - Session start/end times
  - Active apps per session
  - Git repos worked on
  - Session duration and focus level
  - Daily/weekly session statistics

No V3 database dependency — all data lives in ~/.friday/sessions/
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("friday_v4.proactive.session")

_SESSION_DIR = Path.home() / ".friday" / "sessions"
_SESSION_FILE = _SESSION_DIR / "current.json"
_HISTORY_FILE = _SESSION_DIR / "history.jsonl"


# ---------------------------------------------------------------------------
# Session Data Models
# ---------------------------------------------------------------------------


class SessionStore:
    """Tracks work sessions in a lightweight JSON store.

    A "session" begins when the user starts working and ends after
    15 minutes of inactivity. Each session records:
      - When it started and ended
      - What apps were used (most-used first)
      - What git repos were active
      - Estimated focus level

    Usage:
        store = SessionStore()
        store.start_session("kitty")
        ...
        store.end_session()
        stats = store.get_today_stats()
    """

    def __init__(self):
        self._lock = threading.Lock()
        _SESSION_DIR.mkdir(parents=True, exist_ok=True)
        self._current: Optional[dict] = None
        self._load_current()

    # ── Session Lifecycle ─────────────────────────────────────────────

    def _load_current(self):
        """Load current session from disk if it exists."""
        try:
            if _SESSION_FILE.exists():
                with open(_SESSION_FILE) as f:
                    data = json.load(f)
                    # Check if session is still valid (< 15 min idle)
                    last_active = datetime.fromisoformat(data.get("last_active", ""))
                    idle = (datetime.now(timezone.utc) - last_active).total_seconds()
                    if idle < 900:  # 15 minutes
                        # JSON round-trips sets to lists — normalize back to a
                        # set so in-memory callers can use set operations.
                        data["repos_active"] = set(data.get("repos_active") or [])
                        self._current = data
                        return
                    else:
                        # Session expired due to inactivity — archive it
                        self._archive_session(data)
                        _SESSION_FILE.unlink(missing_ok=True)
        except (json.JSONDecodeError, KeyError, ValueError, OSError) as exc:
            logger.debug(f"Could not load current session: {exc}")

        self._current = None

    def start_session(self, app_class: str = "") -> dict:
        """Start a new session or update the current one.

        Args:
            app_class: The window class that triggered the session start.

        Returns:
            Current session dict.
        """
        with self._lock:
            now = datetime.now(timezone.utc)

            if self._current is None:
                self._create_session_locked(now)
            current = self._current
            assert current is not None

            # Update last active
            current["last_active"] = now.isoformat()

            # Track app usage
            if app_class:
                apps = current.setdefault("apps_used", {})
                apps[app_class] = apps.get(app_class, 0) + 1

            self._save_current()
            return dict(current)

    def _create_session_locked(self, now: datetime) -> None:
        """Create a fresh current session. Caller must hold ``_lock``.

        Extracted so ``update_activity`` can create a session without
        re-acquiring the (non-reentrant) lock — previously it called
        ``start_session()`` while holding the lock, which deadlocked.
        """
        self._current = {
            "session_id": now.strftime("%Y%m%d_%H%M%S"),
            "started_at": now.isoformat(),
            "last_active": now.isoformat(),
            "apps_used": {},
            "repos_active": set(),
            "session_count": 0,
        }
        # Count sessions today
        self._current["session_count"] = self._count_sessions_today() + 1

    def end_session(self):
        """End the current session and archive it."""
        with self._lock:
            current = self._current
            if current is None:
                return

            now = datetime.now(timezone.utc)
            current["ended_at"] = now.isoformat()
            current["last_active"] = now.isoformat()

            # Convert repos set to list for JSON
            repos = current.get("repos_active", set())
            if isinstance(repos, set):
                current["repos_active"] = list(repos)

            # Calculate duration
            try:
                start = datetime.fromisoformat(current["started_at"])
                current["duration_minutes"] = int(
                    (now - start).total_seconds() / 60
                )
            except (ValueError, KeyError):
                current["duration_minutes"] = 0

            self._archive_session(current)
            _SESSION_FILE.unlink(missing_ok=True)
            self._current = None

    def update_activity(self, app_class: str = "", repo: str = ""):
        """Update the current session with recent activity.

        Call this periodically to keep the session alive and track activity.
        """
        with self._lock:
            now = datetime.now(timezone.utc)

            if self._current is None:
                # Create inline (never call start_session() here — it would
                # re-acquire the non-reentrant _lock and deadlock).
                self._create_session_locked(now)
            current = self._current
            assert current is not None

            current["last_active"] = now.isoformat()

            if app_class:
                apps = current.setdefault("apps_used", {})
                apps[app_class] = apps.get(app_class, 0) + 1

            if repo:
                repos = current.get("repos_active")
                if not isinstance(repos, set):
                    # Defensive: a persisted session may have been loaded
                    # with repos_active as a list (JSON round-trip).
                    repos = set(repos or [])
                    current["repos_active"] = repos
                repos.add(repo)

            self._save_current()

    # ── Storage ───────────────────────────────────────────────────────

    def _save_current(self):
        """Save the current session to disk."""
        try:
            # Create a serializable copy
            data = dict(self._current)
            repos = data.get("repos_active", set())
            if isinstance(repos, set):
                data["repos_active"] = list(repos)

            with open(_SESSION_FILE, "w") as f:
                json.dump(data, f, indent=2, default=str)
        except OSError as exc:
            logger.warning(f"Could not save session: {exc}")

    def _archive_session(self, session: dict):
        """Append a completed session to the history file."""
        try:
            # Ensure sets are converted to lists
            data = dict(session)
            repos = data.get("repos_active", set())
            if isinstance(repos, set):
                data["repos_active"] = list(repos)

            with open(_HISTORY_FILE, "a") as f:
                json.dump(data, f)
                f.write("\n")
        except OSError as exc:
            logger.warning(f"Could not archive session: {exc}")

    # ── Queries ───────────────────────────────────────────────────────

    def get_current_session(self) -> Optional[dict]:
        """Get the current active session, if any."""
        with self._lock:
            if self._current is None:
                return None
            return dict(self._current)

    def get_sessions_today(self) -> list[dict]:
        """Get all sessions from today."""
        return self._get_sessions_since(
            datetime.now(timezone.utc).replace(hour=0, minute=0, second=0)
        )

    def get_sessions_this_week(self) -> list[dict]:
        """Get all sessions from this week."""
        now = datetime.now(timezone.utc)
        # Monday of this week
        monday = now - timedelta(days=now.weekday())
        monday = monday.replace(hour=0, minute=0, second=0)
        return self._get_sessions_since(monday)

    def get_today_stats(self) -> dict:
        """Get summary statistics for today."""
        sessions = self.get_sessions_today()
        total_minutes = sum(s.get("duration_minutes", 0) for s in sessions)

        # Count unique apps across all sessions
        all_apps: dict[str, int] = {}
        for s in sessions:
            for app, count in s.get("apps_used", {}).items():
                all_apps[app] = all_apps.get(app, 0) + count

        # Most used app
        most_used = max(all_apps, key=all_apps.__getitem__) if all_apps else ""

        return {
            "session_count": len(sessions),
            "total_minutes": total_minutes,
            "most_used_app": most_used,
            "active_now": self._current is not None,
        }

    def get_weekly_stats(self) -> dict:
        """Get summary statistics for this week."""
        sessions = self.get_sessions_this_week()
        total_minutes = sum(s.get("duration_minutes", 0) for s in sessions)

        # Sessions per day
        days: dict[str, int] = {}
        for s in sessions:
            try:
                day = datetime.fromisoformat(s["started_at"]).strftime("%A")
                days[day] = days.get(day, 0) + 1
            except (ValueError, KeyError):
                pass

        return {
            "total_sessions": len(sessions),
            "total_minutes": total_minutes,
            "total_hours": round(total_minutes / 60, 1),
            "sessions_per_day": days,
            "average_per_session": round(total_minutes / max(len(sessions), 1), 1),
        }

    def _get_sessions_since(self, since: datetime) -> list[dict]:
        """Get all sessions that started after a given time."""
        sessions = []
        try:
            if _HISTORY_FILE.exists():
                with open(_HISTORY_FILE) as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            session = json.loads(line)
                            started = datetime.fromisoformat(
                                session.get("started_at", "")
                            )
                            if started >= since:
                                sessions.append(session)
                        except (json.JSONDecodeError, ValueError):
                            continue

            # Include current session if it started today
            if self._current:
                try:
                    started = datetime.fromisoformat(self._current["started_at"])
                    if started >= since:
                        sessions.append(dict(self._current))
                except (ValueError, KeyError):
                    pass

        except OSError as exc:
            logger.debug(f"Could not read session history: {exc}")

        return sessions

    def _count_sessions_today(self) -> int:
        """Count how many sessions have occurred today."""
        return len(self.get_sessions_today())

    def clear_history(self, days: int = 30):
        """Clear session history older than the given number of days."""
        try:
            if not _HISTORY_FILE.exists():
                return

            cutoff = datetime.now(timezone.utc) - timedelta(days=days)
            keep = []
            with open(_HISTORY_FILE) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        session = json.loads(line)
                        started = datetime.fromisoformat(
                            session.get("started_at", "")
                        )
                        if started >= cutoff:
                            keep.append(line)
                    except (json.JSONDecodeError, ValueError):
                        continue

            with open(_HISTORY_FILE, "w") as f:
                for line in keep:
                    f.write(line + "\n")

            logger.info(f"Cleared session history older than {days} days")
        except OSError as exc:
            logger.warning(f"Could not clear history: {exc}")
