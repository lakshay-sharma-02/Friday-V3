"""Day narrative / timeline (Wave 11 §3.3).

``day_narrative(conn)`` walks the audit log + ambient events into a
timeline — the day's story in real order. Deterministic: same state,
same narrative. Never invents.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from .. import db

logger = logging.getLogger("friday_v4.briefing.narrative")


@dataclass
class Narrative:
    """The day's story, from real state."""

    date: str = ""
    entries: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"date": self.date, "entries": self.entries}


def day_narrative(conn, date: str = "") -> Narrative:
    """Build the day's timeline — audit actions + ambient events."""
    from .. import db as _db
    entries: list[str] = []

    actions = db.recent_actions(conn, limit=30) or []
    for a in actions:
        when = (a.get("created_at") or "")[:16]
        status = a.get("status", "")
        mark = "✓" if status == "succeeded" else ("✗" if status == "failed"
                                                  else "·")
        what = a.get("command") or a.get("goal") or a.get("action_type") or "?"
        entries.append(f"[{when}] {mark} {what}")

    events = db.recent_ambient_events(conn, limit=15) or []
    for e in events:
        when = (e.get("created_at") or "")[:16]
        prio = "!" if e.get("priority", 0) >= 2 else ("•" if e.get("priority", 0) == 1 else "·")
        entries.append(f"[{when}] {prio} {e.get('topic')}: {e.get('payload', '')[:80]}")

    return Narrative(date=date or _db.now_iso()[:10], entries=entries)


__all__ = ["Narrative", "day_narrative"]
