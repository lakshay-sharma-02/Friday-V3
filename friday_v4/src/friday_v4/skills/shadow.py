"""ShadowExecutor — run skills in shadow mode, never execute (Wave 10 §3.4).

The wave-10 safety rule: *"new skills run in shadow mode and record what
they *would* do; promotion requires N successful shadow matches +
operator approval."*

``ShadowExecutor`` watches the operator's real activity (recent audited
actions) against a skill's step sequence. When the skill's first step
matches, it records a **shadow match** (bumping the registry's
``shadow_matches`` counter and confidence) and reports what it *would*
do — but it never touches the world. Nothing executes in shadow mode:
no shell, no git, no file writes.

Usage::

    shadow = ShadowExecutor(conn)
    matches = shadow.sweep()      # check all shadow skills once
    result = shadow.check(skill)  # one skill vs recent activity
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from .. import db
from .registry import Skill, SkillRegistry

logger = logging.getLogger("friday_v4.skills.shadow")


def _step_matches(step: dict, action: dict) -> bool:
    """Does one skill step match one audited action?

    Wave 14 generalization: a step scoped to a repo (``step['repo']``,
    the ``cwd`` basename captured when the demonstration ran) only
    matches activity in that repo — the same command elsewhere is a
    different context. Steps without a repo match by type/command only
    (back-compat with wave-10 skills).
    """
    want_type = (step.get("action_type") or "").strip()
    got_type = (action.get("action_type") or "").strip()
    if not want_type or want_type != got_type:
        return False
    want_cmd = (step.get("command") or "").strip()
    got_cmd = (action.get("command") or "").strip()
    if want_cmd and not (want_cmd in got_cmd or got_cmd in want_cmd):
        return False
    want_repo = (step.get("repo") or "").strip()
    if want_repo:
        got_cwd = (action.get("cwd") or "").strip()
        got_repo = Path(got_cwd).name if got_cwd else ""
        if want_repo not in got_cwd:
            return False
    return True


class ShadowExecutor:
    """Observes real activity against skills without executing anything."""

    def __init__(self, conn, registry: Optional[SkillRegistry] = None) -> None:
        self._conn = conn
        self._registry = registry or SkillRegistry(conn)

    # ------------------------------------------------------------------
    # Single check (pure read + registry match record)
    # ------------------------------------------------------------------

    def check(self, skill: Skill,
              recent: Optional[list[dict]] = None) -> Optional[dict]:
        """One skill vs the operator's most recent actions.

        Args:
            skill: the skill to observe.
            recent: injected recent actions (hermetic tests). Defaults to
                the real audit trail (newest first).

        Returns a match report when the skill's first step matches, else
        ``None``. A match records a shadow match in the registry (this is
        the ONLY side effect — the skill is never executed).
        """
        if skill.is_promoted or not skill.steps:
            return None
        actions = recent if recent is not None else \
            (db.recent_actions(self._conn, limit=10) or [])
        if not actions:
            return None
        first = skill.steps[0]
        latest = actions[0] if actions else {}
        if not _step_matches(first, latest):
            return None
        self._registry.record_shadow_match(skill.id)
        would_do = [
            {
                "action_type": (s.get("action_type") or "").strip(),
                "command": (s.get("command") or "").strip(),
            }
            for s in skill.steps[1:]
        ]
        return {
            "skill_id": skill.id,
            "skill_name": skill.name,
            "state": skill.verification_state,
            "matched_action": (latest.get("action_type") or "").strip(),
            "would_do": would_do,
            "note": "shadow mode — nothing executed",
        }

    # ------------------------------------------------------------------
    # Sweep (all non-promoted skills, one pass)
    # ------------------------------------------------------------------

    def sweep(self, limit: int = 50) -> list[dict]:
        """Check every shadow/verified skill against recent activity.

        Returns the list of match reports. Never executes anything —
        the only side effects are shadow-match counters in the registry.

        Wave 14 close-out: the wave-10 lifecycle (shadow → verified once
        the match threshold is reached) was never actually wired — the
        sweep recorded matches but nothing transitioned the state, so a
        skill could never become promotable through real use. After the
        match pass, every shadow skill at the threshold is verified
        (``SkillRegistry.verify`` still guards the threshold + state).
        Promotion (verified → promoted) remains operator-approval only.
        """
        reports: list[dict] = []
        for skill in self._registry.list(limit=limit):
            if skill.verification_state in ("shadow", "verified"):
                report = self.check(skill)
                if report:
                    reports.append(report)
        # Lifecycle transition: shadow → verified at the match threshold.
        # ``verify()`` is a no-op unless the skill is shadow AND has
        # enough matches, so calling it on every shadow skill is safe.
        for skill in self._registry.list(limit=limit):
            if skill.verification_state == "shadow":
                self._registry.verify(skill.id)
        return reports


__all__ = ["ShadowExecutor"]
