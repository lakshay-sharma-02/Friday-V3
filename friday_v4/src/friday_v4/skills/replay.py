"""ReplayExecutor — learn a skill from a demonstrated sequence (Wave 10 §3.4).

The wave-10 spec's *"learn from a demonstrated sequence"*: Friday
watches the operator's real executed actions (the V4 ``actions`` audit
log) and, when the same ordered pattern appears repeatedly, forms a
:class:`~friday_v4.skills.registry.Skill` — parameterized steps with the
evidence trail that produced them.

Nothing executes here: replay only *reads* the audit log and *writes* a
new shadow skill. The skill is inert until the ShadowExecutor observes
matches and the operator approves promotion.

Usage::

    replay = ReplayExecutor(conn)
    formed = replay.learn()          # → [Skill] new shadow skills
    replay.learn(prefix="run-tests")  # name suggestions under a prefix
"""

from __future__ import annotations

import logging
from typing import Optional

from .. import db
from .registry import Skill, SkillRegistry

logger = logging.getLogger("friday_v4.skills.replay")

#: A step appears this many times before a sequence is worth a skill.
DEFAULT_MIN_OCCURRENCES = 2

#: Actions examined for patterns (bounded).
DEFAULT_LOOKBACK = 200

#: Non-executable action types never form skills (they add no workflow).
_SKIPPED_TYPES = {"desktop"}


def _action_signature(action: dict) -> str:
    """A stable signature for one audited action."""
    atype = (action.get("action_type") or "").strip()
    cmd = (action.get("command") or action.get("goal") or "").strip()
    return f"{atype}:{cmd[:80]}" if cmd else atype


def _name_from_sequence(seq: list[str], prefix: str = "") -> str:
    """A slug-like name from an ordered sequence of signatures."""
    base = "-".join(sig.split(":")[0] for sig in seq)
    base = base.replace(" ", "-").replace("_", "-")
    base = "-".join(p for p in base.split("-") if p)[:60]
    if prefix:
        return f"{prefix}-{base}" if base else prefix
    return base or "workflow"


class ReplayExecutor:
    """Finds repeated ordered action sequences and forms shadow skills."""

    def __init__(self, conn,
                 min_occurrences: int = DEFAULT_MIN_OCCURRENCES,
                 lookback: int = DEFAULT_LOOKBACK,
                 registry: Optional[SkillRegistry] = None) -> None:
        self._conn = conn
        self._min_occurrences = min_occurrences
        self._lookback = lookback
        self._registry = registry or SkillRegistry(conn)

    # ------------------------------------------------------------------
    # Pattern discovery (read-only over the audit log)
    # ------------------------------------------------------------------

    def _recent_sequences(self, limit: Optional[int] = None) -> list[dict]:
        """Recent actions as ordered signatures, oldest-first."""
        actions = db.recent_actions(self._conn, limit=limit or self._lookback) or []
        # recent_actions is newest-first; reverse to the actual order.
        actions.reverse()
        return [
            a for a in actions
            if (a.get("action_type") or "").strip() not in _SKIPPED_TYPES
        ]

    def find_patterns(self, length: int = 2) -> list[dict]:
        """Repeated ordered sequences of audited actions (pure read).

        Returns ``[{"sequence": [...], "count": n, "example": {...}}]``
        where each signature occurs at least ``min_occurrences`` times.
        """
        actions = self._recent_sequences()
        if len(actions) < length:
            return []
        sigs = [_action_signature(a) for a in actions]
        counts: dict[tuple[str, ...], int] = {}
        examples: dict[tuple[str, ...], dict] = {}
        for i in range(len(sigs) - length + 1):
            seq = tuple(sigs[i:i + length])
            if any(s.startswith("desktop:") for s in seq):
                continue
            counts[seq] = counts.get(seq, 0) + 1
            examples.setdefault(seq, actions[i])
        return [
            {
                "sequence": list(seq),
                "count": count,
                "example": examples[seq],
            }
            for seq, count in sorted(counts.items(),
                                     key=lambda kv: (-kv[1], kv[0]))
            if count >= self._min_occurrences
        ]

    # ------------------------------------------------------------------
    # Skill formation
    # ------------------------------------------------------------------

    def _steps_from_sequence(self, seq: list[str],
                             example: dict) -> list[dict]:
        """Steps with the evidence trail that produced the pattern."""
        atype = (example.get("action_type") or "").strip()
        steps = [{
            "action_type": atype,
            "command": (example.get("command") or "").strip(),
            "goal": (example.get("goal") or "").strip(),
        }]
        # Longer sequences re-derive each step from the audit trail.
        for sig in seq[1:]:
            parts = sig.split(":", 1)
            steps.append({
                "action_type": parts[0],
                "command": (parts[1] if len(parts) > 1 else "").strip(),
                "goal": "",
            })
        return steps

    def learn(self, prefix: str = "", limit: Optional[int] = None) -> list[Skill]:
        """Form new shadow skills from repeated audit patterns.

        Returns the newly created :class:`Skill` objects (empty when no
        pattern repeats enough). Each new skill starts in ``shadow`` with
        confidence 0 — inert until shadow-verified + operator-promoted.
        """
        formed: list[Skill] = []
        for pattern in self.find_patterns(length=2)[:20]:
            seq = pattern["sequence"]
            name = _name_from_sequence(seq, prefix=prefix)
            if self._registry.get(name) is not None:
                continue  # already learned
            steps = self._steps_from_sequence(seq, pattern["example"])
            sid = self._registry.create(name, steps=steps, confidence=0.0)
            if sid:
                skill = self._registry.get_by_id(sid)
                if skill:
                    formed.append(skill)
        return formed

    def learn_one(self, offer: dict) -> Optional[Skill]:
        """Form ONE shadow skill from a single noticer offer.

        The autonomy loop's self-learn path: the operator accepted an
        "I noticed you keep doing X" offer, so the pattern it carries is
        formed into a skill (with the real example's evidence trail —
        cwd, goal, commands — not a bare signature). Skips when the name
        is already registered (never duplicates). Returns the new shadow
        skill or None. Never raises.
        """
        try:
            seq = offer.get("sequence") or []
            if not seq:
                return None
            name = _name_from_sequence(seq)
            if self._registry.get(name) is not None:
                return None  # already learned
            steps = self._steps_from_sequence(seq, offer.get("example") or {})
            sid = self._registry.create(name, steps=steps, confidence=0.0)
            return self._registry.get_by_id(sid) if sid else None
        except Exception as exc:  # defensive — never crash
            logger.debug(f"replay learn_one failed: {exc}")
            return None


__all__ = ["DEFAULT_LOOKBACK", "DEFAULT_MIN_OCCURRENCES", "ReplayExecutor"]
