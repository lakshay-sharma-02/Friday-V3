"""Skill registry — typed access to the V4 skills table (Wave 10 §3.4).

``SkillRegistry`` is the typed, guarded layer over ``db.py``'s ``skills``
table: create/list/verify/promote/demote plus failure + shadow-match
tracking. It enforces the shadow-first lifecycle:

    shadow → verified (N shadow matches) → promoted (operator approval)
    any    → demoted (failure threshold hit, never silently kept)

Rules (wave-10 doc §3.4):
- A skill starts in ``shadow`` with confidence 0.
- Shadow matches (the ShadowExecutor observing the steps match the
  operator's real workflow) bump ``shadow_matches``.
- ``verify`` promotes shadow → verified once matches reach a threshold.
- ``promote`` (verified → promoted) is the *operator approval* step —
  the CLI asks before calling it; a skill is never auto-promoted.
- ``record_failure`` increments ``failure_count``; hitting the threshold
  demotes the skill (it is not silently kept).

Usage::

    reg = SkillRegistry(conn)
    sid = reg.create("run-tests-after-edit", steps=[...])
    reg.record_shadow_match(sid)
    reg.verify(sid)        # shadow → verified (needs enough matches)
    reg.promote(sid)       # verified → promoted (operator approval)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Optional

from .. import db

logger = logging.getLogger("friday_v6.skills.registry")

#: Verification states (must match the schema comment on skills.verification_state).
STATE_SHADOW = "shadow"
STATE_VERIFIED = "verified"
STATE_PROMOTED = "promoted"
STATE_DEMOTED = "demoted"
_VALID_STATES = (STATE_SHADOW, STATE_VERIFIED, STATE_PROMOTED, STATE_DEMOTED)

#: Shadow matches required before a skill may be verified (promotion gate 1).
DEFAULT_VERIFY_MATCHES = 3

#: Failures that demote a skill (promotion gate 2).
DEFAULT_DEMOTE_FAILURES = 3

#: Confidence granted by a shadow match (toward 1.0).
MATCH_CONFIDENCE_STEP = 0.05


@dataclass
class Skill:
    """A learned workflow: parameterized steps + verification state."""

    id: str
    name: str
    steps: list
    confidence: float = 0.0
    verification_state: str = STATE_SHADOW
    failure_count: int = 0
    shadow_matches: int = 0
    version: int = 1
    last_verified: str = ""
    created_at: str = ""
    updated_at: str = ""

    @property
    def is_promoted(self) -> bool:
        return self.verification_state == STATE_PROMOTED

    @property
    def is_shadow(self) -> bool:
        return self.verification_state == STATE_SHADOW

    @classmethod
    def from_row(cls, row: dict) -> "Skill":
        steps = row.get("steps") or "[]"
        try:
            steps = json.loads(steps) if isinstance(steps, str) else steps
        except (TypeError, ValueError):
            steps = []
        return cls(
            id=row.get("id", ""),
            name=row.get("name", ""),
            steps=steps,
            confidence=row.get("confidence", 0.0),
            verification_state=row.get("verification_state", STATE_SHADOW),
            failure_count=row.get("failure_count", 0),
            shadow_matches=row.get("shadow_matches", 0),
            version=row.get("version", 1),
            last_verified=row.get("last_verified", ""),
            created_at=row.get("created_at", ""),
            updated_at=row.get("updated_at", ""),
        )


class SkillRegistry:
    """Typed, guarded access to the ``skills`` table."""

    def __init__(self, conn, verify_matches: int = DEFAULT_VERIFY_MATCHES,
                 demote_failures: int = DEFAULT_DEMOTE_FAILURES) -> None:
        self._conn = conn
        self._verify_matches = verify_matches
        self._demote_failures = demote_failures

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def create(self, name: str, steps: Optional[list] = None,
               confidence: float = 0.0,
               verification_state: str = STATE_SHADOW) -> Optional[str]:
        """Register a new skill (starts in shadow); returns its id."""
        if verification_state not in _VALID_STATES:
            verification_state = STATE_SHADOW
        return db.create_skill(self._conn, name, steps=steps,
                               confidence=confidence,
                               verification_state=verification_state)

    def record_shadow_match(self, skill_id: str) -> bool:
        """A shadow executor saw the skill's steps match real workflow."""
        if not db.record_skill_shadow_match(self._conn, skill_id):
            return False
        skill = self.get_by_id(skill_id)
        if skill:
            new_conf = min(1.0, skill.confidence + MATCH_CONFIDENCE_STEP)
            db.update_skill(self._conn, skill_id, confidence=new_conf)
        return True

    def verify(self, skill_id: str) -> bool:
        """shadow → verified once shadow matches reach the threshold.

        Returns False when the skill doesn't exist, isn't in shadow, or
        hasn't accumulated enough matches yet (never auto-verifies).
        """
        skill = self.get_by_id(skill_id)
        if not skill or not skill.is_shadow:
            return False
        if skill.shadow_matches < self._verify_matches:
            return False
        return bool(db.update_skill(
            self._conn, skill_id, verification_state=STATE_VERIFIED,
            last_verified=db.now_iso()))

    def promote(self, skill_id: str) -> bool:
        """verified → promoted. THE operator-approval step."""
        skill = self.get_by_id(skill_id)
        if not skill or skill.verification_state != STATE_VERIFIED:
            return False
        return bool(db.update_skill(
            self._conn, skill_id, verification_state=STATE_PROMOTED,
            last_verified=db.now_iso()))

    def record_failure(self, skill_id: str) -> bool:
        """A skill run failed — bump the counter, demote at threshold."""
        skill = self.get_by_id(skill_id)
        if not skill:
            return False
        failures = skill.failure_count + 1
        state = skill.verification_state
        if failures >= self._demote_failures and state != STATE_DEMOTED:
            state = STATE_DEMOTED
        return bool(db.update_skill(
            self._conn, skill_id, failure_count=failures,
            verification_state=state))

    def demote(self, skill_id: str) -> bool:
        """Manually demote a skill (stop dispatching it)."""
        return bool(db.update_skill(self._conn, skill_id,
                                    verification_state=STATE_DEMOTED))

    def bump_version(self, skill_id: str) -> bool:
        """Increment the skill's version (a revised step sequence)."""
        skill = self.get_by_id(skill_id)
        if not skill:
            return False
        return bool(db.update_skill(self._conn, skill_id,
                                    version=skill.version + 1))

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get(self, name: str) -> Optional[Skill]:
        row = db.get_skill(self._conn, name)
        return Skill.from_row(row) if row else None

    def get_by_id(self, skill_id: str) -> Optional[Skill]:
        try:
            cur = self._conn.execute(
                "SELECT * FROM skills WHERE id = ?", (skill_id,))
            row = cur.fetchone()
            return Skill.from_row(dict(row)) if row is not None else None
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug(f"skill get_by_id failed: {exc}")
            return None

    def list(self, verification_state: Optional[str] = None,
             limit: int = 100) -> list[Skill]:
        rows = db.list_skills(self._conn,
                              verification_state=verification_state,
                              limit=limit) or []
        return [Skill.from_row(r) for r in rows]

    def count(self) -> int:
        return len(self.list(limit=100000))


__all__ = ["DEFAULT_DEMOTE_FAILURES", "DEFAULT_VERIFY_MATCHES",
           "MATCH_CONFIDENCE_STEP", "Skill", "SkillRegistry",
           "STATE_DEMOTED", "STATE_PROMOTED", "STATE_SHADOW",
           "STATE_VERIFIED"]
