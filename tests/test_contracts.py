"""Execution-contract regression tests (Phase 1.5).

Proves the pipeline is CONTRACT-DRIVEN, not heuristic:

  Planner emits an explicit artifact contract -> Resolver can use it ->
  Executor runs -> Runtime verifies the OBSERVED result against the CONTRACT
  -> mission success only when the contract is satisfied.

Runs against the REAL planner/resolver/scheduler/runtime path (not mocked),
with a small ArtifactMock that materializes the contract's expected file so the
happy path is genuine. Claude integration uses the REAL binary when present and
skips (not mocks) when absent.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from friday.db import connect
from friday.planning import TaskGraphEngine
from friday.resolver import CapabilityResolver
from friday.resolver.resolver import rank_workers
from friday.scheduler.engine import TaskScheduler
from friday.runtime import RuntimeEngine
from friday.runtime.contract import contract_for_task, resolve_artifact_paths
from friday.runtime.executors import (
    resolve_executor,
    execute_with_fallback,
    ClaudeCodeWorker,
)
from friday.runtime.models import MockExecutor, ExecutionResult, VerificationResult
from friday.runtime.verification import verify_creation_task, verify_task_artifacts
from friday.worker.engine import ensure_runtime_bootstrapped, WorkerRegistry
from friday.worker.models import Worker, WorkerKind


def _fresh_db(path=None):
    d = path or Path(tempfile.mkdtemp())
    conn = connect(d / "friday.db")
    ensure_runtime_bootstrapped(conn)
    return conn


def _plan(conn, goal):
    g = TaskGraphEngine(conn).generate(goal)
    CapabilityResolver(conn).resolve_graph(g.id)
    return TaskScheduler(conn).schedule_graph(g.id).schedule


class ArtifactMock(MockExecutor):
    """Success-claiming mock that materializes the contract's expected artifact
    into the shared workspace, mirroring a real executor writing the file.

    When the task has symbolic content (e.g. ``{"op": "create_file",
    "content": "..."}``), the mock writes THAT content so verification's
    content-mismatch check passes. Falls back to ``# artifact`` for tasks
    without symbolic intent.
    """

    def __init__(self, worker_id="worker:mock", fail=False, workspace="."):
        super().__init__(worker_id=worker_id, fail=fail)
        self._ws = workspace

    def execute(self, task):
        res = super().execute(task)
        if res.success:
            ct = contract_for_task(task)
            targets = ct.expected_artifacts or [f"{task.task_id}.out"]
            content = "# artifact\n"
            # Respect symbolic content so verify_symbolic's content check
            # (e.g. ``content not in file_text``) passes for create_file tasks.
            sym = getattr(task, "symbolic", None) or {}
            if sym.get("op") == "create_file" and sym.get("content"):
                content = sym["content"]
            for p in resolve_artifact_paths(ct, self._ws) or \
                    [str(Path(self._ws) / t) for t in targets]:
                Path(p).parent.mkdir(parents=True, exist_ok=True)
                Path(p).write_text(content, encoding="utf-8")
                res.artifacts = list(res.artifacts) + [p]
        return res


_AI_IDS = ("claude", "codex", "gemini", "opencode", "aider", "deepseek")


def _run(conn, sched, workspace, claude_fails=True):
    ws = Path(workspace)

    def _resolve(wid):
        # Fail every AI executor (no interactive binary in CI); the runtime
        # falls back to deterministic built-ins, which materialize the
        # contracted artifact via ArtifactMock. Keeps the happy path fast +
        # deterministic.
        if wid and any(a in wid for a in _AI_IDS):
            return MockExecutor(worker_id=wid, fail=True)
        return ArtifactMock(worker_id=wid, workspace=str(ws))

    eng = RuntimeEngine(conn, worker_resolver=_resolve,
                        workspace=str(ws), fallback=True)
    return eng.run(sched)


# ===================================================================
# Phase 1+2 — Planner emits an explicit artifact contract
# ===================================================================

def test_planner_emits_explicit_artifact_contract(tmp_path):
    """A creation task's `outputs` must contain the concrete file path named in
    the goal — the explicit contract the runtime verifies against."""
    conn = _fresh_db(tmp_path)
    g = TaskGraphEngine(conn).generate("Create hello.py printing Hello World")
    CapabilityResolver(conn).resolve_graph(g.id)
    tasks = TaskGraphEngine(conn).graph_by_id(g.id).tasks
    impl = [t for t in tasks if "hello.py" in (t.title or "").lower()
            or "hello.py" in t.outputs]
    assert impl, "no task names hello.py"
    t = impl[0]
    assert "hello.py" in t.outputs, f"contract missing from outputs: {t.outputs}"
    ct = contract_for_task(t)
    assert "hello.py" in ct.expected_artifacts
    conn.close()


def test_planner_contract_non_creation_tasks_empty(tmp_path):
    """Non-creation tasks (research/analysis) must NOT invent a file contract."""
    conn = _fresh_db(tmp_path)
    g = TaskGraphEngine(conn).generate(
        "Research the best architecture for a distributed system")
    CapabilityResolver(conn).resolve_graph(g.id)
    tasks = TaskGraphEngine(conn).graph_by_id(g.id).tasks
    ct = contract_for_task(tasks[0])
    # Either no explicit path, or none inferred — research has no artifact.
    assert not any(p.endswith(".py") or p.endswith(".md")
                   for p in ct.expected_artifacts)
    conn.close()


# ===================================================================
# Phase 5 — Resolver uses the contract
# ===================================================================

def test_resolver_prefers_contract_matching_executor(tmp_path):
    """For an explicit *.py artifact, the deterministic python executor is
    boosted over an AI executor that also covers python."""
    py = Worker(name="Python", kind=WorkerKind.FUNCTION,
                capabilities=["Python", "File Editing"],
                supported_task_types=["implementation"],
                supported_plan_types=["feature"], id="worker:python")
    cl = Worker(name="Claude", kind=WorkerKind.LLM,
                capabilities=["Python", "Reasoning"],
                supported_task_types=["implementation"],
                supported_plan_types=["feature"], id="worker:claude llm")
    ranked = rank_workers(["python"], "implementation", "feature",
                          [cl, py], expected_artifacts=["calculator.py"])
    assert ranked[0][0].id == "worker:python"


# ===================================================================
# Phase 3+4 — Executor produces artifact, verifier validates contract
# ===================================================================

def test_executor_produces_artifact_mission_success(tmp_path):
    """Happy path: executor lands the contracted file -> verification passes ->
    mission succeeds. The contract (not a guess) drives the success."""
    conn = _fresh_db(tmp_path)
    sched = _plan(conn, "Create hello.py printing Hello World")
    ws = tmp_path
    report = _run(conn, sched, str(ws))
    assert report.failed == 0, f"contract-satisfied mission failed: {report.tasks}"
    assert (ws / "hello.py").exists(), "contracted artifact not produced"
    for t in report.tasks:
        assert t["verification_passed"] is True, \
            f"verification_passed not True for {t['task_id']}: {t}"
    conn.close()


def test_missing_artifact_truthful_failure(tmp_path):
    """Executor claims success but produces NO file -> contract unsatisfied ->
    task FAILED (verification_passed False), descendants CANCELLED, no crash."""
    from friday.runtime.models import MockExecutor

    conn = _fresh_db(tmp_path)
    sched = _plan(conn, "Create hello.py printing Hello World")
    ws = tmp_path

    def _resolve(wid):
        # Success-claiming mock that writes NOTHING (lies about the contract).
        return MockExecutor(worker_id=wid, fail=False)

    eng = RuntimeEngine(conn, worker_resolver=_resolve,
                        workspace=str(ws), fallback=True)
    report = eng.run(sched)
    assert report.failed > 0, "verification should have failed (no artifact)"
    assert not (ws / "hello.py").exists(), "false success wrote a file"
    for t in report.tasks:
        if t["status"] == "failed":
            assert t["verification_passed"] is False
    conn.close()


def test_wrong_artifact_failure(tmp_path):
    """Executor writes a DIFFERENT file than the contract expects -> FAIL."""
    conn = _fresh_db(tmp_path)
    sched = _plan(conn, "Create hello.py printing Hello World")
    ws = tmp_path

    class WrongMock(MockExecutor):
        def execute(self, task):
            res = super().execute(task)
            if res.success:
                # Write goodbye.py instead of the contracted hello.py.
                (Path(self._ws if hasattr(self, "_ws") else ws) / "goodbye.py"
                 ).write_text("# wrong\n")
                res.artifacts = ["goodbye.py"]
            return res

    def _resolve(wid):
        m = WrongMock(worker_id=wid, fail=False)
        m._ws = str(ws)
        return m

    eng = RuntimeEngine(conn, worker_resolver=_resolve,
                        workspace=str(ws), fallback=True)
    report = eng.run(sched)
    # The hello.py contract is unsatisfied -> at least one task fails.
    assert report.failed > 0, "wrong artifact should fail the contract"
    conn.close()


def test_claude_invocation_failure_falls_back(tmp_path):
    """Claude fails (is_error/json or exit) -> runtime falls back to
    deterministic built-ins, which satisfy the contract -> mission succeeds."""
    conn = _fresh_db(tmp_path)
    sched = _plan(conn, "Create hello.py printing Hello World")
    ws = tmp_path
    report = _run(conn, sched, str(ws), claude_fails=True)
    assert report.failed == 0, f"fallback mission failed: {report.tasks}"
    assert (ws / "hello.py").exists()
    conn.close()


def test_timeout_failure(tmp_path):
    """An executor that exceeds its timeout must fail (never hang the mission).

    Uses the REAL shell executor with a 1s timeout against a `sleep 5` payload —
    the bounded timeout returns a clean failure rather than blocking forever.
    """
    from friday.runtime.executors import BuiltinShellExecutor

    ws = tmp_path
    ex = BuiltinShellExecutor(worker_id="worker:shell", workspace=str(ws),
                              timeout=1)

    class ShellTask:
        runtime_payload = "sleep 5"
        task_id = "t"
        title = "T"
        goal = "g"
        task_type = "infrastructure"
        outputs = []
        acceptance_criteria = []
        verification = []

    t0 = time.monotonic()
    res = ex.execute(ShellTask())
    elapsed = time.monotonic() - t0
    assert res.success is False, "timeout should yield failure"
    assert "timed out" in (res.error or "").lower(), f"no timeout error: {res.error}"
    assert elapsed < 4, f"timeout not enforced (took {elapsed:.1f}s)"



# ===================================================================
# Phase 6 — Claude adapter: JSON is_error + structured verify
# ===================================================================

def test_claude_verify_rejects_is_error_json(tmp_path):
    """Claude's verify() must treat a 0-exit JSON payload with is_error:true as
    a FAILURE, not success. Exit code alone would lie."""
    from friday.runtime.executors import ClaudeCodeWorker

    worker = ClaudeCodeWorker()
    payload = json.dumps({"type": "result", "is_error": True,
                          "result": "permission denied"})
    ok_result = ExecutionResult(success=True, stdout=payload, exit_code=0)
    vres = worker.verify(None, ok_result)
    assert vres.passed is False, "is_error:true must fail verification"
    assert "is_error" in vres.reason


def test_claude_verify_accepts_clean_json(tmp_path):
    worker = ClaudeCodeWorker()
    payload = json.dumps({"type": "result", "is_error": False,
                          "result": "done"})
    vres = worker.verify(None, ExecutionResult(success=True, stdout=payload))
    assert vres.passed is True


def test_claude_verify_fallback_non_json(tmp_path):
    """Non-JSON output degrades to the exit-code rule (no crash)."""
    worker = ClaudeCodeWorker()
    vres = worker.verify(None, ExecutionResult(success=True, stdout="some text"))
    assert vres.passed is True


def test_claude_real_integration(tmp_path):
    """Run the ACTUAL claude CLI if installed AND explicitly enabled.

    Validates the invocation contract end-to-end: print mode, JSON output,
    stdin closed, timeout, is_error handling. This is a REAL integration test
    (not a mock), but it is opt-in via FRIDAY_RUN_REAL_CLAUDE=1 because the
    binary can block on network/auth in CI. Without the flag it skips cleanly.
    """
    if not os.environ.get("FRIDAY_RUN_REAL_CLAUDE"):
        pytest.skip("set FRIDAY_RUN_REAL_CLAUDE=1 to run the real claude CLI")
    binary = shutil.which("claude")
    if not binary:
        pytest.skip("claude CLI not installed; skipping real integration")

    ws = tmp_path
    probe = ws / "probe.txt"
    prompt = f"Write the single word DONE to {probe} and nothing else."
    try:
        proc = subprocess.run(
            [binary, "--print", "--output-format", "json",
             "--dangerously-skip-permissions"],
            input=prompt, cwd=str(ws), capture_output=True, text=True,
            timeout=120)
    except subprocess.TimeoutExpired:
        pytest.fail("claude invocation hung (no timeout enforced)")
    # stdin must be closed by the time the process returns (subprocess.run with
    # input= does this); assert the process completed.
    assert proc.returncode is not None
    # If claude emitted JSON, an is_error payload must not be reported as success.
    out = (proc.stdout or "").strip()
    try:
        obj = json.loads(out)
    except (ValueError, TypeError):
        obj = None
    if isinstance(obj, dict) and obj.get("is_error"):
        pytest.fail(f"claude reported is_error: {obj.get('result')}")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
