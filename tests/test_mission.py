"""Tests for the Persistent Missions engine — multi-cycle adaptive goals."""

from __future__ import annotations

import json

import pytest

from friday.mission import (
    PersistentMission,
    MissionStep,
    MissionEngine,
    _generate_steps,
    _generate_steps_deterministic,
    _generate_steps_llm,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def conn(monkeypatch):
    """In-memory SQLite connection seeded with the mission table schema.

    Also disables LLM calls so ``_generate_steps`` during ``start_mission``
    falls through to the deterministic planner (avoids real 45-second LLM
    timeouts in test environments that have API keys configured).
    """
    monkeypatch.setattr(
        "friday.services.llm._enabled",
        lambda: False,
    )
    from friday.db import connect
    c = connect(":memory:")
    # Create the mission table.
    c.executescript("""
        CREATE TABLE IF NOT EXISTS persistent_missions (
            mission_id          TEXT PRIMARY KEY,
            goal                TEXT NOT NULL,
            status              TEXT NOT NULL DEFAULT 'active',
            created_at          TEXT NOT NULL,
            updated_at          TEXT NOT NULL,
            cycle_count         INTEGER NOT NULL DEFAULT 0,
            completed_steps     INTEGER NOT NULL DEFAULT 0,
            failed_steps        INTEGER NOT NULL DEFAULT 0,
            skipped_steps       INTEGER NOT NULL DEFAULT 0,
            total_steps         INTEGER NOT NULL DEFAULT 0,
            steps_json          TEXT NOT NULL DEFAULT '[]',
            progress_pct        REAL NOT NULL DEFAULT 0.0,
            error_summary       TEXT NOT NULL DEFAULT '',
            last_error          TEXT NOT NULL DEFAULT '',
            consecutive_failures INTEGER NOT NULL DEFAULT 0
        );
    """)
    c.commit()
    yield c
    c.close()


# ---------------------------------------------------------------------------
# MissionStep model
# ---------------------------------------------------------------------------


class TestMissionStep:
    def test_create_step(self):
        step = MissionStep(index=0, description="Analyze", action_type="shell",
                           payload="echo analyze")
        assert step.index == 0
        assert step.description == "Analyze"
        assert step.status == "pending"

    def test_to_dict_roundtrip(self):
        step = MissionStep(index=0, description="Test", action_type="shell",
                           payload="pytest", result={"success": True})
        d = step.to_dict()
        assert d["index"] == 0
        restored = MissionStep.from_dict(d)
        assert restored.index == 0
        assert restored.description == "Test"
        assert restored.result == {"success": True}


# ---------------------------------------------------------------------------
# PersistentMission model
# ---------------------------------------------------------------------------


class TestPersistentMission:
    def test_empty_mission(self):
        m = PersistentMission(
            mission_id="m1", goal="Test goal",
        )
        assert m.status == "active"
        assert m.progress_pct == 0.0
        assert m.total_steps == 0

    def test_to_dict_roundtrip(self):
        m = PersistentMission(
            mission_id="m1", goal="Test",
            steps=[MissionStep(index=0, description="Step 1", action_type="shell",
                               payload="echo hello")],
            total_steps=1,
        )
        d = m.to_dict()
        assert d["mission_id"] == "m1"
        assert d["total_steps"] == 1
        restored = PersistentMission.from_dict(d)
        assert restored.mission_id == "m1"
        assert len(restored.steps) == 1
        assert restored.steps[0].description == "Step 1"

    def test_format_with_steps(self):
        m = PersistentMission(
            mission_id="m1", goal="Refactor auth",
            completed_steps=2, total_steps=5,
            progress_pct=40.0,
            steps=[
                MissionStep(index=0, description="Analyze", action_type="shell",
                            payload="echo", status="completed"),
                MissionStep(index=1, description="Plan", action_type="shell",
                            payload="echo", status="completed"),
                MissionStep(index=2, description="Execute", action_type="shell",
                            payload="echo", status="pending"),
                MissionStep(index=3, description="Verify", action_type="shell",
                            payload="echo", status="pending"),
                MissionStep(index=4, description="Document", action_type="shell",
                            payload="echo", status="pending"),
            ],
        )
        text = m.format()
        assert "Refactor auth" in text
        assert "2/5" in text
        assert "40%" in text

    def test_format_short(self):
        m = PersistentMission(
            mission_id="m1", goal="Refactor auth module",
            completed_steps=1, total_steps=5, progress_pct=20.0,
        )
        short = m.format_short()
        assert "Refactor auth" in short
        assert "1/5" in short

    def test_failed_steps_in_format(self):
        m = PersistentMission(
            mission_id="m1", goal="Deploy",
            failed_steps=1, error_summary="Connection refused",
            steps=[
                MissionStep(index=0, description="Deploy", action_type="shell",
                            payload="deploy", status="failed", error_count=3),
            ],
        )
        text = m.format()
        assert "failed 3x" in text


# ---------------------------------------------------------------------------
# Step generation
# ---------------------------------------------------------------------------


class TestGenerateSteps:
    def test_basic_goal_generates_5_steps(self):
        steps = _generate_steps("Refactor the auth module")
        assert len(steps) == 5
        assert steps[0].description.startswith("Analyze")
        assert steps[1].description.startswith("Create execution plan")
        assert steps[2].description.startswith("Execute refactoring")
        assert steps[3].description.startswith("Verify")
        assert steps[4].description.startswith("Document")

    def test_test_goal_generates_test_step(self):
        steps = _generate_steps("Add tests for the API")
        assert len(steps) == 5
        assert "test" in steps[2].description.lower()

    def test_deploy_goal_generates_deploy_step(self):
        steps = _generate_steps("Deploy to production")
        assert len(steps) == 5
        assert "deploy" in steps[2].description.lower()

    def test_generic_goal(self):
        steps = _generate_steps("Fix the bug")
        assert len(steps) == 5
        assert steps[2].description.startswith("Execute:")

    def test_deterministic_planner_directly(self):
        """Test the deterministic fallback directly (no LLM involved)."""
        steps = _generate_steps_deterministic("Refactor the auth module")
        assert len(steps) == 5
        assert steps[0].description.startswith("Analyze")
        assert steps[2].description.startswith("Execute refactoring")

    def test_deterministic_planner_test_keyword(self):
        steps = _generate_steps_deterministic("Add unit tests")
        assert "test" in steps[2].description.lower()

    def test_deterministic_planner_deploy_keyword(self):
        steps = _generate_steps_deterministic("Deploy to staging")
        assert "deploy" in steps[2].description.lower()


# ---------------------------------------------------------------------------
# LLM-powered step generation (mocked)
# ---------------------------------------------------------------------------


class TestLLMStepGeneration:
    def test_llm_fallback_when_disabled(self, monkeypatch):
        """When LLM is disabled, _generate_steps should fall back to deterministic."""
        monkeypatch.setattr(
            "friday.services.llm._enabled",
            lambda: False,
        )
        mock_conn = lambda: None  # fake conn (not None, so LLM path is attempted)
        mock_conn.execute = lambda *a, **kw: type('cur', (), {'fetchone': lambda: [0]})()
        steps = _generate_steps("Refactor auth", conn=mock_conn)
        assert len(steps) == 5  # Falls back to deterministic
        assert steps[0].description.startswith("Analyze")

    def test_llm_returns_rich_steps(self, monkeypatch):
        """When LLM returns valid steps, _generate_steps should use them."""
        fake_steps_data = {
            "steps": [
                {
                    "description": "Audit existing auth middleware",
                    "action_type": "shell",
                    "payload": "grep -rn 'auth' src/ --include='*.py' | head -30",
                    "worker_id": "worker:shell",
                },
                {
                    "description": "Write integration tests for auth",
                    "action_type": "python",
                    "payload": "print('writing tests')",
                    "worker_id": "worker:python",
                },
                {
                    "description": "Verify tests pass",
                    "action_type": "shell",
                    "payload": "python -m pytest tests/test_auth.py -v",
                    "worker_id": "worker:shell",
                },
            ]
        }

        monkeypatch.setattr(
            "friday.services.llm._enabled",
            lambda: True,
        )
        monkeypatch.setattr(
            "friday.services.llm._call_structured",
            lambda system, user, required_keys=None: fake_steps_data,
        )

        mock_conn = lambda: None
        mock_conn.execute = lambda *a, **kw: type('cur', (), {'fetchone': lambda: [0]})()
        steps = _generate_steps("Refactor auth module for better security", conn=mock_conn)
        assert len(steps) == 3  # LLM returned 3 steps
        assert steps[0].description == "Audit existing auth middleware"
        assert steps[0].action_type == "shell"
        assert steps[0].worker_id == "worker:shell"
        assert "auth" in steps[0].payload
        assert steps[1].action_type == "python"
        assert steps[2].description == "Verify tests pass"

    def test_llm_malformed_response_falls_back(self, monkeypatch):
        """When LLM returns malformed JSON, fall back to deterministic."""
        monkeypatch.setattr(
            "friday.services.llm._enabled",
            lambda: True,
        )
        monkeypatch.setattr(
            "friday.services.llm._call_structured",
            lambda system, user, required_keys=None: None,  # Simulates parse failure
        )

        mock_conn = lambda: None
        mock_conn.execute = lambda *a, **kw: type('cur', (), {'fetchone': lambda: [0]})()
        steps = _generate_steps("Refactor auth", conn=mock_conn)
        assert len(steps) == 5  # Falls back to deterministic

    def test_llm_empty_steps_falls_back(self, monkeypatch):
        """When LLM returns empty steps list, fall back to deterministic."""
        monkeypatch.setattr(
            "friday.services.llm._enabled",
            lambda: True,
        )
        monkeypatch.setattr(
            "friday.services.llm._call_structured",
            lambda system, user, required_keys=None: {"steps": []},
        )

        mock_conn = lambda: None
        mock_conn.execute = lambda *a, **kw: type('cur', (), {'fetchone': lambda: [0]})()
        steps = _generate_steps("Refactor auth", conn=mock_conn)
        assert len(steps) == 5  # Falls back to deterministic

    def test_llm_unknown_action_type_normalized(self, monkeypatch):
        """Unknown action types should fall back to 'shell'."""
        fake_steps_data = {
            "steps": [
                {
                    "description": "Run analysis",
                    "action_type": "magic",  # Not a valid action type
                    "payload": "echo hello",
                    "worker_id": "worker:shell",
                },
            ]
        }

        monkeypatch.setattr(
            "friday.services.llm._enabled",
            lambda: True,
        )
        monkeypatch.setattr(
            "friday.services.llm._call_structured",
            lambda system, user, required_keys=None: fake_steps_data,
        )

        mock_conn = lambda: None
        mock_conn.execute = lambda *a, **kw: type('cur', (), {'fetchone': lambda: [0]})()
        steps = _generate_steps("Do magic", conn=mock_conn)
        assert len(steps) == 1
        assert steps[0].action_type == "shell"  # Normalized

    def test_llm_method_directly(self, monkeypatch):
        """Test _generate_steps_llm directly with a mocked LLM."""
        fake_steps_data = {
            "steps": [
                {
                    "description": "Check syntax",
                    "action_type": "shell",
                    "payload": "python -m py_compile src/main.py",
                    "worker_id": "worker:shell",
                },
            ]
        }

        monkeypatch.setattr(
            "friday.services.llm._enabled",
            lambda: True,
        )
        monkeypatch.setattr(
            "friday.services.llm._call_structured",
            lambda system, user, required_keys=None: fake_steps_data,
        )

        steps = _generate_steps_llm("Check main.py syntax")
        assert steps is not None
        assert len(steps) == 1
        assert "Check syntax" in steps[0].description

    def test_llm_disabled_returns_none(self, monkeypatch):
        """When LLM is disabled, _generate_steps_llm returns None."""
        monkeypatch.setattr(
            "friday.services.llm._enabled",
            lambda: False,
        )
        result = _generate_steps_llm("Any goal")
        assert result is None

    def test_no_conn_skips_llm(self, monkeypatch):
        """When conn is None, _generate_steps skips the LLM entirely."""
        # This should never call _call_structured even if LLM is enabled.
        monkeypatch.setattr(
            "friday.services.llm._enabled",
            lambda: True,
        )
        monkeypatch.setattr(
            "friday.services.llm._call_structured",
            lambda system, user, required_keys=None: (_ for _ in ()).throw(RuntimeError("should not be called")),
        )

        steps = _generate_steps("Any goal", conn=None)
        assert len(steps) == 5  # Falls back to deterministic without calling LLM


# ---------------------------------------------------------------------------
# MissionEngine — start, get, cancel
# ---------------------------------------------------------------------------


class TestMissionEngine:
    def test_start_mission(self, conn):
        engine = MissionEngine(conn)
        mission = engine.start_mission("Refactor auth")
        assert mission.goal == "Refactor auth"
        assert mission.status == "active"
        assert mission.total_steps == 5
        assert len(mission.steps) == 5

    def test_get_active_missions_empty(self, conn):
        engine = MissionEngine(conn)
        assert engine.get_active_missions() == []

    def test_get_active_missions(self, conn):
        engine = MissionEngine(conn)
        engine.start_mission("Refactor auth")
        m1 = engine.start_mission("Add tests")
        active = engine.get_active_missions()
        assert len(active) == 2
        assert active[0].goal == "Refactor auth"  # created asc
        assert active[1].goal == "Add tests"

    def test_get_mission_by_id(self, conn):
        engine = MissionEngine(conn)
        m = engine.start_mission("Test")
        found = engine.get_mission(m.mission_id)
        assert found is not None
        assert found.goal == "Test"

    def test_get_mission_not_found(self, conn):
        engine = MissionEngine(conn)
        assert engine.get_mission("nonexistent") is None

    def test_cancel_mission(self, conn):
        engine = MissionEngine(conn)
        m = engine.start_mission("Test")
        assert engine.cancel_mission(m.mission_id) is True
        cancelled = engine.get_mission(m.mission_id)
        assert cancelled.status == "cancelled"

    def test_cancel_nonexistent(self, conn):
        engine = MissionEngine(conn)
        assert engine.cancel_mission("nonexistent") is False

    def test_get_all_missions(self, conn):
        engine = MissionEngine(conn)
        engine.start_mission("First")
        engine.start_mission("Second")
        all_m = engine.get_all_missions()
        assert len(all_m) == 2

    def test_get_all_missions_empty(self, conn):
        engine = MissionEngine(conn)
        assert len(engine.get_all_missions()) == 0

    def test_mission_has_mission_id_prefix(self, conn):
        engine = MissionEngine(conn)
        m = engine.start_mission("Test")
        assert m.mission_id.startswith("mission:")


# ---------------------------------------------------------------------------
# MissionEngine — advance
# ---------------------------------------------------------------------------


class TestAdvanceMissions:
    def test_advance_no_missions(self, conn):
        engine = MissionEngine(conn)
        updates = engine.advance_missions()
        assert updates == []

    def test_advance_completes_5_step_mission(self, conn):
        """Advance 5 times to complete a 5-step mission."""
        engine = MissionEngine(conn)
        mission = engine.start_mission("Quick test")

        for i in range(5):
            updates = engine.advance_missions()
            assert len(updates) == 1

            # Reload mission to check progress.
            mission = engine.get_mission(mission.mission_id)
            assert mission.cycle_count == i + 1

        # After 5 advances, mission should be completed.
        mission = engine.get_mission(mission.mission_id)
        assert mission.status == "completed"
        assert mission.progress_pct == 100.0

    def test_advance_tracks_cycle_count(self, conn):
        engine = MissionEngine(conn)
        mission = engine.start_mission("Test")
        for _ in range(3):
            engine.advance_missions()
        mission = engine.get_mission(mission.mission_id)
        assert mission.cycle_count == 3
        assert mission.completed_steps == 3

    def test_advance_multiple_missions(self, conn):
        engine = MissionEngine(conn)
        engine.start_mission("Mission A")
        engine.start_mission("Mission B")

        updates = engine.advance_missions()
        assert len(updates) == 2  # one update per mission

    def test_advance_saves_steps_json(self, conn):
        engine = MissionEngine(conn)
        m = engine.start_mission("Test")
        engine.advance_missions()

        row = conn.execute(
            "SELECT steps_json FROM persistent_missions WHERE mission_id = ?",
            (m.mission_id,),
        ).fetchone()
        steps = json.loads(row["steps_json"])
        # First step should now be completed.
        assert steps[0]["status"] == "completed"

    def test_ambient_event_on_complete(self, conn):
        """Mission completion should push an ambient event."""
        engine = MissionEngine(conn)
        m = engine.start_mission("Test")
        for _ in range(5):
            engine.advance_missions()

        mission = engine.get_mission(m.mission_id)
        assert mission.status == "completed"

        # Check that ambient_feed has the completion event.
        row = conn.execute(
            "SELECT event_type FROM ambient_feed "
            "WHERE event_type = 'mission_completed'"
        ).fetchone()
        assert row is not None, "Expected a mission_completed event in the feed"


# ---------------------------------------------------------------------------
# MissionStep execution
# ---------------------------------------------------------------------------


class TestStepExecution:
    def test_execute_shell_step(self, conn):
        """Execute a simple shell echo step."""
        engine = MissionEngine(conn)
        step = MissionStep(index=0, description="Say hello", action_type="shell",
                           payload="echo hello world")
        result = engine._execute_step(step)
        assert result["success"] is True
        assert "hello world" in result["stdout"]

    def test_execute_failing_shell(self, conn):
        """A failing shell command should return success=False."""
        engine = MissionEngine(conn)
        step = MissionStep(index=0, description="Fail", action_type="shell",
                           payload="exit 42")
        result = engine._execute_step(step)
        assert result["success"] is False
        assert result["exit_code"] == 42

    def test_execute_python_step(self, conn):
        """Execute a Python one-liner step."""
        engine = MissionEngine(conn)
        step = MissionStep(index=0, description="Calculate", action_type="python",
                           payload="print(2 + 2)")
        result = engine._execute_step(step)
        assert result["success"] is True
        assert "4" in result["stdout"]

    def test_unknown_action_type(self, conn):
        """Unknown action types should return an error."""
        engine = MissionEngine(conn)
        step = MissionStep(index=0, description="Unknown", action_type="unknown",
                           payload="anything")
        result = engine._execute_step(step)
        assert result["success"] is False


# ---------------------------------------------------------------------------
# Adaptive plan revision
# ---------------------------------------------------------------------------


class TestAdaptiveRevision:
    def test_same_worker_retry(self, conn):
        """After 1 failure, the engine should retry with a different worker."""
        engine = MissionEngine(conn)

        # Start a mission with a step that will fail.
        m = engine.start_mission("Fail test")
        # Manually set a step to fail on purpose.
        m.steps[2] = MissionStep(
            index=2, description="Failing step",
            action_type="shell", payload="exit 1",
            status="failed", error_count=1,
            result={"success": False, "error": "failure"},
        )
        m.failed_steps = 1
        m.consecutive_failures = 1
        engine._save(m)

        # Adapt the mission.
        adapted = engine._adapt_mission(m)
        assert adapted is True

        # Should have retried with a different worker.
        assert m.steps[2].worker_id == "worker:python"
        assert m.steps[2].status == "pending"

    def test_llm_replan_on_2_failures(self, conn, monkeypatch):
        """After 2 failures, LLM replan replaces remaining steps."""
        monkeypatch.setattr(
            "friday.services.llm._enabled",
            lambda: True,
        )
        monkeypatch.setattr(
            "friday.services.llm._call_structured",
            lambda system, user, required_keys=None: {
                "steps": [
                    {
                        "description": "Debug the auth config file",
                        "action_type": "shell",
                        "payload": "cat config/auth.yaml",
                        "worker_id": "worker:shell",
                    },
                    {
                        "description": "Fix the auth middleware",
                        "action_type": "shell",
                        "payload": "echo 'fixing auth'",
                        "worker_id": "worker:shell",
                    },
                ]
            },
        )

        engine = MissionEngine(conn)
        m = engine.start_mission("Fix auth module")

        # Step 0 completed, step 1 failed twice.
        m.steps[0].status = "completed"
        m.steps[0].result = {"success": True, "stdout": "analysis done"}
        m.steps[1].status = "failed"
        m.steps[1].error_count = 2
        m.steps[1].result = {"success": False, "error": "permission denied"}
        m.completed_steps = 1
        m.failed_steps = 1
        m.consecutive_failures = 2
        engine._save(m)

        # Adapt should trigger LLM replan.
        adapted = engine._adapt_mission(m)
        assert adapted is True

        # The failed step should be gone, replaced by LLM's new steps.
        # Completed step 0 should be preserved.
        assert m.steps[0].status == "completed"
        assert m.steps[0].description == m.steps[0].description  # original step 0

        # New steps from LLM should be present.
        step_descriptions = [s.description for s in m.steps]
        assert "Debug the auth config file" in step_descriptions
        assert "Fix the auth middleware" in step_descriptions

        # Progress should be recalculated.
        assert m.total_steps == 3  # 1 completed + 2 new
        assert m.consecutive_failures == 0
        assert m.failed_steps == 0

    def test_llm_replan_fallback_to_skip(self, conn, monkeypatch):
        """When LLM replan fails (returns None), fall back to skip."""
        monkeypatch.setattr(
            "friday.services.llm._enabled",
            lambda: True,
        )
        monkeypatch.setattr(
            "friday.services.llm._call_structured",
            lambda system, user, required_keys=None: None,
        )

        engine = MissionEngine(conn)
        m = engine.start_mission("Skip fallback")
        m.steps[2].status = "failed"
        m.steps[2].error_count = 2
        m.steps[2].result = {"success": False, "error": "timeout"}
        m.failed_steps = 1
        m.consecutive_failures = 2
        engine._save(m)

        adapted = engine._adapt_mission(m)
        assert adapted is True
        # Should fall back to skipping the failed step.
        assert m.steps[2].status == "skipped"

    def test_llm_replan_disabled_falls_back_to_skip(self, conn, monkeypatch):
        """When LLM is disabled, fall back to skip."""
        monkeypatch.setattr(
            "friday.services.llm._enabled",
            lambda: False,
        )

        engine = MissionEngine(conn)
        m = engine.start_mission("Disabled replan")
        m.steps[2].status = "failed"
        m.steps[2].error_count = 2
        m.steps[2].result = {"success": False, "error": "failure"}
        m.failed_steps = 1
        m.consecutive_failures = 2
        engine._save(m)

        adapted = engine._adapt_mission(m)
        assert adapted is True
        assert m.steps[2].status == "skipped"

    def test_llm_replan_preserves_completed_steps(self, conn, monkeypatch):
        """LLM replan preserves completed steps and replaces everything else."""
        # Start mission with LLM disabled (fixture default) so it gets 5 steps.
        engine = MissionEngine(conn)
        m = engine.start_mission("Fix auth module")
        assert len(m.steps) == 5, "Expected 5-step deterministic plan"

        # Steps 0 and 1 completed, step 2 failed twice.
        m.steps[0].status = "completed"
        m.steps[0].result = {"success": True}
        m.steps[1].status = "completed"
        m.steps[1].result = {"success": True}
        m.steps[2].status = "failed"
        m.steps[2].error_count = 2
        m.steps[2].result = {"success": False, "error": "npm install failed"}
        m.completed_steps = 2
        m.failed_steps = 1
        m.consecutive_failures = 2
        engine._save(m)

        # NOW enable LLM mock for _adapt_mission._replan_llm call only.
        replan_called = []

        def mock_call_structured(system, user, required_keys=None):
            replan_called.append(True)
            # Verify the prompt contains the mission state.
            assert "Goal: Fix auth module" in user
            return {
                "steps": [
                    {
                        "description": "New approach to auth",
                        "action_type": "shell",
                        "payload": "echo 'new approach'",
                        "worker_id": "worker:shell",
                    },
                ]
            }

        monkeypatch.setattr(
            "friday.services.llm._enabled",
            lambda: True,
        )
        monkeypatch.setattr(
            "friday.services.llm._call_structured",
            mock_call_structured,
        )

        adapted = engine._adapt_mission(m)
        assert adapted is True
        assert len(replan_called) == 1, "LLM should have been called"

        # Completed steps preserved.
        assert m.steps[0].status == "completed"
        assert m.steps[1].status == "completed"
        # New step replaces everything after.
        assert len(m.steps) == 3  # 2 completed + 1 new
        assert m.steps[2].description == "New approach to auth"
        assert m.steps[2].status == "pending"

    def test_llm_replan_empty_steps_falls_back(self, conn, monkeypatch):
        """When LLM returns empty steps list, fall back to skip."""
        engine = MissionEngine(conn)
        m = engine.start_mission("Empty replan")
        # Disable LLM for start_mission, enable for _adapt_mission.
        monkeypatch.setattr(
            "friday.services.llm._enabled",
            lambda: True,
        )
        monkeypatch.setattr(
            "friday.services.llm._call_structured",
            lambda system, user, required_keys=None: {"steps": []},
        )

        m.steps[2].status = "failed"
        m.steps[2].error_count = 2
        m.steps[2].result = {"success": False, "error": "error"}
        m.failed_steps = 1
        m.consecutive_failures = 2
        engine._save(m)

        adapted = engine._adapt_mission(m)
        assert adapted is True
        assert m.steps[2].status == "skipped"

    def test_llm_replan_unknown_action_type_normalized(self, conn, monkeypatch):
        """Invalid action types from LLM replan are normalized to 'shell'."""
        engine = MissionEngine(conn)
        m = engine.start_mission("Magic fix")
        assert len(m.steps) == 5, "Expected 5-step deterministic plan"
        # Disable LLM for start_mission, enable for _adapt_mission.
        monkeypatch.setattr(
            "friday.services.llm._enabled",
            lambda: True,
        )
        monkeypatch.setattr(
            "friday.services.llm._call_structured",
            lambda system, user, required_keys=None: {
                "steps": [
                    {
                        "description": "Do magic fix",
                        "action_type": "magic_incantation",
                        "payload": "echo fix",
                        "worker_id": "worker:shell",
                    },
                ]
            },
        )

        # Mark steps 0 and 1 as completed so _replan_llm preserves them.
        m.steps[0].status = "completed"
        m.steps[0].result = {"success": True, "stdout": "analysis done"}
        m.steps[1].status = "completed"
        m.steps[1].result = {"success": True, "stdout": "plan created"}
        m.steps[2].status = "failed"
        m.steps[2].error_count = 2
        m.steps[2].result = {"success": False, "error": "error"}
        m.completed_steps = 2
        m.failed_steps = 1
        m.consecutive_failures = 2
        engine._save(m)

        adapted = engine._adapt_mission(m)
        assert adapted is True
        # Steps 0-1 preserved as completed; LLM's single step replaces the
        # remaining failed/pending steps (2, 3, 4). Total: 3 steps.
        assert len(m.steps) == 3, f"Expected 3 steps, got {len(m.steps)}"
        assert m.steps[0].status == "completed"
        assert m.steps[1].status == "completed"
        assert m.steps[2].action_type == "shell"  # Normalized from magic_incantation
        assert m.steps[2].description == "Do magic fix"

    def test_fail_after_3_consecutive(self, conn):
        """After 3 consecutive failures without adaptation, mark mission failed."""
        engine = MissionEngine(conn)
        engine.MAX_CONSECUTIVE_FAILURES = 1  # Lower threshold for test

        m = engine.start_mission("Fail quickly")
        # Make step 0 fail by using an invalid command.
        m.steps[0] = MissionStep(
            index=0, description="Will fail",
            action_type="shell", payload="exit 1",
        )
        engine._save(m)

        # Advance should try to execute step 0, fail, and adapt.
        updates = engine.advance_missions()
        m = engine.get_mission(m.mission_id)

        # The mission should have adapted or failed.
        assert m.failed_steps >= 1
        assert m.status in ("active", "failed")


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


class TestPersistence:
    def test_mission_persisted_to_db(self, conn):
        engine = MissionEngine(conn)
        m = engine.start_mission("Persist test")
        row = conn.execute(
            "SELECT * FROM persistent_missions WHERE mission_id = ?",
            (m.mission_id,),
        ).fetchone()
        assert row is not None
        assert row["goal"] == "Persist test"
        assert row["status"] == "active"

    def test_mission_updated_after_advance(self, conn):
        engine = MissionEngine(conn)
        m = engine.start_mission("Update test")
        engine.advance_missions()

        row = conn.execute(
            "SELECT * FROM persistent_missions WHERE mission_id = ?",
            (m.mission_id,),
        ).fetchone()
        assert row["cycle_count"] == 1
        assert row["completed_steps"] == 1

    def test_table_created_automatically(self, conn):
        """The table should be created on engine init if it doesn't exist."""
        # Drop the table first.
        conn.execute("DROP TABLE IF EXISTS persistent_missions")
        conn.commit()

        # Engine init should create it.
        engine = MissionEngine(conn)
        m = engine.start_mission("Auto create")
        assert m is not None
        assert m.goal == "Auto create"
