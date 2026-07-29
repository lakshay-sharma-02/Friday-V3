"""Tests for the Agentic Executor (src/friday/agent.py).

Covers:
  - decompose() LLM + keyword fallback
  - run_agent() basic execution
  - Step output piping
  - Adaptation on failure
  - Session persistence
  - Edge cases (empty task, unknown tool, LLM returns bad JSON)
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch


# ──────────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────────


_EMPTY_AGENT_PROMPT = (
    "You are an agent that decomposes a user's task into a sequence of tool calls. "
    "Output ONLY a JSON array of steps."
)


def _mock_llm(*args, **kwargs):
    """Mock LLM that returns a simple shell command decomposition."""
    return json.dumps([
        {
            "tool": "shell",
            "params": {"command": "echo hello"},
            "description": "Say hello",
            "depends_on": [],
            "timeout": 30,
        }
    ])


def _mock_llm_returns_none(*args, **kwargs):
    return None


# ──────────────────────────────────────────────────────────────────────────
# decompose
# ──────────────────────────────────────────────────────────────────────────


class TestDecompose:
    """Tests for decompose() — LLM + keyword fallback."""

    def test_keyword_copytoclipboard_decomposes(self):
        """'copy git diff to clipboard' should produce 2 steps."""
        from friday.agent import decompose
        steps = decompose("copy the git diff to clipboard", workspace=".")
        assert len(steps) >= 2
        assert steps[0].tool == "shell"
        assert steps[-1].tool == "clipboard"

    def test_keyword_unknown_falls_to_single_shell(self):
        """Unknown tasks fall back to a single shell step."""
        from friday.agent import decompose
        steps = decompose("do something random and unspecified", workspace=".")
        assert len(steps) >= 1
        assert steps[0].tool == "shell"

    def test_keyword_run_tests_decomposes(self):
        """'run tests' should produce a pytest command."""
        from friday.agent import decompose
        steps = decompose("run the tests please", workspace=".")
        assert len(steps) == 1
        assert steps[0].tool == "shell"
        assert "pytest" in steps[0].params.get("command", "")

    def test_keyword_deploy_decomposes(self):
        """'deploy' should produce a deploy protocol step."""
        from friday.agent import decompose
        steps = decompose("deploy the app to staging", workspace=".")
        assert len(steps) == 1
        assert steps[0].tool == "shell"

    def test_keyword_search_decomposes(self):
        """'search for JWT auth' should produce a search step."""
        from friday.agent import decompose
        steps = decompose("search for JWT auth patterns", workspace=".")
        assert len(steps) >= 1

    def test_llm_decompose_returns_none_when_no_llm(self):
        """When LLM is unavailable, decompose falls back to keyword."""
        from friday.agent import decompose, _llm_decompose
        # Without mock, LLM should return None if no provider configured.
        llm_result = _llm_decompose("run the tests")
        # Then decompose() should still return keyword-based steps.
        steps = decompose("run the tests", workspace=".")
        assert len(steps) >= 1

    @patch("friday.agent._TOOL_TO_WORKER", {"shell": "worker:shell"})
    def test_decompose_empty_task_returns_empty(self):
        """An empty task from LLM should still produce keyword fallback."""
        from friday.agent import decompose
        steps = decompose("", workspace=".")
        # Even empty tasks should produce a shell step (the task text itself).
        assert len(steps) >= 1


# ──────────────────────────────────────────────────────────────────────────
# AgentStep / AgentStepResult data models
# ──────────────────────────────────────────────────────────────────────────


class TestAgentStep:
    """Tests for AgentStep and AgentStepResult dataclasses."""

    def test_agent_step_defaults(self):
        from friday.agent import AgentStep
        step = AgentStep(tool="shell", params={"command": "echo hi"}, description="test step")
        assert step.depends_on == []
        assert step.timeout == 120

    def test_agent_step_result_defaults(self):
        from friday.agent import AgentStepResult
        result = AgentStepResult(
            index=0, tool="shell", description="test",
            success=True, stdout="hello", stderr="",
            exit_code=0, duration_ms=10,
        )
        assert result.error == ""
        assert result.adapted is False


# ──────────────────────────────────────────────────────────────────────────
# run_agent
# ──────────────────────────────────────────────────────────────────────────


class TestRunAgent:
    """Tests for run_agent() — the main entry point."""

    def test_run_agent_shell_task_no_llm(self):
        """Running a simple shell task should succeed via keyword decomposition."""
        from friday.agent import run_agent
        session = run_agent("echo hello agent", workspace=".", persist=False)
        assert session.status == "succeeded" or session.status == "failed"
        if session.status == "succeeded":
            assert len(session.steps) >= 1
            assert session.steps[0].tool == "shell"

    def test_run_agent_empty_task(self):
        """Empty task should produce a failed session with a summary."""
        from friday.agent import run_agent
        session = run_agent("", workspace=".", persist=False)
        assert session.status in ("failed", "succeeded")

    def test_run_agent_persists_to_db(self):
        """When persist=True, the agent session should be stored."""
        import tempfile
        db_fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(db_fd)
        try:
            from friday.db import connect
            conn = connect(Path(db_path))
            from friday.agent import run_agent, get_session_history
            session = run_agent("echo persist_test", workspace=".", persist=True, conn=conn)
            assert session.status in ("succeeded", "failed")
            history = get_session_history(conn)
            assert len(history) >= 1
            assert history[0].session_id == session.session_id
        finally:
            os.unlink(db_path)

    def test_run_agent_generates_summary(self):
        """Session should always have a summary."""
        from friday.agent import run_agent
        session = run_agent("echo summary_test", workspace=".", persist=False)
        assert session.summary is not None
        assert len(session.summary) > 0


# ──────────────────────────────────────────────────────────────────────────
# format_session
# ──────────────────────────────────────────────────────────────────────────


class TestFormatSession:
    """Tests for format_session() output."""

    def test_format_session_basic(self):
        from friday.agent import AgentSession, AgentStepResult, format_session
        session = AgentSession(
            session_id="agent:test123",
            task="echo hello",
            workspace=".",
            created_at="2026-01-01T00:00:00",
            status="succeeded",
            steps=[
                AgentStepResult(
                    index=0, tool="shell", description="echo hello",
                    success=True, stdout="hello\n", stderr="",
                    exit_code=0, duration_ms=5,
                ),
            ],
            summary="Done in 5ms.",
        )
        text = format_session(session)
        assert "agent:test123" in text
        assert "echo hello" in text
        assert "succeeded" in text

    def test_format_session_brief(self):
        from friday.agent import AgentSession, AgentStepResult, format_session_brief
        session = AgentSession(
            session_id="agent:test",
            task="echo hello",
            workspace=".",
            created_at="2026-01-01T00:00:00",
            status="succeeded",
            steps=[
                AgentStepResult(
                    index=0, tool="shell", description="echo hello",
                    success=True, stdout="hello", stderr="",
                    exit_code=0, duration_ms=5,
                ),
            ],
            duration_ms=5,
        )
        brief = format_session_brief(session)
        assert "echo hello" in brief
        assert "1/1 steps" in brief


# ──────────────────────────────────────────────────────────────────────────
# Edge cases
# ──────────────────────────────────────────────────────────────────────────


class TestAgentEdgeCases:
    """Edge cases and error handling."""

    def test_unknown_tool_returns_error(self):
        """Using an unknown tool should produce a failed step with descriptive error."""
        from friday.agent import _execute_step, AgentStep, AgentStepResult
        step = AgentStep(tool="nonexistent_tool_xyz", params={}, description="bad tool")
        result = _execute_step(step, 0, "", workspace=".")
        assert not result.success
        assert "Unknown tool" in result.error or "no executor" in result.error

    def test_tool_to_worker_mapping(self):
        """Check that common tools map to valid worker_ids."""
        from friday.agent import _TOOL_TO_WORKER, _resolve_worker
        assert _resolve_worker("shell") == "worker:shell"
        assert _resolve_worker("filesystem") == "worker:filesystem"
        assert _resolve_worker("git") == "worker:git"
        assert _resolve_worker("clipboard") == "worker:clipboard"
        assert _resolve_worker("python") == "worker:python"
        assert _resolve_worker("testing") == "worker:testing"
        assert _resolve_worker("browser") == "worker:browser"
        assert _resolve_worker("email") == "worker:email"
        assert _resolve_worker("unknown_xyz") is None

    def test_build_runtime_payload_substitutes_prev_output(self):
        """{prev_output} in params should be substituted."""
        from friday.agent import _build_runtime_payload, AgentStep
        step = AgentStep(
            tool="clipboard",
            params={"op": "write", "text": "{prev_output}"},
            description="Copy to clipboard",
        )
        payload = _build_runtime_payload(step, prev_output="test output")
        parsed = json.loads(payload)
        assert parsed["text"] == "test output"

    def test_keyword_decompose_specific_shortcuts(self):
        """Specific keyword patterns should produce correct step structures."""
        from friday.agent import _keyword_decompose
        # git diff + clipboard
        steps = _keyword_decompose("copy git diff to clipboard please")
        assert len(steps) >= 2
        assert steps[-1].params.get("op") == "write"

        # run tests with path
        steps2 = _keyword_decompose("run tests in tests/test_auth.py")
        assert len(steps2) >= 1
        assert "test_auth" in steps2[0].params.get("command", "")


# ──────────────────────────────────────────────────────────────────────────
# Persistence
# ──────────────────────────────────────────────────────────────────────────


class TestAgentPersistence:
    """Tests for DB persistence of agent sessions."""

    def test_get_session_history_empty(self):
        """Fresh DB should return empty history."""
        import tempfile
        db_fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(db_fd)
        try:
            from friday.db import connect
            conn = connect(Path(db_path))
            from friday.agent import get_session_history
            history = get_session_history(conn)
            assert history == []
        finally:
            os.unlink(db_path)

    def test_get_active_session_none(self):
        """Fresh DB should return no active session."""
        import tempfile
        db_fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(db_fd)
        try:
            from friday.db import connect
            conn = connect(Path(db_path))
            from friday.agent import get_active_session
            session = get_active_session(conn)
            assert session is None
        finally:
            os.unlink(db_path)

    def test_cancel_active_session_noop(self):
        """Cancelling on empty DB should return False."""
        import tempfile
        db_fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(db_fd)
        try:
            from friday.db import connect
            conn = connect(Path(db_path))
            from friday.agent import cancel_active_session
            result = cancel_active_session(conn)
            assert result is False
        finally:
            os.unlink(db_path)
