"""Session working memory — ephemeral "what am I doing right now" (Wave 10).

V4-native port of V3's ``WorkingMemory``: short-lived context about the
current task, pending decisions, and recent daemon activity. Entries carry
a TTL (default 1 hour), a priority, a category, and a source; expired
entries are pruned on every access; over ``MAX_ENTRIES`` the lowest-
priority entries are evicted.

Usage::

    wm = WorkingMemory(conn)
    wm.set("current_task", "Refactoring auth module", priority=3)
    ctx = wm.current_context()
    # → "Current working context:\n- current task: ... (high)"
    wm.clear_expired()
"""

from __future__ import annotations

from typing import Optional

from friday_v6 import db

#: Maximum live working-memory entries before priority-based eviction.
MAX_ENTRIES = 50

#: Default TTL for working-memory entries (1 hour).
DEFAULT_TTL_SECONDS = 3600

_PRIORITY_LABELS = {
    0: "low", 1: "normal", 2: "medium", 3: "high", 4: "critical",
    5: "blocking",
}


def _priority_label(priority: int) -> str:
    return _PRIORITY_LABELS.get(priority, f"priority={priority}")


class WorkingMemory:
    """Ephemeral session context with TTL + priority eviction.

    All reads prune expired entries first; all writes evict the lowest-
    priority entries when the table exceeds ``MAX_ENTRIES``. Pure
    ``db.py`` helpers underneath — guarded, hermetic-testable.
    """

    def __init__(self, conn) -> None:
        self._conn = conn

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def set(self, context_key: str, value: str, category: str = "working",
            source: str = "system", priority: int = 0,
            ttl_seconds: int = DEFAULT_TTL_SECONDS,
            now: Optional[str] = None) -> Optional[str]:
        """Set/upsert a working-memory entry; returns its id."""
        self.clear_expired(now=now)
        wid = db.set_working_context(
            self._conn, context_key, value, category=category, source=source,
            priority=priority, ttl_seconds=ttl_seconds, now=now)
        self._evict_if_needed()
        return wid

    def clear(self, context_key: str) -> bool:
        """Delete one working-memory entry."""
        return db.delete_working_context(self._conn, context_key)

    def clear_expired(self, now: Optional[str] = None) -> int:
        """Delete expired entries; returns count removed."""
        return db.clear_expired_working(self._conn, now=now)

    def clear_all(self) -> int:
        """Delete ALL working-memory entries; returns count removed."""
        return db.clear_working(self._conn)

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get(self, context_key: str,
            now: Optional[str] = None) -> Optional[dict]:
        """Fetch one entry (or None if absent/expired).

        ``now`` may be injected for deterministic tests (defaults to the
        real clock when pruning expired entries).
        """
        self.clear_expired(now=now)
        return db.get_working_context(self._conn, context_key)

    def by_category(self, category: str, limit: int = 10) -> list[dict]:
        """Entries in a category, highest-priority first."""
        self.clear_expired()
        return [r for r in db.list_working_contexts(self._conn, limit=100000)
                if r["category"] == category][:limit]

    def all(self, limit: int = 100) -> list[dict]:
        """All live entries, highest-priority first."""
        self.clear_expired()
        return db.list_working_contexts(self._conn, limit=limit)

    def count(self) -> int:
        return db.count_working(self._conn)

    def current_context(self, limit: int = 10,
                        min_priority: int = 0) -> str:
        """Natural-language block of what Friday is doing right now.

        Empty string when nothing is live. Used by the briefing/status
        surfaces — ``"Current working context:\\n- ..."``
        """
        rows = self.all(limit=100000)
        rows = [r for r in rows if r["priority"] >= min_priority][:limit]
        if not rows:
            return ""
        lines = ["Current working context:"]
        for r in rows:
            label = _priority_label(r["priority"])
            src = f" ({r['source']})" if r["source"] else ""
            lines.append(f"- {r['context_key']}: {r['value']} ({label}){src}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Eviction
    # ------------------------------------------------------------------

    def _evict_if_needed(self) -> int:
        """Evict lowest-priority entries beyond ``MAX_ENTRIES``."""
        return db.evict_working_contexts(self._conn, MAX_ENTRIES)
