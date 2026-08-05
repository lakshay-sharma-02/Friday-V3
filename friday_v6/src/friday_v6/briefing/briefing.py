"""Morning/evening briefings from real V4 state (Wave 11 §3.3).

``build_briefing(conn, kind)`` reads actual state — missions, recent
actions, security grade, memory facts, ambient events — and composes a
tone-adapted briefing. Never invents: a section with nothing is simply
omitted. Tone adapts to relationship depth (Wave 10) and time of day.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from .. import db

logger = logging.getLogger("friday_v6.briefing.briefing")


@dataclass
class Briefing:
    """One morning/evening briefing — real state, tone-adapted."""

    kind: str            # morning | evening
    text: str = ""
    sections: list[str] = field(default_factory=list)
    tone: str = "neutral"
    depth: float = 0.0

    def to_dict(self) -> dict:
        return {"kind": self.kind, "text": self.text,
                "sections": self.sections, "tone": self.tone,
                "depth": self.depth}


def _relationship_depth(conn) -> float:
    try:
        from ..relationship import RelationshipEngine
        engine = RelationshipEngine(conn)
        return engine.depth()
    except Exception:
        return 0.0


def _tone_for(conn, depth: float) -> str:
    """Briefing tone — Wave 17: an explicit tone-direction wins.

    The operator's "be more casual" (stored in the relationship layer)
    overrides the depth-derived briefing tone, so the morning brief
    matches how Friday talks everywhere else. Falls back to the
    depth-derived tone when no direction is stored.
    """
    try:
        from ..relationship import RelationshipEngine
        status = RelationshipEngine(conn).status()
        if status.get("tone"):
            return status["tone"]
    except Exception:
        pass
    if depth >= 0.3:
        return "warm"
    if depth >= 0.1:
        return "friendly"
    return "neutral"


def _mission_section(conn, kind: str) -> Optional[str]:
    missions = db.list_missions(conn, limit=10) or []
    active = [m for m in missions if m.get("status") not in ("done", "cancelled")]
    if not active:
        return None
    total = len(active)
    done_steps = sum(
        len([s for s in (db.list_mission_steps(conn, m.get("id", "")) or [])
             if s.get("status") == "completed"]) for m in active)
    heads = ", ".join(m.get("title", "?")[:40] for m in active[:3])
    suffix = f" {done_steps} step(s) done" if done_steps else ""
    return f"{total} mission(s) in flight — {heads}{suffix}"


def _actions_section(conn, kind: str, hours: int = 24) -> Optional[str]:
    recent = db.recent_actions(conn, limit=15) or []
    recent = [a for a in recent if _within(a, hours)]
    if not recent:
        return None
    ok = sum(1 for a in recent if a.get("status") == "succeeded")
    top = recent[:3]
    what = "; ".join(f"{(a.get('action_type') or '?')}: "
                     f"{(a.get('command') or a.get('goal') or '')[:40]}"
                     for a in top)
    return f"{len(recent)} action(s) in the last {hours}h, {ok} succeeded — {what}"


def _within(action: dict, hours: int) -> bool:
    when = action.get("created_at") or ""
    if not when:
        return False
    try:
        from datetime import datetime, timezone
        t = datetime.fromisoformat(when)
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        delta = (datetime.now(timezone.utc) - t).total_seconds()
        return delta <= hours * 3600
    except ValueError:
        return False


def _security_section(conn) -> Optional[str]:
    """Last scan state from the daemon's persisted status file."""
    try:
        import json
        from pathlib import Path
        state_file = Path.home() / ".friday" / "v4_security_last.json"
        if not state_file.exists():
            return None
        state = json.loads(state_file.read_text())
        report = state.get("report") or {}
        grade = report.get("grade") or "unknown"
        counts = report.get("counts_by_severity") or {}
        critical = int(counts.get("critical", 0) or 0)
        high = int(counts.get("high", 0) or 0)
        if grade in ("A", "B") and not critical:
            return f"security grade {grade} — clean"
        return f"security grade {grade} — {critical} critical, {high} high"
    except Exception:
        return None


def _memory_section(conn, kind: str) -> Optional[str]:
    memories = db.list_memories(conn, limit=5) or []
    if not memories:
        return None
    top = "; ".join(f"{m.get('key')}: {m.get('value')[:40]}" for m in memories[:3])
    return f"remembered: {top}"


def _ambient_section(conn, kind: str) -> Optional[str]:
    events = db.recent_ambient_events(conn, limit=5) or []
    if not events:
        return None
    top = "; ".join(f"{e.get('topic')}: {e.get('payload', '')[:40]}"
                    for e in events[:3])
    return f"events: {top}"


def build_briefing(conn, kind: str = "morning") -> Briefing:
    """Compose a tone-adapted briefing from real state — never raises."""
    depth = _relationship_depth(conn)
    tone = _tone_for(conn, depth)

    sections: list[str] = []
    for builder in (_mission_section, _actions_section, _security_section,
                    _memory_section, _ambient_section):
        try:
            s = builder(conn, kind)
        except Exception:
            s = None
        if s:
            sections.append(s)

    if not sections:
        text = ("Nothing to report yet — Friday is quiet. "
                "Ask me about your projects when you're ready.")
    else:
        opener = "Good morning." if kind == "morning" else "End of day."
        text = opener + " Here's what I know: " + " ".join(sections) + "."
        if tone in ("warm", "friendly", "casual"):
            text += " Let me know what you want to dig into."
        elif tone == "formal":
            text += " I'm ready when you are."

    return Briefing(kind=kind, text=text, sections=sections,
                    tone=tone, depth=depth)


__all__ = ["Briefing", "build_briefing"]
