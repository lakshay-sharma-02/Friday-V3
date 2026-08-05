"""SkillDispatcher — auto-dispatch suggestions when context matches (Wave 10 §3.4).

Promoted skills (verified + operator-approved) may *suggest* their next
step when the operator's current activity matches the skill's trigger.
The dispatcher never executes anything itself: it returns a suggestion
payload the caller (voice/CLI/web) can surface for the operator to
approve — the confirm gate stays in the execution layer.

Usage::

    dispatch = SkillDispatcher(conn)
    suggestions = dispatch.suggest()   # → [{"next_steps": [...]}, ...]
"""

from __future__ import annotations

import logging
from typing import Optional

from .. import db
from .registry import SkillRegistry
from .shadow import _step_matches

logger = logging.getLogger("friday_v6.skills.dispatch")


class SkillDispatcher:
    """Suggests the next step of a promoted skill on context match."""

    def __init__(self, conn, registry: Optional[SkillRegistry] = None) -> None:
        self._conn = conn
        self._registry = registry or SkillRegistry(conn)

    def suggest(self, recent: Optional[list[dict]] = None,
                limit: int = 5) -> list[dict]:
        """Next-step suggestions from promoted skills on match.

        Args:
            recent: injected recent actions (hermetic tests). Defaults to
                the real audit trail (newest first).
            limit: max suggestions to return.

        Returns a list of suggestion dicts — these are *offers* for the
        operator to accept; nothing is ever executed here.
        """
        actions = recent if recent is not None else \
            (db.recent_actions(self._conn, limit=10) or [])
        if not actions:
            return []
        latest = actions[0] if actions else {}
        suggestions: list[dict] = []
        for skill in self._registry.list(limit=100):
            if skill.verification_state != "promoted":
                continue
            if not skill.steps or not _step_matches(skill.steps[0], latest):
                continue
            next_steps = skill.steps[1:]
            suggestions.append({
                "skill_id": skill.id,
                "skill_name": skill.name,
                "next_steps": [
                    {
                        "action_type": (s.get("action_type") or "").strip(),
                        "command": (s.get("command") or "").strip(),
                    }
                    for s in next_steps
                ],
                "confidence": round(skill.confidence, 3),
                "pending_approval": True,
            })
            if len(suggestions) >= limit:
                break
        return suggestions

    def prompt(self, suggestion: dict) -> str:
        """A natural-language offer for a suggestion (operator-facing)."""
        name = suggestion.get("skill_name") or "this workflow"
        nexts = suggestion.get("next_steps") or []
        if not nexts:
            return f"I noticed your pattern '{name}' — run it?"
        first = nexts[0]
        cmd = first.get("command") or first.get("action_type") or ""
        return (f"That matches your '{name}' skill — want me to run "
                f"\"{cmd}\" next?")


__all__ = ["SkillDispatcher"]
