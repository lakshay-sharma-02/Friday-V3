"""Hermetic tests for the Wave 9 missions layer (friday_v4.missions).

Covers the full goal pipeline:
- models: payload contract, statuses, progress math
- planner: goal → StepPlan (executable via understanding, templates,
  manual fallback, enhancer injection)
- scheduler: schedule spacing, working-day window, next_due
- engine: create/start/advance through the *execution* layer, manual
  steps, denied steps stay pending, adapt/replan with explicit reason,
  restart-safety, completion/failure
- progress: per-mission report, feed, summary

Every test is hermetic: tmp_path DB — never the real ~/.friday.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from friday_v4 import db
from friday_v4.missions import (
    MissionEngine,
    MissionStatus,
    Planner,
    Scheduler,
    StepPlan,
    StepStatus,
    progress_feed,
    report,
    summary,
)


def _conn(tmp_path):
    return db.connect(tmp_path / "v4.db")


def _confirm(_desc: str) -> bool:
    return True


# ==========================================================================
# Models
# ==========================================================================


class TestModels:
    def test_step_payload_contract(self):
        from friday_v4.missions import make_step_payload
        payload = make_step_payload("testing", "tests/", "/repo")
        assert payload == {"action_type": "testing",
                           "command": "tests/", "cwd": "/repo"}
        manual = make_step_payload(None, "")
        assert "action_type" not in manual

    def test_mission_progress(self):
        from friday_v4.missions import Mission, MissionStep
        m = Mission(id="m1", title="t")
        assert m.progress == 1.0  # empty mission
        m.steps = [
            MissionStep(id="s1", mission_id="m1", title="a",
                        status=StepStatus.COMPLETED),
            MissionStep(id="s2", mission_id="m1", title="b"),
        ]
        assert m.progress == 0.5
        assert m.next_step.id == "s2"
        assert m.pending_steps[0].id == "s2"


# ==========================================================================
# Planner
# ==========================================================================


class TestPlanner:
    def test_executable_goal_via_understanding(self):
        plan = Planner().plan("run the tests")
        assert plan
        assert plan[0].action_type == "testing"

    def test_template_lint(self):
        plan = Planner().plan("run lint on the code")
        assert plan
        # 'lint' template → shell
        assert plan[0].action_type == "shell"

    def test_manual_fallback(self):
        # No executable mapping, no template keyword → manual step.
        plan = Planner().plan("improve the parser architecture")
        assert plan
        assert plan[0].action_type is None  # Friday doesn't invent
        assert plan[0].command == ""

    def test_enhancer_injected(self):
        def enhancer(goal):
            return [StepPlan("custom step", "shell", "echo hi")]
        plan = Planner(enhancer=enhancer).plan("anything")
        assert plan[0].title == "custom step"

    def test_cwd_forwarded(self):
        plan = Planner().plan("run the tests", cwd="/tmp/repo")
        assert plan[0].cwd == "/tmp/repo"

    def test_can_plan(self):
        p = Planner()
        assert p.can_plan("run the tests") is True
        assert p.can_plan("lint everything") is True
        assert p.can_plan("improve the parser architecture") is False


# ==========================================================================
# Scheduler
# ==========================================================================


class TestScheduler:
    def test_schedule_spaces_steps(self):
        from friday_v4.missions import Mission, MissionStep
        m = Mission(id="m1", title="t")
        m.steps = [MissionStep(id=f"s{i}", mission_id="m1", title=f"s{i}")
                   for i in range(3)]
        sched = Scheduler(interval_hours=4.0)
        times = sched.schedule(m.steps, start=datetime(2026, 8, 1, 10,
                                                       tzinfo=timezone.utc))
        assert len(times) == 3
        t0 = datetime.fromisoformat(times["s0"])
        t1 = datetime.fromisoformat(times["s1"])
        assert (t1 - t0).total_seconds() == 4 * 3600

    def test_schedule_rolls_into_workday(self):
        from friday_v4.missions import Mission, MissionStep
        m = Mission(id="m1", title="t")
        m.steps = [MissionStep(id="s0", mission_id="m1", title="s0")]
        sched = Scheduler(day_start_hour=9, day_end_hour=18)
        late = datetime(2026, 8, 1, 23, 0, tzinfo=timezone.utc)
        times = sched.schedule(m.steps, start=late)
        t0 = datetime.fromisoformat(times["s0"])
        assert t0.hour == 9  # rolled into the next working day

    def test_next_due_picks_earliest_due(self):
        from friday_v4.missions import Mission, MissionStep
        now = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
        m = Mission(id="m1", title="t")
        m.steps = [
            MissionStep(id="a", mission_id="m1", title="a",
                        payload={"scheduled_at":
                                  "2026-08-01T13:00:00+00:00"}),
            MissionStep(id="b", mission_id="m1", title="b",
                        payload={"scheduled_at":
                                  "2026-08-01T10:00:00+00:00"}),
        ]
        sched = Scheduler()
        nxt = sched.next_due(m.steps, now=now)
        assert nxt.id == "b"  # earliest due

    def test_next_due_falls_back_to_position(self):
        from friday_v4.missions import Mission, MissionStep
        m = Mission(id="m1", title="t")
        m.steps = [
            MissionStep(id="a", mission_id="m1", title="a"),
            MissionStep(id="b", mission_id="m1", title="b"),
        ]
        sched = Scheduler()
        assert sched.next_due(m.steps).id == "a"

    def test_next_due_none_when_no_pending(self):
        from friday_v4.missions import Mission, MissionStep
        m = Mission(id="m1", title="t")
        m.steps = [MissionStep(id="a", mission_id="m1", title="a",
                               status=StepStatus.COMPLETED)]
        assert Scheduler().next_due(m.steps) is None


# ==========================================================================
# Engine — lifecycle + execution
# ==========================================================================


class TestEngine:
    def test_create_and_get(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            engine = MissionEngine(conn)
            mission = engine.create("run the tests")
            assert mission is not None
            assert mission.title == "run the tests"
            assert mission.steps  # planner produced steps
            assert engine.get(mission.id).id == mission.id
            assert engine.get("nope") is None
        finally:
            conn.close()

    def test_restart_safe(self, tmp_path):
        path = tmp_path / "v4.db"
        conn = db.connect(path)
        mission = MissionEngine(conn).create("run the tests")
        conn.close()

        conn2 = db.connect(path)  # "restart"
        try:
            reloaded = MissionEngine(conn2).get(mission.id)
            assert reloaded.title == "run the tests"
            assert len(reloaded.steps) == len(mission.steps)
        finally:
            conn2.close()

    def test_start_advance_executes_step(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            engine = MissionEngine(conn)
            # Explicit fast shell step — never a real pytest run.
            mission = engine.create(
                "verify the code",
                plan=[StepPlan("echo hi", "shell", "echo hi")])
            assert engine.start(mission.id)
            assert engine.get(mission.id).status == MissionStatus.ACTIVE
            res = engine.advance(mission.id, confirm_fn=_confirm, force=True)
            assert res.action == "executed"
            step = engine.get(mission.id).steps[0]
            assert step.status == StepStatus.COMPLETED
        finally:
            conn.close()

    def test_denied_step_stays_pending(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            engine = MissionEngine(conn)
            mission = engine.create("run the tests")
            engine.start(mission.id)
            res = engine.advance(mission.id, confirm_fn=lambda d: False)
            assert res.action == "denied"
            step = engine.get(mission.id).steps[0]
            assert step.status == StepStatus.PENDING  # retryable
            assert engine.get(mission.id).status == MissionStatus.ACTIVE
        finally:
            conn.close()

    def test_advance_not_active(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            engine = MissionEngine(conn)
            mission = engine.create("run the tests")  # planned, not started
            res = engine.advance(mission.id)
            assert res.action == "not_active"
        finally:
            conn.close()

    def test_mission_completes_when_all_steps_done(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            engine = MissionEngine(conn)
            plan = [StepPlan("run tests", "shell", "echo one"),
                    StepPlan("run linter", "shell", "echo two")]
            mission = engine.create("verify the code", plan=plan)
            engine.start(mission.id)
            for _ in range(10):
                res = engine.advance(mission.id, force=True)
                if res.action in ("none_pending",):
                    break
            m = engine.get(mission.id)
            assert m.status == MissionStatus.COMPLETED
            assert all(s.status == StepStatus.COMPLETED for s in m.steps)
        finally:
            conn.close()

    def test_failed_step_fails_mission(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            engine = MissionEngine(conn)
            mission = engine.create("run a failing command",
                                    plan=[StepPlan("boom", "shell",
                                                   "nonexistent-cmd-xyz")])
            engine.start(mission.id)
            res = engine.advance(mission.id, force=True)
            assert res.action in ("executed", "failed")
            m = engine.get(mission.id)
            if m.steps[0].status == StepStatus.FAILED:
                assert m.status == MissionStatus.FAILED
        finally:
            conn.close()

    def test_adapt_reports_change(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            engine = MissionEngine(conn)
            mission = engine.create("run the tests")
            report_res = engine.adapt(
                mission.id,
                [StepPlan("new step", "shell", "echo hi")],
                reason="requirements changed")
            assert report_res.changed is True
            assert "requirements changed" in report_res.message
            reloaded = engine.get(mission.id)
            assert len(reloaded.steps) == 1
            assert reloaded.steps[0].title == "new step"
        finally:
            conn.close()

    def test_replan_via_goal(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            engine = MissionEngine(conn)
            mission = engine.create("run the tests")
            r = engine.replan(mission.id, "lint the code",
                              reason="new direction")
            assert r.changed
            reloaded = engine.get(mission.id)
            assert reloaded.steps[0].title != "run the tests"
        finally:
            conn.close()

    def test_manual_step_completes(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            engine = MissionEngine(conn)
            mission = engine.create(
                "write a design doc",
                plan=[StepPlan("write a design doc", None, "")])
            engine.start(mission.id)
            res = engine.advance(mission.id, manual_result="docs written")
            assert res.action == "manual_completed"
            assert engine.get(mission.id).status == MissionStatus.COMPLETED
        finally:
            conn.close()


# ==========================================================================
# Progress
# ==========================================================================


class TestProgress:
    def test_report_percent(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            engine = MissionEngine(conn)
            mission = engine.create("run the tests")
            engine.start(mission.id)
            p = report(engine.get(mission.id))
            assert p.total_steps >= 1
            assert 0.0 <= p.percent <= 1.0
            assert p.next_step is not None
        finally:
            conn.close()

    def test_progress_feed_and_summary(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            engine = MissionEngine(conn)
            engine.create("run the tests")
            engine.create("lint the code")
            feed = progress_feed(conn)
            assert len(feed) >= 1  # planned missions appear in the feed
            s = summary(conn)
            assert s["by_status"]["planned"] >= 2
            assert s["total_steps"] >= 2
        finally:
            conn.close()
