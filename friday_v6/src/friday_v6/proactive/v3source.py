"""V3DataSource — read-only bridge from V4 proactive/intelligence to V3's DB.

The docs promise the anticipation engine queries V3's observations,
action log, and daemon status. V4 is self-contained by default; this
module adds that V3 signal *additively* — never replacing local data,
never requiring V3, and never mutating V3's DB.

Design:
- Lazy import of ``friday.db`` / ``friday.ambient`` (V3 may be absent).
- Every query is wrapped; any failure degrades to empty/False.
- Connections are opened per-call and closed in a finally (V3's sqlite
  DB is owned by V3's daemon — we only peek).

Usage:
    src = V3DataSource()
    if src.is_available():
        digest = src.workspace_digest()
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("friday_v6.proactive.v3source")

_DB_PATH = Path.home() / ".friday" / "friday.db"
_STATUS_PATH = Path.home() / ".friday" / "daemon.status"

#: Tables we read from V3's DB. Queries use these names directly and
#: tolerate missing columns defensively (V3 migrations evolve).
_OBSERVATIONS = "observations"
_ACTIONS = "actions"
_AMBIENT = "ambient_feed"


class V3DataSource:
    """Read-only access to V3's observation/action/ambient data.

    Available when ``~/.friday/friday.db`` exists AND opens cleanly.
    Every method is safe to call when unavailable (returns empty values).
    """

    def __init__(self, db_path: Optional[Path] = None,
                 status_path: Optional[Path] = None):
        self._db_path = Path(db_path) if db_path else _DB_PATH
        self._status_path = Path(status_path) if status_path else _STATUS_PATH
        self._cached_available: Optional[bool] = None

    # ── Availability ───────────────────────────────────────────────

    def is_available(self) -> bool:
        """True when the V3 DB opens cleanly (probing the tables we read).

        Reads V3's SQLite directly (no ``friday`` import) so the bridge
        works even when V3's package isn't installed in this venv — the
        schema of the 3 tables we read is stable.
        """
        if self._cached_available is not None:
            return self._cached_available
        ok = False
        conn = self._open()
        if conn is not None:
            try:
                cur = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name IN (?, ?, ?)",
                    (_OBSERVATIONS, _ACTIONS, _AMBIENT))
                tables = {r[0] for r in cur.fetchall()}
                ok = {_OBSERVATIONS, _ACTIONS, _AMBIENT} <= tables
            except Exception as exc:
                logger.debug(f"V3 DB probe failed: {exc}")
            finally:
                conn.close()
        self._cached_available = ok
        return ok

    def _open(self) -> Optional[sqlite3.Connection]:
        """Open the V3 DB read-only, or None when unavailable."""
        if not self._db_path.exists():
            return None
        try:
            return sqlite3.connect(
                f"file:{self._db_path}?mode=ro",
                uri=True, timeout=3.0)
        except (sqlite3.Error, OSError) as exc:
            logger.debug(f"V3 DB open failed: {exc}")
            return None

    # ── Queries ────────────────────────────────────────────────────

    def recent_observations(self, hours: float = 24.0,
                            limit: int = 100) -> list[dict]:
        """Most recent observations within ``hours`` (newest first)."""
        conn = self._open()
        if conn is None:
            return []
        try:
            cur = conn.execute(
                f"SELECT source, subject, aspect, value, observed_at "
                f"FROM {_OBSERVATIONS} "
                f"WHERE observed_at > datetime('now', ?) "
                f"ORDER BY observed_at DESC LIMIT ?",
                (f"-{int(hours)} hours", limit))
            return [dict(zip(
                ("source", "subject", "aspect", "value", "observed_at"), row))
                for row in cur.fetchall()]
        except Exception as exc:
            logger.debug(f"Observations query failed: {exc}")
            return []
        finally:
            conn.close()

    def recent_actions(self, hours: float = 24.0,
                       limit: int = 100) -> list[dict]:
        """Most recent action-log entries within ``hours`` (newest first)."""
        conn = self._open()
        if conn is None:
            return []
        try:
            cur = conn.execute(
                f"SELECT action_type, target, project, observed_at "
                f"FROM {_ACTIONS} "
                f"WHERE observed_at > datetime('now', ?) "
                f"ORDER BY observed_at DESC LIMIT ?",
                (f"-{int(hours)} hours", limit))
            return [dict(zip(
                ("action_type", "target", "project", "observed_at"), row))
                for row in cur.fetchall()]
        except Exception as exc:
            logger.debug(f"Actions query failed: {exc}")
            return []
        finally:
            conn.close()

    def recent_ambient_events(self, hours: float = 24.0,
                              limit: int = 50) -> list[dict]:
        """Recent ambient-feed events within ``hours`` (newest first)."""
        conn = self._open()
        if conn is None:
            return []
        try:
            cur = conn.execute(
                f"SELECT id, event_type, title, priority, timestamp "
                f"FROM {_AMBIENT} "
                f"WHERE timestamp > datetime('now', ?) "
                f"ORDER BY id DESC LIMIT ?",
                (f"-{int(hours)} hours", limit))
            return [dict(zip(
                ("id", "event_type", "title", "priority", "timestamp"), row))
                for row in cur.fetchall()]
        except Exception as exc:
            logger.debug(f"Ambient query failed: {exc}")
            return []
        finally:
            conn.close()

    def observation_counts(self, hours: float = 24.0) -> dict:
        """Counts of observations/actions within ``hours`` by source."""
        conn = self._open()
        if conn is None:
            return {}
        out: dict[str, Any] = {}
        try:
            for table, col, label in (
                (_OBSERVATIONS, "source", "by_source"),
                (_ACTIONS, "action_type", "by_type"),
            ):
                cur = conn.execute(
                    f"SELECT {col}, COUNT(*) FROM {table} "
                    f"WHERE observed_at > datetime('now', ?) "
                    f"GROUP BY {col} ORDER BY COUNT(*) DESC",
                    (f"-{int(hours)} hours",))
                out[label] = dict(cur.fetchall())
        except Exception as exc:
            logger.debug(f"Observation counts failed: {exc}")
        finally:
            conn.close()
        return out

    def daemon_state(self) -> dict:
        """V3 daemon status from ``~/.friday/daemon.status`` JSON."""
        try:
            if self._status_path.exists():
                return json.loads(self._status_path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            logger.debug(f"Daemon status read failed: {exc}")
        return {}

    # ── Digest ─────────────────────────────────────────────────────

    def workspace_digest(self, hours: float = 24.0) -> str:
        """A short natural-language summary of V3's view of the workspace.

        Used by the anticipation engine to enrich briefings when V3 data
        is present. Returns '' when unavailable or nothing notable.
        """
        if not self.is_available():
            return ""
        obs = self.recent_observations(hours, limit=5)
        counts = self.observation_counts(hours)
        events = self.recent_ambient_events(hours, limit=5)
        daemon = self.daemon_state()

        parts: list[str] = []
        by_source = counts.get("by_source", {})
        total_obs = sum(by_source.values())
        if total_obs:
            top = ", ".join(f"{k}: {v}" for k, v in
                            list(by_source.items())[:3])
            parts.append(f"{total_obs} observations in the last {int(hours)}h"
                         f" ({top})")
        if obs:
            subjects = sorted({o["subject"] for o in obs if o["subject"]})
            if subjects:
                parts.append("recent subjects: " + ", ".join(subjects[:5]))
        if events:
            high = [e for e in events if int(e.get("priority") or 0) >= 2]
            if high:
                titles = "; ".join(e["title"][:60] for e in high[:3])
                parts.append(f"high-priority feed: {titles}")
        state = daemon.get("state")
        if state:
            parts.append(f"V3 daemon {state}")
        return ". ".join(parts) + "." if parts else ""
