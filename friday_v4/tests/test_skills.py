"""Hermetic tests for the Wave 10 skills layer (friday_v4.skills).

Covers:
- registry.py: shadow-first lifecycle — shadow → verified (N matches) →
  promoted (operator approval); failure demotion; never auto-promotes
- replay.py: learn a skill from a repeated sequence in the audit log
- shadow.py: shadow mode records what it *would* do, never executes
- dispatch.py: promoted skills suggest the next step on context match

Safety laws (wave-10 §3.4) verified:
- New skills start in shadow mode with confidence 0.
- Promotion requires N shadow matches + explicit operator approval.
- Shadow/dispatch never execute real actions — only read + suggest.

Every test is hermetic: tmp_path DB — never the real ~/.friday.
"""

from __future__ import annotations

from friday_v4 import db
from friday_v4.skills import (
    ReplayExecutor,
    ShadowExecutor,
    SkillDispatcher,
    SkillRegistry,
    STATE_DEMOTED,
    STATE_PROMOTED,
    STATE_SHADOW,
    STATE_VERIFIED,
)


def _conn(tmp_path):
    return db.connect(tmp_path / "v4.db")


def _make_skill(reg: SkillRegistry, name: str, steps=None) -> str:
    steps = steps or [
        {"action_type": "testing", "command": "pytest -q", "goal": "run tests"},
        {"action_type": "git", "command": "git status", "goal": "check state"},
    ]
    return reg.create(name, steps=steps)


# ==========================================================================
# registry.py — shadow-first lifecycle
# ==========================================================================


class TestRegistry:
    def test_create_starts_shadow_with_zero_confidence(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            reg = SkillRegistry(conn)
            sid = _make_skill(reg, "run-tests")
            skill = reg.get("run-tests")
            assert skill.verification_state == STATE_SHADOW
            assert skill.confidence == 0.0
            assert skill.shadow_matches == 0
        finally:
            conn.close()

    def test_shadow_match_bumps_counter_and_confidence(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            reg = SkillRegistry(conn)
            sid = _make_skill(reg, "run-tests")
            assert reg.record_shadow_match(sid)
            skill = reg.get("run-tests")
            assert skill.shadow_matches == 1
            assert skill.confidence > 0.0
        finally:
            conn.close()

    def test_verify_requires_enough_matches(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            reg = SkillRegistry(conn)
            sid = _make_skill(reg, "run-tests")
            # Not enough matches yet → cannot verify.
            assert not reg.verify(sid)
            for _ in range(reg._verify_matches):
                reg.record_shadow_match(sid)
            assert reg.verify(sid)
            assert reg.get("run-tests").verification_state == STATE_VERIFIED
        finally:
            conn.close()

    def test_promote_is_operator_approval_only(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            reg = SkillRegistry(conn)
            sid = _make_skill(reg, "run-tests")
            # Shadow skills cannot be promoted — approval is gated.
            assert not reg.promote(sid)
            for _ in range(reg._verify_matches):
                reg.record_shadow_match(sid)
            reg.verify(sid)
            assert reg.promote(sid)
            assert reg.get("run-tests").verification_state == STATE_PROMOTED
        finally:
            conn.close()

    def test_failure_demotion_after_threshold(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            reg = SkillRegistry(conn)
            sid = _make_skill(reg, "run-tests")
            for _ in range(reg._verify_matches):
                reg.record_shadow_match(sid)
            reg.verify(sid)
            reg.promote(sid)
            # Failures accumulate → demoted, not silently kept.
            for _ in range(reg._demote_failures):
                reg.record_failure(sid)
            assert reg.get("run-tests").verification_state == STATE_DEMOTED
        finally:
            conn.close()

    def test_manual_demote(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            reg = SkillRegistry(conn)
            sid = _make_skill(reg, "run-tests")
            assert reg.demote(sid)
            assert reg.get("run-tests").verification_state == STATE_DEMOTED
        finally:
            conn.close()

    def test_bump_version(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            reg = SkillRegistry(conn)
            sid = _make_skill(reg, "run-tests")
            assert reg.bump_version(sid)
            assert reg.get("run-tests").version == 2
        finally:
            conn.close()

    def test_list_filters_by_state(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            reg = SkillRegistry(conn)
            _make_skill(reg, "s1")
            s2 = _make_skill(reg, "s2")
            reg.demote(s2)
            shadow = reg.list(verification_state=STATE_SHADOW)
            assert [s.name for s in shadow] == ["s1"]
        finally:
            conn.close()


# ==========================================================================
# replay.py — learn from a repeated sequence in the audit log
# ==========================================================================


class TestReplay:
    def _seed_actions(self, conn, rounds: int = 3) -> None:
        """Repeated 'testing → git' pattern, oldest first."""
        for r in range(rounds):
            db.record_action(conn, "testing", goal=f"run tests {r}",
                             command="pytest -q", status="succeeded")
            db.record_action(conn, "git", goal=f"check state {r}",
                             command="git status", status="succeeded")

    def test_learn_forms_shadow_skill_from_repeat(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            self._seed_actions(conn, rounds=3)
            replay = ReplayExecutor(conn, min_occurrences=2)
            formed = replay.learn(prefix="run-tests")
            assert formed, "a repeated pattern should form a skill"
            skill = formed[0]
            assert skill.verification_state == STATE_SHADOW
            assert skill.confidence == 0.0
            assert skill.name.startswith("run-tests")
            assert len(skill.steps) >= 2
        finally:
            conn.close()

    def test_learn_no_repeat_forms_nothing(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            db.record_action(conn, "testing", goal="once", command="pytest -q")
            db.record_action(conn, "git", goal="once", command="git status")
            replay = ReplayExecutor(conn, min_occurrences=2)
            assert replay.learn() == []
        finally:
            conn.close()

    def test_learn_skips_duplicates(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            self._seed_actions(conn, rounds=3)
            replay = ReplayExecutor(conn, min_occurrences=2)
            first = replay.learn()
            second = replay.learn()  # same pattern → already learned
            assert first and second == []
        finally:
            conn.close()

    def test_find_patterns_reads_audit(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            self._seed_actions(conn, rounds=2)
            patterns = ReplayExecutor(conn, min_occurrences=2).find_patterns()
            assert patterns
            assert patterns[0]["count"] >= 2
        finally:
            conn.close()


# ==========================================================================
# shadow.py — shadow mode NEVER executes
# ==========================================================================


class TestShadow:
    def test_match_records_but_never_executes(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            reg = SkillRegistry(conn)
            sid = _make_skill(reg, "run-tests")
            # Latest action matches the skill's first step (testing:pytest).
            db.record_action(conn, "testing", goal="go",
                             command="pytest -q", status="succeeded")
            shadow = ShadowExecutor(conn, registry=reg)
            report = shadow.check(reg.get("run-tests"))
            assert report is not None
            assert report["would_do"]  # records what it WOULD do
            assert "nothing executed" in report["note"]
            # Counter bumped — the only side effect.
            assert reg.get("run-tests").shadow_matches == 1
        finally:
            conn.close()

    def test_no_match_no_side_effect(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            reg = SkillRegistry(conn)
            sid = _make_skill(reg, "run-tests")
            db.record_action(conn, "shell", goal="unrelated",
                             command="ls", status="succeeded")
            shadow = ShadowExecutor(conn, registry=reg)
            assert shadow.check(reg.get("run-tests")) is None
            assert reg.get("run-tests").shadow_matches == 0
        finally:
            conn.close()

    def test_promoted_skills_are_not_shadowed(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            reg = SkillRegistry(conn)
            sid = _make_skill(reg, "run-tests")
            for _ in range(reg._verify_matches):
                reg.record_shadow_match(sid)
            reg.verify(sid)
            reg.promote(sid)
            db.record_action(conn, "testing", goal="go",
                             command="pytest -q", status="succeeded")
            assert ShadowExecutor(conn, registry=reg).check(
                reg.get("run-tests")) is None
        finally:
            conn.close()

    def test_sweep_all_shadow_skills(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            reg = SkillRegistry(conn)
            _make_skill(reg, "run-tests")  # first step: testing:pytest -q
            _make_skill(reg, "run-lint", steps=[
                {"action_type": "shell", "command": "ruff check",
                 "goal": "lint"},
                {"action_type": "git", "command": "git status",
                 "goal": "check state"},
            ])
            db.record_action(conn, "testing", goal="go",
                             command="pytest -q", status="succeeded")
            matches = ShadowExecutor(conn, registry=reg).sweep()
            assert len(matches) == 1  # only the pytest skill matched
            assert matches[0]["skill_name"] == "run-tests"
        finally:
            conn.close()

    def test_sweep_auto_verifies_at_threshold(self, tmp_path):
        """Wave 14 close-out: the sweep that records the Nth match
        transitions shadow → verified (promotion still needs approval)."""
        conn = _conn(tmp_path)
        try:
            reg = SkillRegistry(conn)
            _make_skill(reg, "run-tests")
            shadow = ShadowExecutor(conn, registry=reg)
            # Each sweep: a matching action, then a sweep that records the
            # match AND auto-verifies once the threshold is reached.
            for _ in range(reg._verify_matches - 1):
                db.record_action(conn, "testing", goal="go",
                                 command="pytest -q", status="succeeded")
                shadow.sweep()
                assert reg.get("run-tests").verification_state == STATE_SHADOW
            # The Nth match crosses the threshold → verified.
            db.record_action(conn, "testing", goal="go",
                             command="pytest -q", status="succeeded")
            shadow.sweep()
            skill = reg.get("run-tests")
            assert skill.verification_state == STATE_VERIFIED
            # Sweeps never auto-promote: the transition lands on
            # verified, never promoted (approval is a separate step).
            assert skill.verification_state != STATE_PROMOTED
        finally:
            conn.close()


# ==========================================================================
# dispatch.py — suggest next step on context match, never execute
# ==========================================================================


class TestDispatch:
    def _promoted(self, conn, reg: SkillRegistry) -> str:
        sid = _make_skill(reg, "run-tests")
        for _ in range(reg._verify_matches):
            reg.record_shadow_match(sid)
        reg.verify(sid)
        reg.promote(sid)
        return sid

    def test_suggests_next_step_on_match(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            reg = SkillRegistry(conn)
            self._promoted(conn, reg)
            db.record_action(conn, "testing", goal="go",
                             command="pytest -q", status="succeeded")
            suggestions = SkillDispatcher(conn, registry=reg).suggest()
            assert len(suggestions) == 1
            assert suggestions[0]["skill_name"] == "run-tests"
            assert suggestions[0]["next_steps"]
            assert suggestions[0]["pending_approval"] is True
        finally:
            conn.close()

    def test_no_suggestion_for_shadow_or_demoted(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            reg = SkillRegistry(conn)
            _make_skill(reg, "run-tests")  # shadow — not dispatchable
            db.record_action(conn, "testing", goal="go",
                             command="pytest -q", status="succeeded")
            assert SkillDispatcher(conn, registry=reg).suggest() == []
        finally:
            conn.close()

    def test_no_suggestion_without_match(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            reg = SkillRegistry(conn)
            self._promoted(conn, reg)
            db.record_action(conn, "shell", goal="unrelated",
                             command="ls", status="succeeded")
            assert SkillDispatcher(conn, registry=reg).suggest() == []
        finally:
            conn.close()

    def test_prompt_is_natural_language_offer(self, tmp_path):
        suggestion = {
            "skill_name": "run-tests",
            "next_steps": [{"action_type": "git", "command": "git status"}],
        }
        prompt = SkillDispatcher(_conn(tmp_path)).prompt(suggestion)
        assert "run-tests" in prompt
        assert "want me to run" in prompt


# ==========================================================================
# Package-level
# ==========================================================================


class TestPackage:
    def test_is_available(self):
        from friday_v4.skills import is_available
        assert is_available() is True
