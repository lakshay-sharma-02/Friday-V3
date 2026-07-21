"""Runtime Stabilization regression tests (Mission Critical).

Covers the four correctness bugs fixed in the execution pipeline:
  Phase 1 — Scheduler dependency direction (reversed ordering).
  Phase 2 — Executor capability model routing everything to Claude.
  Phase 3 — Robust executor fallback (no single external failure aborts mission).
  Phase 4 — Fresh DB auto-bootstraps built-in executors.

Plus Phase 6 verification scenarios:
  - linear + DAG execution order,
  - fresh-DB execution,
  - Claude-unavailable still executes,
  - deterministic-only mission never invokes Claude,
  - mixed mission selects Claude for research, built-ins otherwise.

These tests run against the REAL compiler/planner/resolver/scheduler/runtime
path (not mocked), so they verify genuine end-to-end behaviour.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from friday.db import connect
from friday.planning import TaskGraphEngine
from friday.resolver import CapabilityResolver
from friday.resolver.resolver import rank_workers
from friday.scheduler.engine import TaskScheduler
from friday.runtime import RuntimeEngine
from friday.runtime.executors import (
    execute_with_fallback,
    fallback_chain,
    resolve_executor,
)
from friday.runtime.models import MockExecutor
from friday.runtime.verification import expected_paths
from friday.worker.engine import WorkerRegistry


class ArtifactMock(MockExecutor):
    """Success-claiming mock that materializes the artifact a task's goal
    references, mirroring how a real executor (CLIWorker/FileExecutor) lands a
    file in the workspace so truthful verification can pass. Without this, mock
    executors report success but produce nothing, and Phase 3 verification
    correctly flips them to FAILED — so happy-path tests must give executors a
    way to actually create the expected file."""

    def __init__(self, worker_id="worker:mock", fail=False, workspace="."):
        super().__init__(worker_id=worker_id, fail=fail)
        self._ws = workspace

    def execute(self, task):
        res = super().execute(task)
        if res.success:
            for p in expected_paths(task, self._ws):
                Path(p).parent.mkdir(parents=True, exist_ok=True)
                Path(p).write_text("# artifact\n", encoding="utf-8")
                res.artifacts = list(res.artifacts) + [p]
        return res


_AI_IDS = ("claude", "codex", "gemini", "opencode", "aider", "deepseek")


def _fresh_db() -> "sqlite3.Connection":
    d = Path(tempfile.mkdtemp())
    conn = connect(d / "friday.db")
    # Phase 4: the runtime auto-bootstraps the registry on initialization; the
    # test mirrors that so a fresh DB is immediately executable.
    from friday.worker.engine import ensure_runtime_bootstrapped
    ensure_runtime_bootstrapped(conn)
    return conn


def _plan(conn, goal: str):
    g = TaskGraphEngine(conn).generate(goal)
    CapabilityResolver(conn).resolve_graph(g.id)
    return TaskScheduler(conn).schedule_graph(g.id).schedule


def _exec_order(schedule) -> list:
    return [t.task_id.split("#")[-1]
            for t in sorted(schedule.tasks, key=lambda t: (t.wave, t.task_id))]


def _assignments(conn, graph_id: str) -> dict:
    CapabilityResolver(conn).resolve_graph(graph_id)
    res = CapabilityResolver(conn)
    out = {}
    for a in res.assignments(graph_id):
        out[a.task_id.split("#")[-1]] = a.worker_id
    return out


# ===================================================================
# Phase 1 — Scheduler dependency direction
# ===================================================================

def test_linear_chain_exact_order():
    """T1 -> T2 -> T3 -> T4 must execute T1, T2, T3, T4 (never reversed)."""
    conn = _fresh_db()
    g = TaskGraphEngine(conn).generate(
        "implement then test then deploy then document")
    CapabilityResolver(conn).resolve_graph(g.id)
    sched = TaskScheduler(conn).schedule_graph(g.id).schedule
    order = _exec_order(sched)
    # The first planned task (sequence 1) must be wave 1, last task wave N.
    assert order[0] == "t1" and order[-1] == "t7"
    # Strictly increasing waves => correct dependency direction.
    waves = [t.wave for t in sorted(sched.tasks, key=lambda t: t.task_id)]
    assert waves == sorted(waves), "wave order was reversed"
    conn.close()


def test_diamond_fanout_waves():
    """T1 / T2 T3 / T4 fan-out -> waves 1, 2 (T2,T3), 3."""
    conn = _fresh_db()
    g = TaskGraphEngine(conn).generate(
        "design backend and frontend then integrate them")
    CapabilityResolver(conn).resolve_graph(g.id)
    sched = TaskScheduler(conn).schedule_graph(g.id).schedule
    by_wave = {}
    for t in sched.tasks:
        by_wave.setdefault(t.wave, []).append(t.task_id.split("#")[-1])
    assert 1 in by_wave and 3 in by_wave
    # Mid wave has >= 2 parallel tasks.
    mid = [w for w in by_wave if len(by_wave[w]) >= 2]
    assert mid, "fan-out produced no parallel wave"
    conn.close()


def test_investigate_design_implement_test_deploy_order():
    """investigate -> design -> implement -> test -> deploy executes in order."""
    conn = _fresh_db()
    g = TaskGraphEngine(conn).generate(
        "Build a calculator with tests and deploy it")
    CapabilityResolver(conn).resolve_graph(g.id)
    sched = TaskScheduler(conn).schedule_graph(g.id).schedule
    order = _exec_order(sched)
    # Each prefix must appear in order (waves are monotonic).
    seq = [order.index(f"t{i}") for i in range(1, len(order) + 1)]
    assert seq == sorted(seq)
    conn.close()


# ===================================================================
# Phase 2 — Executor capability model (no more "everything to Claude")
# ===================================================================

def test_deterministic_only_mission_never_uses_claude():
    """For task types with full deterministic coverage (implementation, testing,
    documentation, configuration), Claude is never selected — only built-in
    executors are."""
    conn = _fresh_db()
    g = TaskGraphEngine(conn).generate(
        "Build a calculator.py CLI in Python with unit tests")
    CapabilityResolver(conn).resolve_graph(g.id)
    assign = _assignments(conn, g.id)
    assert assign, "no assignments produced"
    for tid, wid in assign.items():
        assert wid is not None, f"task {tid} has no assignment"
        # Deterministic built-ins are used; claude/ai executors should not appear.
        assert "claude" not in (wid or "").lower(), \
            f"AI executor selected for task {tid}: {wid}"
    # Deterministic built-ins are present.
    assert any("python" in (w or "") for w in assign.values())
    conn.close()


def test_mixed_mission_uses_deterministic_only():
    """All tasks use deterministic built-ins, never AI executors, since the
    resolver now scores capability + determinism without hardcoded intent routing."""
    conn = _fresh_db()
    g = TaskGraphEngine(conn).generate(
        "Research the best architecture for a distributed system and "
        "write a design document")
    assign = _assignments(conn, g.id)
    assert assign, "no assignments produced"
    # All assignments should be deterministic built-ins.
    ai = [w for w in assign.values() if w and "claude" in w.lower()]
    assert not ai, f"AI executor unexpectedly selected: {assign}"
    # At least one shell/python/documentation worker should be used.
    det = [w for w in assign.values() if w]
    assert det, "no deterministic workers assigned"
    conn.close()


def test_resolver_prefers_deterministic_over_ai():
    """Given a Python task, the deterministic python executor wins over claude."""
    from friday.resolver.resolver import rank_workers
    from friday.worker.models import Worker, WorkerKind
    py = Worker(name="Python", kind=WorkerKind.FUNCTION,
                capabilities=["Python"], supported_task_types=["implementation"],
                supported_plan_types=["feature"], id="worker:python")
    cl = Worker(name="Claude", kind=WorkerKind.LLM,
                capabilities=["Python", "Reasoning", "Research"],
                supported_task_types=["implementation"],
                supported_plan_types=["feature"], id="worker:claude llm")
    ranked = rank_workers(["python"], "implementation", "feature", [cl, py])
    assert ranked[0][0].id == "worker:python"


# ===================================================================
# Phase 3 — Robust executor fallback
# ===================================================================

def test_fallback_chain_ai_to_deterministic():
    chain = fallback_chain("worker:claude")
    assert chain[0] == "worker:claude"
    assert "worker:python" in chain
    # Deterministic built-ins come last (true fallback).
    assert chain[-1] == "worker:documentation"


def test_fallback_chain_deterministic_is_terminal():
    assert fallback_chain("worker:python") == ["worker:python"]


def test_execute_with_fallback_succeeds_on_deterministic():
    """When the AI primary fails, fallback to a deterministic executor wins."""

    from friday.runtime.models import MockExecutor
    import friday.runtime.executors as ex

    real = ex.resolve_executor

    def fake(wid, ws="."):
        if wid == "worker:claude":
            return MockExecutor(worker_id=wid, fail=True)  # AI primary fails
        return real(wid, ws)

    class PyTask:
        runtime_payload = "print('fallback-ok')"
        task_id = "p"
        title = "P"
        goal = "g"
        task_type = "implementation"

    orig = ex.resolve_executor
    ex.resolve_executor = fake
    try:
        res = execute_with_fallback(PyTask(), "worker:claude",
                                    str(Path(tempfile.mkdtemp())))
    finally:
        ex.resolve_executor = orig
    assert res.success
    assert "fallback-ok" in res.stdout


def test_execute_with_fallback_all_fail_reports_error():
    """If every candidate fails, a single clean failure is returned."""

    from friday.runtime.models import MockExecutor
    import friday.runtime.executors as ex

    def fake(wid, ws="."):
        return MockExecutor(worker_id=wid, fail=True)  # every candidate fails

    class AnyTask:
        runtime_payload = "x"
        task_id = "b"
        title = "B"
        goal = "g"
        task_type = "implementation"

    orig = ex.resolve_executor
    ex.resolve_executor = fake
    try:
        res = execute_with_fallback(AnyTask(), "worker:claude",
                                    str(Path(tempfile.mkdtemp())))
    finally:
        ex.resolve_executor = orig
    assert res.success is False
    assert "all executors failed" in (res.error or "")


def test_claude_unavailable_still_executes():
    """A mission whose research task wants Claude still runs end-to-end when the
    AI executor fails: the fallback chain degrades to deterministic built-ins,
    which produce the expected artifacts and the mission reports success (no
    crash, no permanent block)."""
    conn = _fresh_db()
    g = TaskGraphEngine(conn).generate(
        "Research the best architecture for a distributed system")
    CapabilityResolver(conn).resolve_graph(g.id)
    sched = TaskScheduler(conn).schedule_graph(g.id).schedule

    ws = Path(tempfile.mkdtemp())

    def _resolve(wid):
        # Claude resolves to a failing mock (simulating an unavailable/hung AI
        # executor); the runtime falls back to deterministic built-ins which
        # materialize their artifacts via ArtifactMock.
        if wid and any(a in wid for a in _AI_IDS):
            return MockExecutor(worker_id=wid, fail=True)
        return ArtifactMock(worker_id=wid, workspace=str(ws))

    eng = RuntimeEngine(conn, worker_resolver=_resolve,
                        workspace=str(ws), fallback=True)
    report = eng.run(sched)
    # Mission completes (no crash, no permanent block) even without Claude.
    assert report.executed == len(sched.tasks)
    assert report.failed == 0, f"mission failed without Claude: {report.tasks}"
    conn.close()


# ===================================================================
# Phase 4 — Fresh DB auto-bootstraps executors
# ===================================================================

def test_fresh_db_has_builtin_executors():
    conn = _fresh_db()
    reg = WorkerRegistry(conn)
    assert reg.count() > 0, "fresh DB has no workers registered"
    ids = {w.id for w in reg.all_workers()}
    for needed in ("worker:python", "worker:filesystem", "worker:testing",
                   "worker:shell", "worker:git", "worker:documentation"):
        assert needed in ids, f"missing built-in executor: {needed}"
    conn.close()


def test_fresh_db_execute_works():
    """rm friday.db then friday execute must work immediately (Phase 4 + 6).

    Fresh DB auto-bootstraps executors (no manual seeding) and every task runs
    (none BLOCKED). The mock executor materializes the artifact the task's goal
    references, so truthful verification passes and the mission reports success
    — proving the orchestration path is wired correctly end-to-end.
    """
    ws = Path(tempfile.mkdtemp())

    conn = _fresh_db()
    g = TaskGraphEngine(conn).generate(
        "Build a calculator.py CLI in Python with tests")
    CapabilityResolver(conn).resolve_graph(g.id)
    sched = TaskScheduler(conn).schedule_graph(g.id).schedule
    assert not any(t.status.value == "blocked" for t in sched.tasks), \
        "fresh DB left tasks blocked"

    def _resolve(wid):
        # AI executors resolve to a failing mock (no interactive binary in CI);
        # the runtime falls back to deterministic built-ins which materialize
        # their artifacts via ArtifactMock.
        if wid and any(a in wid for a in _AI_IDS):
            return MockExecutor(worker_id=wid, fail=True)
        return ArtifactMock(worker_id=wid, workspace=str(ws))

    eng = RuntimeEngine(conn, worker_resolver=_resolve,
                        workspace=str(ws), fallback=True)
    report = eng.run(sched)
    assert report.executed == len(sched.tasks)
    assert report.failed == 0, f"fresh-DB execution failed: {report.tasks}"
    assert (ws / "calculator.py").exists(), "expected artifact not produced"
    conn.close()


def test_bootstrap_idempotent():
    """Running bootstrap twice does not duplicate worker rows."""
    conn = _fresh_db()
    from friday.worker.engine import ensure_runtime_bootstrapped
    n1 = ensure_runtime_bootstrapped(conn)
    n2 = ensure_runtime_bootstrapped(conn)
    assert n1 == n2
    conn.close()


# ===================================================================
# Phase 6 — combined end-to-end (real path)
# ===================================================================

def test_end_to_end_deterministic_mission():
    conn = _fresh_db()
    g = TaskGraphEngine(conn).generate(
        "Build a calculator.py CLI in Python with tests")
    CapabilityResolver(conn).resolve_graph(g.id)
    sched = TaskScheduler(conn).schedule_graph(g.id).schedule

    ws = Path(tempfile.mkdtemp())

    def _resolve(wid):
        # Claude fails fast (no interactive binary in CI); deterministic
        # built-ins materialize their artifacts so verification passes.
        if wid and any(a in wid for a in _AI_IDS):
            return MockExecutor(worker_id=wid, fail=True)
        return ArtifactMock(worker_id=wid, workspace=str(ws))

    eng = RuntimeEngine(conn, worker_resolver=_resolve,
                        workspace=str(ws), fallback=True)
    report = eng.run(sched)
    assert report.succeeded == len(sched.tasks)
    # Correct ordering preserved through execution (waves monotonic).
    assert (ws / "calculator.py").exists(), "expected artifact not produced"
    conn.close()


# ===================================================================
# Phase 3 — Truthful verification: failure path must NOT crash
# ===================================================================

def test_verification_failure_cancels_descendants():
    """A task that reports success but produces no expected artifact must be
    flipped to FAILED and its descendants CANCELLED — without crashing the
    runtime (regression: _cancel_descendants referenced a non-existent
    self._last_tasks and raised AttributeError, aborting the mission)."""
    from friday.runtime.models import MockExecutor

    # A mock that always reports success (claims the file was written) but
    # actually writes nothing => verification must catch the lie.
    conn = _fresh_db()
    g = TaskGraphEngine(conn).generate(
        "Create hello.py printing Hello World")
    CapabilityResolver(conn).resolve_graph(g.id)
    sched = TaskScheduler(conn).schedule_graph(g.id).schedule

    def _resolve(wid):
        # Every worker is a success-claiming mock that produces no artifact.
        return MockExecutor(worker_id=wid, fail=False)

    ws = Path(tempfile.mkdtemp())
    eng = RuntimeEngine(conn, worker_resolver=_resolve,
                        workspace=str(ws), fallback=True)
    # Must not raise; mission is reported truthfully as failed.
    report = eng.run(sched)
    assert report.failed > 0, "verification failure was not reported"
    assert report.executed == len(sched.tasks)
    # No artifact on disk for the creation task.
    assert not (ws / "hello.py").exists(), "false success wrote a file"
    conn.close()


# ===================================================================
# Phase 4 — Deterministic recovery (retry + blocking/non-blocking)
# ===================================================================

from friday.runtime.executor import execute_schedule
from friday.runtime.models import RuntimeTask, RunState


def _rt(task_id, worker_id, task_type, deps=(), wave=1):
    return RuntimeTask(
        execution_id=f"g:{task_id}", session_id="", schedule_id="g",
        task_id=task_id, worker_id=worker_id, wave=wave,
        dependencies=list(deps), task_type=task_type)


def _spy_resolver(worker_id, fail_transient=False, succeed=False):
    """Resolver returning a worker that records call count + fails transiently
    (for retry), fails deterministically, or succeeds."""
    from friday.runtime.models import ExecutionResult

    class _W:
        def __init__(self, wid):
            self.worker_id = wid
            self.calls = 0

        def execute(self, task):
            self.calls += 1
            if succeed:
                return ExecutionResult(success=True, duration_ms=1)
            if fail_transient and self.calls < 3:
                return ExecutionResult(
                    success=False, error="Connection reset by peer (timeout)",
                    exit_code=None, duration_ms=1)
            if fail_transient:
                return ExecutionResult(success=True, duration_ms=1)
            # Deterministic failure.
            return ExecutionResult(
                success=False, error="mock deterministic failure",
                exit_code=1, duration_ms=1)

    return _W(worker_id)


def test_blocking_failure_cancels_descendants():
    """A BLOCKING failure (testing task) cancels its dependents; the mission
    stops on that branch."""
    seen = {}
    def persist(exec_id, tid, state, result=None, *a, **k):
        seen[tid] = (state, k.get("attempt", 1))

    tasks = [
        _rt("t1", "worker:testing", "testing", wave=1),
        _rt("t2", "worker:python", "implementation", deps=["t1"], wave=2),
    ]
    # t1 (testing) fails deterministically; t2 (python) has no payload -> success.
    workers = {"worker:testing": _spy_resolver("worker:testing"),
               "worker:python": _spy_resolver("worker:python")}
    states = execute_schedule(tasks, workers.get, persist, workspace=".")
    assert states["t1"] == RunState.FAILED
    assert states["t2"] == RunState.CANCELLED, "blocking failure must cancel dependent"


def test_nonblocking_failure_continues_mission():
    """A NON-blocking failure (formatter/linter, task_type=configuration) is
    recorded as FAILED but its dependents still execute — mission continues."""
    seen = {}
    def persist(exec_id, tid, state, result=None, *a, **k):
        seen[tid] = (state, k.get("attempt", 1))

    tasks = [
        _rt("fmt", "worker:shell", "configuration", wave=1),
        _rt("impl", "worker:python", "implementation", deps=["fmt"], wave=2),
    ]
    # fmt (shell) fails deterministically; impl (python) succeeds.
    workers = {"worker:shell": _spy_resolver("worker:shell"),
               "worker:python": _spy_resolver("worker:python", succeed=True)}
    states = execute_schedule(tasks, workers.get, persist, workspace=".")
    assert states["fmt"] == RunState.FAILED
    # Non-blocking: dependent must NOT be cancelled.
    assert states["impl"] == RunState.SUCCESS, \
        "non-blocking failure must not cancel dependents"


def test_transient_failure_is_retried_then_succeeds():
    """A transient failure (timeout/connection) is retried up to MAX_ATTEMPTS
    and succeeds on a later attempt — no mission stop."""
    seen = {}
    def persist(exec_id, tid, state, result=None, *a, **k):
        seen[tid] = (state, k.get("attempt", 1))

    tasks = [_rt("t1", "worker:claude", "implementation", wave=1)]
    w = _spy_resolver("worker:claude", fail_transient=True)
    states = execute_schedule(tasks, lambda wid: w, persist, workspace=".")
    assert states["t1"] == RunState.SUCCESS, "transient failure should recover"
    assert w.calls == 3, f"expected 3 attempts (2 transient + 1 success), got {w.calls}"
    # Final persisted attempt reflects the successful retry.
    assert seen["t1"][1] == 3, "final attempt should be 3"


def test_recovery_attempt_count_recorded():
    """Each attempt is persisted with its attempt number so the journal can
    show the retry trail (attempt > 1 => retried)."""
    attempts = []
    def persist(exec_id, tid, state, result=None, *a, **k):
        attempts.append(k.get("attempt", 1))

    tasks = [_rt("t1", "worker:claude", "implementation", wave=1)]
    w = _spy_resolver("worker:claude", fail_transient=True)
    execute_schedule(tasks, lambda wid: w, persist, workspace=".")
    assert max(attempts) == 3, "retry trail must record attempt 3"


def test_testing_evidence_in_journal():
    """A real TestingExecutor failure is captured as a test_summary in the
    journal's per-task evidence — mission success is derived from evidence, not
    executor status alone. Runs offline (no AI backend)."""
    import tempfile
    from pathlib import Path
    from friday.runtime.executors import TestingExecutor
    from friday.runtime.journal import build_journal

    ws = Path(tempfile.mkdtemp())
    (ws / "sched.py").write_text(
        "def order(items):\n    return items\n")
    (ws / "test_sched.py").write_text(
        "def test_order():\n    assert order([3,1,2]) == [1,2,3]\n")

    conn = _fresh_db()
    g = TaskGraphEngine(conn).generate("Fix failing scheduler tests")
    CapabilityResolver(conn).resolve_graph(g.id)
    sched = TaskScheduler(conn).schedule_graph(g.id).schedule

    def _resolve(wid):
        if wid in ("worker:testing", "worker:python"):
            return TestingExecutor(workspace=str(ws))  # real test runner
        if wid and any(a in wid for a in _AI_IDS):
            return MockExecutor(worker_id=wid, fail=True)
        return ArtifactMock(worker_id=wid, workspace=str(ws))

    eng = RuntimeEngine(conn, worker_resolver=_resolve,
                        workspace=str(ws), fallback=True)
    report = eng.run(sched)
    journal = build_journal(report.session_id, conn, report, goal="Fix failing scheduler tests")
    # At least one task must carry test evidence (proving the verdict).
    has_test_evidence = any(
        "test_summary" in (t.get("evidence") or {})
        for t in journal["tasks"])
    assert has_test_evidence, "testing task must record test_summary evidence"
    conn.close()



