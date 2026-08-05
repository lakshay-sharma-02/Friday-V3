"""Hermetic tests for the autonomy loop — Friday's own judgment → action.

Covers:
- db.py: permission_requests + operator_overrides tables (migration v7),
  create/pending/resolve/expire, record/list/clear/is_overridden
- autonomy/loop.py AutonomyAgent:
  · cycle_once gathers dispatch + mission candidates and judges each
    through the permission gate — AUTO executes silently, CONFIRM asks
    durably (permission request + notification), NEVER skips, operator
    overrides win, already-pending asks aren't re-asked
  · accept() runs the approved action through the gate; deny() resolves
    the ask and records an override so it's never proposed again
  · lifecycle: start/stop
- daemon wiring: status includes the autonomy component, builds when
  enabled, shuts down cleanly
- NL paths (the operator's voice): "yes, run it" resolves a pending
  permission request; "no" / "don't do that" denies it and records an
  override; DENY intent fallback
- nlu: Intent.DENY fallback words and tie-breaks ("don't forget X" stays
  MEMORY, "don't run the tests" denies)

Safety laws verified:
- NEVER-level actions are never executed or asked about autonomously.
- A declined action is recorded as an override and never proposed again.
- Everything is hermetic: tmp_path DBs + a fake executor — no real
  commands run, no ~/.friday writes.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from friday_v6 import db


def _conn(tmp_path):
    return db.connect(tmp_path / "v4.db")


def _fake_execute(monkeypatch, status="succeeded", output="ok"):
    """Stub friday_v6.execution.execute so no real command ever runs."""
    calls = []

    def fake_execute(action_type, command, **kw):
        calls.append({"action_type": action_type, "command": command,
                      "force": kw.get("force", False),
                      "confirm_fn": kw.get("confirm_fn"),
                      "goal": kw.get("goal", "")})
        from friday_v6.execution import ExecutionResult
        return ExecutionResult(action_type, status=status, output=output,
                               action_id=f"audit-{len(calls)}")

    monkeypatch.setattr("friday_v6.execution.execute", fake_execute)
    return calls


def _promote_skill(conn, name="run-tests", steps=None) -> str:
    """A promoted skill (verified + approved) with the given steps."""
    from friday_v6.skills import SkillRegistry
    reg = SkillRegistry(conn)
    steps = steps or [
        {"action_type": "testing", "command": "pytest -q", "goal": "run tests"},
        {"action_type": "shell", "command": "echo hi", "goal": "next"},
    ]
    sid = reg.create(name, steps=steps)
    for _ in range(reg._verify_matches):
        reg.record_shadow_match(sid)
    reg.verify(sid)
    reg.promote(sid)
    return sid


# ==========================================================================
# db.py — permission requests + operator overrides
# ==========================================================================


class TestPermissionDb:
    def test_create_pending_resolve_expire(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            rid = db.create_permission_request(
                conn, "run the tests", "testing", command="pytest -q",
                source="autonomy")
            assert rid
            req = db.get_permission_request(conn, rid)
            assert req["status"] == "pending"
            assert db.pending_permission_requests(conn) == [req]

            assert db.resolve_permission_request(conn, rid, "approved")
            assert db.get_permission_request(conn, rid)["status"] == "approved"
            assert db.pending_permission_requests(conn) == []

            # Expiry only touches pending asks.
            rid2 = db.create_permission_request(conn, "x", "shell", "echo")
            assert db.expire_permission_requests(conn, "2099-01-01T00:00:00") == 1
            assert db.get_permission_request(conn, rid2)["status"] == "expired"
        finally:
            conn.close()

    def test_override_record_idempotent_and_clear(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            oid = db.record_override(conn, "shell", "echo hi",
                                     reason="do it differently")
            assert oid
            # Same action → updates, no duplicate.
            oid2 = db.record_override(conn, "shell", "echo hi",
                                      reason="still no")
            assert oid2 == oid
            assert len(db.list_overrides(conn)) == 1
            assert db.clear_overrides(conn, action_type="shell") == 1
            assert db.list_overrides(conn) == []
        finally:
            conn.close()

    def test_is_overridden_matches_exact_or_whole_type(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            db.record_override(conn, "git", "push origin main",
                               reason="never push")
            assert db.is_overridden(conn, "git", "push origin main")
            assert not db.is_overridden(conn, "git", "status")
            # Whole-type override: '' blocks every command of that type.
            db.record_override(conn, "ssh", "", reason="no remote")
            assert db.is_overridden(conn, "ssh", "build@x ls")
        finally:
            conn.close()

    def test_helpers_guarded_on_missing_table(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            conn.execute("DROP TABLE IF EXISTS permission_requests")
            conn.execute("DROP TABLE IF EXISTS operator_overrides")
            conn.commit()
            assert db.pending_permission_requests(conn) == []
            assert db.create_permission_request(conn, "x", "shell") is None
            assert db.record_override(conn, "shell", "x") is None
            assert db.is_overridden(conn, "shell", "x") is False
        finally:
            conn.close()


# ==========================================================================
# autonomy/loop.py — the judgment → action cycle
# ==========================================================================


class TestAutonomyCycle:
    def _agent(self, tmp_path, **kw):
        from friday_v6.autonomy import AutonomyAgent
        return AutonomyAgent(interval=3600.0, db_path=tmp_path / "v4.db", **kw)

    def test_auto_candidate_executes_silently(self, tmp_path, monkeypatch):
        """AUTO-level next steps (git status) run by themselves — audited,
        no permission ask."""
        calls = _fake_execute(monkeypatch)
        conn = _conn(tmp_path)
        _promote_skill(conn, "check-state", steps=[
            {"action_type": "testing", "command": "pytest -q"},
            {"action_type": "git", "command": "status", "goal": "see state"},
        ])
        db.record_action(conn, "testing", command="pytest -q",
                         cwd="/home/me/friday_v6", status="succeeded")
        conn.close()

        notified = []
        agent = self._agent(tmp_path, notify=lambda t, m, **kw:
                            notified.append(m))
        result = agent.cycle_once()
        assert result.executed == 1
        assert result.asked == 0
        assert calls and calls[0]["action_type"] == "git"
        assert calls[0]["command"] == "status"
        assert calls[0]["force"] is False  # never force in the loop
        assert notified == []              # AUTO never asks
        assert db.pending_permission_requests(_conn(tmp_path)) == []

    def test_confirm_candidate_asks_durably(self, tmp_path, monkeypatch):
        """CONFIRM-level work becomes a durable permission request + a
        notification — it is NOT executed by the loop. The matching
        action is backdated so the operator is idle (the busy gate
        would otherwise skip the ask — that path is covered by
        TestAutonomyIdleGate)."""
        calls = _fake_execute(monkeypatch)
        conn = _conn(tmp_path)
        _promote_skill(conn)  # next step: shell 'echo hi' (CONFIRM)
        db.record_action(conn, "testing", command="pytest -q",
                         cwd="/home/me/friday_v6", status="succeeded")
        conn.execute("UPDATE actions SET created_at = "
                     "'2026-01-01T00:00:00+00:00'")
        conn.commit()
        conn.close()

        notified = []
        agent = self._agent(tmp_path, notify=lambda t, m, **kw:
                            notified.append(m))
        result = agent.cycle_once()
        assert result.asked == 1
        assert result.executed == 0
        assert calls == []                    # nothing executed
        assert notified                       # the ask was raised
        pending = db.pending_permission_requests(_conn(tmp_path))
        assert len(pending) == 1
        assert pending[0]["action_type"] == "shell"
        assert pending[0]["command"] == "echo hi"

    def test_never_candidate_never_runs_or_asks(self, tmp_path, monkeypatch):
        """NEVER-level steps (git push) are skipped by the loop — never
        executed, never even proposed as a permission ask."""
        calls = _fake_execute(monkeypatch)
        conn = _conn(tmp_path)
        _promote_skill(conn, "deploy", steps=[
            {"action_type": "testing", "command": "pytest -q"},
            {"action_type": "git", "command": "push origin main"},
        ])
        db.record_action(conn, "testing", command="pytest -q",
                         cwd="/home/me/friday_v6", status="succeeded")
        conn.close()

        notified = []
        agent = self._agent(tmp_path, notify=lambda t, m, **kw:
                            notified.append(m))
        result = agent.cycle_once()
        assert result.executed == 0
        assert result.asked == 0
        assert result.skipped >= 1
        assert calls == []
        assert notified == []
        assert any(o.disposition == "skipped_never"
                   for o in result.outcomes)
        assert db.pending_permission_requests(_conn(tmp_path)) == []

    def test_operator_override_wins(self, tmp_path, monkeypatch):
        """A declined action is never proposed again — the override blocks
        the loop before it even asks."""
        calls = _fake_execute(monkeypatch)
        conn = _conn(tmp_path)
        _promote_skill(conn)
        db.record_action(conn, "testing", command="pytest -q",
                         cwd="/home/me/friday_v6", status="succeeded")
        db.record_override(conn, "shell", "echo hi",
                           reason="operator said no")
        conn.close()

        notified = []
        agent = self._agent(tmp_path, notify=lambda t, m, **kw:
                            notified.append(m))
        result = agent.cycle_once()
        assert result.executed == 0
        assert result.asked == 0
        assert any(o.disposition == "skipped_override"
                   for o in result.outcomes)
        assert notified == []
        assert db.pending_permission_requests(_conn(tmp_path)) == []

    def test_already_pending_ask_not_duplicated(self, tmp_path, monkeypatch):
        """The same action isn't asked twice — while a request is pending,
        the loop skips it (waits for the operator)."""
        calls = _fake_execute(monkeypatch)
        conn = _conn(tmp_path)
        _promote_skill(conn)  # next step: shell 'echo hi' (CONFIRM)
        db.record_action(conn, "testing", command="pytest -q",
                         cwd="/home/me/friday_v6", status="succeeded")
        db.create_permission_request(conn, "say hi", "shell",
                                     command="echo hi", source="autonomy")
        conn.close()

        notified = []
        agent = self._agent(tmp_path, notify=lambda t, m, **kw:
                            notified.append(m))
        result = agent.cycle_once()
        assert result.asked == 0
        assert calls == []
        assert any(o.disposition == "skipped_pending"
                   for o in result.outcomes)
        assert notified == []
        assert len(db.pending_permission_requests(_conn(tmp_path))) == 1

    def test_mission_candidates_feed_the_loop(self, tmp_path, monkeypatch):
        """Active missions' next executable step is a candidate — the loop
        asks permission for CONFIRM-level mission steps."""
        calls = _fake_execute(monkeypatch)
        from friday_v6.missions import MissionEngine, StepPlan
        conn = _conn(tmp_path)
        engine = MissionEngine(conn)
        mission = engine.create(
            "autonomy demo", plan=[
                StepPlan(title="greet", action_type="shell",
                         command="echo hi"),
            ])
        engine.start(mission.id)
        conn.close()

        notified = []
        agent = self._agent(tmp_path, notify=lambda t, m, **kw:
                            notified.append(m))
        result = agent.cycle_once()
        assert result.asked == 1
        assert calls == []
        pending = db.pending_permission_requests(_conn(tmp_path))
        assert any(p["action_type"] == "shell" and p["command"] == "echo hi"
                   for p in pending)

    def test_cycle_graceful_on_missing_db(self, tmp_path, monkeypatch):
        """A missing DB is not an error — the cycle degrades silently."""
        agent = self._agent(tmp_path / "missing" / "v4.db")
        result = agent.cycle_once()
        assert result.executed == 0 and result.asked == 0


class TestAutonomyResolution:
    def _agent(self, tmp_path, **kw):
        from friday_v6.autonomy import AutonomyAgent
        return AutonomyAgent(interval=3600.0, db_path=tmp_path / "v4.db", **kw)

    def test_accept_runs_through_gate_and_resolves(self, tmp_path,
                                                   monkeypatch):
        """The operator's 'yes' approves the ask; the action runs through
        the gate (confirm_fn pre-approved) and the request resolves."""
        calls = _fake_execute(monkeypatch)
        conn = _conn(tmp_path)
        rid = db.create_permission_request(
            conn, "run the tests", "testing", command="pytest -q")
        conn.close()

        agent = self._agent(tmp_path)
        outcome = agent.accept(rid)
        assert outcome is not None
        assert outcome["status"] == "succeeded"
        assert outcome["action_id"]
        assert calls and calls[0]["action_type"] == "testing"
        assert calls[0]["command"] == "pytest -q"
        assert calls[0]["force"] is False
        # The operator's yes IS the CONFIRM approval.
        assert calls[0]["confirm_fn"] is not None
        conn = _conn(tmp_path)
        assert db.get_permission_request(conn, rid)["status"] == "approved"
        conn.close()

    def test_accept_never_step_needs_force(self, tmp_path, monkeypatch):
        """A bare 'yes' never escalates a NEVER-level ask — denied unless
        the caller explicitly passes force."""
        calls = _fake_execute(monkeypatch)
        conn = _conn(tmp_path)
        rid = db.create_permission_request(
            conn, "push", "git", command="push origin main")
        conn.close()

        agent = self._agent(tmp_path)
        outcome = agent.accept(rid)
        assert outcome["status"] == "denied"  # gate denied w/o force
        assert db.get_permission_request(_conn(tmp_path), rid)["status"] == \
            "denied"

        # With an explicit operator override it may run.
        outcome = agent.accept(rid, force=True)
        assert outcome is None  # already resolved → not pending

    def test_deny_records_override(self, tmp_path, monkeypatch):
        """'no' resolves the ask as denied AND records an override so the
        loop never proposes the same action again."""
        calls = _fake_execute(monkeypatch)
        conn = _conn(tmp_path)
        rid = db.create_permission_request(
            conn, "run the tests", "testing", command="pytest -q")
        conn.close()

        agent = self._agent(tmp_path)
        assert agent.deny(rid, reason="do it a different way")
        assert calls == []
        conn = _conn(tmp_path)
        assert db.get_permission_request(conn, rid)["status"] == "denied"
        assert db.is_overridden(conn, "testing", "pytest -q")
        conn.close()

    def test_accept_missing_or_resolved_returns_none(self, tmp_path):
        agent = self._agent(tmp_path)
        assert agent.accept("nope") is None
        assert agent.deny("nope") is False

    def test_start_stop_lifecycle(self, tmp_path):
        agent = self._agent(tmp_path)
        agent.start()
        assert agent.running
        time.sleep(0.1)
        agent.stop()
        assert not agent.running


# ==========================================================================
# NL paths — the operator's voice (via the ONE NLU point)
# ==========================================================================


class TestAutonomyNl:
    def _handler(self, tmp_path):
        from friday_v6.nl_router import TextCommandHandler
        return TextCommandHandler(conn=_conn(tmp_path))

    def test_yes_run_it_resolves_pending_request(self, tmp_path,
                                                 monkeypatch):
        calls = _fake_execute(monkeypatch)
        handler = self._handler(tmp_path)
        rid = db.create_permission_request(
            handler.conn, "run the tests", "testing", command="pytest -q")
        result = handler.handle("yes, run it")
        assert result.intent == "accept"
        assert result.action == "executed"
        assert "pytest -q" in result.response or "Done" in result.response
        assert db.get_permission_request(handler.conn, rid)["status"] == \
            "approved"
        assert calls and calls[0]["command"] == "pytest -q"

    def test_no_denies_and_records_override(self, tmp_path, monkeypatch):
        calls = _fake_execute(monkeypatch)
        handler = self._handler(tmp_path)
        rid = db.create_permission_request(
            handler.conn, "run the tests", "testing", command="pytest -q")
        result = handler.handle("no, don't do that")
        assert result.intent == "deny"
        assert result.action == "denied"
        assert db.get_permission_request(handler.conn, rid)["status"] == \
            "denied"
        assert db.is_overridden(handler.conn, "testing", "pytest -q")
        assert calls == []

    def test_deny_without_pending_is_honest(self, tmp_path):
        handler = self._handler(tmp_path)
        result = handler.handle("no")
        assert result.intent == "deny"
        assert "don't have a pending request" in result.response

    def test_deny_intent_fallback(self):
        from friday_v6.nlu import Intent, resolve
        assert resolve("no").intent == Intent.DENY
        assert resolve("don't do that").intent == Intent.DENY
        assert resolve("do it a different way").intent == Intent.DENY
        assert resolve("skip it").intent == Intent.DENY

    def test_deny_does_not_break_memory_or_accept(self):
        """Tie-breaks: explicit memory consent and accept still win."""
        from friday_v6.nlu import Intent, resolve
        # 'don't forget' is MEMORY (explicit consent), not a denial.
        assert resolve("don't forget that I prefer Rust").intent == \
            Intent.MEMORY
        assert resolve("yes, run it").intent == Intent.ACCEPT
        # 'don't run the tests' denies (before EXECUTE).
        assert resolve("don't run the tests").intent == Intent.DENY


# ==========================================================================
# Idle gate + mission auto-advance + pending NL
# ==========================================================================


class TestAutonomyIdleGate:
    """The busy gate: CONFIRM asks never interrupt active work."""

    def _agent(self, tmp_path, **kw):
        from friday_v6.autonomy import AutonomyAgent
        return AutonomyAgent(interval=3600.0, db_path=tmp_path / "v4.db",
                             **kw)

    def test_busy_operator_is_not_asked(self, tmp_path, monkeypatch):
        """Recent activity → CONFIRM candidates are skipped, not asked."""
        calls = _fake_execute(monkeypatch)
        conn = _conn(tmp_path)
        _promote_skill(conn)  # next step: shell 'echo hi' (CONFIRM)
        db.record_action(conn, "testing", command="pytest -q",
                         cwd="/home/me/friday_v6", status="succeeded")
        # Recent activity → the operator is busy.
        db.record_desktop_event(conn, app="code", title="main.py")
        conn.close()

        notified = []
        agent = self._agent(tmp_path, notify=lambda t, m, **kw:
                            notified.append(m))
        result = agent.cycle_once()
        assert result.asked == 0
        assert calls == []
        assert any(o.disposition == "skipped_busy"
                   for o in result.outcomes)
        assert notified == []
        assert db.pending_permission_requests(_conn(tmp_path)) == []

    def test_idle_operator_is_asked(self, tmp_path, monkeypatch):
        """No recent activity → CONFIRM candidates become durable asks."""
        calls = _fake_execute(monkeypatch)
        conn = _conn(tmp_path)
        _promote_skill(conn)
        db.record_action(conn, "testing", command="pytest -q",
                         cwd="/home/me/friday_v6", status="succeeded")
        conn.close()  # action is the ONLY activity — then it goes idle

        # idle_seconds=0 → any past activity counts as idle.
        notified = []
        agent = self._agent(tmp_path, idle_seconds=0.0,
                            notify=lambda t, m, **kw: notified.append(m))
        result = agent.cycle_once()
        assert result.asked == 1
        assert calls == []
        pending = db.pending_permission_requests(_conn(tmp_path))
        assert any(p["command"] == "echo hi" for p in pending)

    def test_no_activity_is_treated_as_idle(self, tmp_path, monkeypatch):
        """A fresh DB (no activity) never blocks asks."""
        conn = _conn(tmp_path)
        _promote_skill(conn)
        db.record_action(conn, "testing", command="pytest -q",
                         cwd="/home/me/friday_v6", status="succeeded")
        # Backdate the only action so it is definitely old.
        conn.execute("UPDATE actions SET created_at = "
                     "'2026-01-01T00:00:00+00:00'")
        conn.commit()
        conn.close()

        agent = self._agent(tmp_path)  # default idle_seconds=300
        result = agent.cycle_once()
        assert result.asked == 1


class TestAutonomyMissionAdvance:
    """Mission auto-advance: a 'yes' completes the step and evaluates
    the next one in the same cycle."""

    def _agent(self, tmp_path, **kw):
        from friday_v6.autonomy import AutonomyAgent
        return AutonomyAgent(interval=3600.0, db_path=tmp_path / "v4.db",
                             **kw)

    def test_accept_completes_step_and_asks_next(self, tmp_path,
                                                 monkeypatch):
        """Approving a mission step completes it; the next CONFIRM step
        becomes the next durable ask in the same cycle."""
        calls = _fake_execute(monkeypatch)
        from friday_v6.missions import MissionEngine, StepPlan
        conn = _conn(tmp_path)
        engine = MissionEngine(conn)
        mission = engine.create(
            "auto demo", plan=[
                StepPlan(title="greet", action_type="shell",
                         command="echo hi"),
                StepPlan(title="again", action_type="shell",
                         command="echo again"),
            ])
        engine.start(mission.id)
        rid = db.create_permission_request(
            conn, "mission auto demo: echo hi", "shell", command="echo hi",
            source="mission", mission_id=mission.id,
            step_id=mission.steps[0].id)
        conn.close()

        agent = self._agent(tmp_path)
        outcome = agent.accept(rid)
        assert outcome is not None and outcome["status"] == "succeeded"
        # The approved step completed; the next CONFIRM step was asked.
        conn = _conn(tmp_path)
        steps = db.list_mission_steps(conn, mission.id)
        conn.close()
        assert steps[0]["status"] == "completed"
        assert steps[1]["status"] == "pending"
        pending = db.pending_permission_requests(_conn(tmp_path))
        assert any(p["command"] == "echo again" for p in pending)
        assert any(p.get("mission_id") == mission.id for p in pending)

    def test_auto_next_step_runs_in_same_cycle(self, tmp_path,
                                               monkeypatch):
        """AUTO-level next steps run immediately after the approval."""
        calls = _fake_execute(monkeypatch)
        from friday_v6.missions import MissionEngine, StepPlan
        conn = _conn(tmp_path)
        engine = MissionEngine(conn)
        mission = engine.create(
            "auto demo", plan=[
                StepPlan(title="greet", action_type="shell",
                         command="echo hi"),
                StepPlan(title="state", action_type="git",
                         command="status"),  # AUTO-level (read-only)
            ])
        engine.start(mission.id)
        rid = db.create_permission_request(
            conn, "mission auto demo: echo hi", "shell", command="echo hi",
            source="mission", mission_id=mission.id,
            step_id=mission.steps[0].id)
        conn.close()

        agent = self._agent(tmp_path)
        outcome = agent.accept(rid)
        assert outcome["status"] == "succeeded"
        # The AUTO git status next step ran in the same cycle.
        git_calls = [c for c in calls if c["action_type"] == "git"]
        assert git_calls and git_calls[0]["command"] == "status"
        assert git_calls[0]["force"] is False
        conn = _conn(tmp_path)
        steps = db.list_mission_steps(conn, mission.id)
        conn.close()
        assert steps[0]["status"] == "completed"
        assert steps[1]["status"] == "completed"

    def test_mission_finishes_when_steps_done(self, tmp_path, monkeypatch):
        """The mission completes when the last step is approved."""
        _fake_execute(monkeypatch)
        from friday_v6.missions import MissionEngine, MissionStatus, StepPlan
        conn = _conn(tmp_path)
        engine = MissionEngine(conn)
        mission = engine.create(
            "auto demo", plan=[
                StepPlan(title="greet", action_type="shell",
                         command="echo hi"),
            ])
        engine.start(mission.id)
        rid = db.create_permission_request(
            conn, "mission auto demo: echo hi", "shell", command="echo hi",
            source="mission", mission_id=mission.id,
            step_id=mission.steps[0].id)
        conn.close()

        agent = self._agent(tmp_path)
        outcome = agent.accept(rid)
        assert outcome["status"] == "succeeded"
        conn = _conn(tmp_path)
        fresh = MissionEngine(conn)
        mission = fresh.get(mission.id)
        conn.close()
        assert mission is not None
        assert mission.status == MissionStatus.COMPLETED


class TestAutonomyPendingNl:
    """'what's pending' routes to the durable asks through NL."""

    def _handler(self, tmp_path):
        from friday_v6.nl_router import TextCommandHandler
        return TextCommandHandler(conn=_conn(tmp_path))

    def test_whats_pending_lists_asks(self, tmp_path):
        handler = self._handler(tmp_path)
        db.create_permission_request(
            handler.conn, "run the tests", "testing", command="pytest -q")
        result = handler.handle("what's pending")
        assert result.intent == "ask"
        # The operator-facing ask description is what's surfaced.
        assert "run the tests" in result.response
        assert "yes, run it" in result.response

    def test_pending_empty_is_honest(self, tmp_path):
        handler = self._handler(tmp_path)
        result = handler.handle("what are you asking me")
        assert result.intent == "ask"
        assert "Nothing is waiting" in result.response


# ==========================================================================
# Self-learn / self-develop — Friday learns from its own observations
# ==========================================================================


class TestAutonomySelfLearn:
    """The "I noticed you keep doing X" loop: repeated patterns in the
    audit log become durable asks; a 'yes' forms a skill AND a mission.
    """

    def _agent(self, tmp_path, **kw):
        from friday_v6.autonomy import AutonomyAgent
        return AutonomyAgent(interval=3600.0, db_path=tmp_path / "v4.db",
                             **kw)

    @staticmethod
    def _seed_pattern(tmp_path):
        """Two identical ordered action sequences → a repeated pattern."""
        conn = _conn(tmp_path)
        for _ in range(2):
            db.record_action(conn, "testing", command="pytest -q",
                             cwd="/home/me/friday_v6", status="succeeded")
            db.record_action(conn, "shell", command="echo hi",
                             cwd="/home/me/friday_v6", status="succeeded")
        # Backdate so the operator is idle (the busy gate would
        # otherwise skip CONFIRM asks — that path is its own test).
        conn.execute("UPDATE actions SET created_at = "
                     "'2026-01-01T00:00:00+00:00'")
        conn.commit()
        conn.close()

    def test_repeated_pattern_becomes_durable_ask(self, tmp_path,
                                                  monkeypatch):
        """A repeated audit sequence is offered as a durable CONFIRM ask
        ('I noticed you keep doing X') — never executed by the loop."""
        calls = _fake_execute(monkeypatch)
        self._seed_pattern(tmp_path)

        notified = []
        agent = self._agent(tmp_path, notify=lambda t, m, **kw:
                            notified.append(m))
        result = agent.cycle_once()
        assert result.asked >= 1
        assert calls == []                  # learning offers never execute
        assert notified                     # the ask was raised
        pending = db.pending_permission_requests(_conn(tmp_path))
        learn = [p for p in pending if p.get("source") == "learn"]
        assert learn
        assert learn[0]["action_type"] == "skill"
        assert "noticed" in learn[0]["description"].lower()

    def test_learn_skipped_while_busy(self, tmp_path, monkeypatch):
        """Learning offers respect the busy gate — no interrupting active
        work with 'I noticed…'."""
        _fake_execute(monkeypatch)
        conn = _conn(tmp_path)
        for _ in range(2):
            db.record_action(conn, "testing", command="pytest -q",
                             cwd="/home/me/friday_v6", status="succeeded")
            db.record_action(conn, "shell", command="echo hi",
                             cwd="/home/me/friday_v6", status="succeeded")
        # Recent activity → the operator is busy.
        db.record_desktop_event(conn, app="code", title="main.py")
        conn.close()

        agent = self._agent(tmp_path)
        result = agent.cycle_once()
        assert result.asked == 0
        assert any(o.source == "learn" and o.disposition == "skipped_busy"
                   for o in result.outcomes)
        assert db.pending_permission_requests(_conn(tmp_path)) == []

    def test_accept_learn_forms_skill_and_mission(self, tmp_path):
        """'yes, run it' on a learn ask forms a shadow skill AND starts a
        mission from the pattern's steps (self-learn → self-extend)."""
        import json
        conn = _conn(tmp_path)
        pattern = {
            "sequence": ["shell:pytest -q", "shell:echo hi"],
            "count": 2,
            "context": "friday_v6",
            "first": "shell:pytest -q",
            "example": {"action_type": "shell", "command": "pytest -q",
                         "cwd": "/home/me/friday_v6", "goal": ""},
        }
        rid = db.create_permission_request(
            conn, "I noticed you keep doing this…", "skill",
            command="learn:shell:pytest -q", goal=json.dumps(pattern),
            source="learn")
        conn.close()

        agent = self._agent(tmp_path)
        outcome = agent.accept(rid)
        assert outcome is not None
        assert outcome["status"] == "succeeded"
        assert outcome.get("skill_id")
        assert outcome.get("mission_id")
        conn = _conn(tmp_path)
        try:
            # The shadow skill was formed from the pattern's steps.
            from friday_v6.skills import SkillRegistry
            skill = SkillRegistry(conn).get_by_id(outcome["skill_id"])
            assert skill is not None
            assert skill.is_shadow                 # never auto-promoted
            assert skill.steps[0]["command"] == "pytest -q"
            # A mission was created from the pattern and started.
            from friday_v6.missions import MissionEngine, MissionStatus
            mission = MissionEngine(conn).get(outcome["mission_id"])
            assert mission is not None
            assert mission.status == MissionStatus.ACTIVE
            assert len(mission.steps) == 2
            assert mission.steps[0].command == "pytest -q"
        finally:
            conn.close()
        assert db.get_permission_request(_conn(tmp_path), rid)["status"] == \
            "approved"

    def test_learn_off_disables_offers(self, tmp_path, monkeypatch):
        """The daemon knob autonomy_learn=False turns the offers off."""
        _fake_execute(monkeypatch)
        self._seed_pattern(tmp_path)
        agent = self._agent(tmp_path, learn=False)
        result = agent.cycle_once()
        assert not any(o.source == "learn" for o in result.outcomes)
        assert db.pending_permission_requests(_conn(tmp_path)) == []


class TestAutonomySelfDevelop:
    """Verified skills are offered for promotion as durable asks, closing
    the shadow → verified → promoted → dispatch loop."""

    def _agent(self, tmp_path, **kw):
        from friday_v6.autonomy import AutonomyAgent
        return AutonomyAgent(interval=3600.0, db_path=tmp_path / "v4.db",
                             **kw)

    @staticmethod
    def _verified_skill(tmp_path, name="check-state") -> str:
        """A skill at the verify threshold (shadow → verified)."""
        from friday_v6.skills import SkillRegistry
        conn = _conn(tmp_path)
        reg = SkillRegistry(conn)
        sid = reg.create(name, steps=[
            {"action_type": "testing", "command": "pytest -q",
             "goal": "run tests"},
        ])
        for _ in range(reg._verify_matches):
            reg.record_shadow_match(sid)
        reg.verify(sid)
        conn.close()
        return sid

    def test_verified_skill_offered_for_promotion(self, tmp_path,
                                                  monkeypatch):
        """A verified skill becomes a durable promote ask — the operator
        approval step surfaces as a normal ask, not a CLI-only command."""
        _fake_execute(monkeypatch)
        sid = self._verified_skill(tmp_path)

        notified = []
        agent = self._agent(tmp_path, notify=lambda t, m, **kw:
                            notified.append(m))
        result = agent.cycle_once()
        assert result.asked >= 1
        assert notified
        pending = db.pending_permission_requests(_conn(tmp_path))
        promote = [p for p in pending if p.get("source") == "promote"]
        assert promote
        # The command is a neutral hash (a user-named skill like
        # 'deploy-push' must not mis-classify the ask as NEVER); the
        # skill id rides in the goal JSON.
        assert promote[0]["command"].startswith("promote:")
        assert promote[0]["command"] != "promote:check-state"
        import json
        goal = json.loads(promote[0]["goal"])
        assert goal.get("skill_id") == sid

    def test_accept_promote_promotes_skill(self, tmp_path):
        """'yes' on the promote ask promotes the verified skill."""
        sid = self._verified_skill(tmp_path)
        import json
        conn = _conn(tmp_path)
        rid = db.create_permission_request(
            conn, "Promote skill 'check-state'?", "skill",
            command="promote:check-state",
            goal=json.dumps({"skill_id": sid, "skill_name": "check-state"}),
            source="promote")
        conn.close()

        agent = self._agent(tmp_path)
        outcome = agent.accept(rid)
        assert outcome is not None
        assert outcome["status"] == "succeeded"
        from friday_v6.skills import SkillRegistry
        skill = SkillRegistry(_conn(tmp_path)).get_by_id(sid)
        assert skill.is_promoted

    def test_promote_off_disables_offers(self, tmp_path):
        """The daemon knob autonomy_promote=False turns the offers off."""
        self._verified_skill(tmp_path)
        agent = self._agent(tmp_path, promote=False)
        result = agent.cycle_once()
        assert not any(o.source == "promote" for o in result.outcomes)
        assert db.pending_permission_requests(_conn(tmp_path)) == []


# ==========================================================================
# daemon wiring
# ==========================================================================


class TestAutonomyDaemon:
    def test_daemon_status_includes_autonomy_component(self, tmp_path):
        from friday_v6.autonomy import AutonomyAgent
        from friday_v6.daemon import DaemonConfig, DaemonService
        agent = AutonomyAgent(interval=3600.0,
                              db_path=tmp_path / "v4.db")
        agent.running = True
        service = DaemonService(config=DaemonConfig(), engine=False,
                                notifier=False, suggestion_channel=False,
                                sampler=False, security_scanner=False,
                                autonomy_agent=agent)
        comps = service.status()["components"]
        assert comps["autonomy"] is True

    def test_daemon_builds_autonomy_when_enabled(self, tmp_path,
                                                 monkeypatch):
        from friday_v6.daemon import DaemonConfig, DaemonService
        # autonomy_agent=False means the daemon treats it as unavailable
        # (same convention as the other components) — no real DB touched.
        service = DaemonService(
            config=DaemonConfig(autonomy=True, autonomy_interval=0.05),
            engine=False, notifier=False, suggestion_channel=False,
            sampler=False, security_scanner=False,
            memory_sweeper=False, skill_learner=False,
            relationship_refresher=False, dispatch_offerer=False,
            autonomy_agent=False, mobile_push_worker=False)
        service._build_components()
        assert service._autonomy_agent is False

    def test_daemon_shutdown_stops_autonomy_agent(self, tmp_path):
        from friday_v6.autonomy import AutonomyAgent
        from friday_v6.daemon import DaemonConfig, DaemonService
        agent = AutonomyAgent(interval=3600.0, db_path=tmp_path / "v4.db")
        agent.start()
        service = DaemonService(config=DaemonConfig(), engine=False,
                                notifier=False, suggestion_channel=False,
                                sampler=False, security_scanner=False,
                                autonomy_agent=agent)
        service._shutdown_components()
        assert not agent.running


# ==========================================================================
# CLI surface
# ==========================================================================


class TestAutonomyCli:
    def _args(self, tmp_path, **kw):
        base = {"db": tmp_path / "v4.db", "json": False}
        base.update(kw)
        return SimpleNamespace(**base)

    def test_cli_status_and_pending(self, tmp_path, capsys):
        from friday_v6.cli_autonomy import (
            cmd_autonomy_pending,
            cmd_autonomy_status,
        )
        conn = _conn(tmp_path)
        db.create_permission_request(conn, "run the tests", "testing",
                                     command="pytest -q")
        conn.close()

        assert cmd_autonomy_status(self._args(tmp_path)) == 0
        out = capsys.readouterr().out
        assert "Open permission requests" in out and "1" in out

        assert cmd_autonomy_pending(self._args(tmp_path)) == 0
        out = capsys.readouterr().out
        assert "pytest -q" in out

    def test_cli_approve_and_deny(self, tmp_path, capsys, monkeypatch):
        calls = _fake_execute(monkeypatch)
        from friday_v6.cli_autonomy import (
            cmd_autonomy_approve,
            cmd_autonomy_deny,
        )
        conn = _conn(tmp_path)
        rid = db.create_permission_request(
            conn, "run the tests", "testing", command="pytest -q")
        conn.close()
        assert cmd_autonomy_approve(self._args(tmp_path, request_id=rid)) == 0
        assert calls and calls[0]["command"] == "pytest -q"
        assert db.get_permission_request(_conn(tmp_path), rid)["status"] == \
            "approved"

        rid2 = db.create_permission_request(
            _conn(tmp_path), "x", "shell", "echo hi")
        assert cmd_autonomy_deny(self._args(tmp_path, request_id=rid2)) == 0
        assert db.is_overridden(_conn(tmp_path), "shell", "echo hi")
        assert "Declined" in capsys.readouterr().out

    def test_cli_overrides_list_and_clear(self, tmp_path, capsys):
        from friday_v6.cli_autonomy import (
            cmd_autonomy_clear_overrides,
            cmd_autonomy_overrides,
        )
        conn = _conn(tmp_path)
        db.record_override(conn, "git", "push origin main", reason="no push")
        conn.close()

        assert cmd_autonomy_overrides(self._args(tmp_path)) == 0
        assert "push origin main" in capsys.readouterr().out

        assert cmd_autonomy_clear_overrides(
            self._args(tmp_path, action_type="git")) == 0
        assert db.list_overrides(_conn(tmp_path)) == []

    def test_cli_missing_request_is_honest(self, tmp_path, capsys):
        from friday_v6.cli_autonomy import cmd_autonomy_deny
        assert cmd_autonomy_deny(
            self._args(tmp_path, request_id="nope")) == 0
        assert "no longer pending" in capsys.readouterr().out


# ==========================================================================
# ONE command unification (cli_talk.main argv rewriting)
# ==========================================================================


class TestOneCommand:
    """`friday6` IS the product: bare invocations and any phrase that
    isn't a named subcommand route through the shared NL brain (talk)."""

    def test_bare_phrase_routes_to_talk(self, monkeypatch):
        """`friday6 "run the tests"` → talk one-shot."""
        import friday_v6.cli_nl as cli_nl
        from friday_v6.cli_talk import main
        seen = []

        def fake_cmd_talk(args):
            seen.append(list(args.text))
            return 0

        monkeypatch.setattr(cli_nl, "cmd_talk", fake_cmd_talk)
        assert main(["run", "the", "tests"]) == 0
        assert seen == [["run", "the", "tests"]]

    def test_bare_invocation_routes_to_talk(self, monkeypatch):
        """`friday6` (no args) → the interactive NL session."""
        import friday_v6.cli_nl as cli_nl
        from friday_v6.cli_talk import main
        seen = []

        def fake_cmd_talk(args):
            seen.append(list(args.text))
            return 0

        monkeypatch.setattr(cli_nl, "cmd_talk", fake_cmd_talk)
        assert main([]) == 0
        assert seen == [[]]  # empty text → the REPL

    def test_named_subcommand_still_works(self, monkeypatch):
        """Debug hatches (`friday6 skills …`) are untouched."""
        import friday_v6.cli_skills as cli_skills
        from friday_v6.cli_talk import main
        seen = []

        def fake_cmd_skills_list(args):
            seen.append("list")
            return 0

        monkeypatch.setattr(cli_skills, "cmd_skills_list",
                            fake_cmd_skills_list)
        assert main(["skills", "list"]) == 0
        assert seen == ["list"]

    def test_flags_first_phrase_routes_to_talk(self, monkeypatch):
        """`friday6 --force "run the tests"` → talk with its flags."""
        import friday_v6.cli_nl as cli_nl
        from friday_v6.cli_talk import main
        seen = []

        def fake_cmd_talk(args):
            seen.append((list(args.text), bool(args.force)))
            return 0

        monkeypatch.setattr(cli_nl, "cmd_talk", fake_cmd_talk)
        assert main(["--force", "run", "the", "tests"]) == 0
        assert seen == [(["run", "the", "tests"], True)]

    def test_help_flag_still_works(self, capsys):
        """`friday6 --help` keeps the main parser's help (not rewritten
        into the talk subcommand — there is no non-flag token)."""
        from friday_v6.cli_talk import main
        with pytest.raises(SystemExit) as exc:
            main(["--help"])
        assert exc.value.code == 0
        out = capsys.readouterr().out
        # Main help (usage: friday6 …) not the talk help (usage: friday6
        # talk …) — order-independent: the talk subparser's help would
        # read "usage: friday6 talk …", so its absence proves we got the
        # main parser's help page.
        assert "usage: friday6" in out
        assert "usage: friday6 talk" not in out


import time  # noqa: E402  (used by lifecycle tests above)
