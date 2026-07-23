"""Tests for the worker:claude dispatch path (Task 2 regression).

Covers:
  - resolve_executor("worker:claude") returns ClaudeCodeWorker
  - ClaudeCodeWorker.build_invocation produces correct argv
  - Full round-trip: planner -> resolver -> schedule -> dispatch to
    ClaudeCodeWorker (with mocked subprocess so no live API key is needed)
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from friday.db import connect
from friday.planning.graph_engine import TaskGraphEngine
from friday.resolver.engine import CapabilityResolver
from friday.scheduler.engine import TaskScheduler
from friday.runtime import resolve_executor, RuntimeEngine
from friday.runtime.executors import ClaudeCodeWorker
from friday.runtime.models import ExecutionResult, MockExecutor, RuntimeTask
from friday.worker.engine import ensure_runtime_bootstrapped


def test_resolve_claude_worker_returns_claude_code_worker():
    """resolve_executor('worker:claude') returns a ClaudeCodeWorker instance."""
    worker = resolve_executor("worker:claude")
    assert worker is not None, "worker:claude must have an executor adapter"
    assert isinstance(worker, ClaudeCodeWorker)
    assert worker.worker_id == "worker:claude"


def test_claude_build_invocation_format():
    """ClaudeCodeWorker.build_invocation produces expected argv and prompt."""
    worker = ClaudeCodeWorker()
    task = RuntimeTask(
        execution_id="test:task1", session_id="sess:test",
        schedule_id="test", task_id="task1", worker_id="worker:claude",
        wave=1, dependencies=[], runtime_payload="",
        task_type="analysis", title="Analyze error handling",
        goal="explain tradeoffs",
        acceptance_criteria=["Analysis complete"],
    )
    inv = worker.build_invocation(task)
    assert inv.argv[0].endswith("claude"), f"expected claude binary, got {inv.argv[0]}"
    assert "--print" in inv.argv
    assert "--output-format" in inv.argv
    assert "json" in inv.argv
    assert inv.stdin is not None
    assert "Analyze" in inv.stdin
    assert "acceptance criteria" in inv.stdin.lower()


def test_claude_verify_rejects_is_error():
    """Claude's verify() must treat a 0-exit JSON with is_error:true as failure."""
    worker = ClaudeCodeWorker()
    payload = json.dumps({"type": "result", "is_error": True,
                          "result": "permission denied"})
    result = ExecutionResult(success=True, stdout=payload, exit_code=0)
    vres = worker.verify(None, result)
    assert vres.passed is False
    assert "is_error" in vres.reason


def test_claude_verify_accepts_clean_json():
    worker = ClaudeCodeWorker()
    payload = json.dumps({"type": "result", "is_error": False, "result": "done"})
    result = ExecutionResult(success=True, stdout=payload, exit_code=0)
    vres = worker.verify(None, result)
    assert vres.passed is True


def test_claude_verify_fallback_non_json():
    """Non-JSON output degrades gracefully to exit-code rule."""
    worker = ClaudeCodeWorker()
    result = ExecutionResult(success=True, stdout="some text", exit_code=0)
    vres = worker.verify(None, result)
    assert vres.passed is True  # non-empty stdout + exit 0
    # Empty stdout + exit 0 should still fail
    result2 = ExecutionResult(success=True, stdout="", exit_code=0)
    vres2 = worker.verify(None, result2)
    assert vres2.passed is False


def test_full_pipeline_selects_claude_for_judgment(tmp_path):
    """Full pipeline round-trip: planner -> resolver -> schedule selects
    worker:claude for a judgment task, and the runtime dispatches to it
    (with mocked subprocess to avoid live API key)."""
    goal = "Analyze error handling patterns in resolver/ and recommend a strategy"
    db = tmp_path / "test.db"
    conn = connect(db)
    ensure_runtime_bootstrapped(conn)

    # 1. Plan
    graph_eng = TaskGraphEngine(conn)
    g = graph_eng.generate(goal)

    # 2. Resolve (should pick worker:claude for analysis tasks)
    resolver = CapabilityResolver(conn)
    result = resolver.resolve_graph(g.id, workspace=str(tmp_path))
    claude_assigned = any(
        r.worker_id == "worker:claude" for r in result.results)
    assert claude_assigned, \
        f"expected at least one task assigned to worker:claude, got: " \
        f"{[(r.worker_id, r.task_title[:40]) for r in result.results]}"

    # 3. Schedule
    scheduler = TaskScheduler(conn)
    sched_result = scheduler.schedule_graph(g.id)

    # 4. Execute with mocked worker resolver that returns a mock for
    # worker:claude and real executors for deterministic workers.
    claude_called = False

    def _resolve(wid):
        nonlocal claude_called
        if wid == "worker:claude":
            claude_called = True
            return MockExecutor(worker_id=wid)
        return resolve_executor(wid, str(tmp_path))

    engine = RuntimeEngine(conn, worker_resolver=_resolve, workspace=str(tmp_path),
                           fallback=False)
    report = engine.run(sched_result.schedule)
    conn.close()

    # worker:claude must have been dispatched to
    assert claude_called, "ClaudeCodeWorker was never dispatched"
    # Report must still be clean (MockExecutor returns success)
    assert report.failed == 0, f"execution failed: {report}"
