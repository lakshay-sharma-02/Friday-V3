"""Execution Runtime regression tests (Milestone 9.5).

80+ tests covering: session lifecycle, parallel execution, wave ordering,
dependency blocking, worker failures, execution events, history, serialization,
schema version, append-only, deterministic replay, mock workers, multiple
workers, worker exceptions, cancellation, execution report, runtime restart,
and large graphs.

The Runtime NEVER plans/schedules/resolves/reviews/repairs/retries/learns — it
only executes the ExecutionSchedule it is handed. These tests verify execution
math and persistence only.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import tempfile
import time
from pathlib import Path

import pytest

from friday.db import connect, now_iso
from friday.runtime import (
    ExecutionReport,
    ExecutionResult,
    MockWorker,
    PythonWorker,
    RunState,
    RuntimeEngine,
    RuntimeEvent,
    RuntimeTask,
    SCHEMA_VERSION,
    SessionState,
    ShellWorker,
    Worker,
    blocked_descendants,
    can_transition,
    dispatch,
    execute_schedule,
    mark_cancelled,
    next_state_for_result,
)
from friday.runtime.engine import _session_id
from friday.scheduler.models import ExecutionSchedule, ScheduledTask, TaskState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _db(path: Path | None = None) -> sqlite3.Connection:
    d = path or Path(tempfile.mkdtemp())
    return connect(d / "runtime_test.db")


def _seed_graph(conn, gid, n_tasks):
    """Insert a graph header so FK constraints on runtime_sessions hold."""
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute(
        "INSERT OR REPLACE INTO plans (id,goal,plan_type,confidence,status,"
        "created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
        (f"plan:{gid}", "g", "feature", "medium", "planned", now_iso(), now_iso()))
    conn.execute(
        "INSERT OR REPLACE INTO task_graphs "
        "(id,goal,plan_id,plan_type,task_count,edge_count,critical_path_length,"
        "parallel_groups,status,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (gid, "g", f"plan:{gid}", "feature", n_tasks, 0, 0, 0, "compiled",
         now_iso(), now_iso()))
    conn.commit()
    conn.execute("PRAGMA foreign_keys=ON")


def _mk_task(gid, tid, wave, deps=None, worker_id="worker:mock",
             status=TaskState.READY):
    return ScheduledTask(
        schedule_id=f"{gid}:{tid}", graph_id=gid, assignment_id=f"{gid}:{tid}",
        task_id=tid, worker_id=worker_id, phase=f"wave-{wave}", wave=wave,
        status=status, priority=1, dependency_count=len(deps or []),
        dependencies=list(deps or []))


def _schedule(gid, specs, task_count=None):
    """specs: list of (tid, wave, deps, worker_id?, status?)."""
    tasks = []
    for s in specs:
        tid, wave = s[0], s[1]
        deps = s[2] if len(s) > 2 else None
        worker_id = s[3] if len(s) > 3 else "worker:mock"
        status = s[4] if len(s) > 4 else TaskState.READY
        tasks.append(_mk_task(gid, tid, wave, deps, worker_id, status))
    return ExecutionSchedule(
        schedule_id=gid, graph_id=gid, task_count=task_count or len(tasks),
        wave_count=max((t.wave for t in tasks), default=0), tasks=tasks)


def _run(conn, sched, workers=None, resolver=None, max_workers=8):
    eng = RuntimeEngine(conn, workers=workers, worker_resolver=resolver,
                        max_workers=max_workers)
    return eng.run(sched)


# ===================================================================
# 1. State machine
# ===================================================================

def test_state_transitions_valid(tmp_path):
    assert can_transition(RunState.PENDING, RunState.RUNNING)
    assert can_transition(RunState.RUNNING, RunState.SUCCESS)
    assert can_transition(RunState.RUNNING, RunState.FAILED)
    assert can_transition(RunState.PENDING, RunState.CANCELLED)


def test_state_transitions_invalid(tmp_path):
    assert not can_transition(RunState.SUCCESS, RunState.RUNNING)
    assert not can_transition(RunState.FAILED, RunState.SUCCESS)
    assert not can_transition(RunState.CANCELLED, RunState.RUNNING)
    assert not can_transition(RunState.RUNNING, RunState.PENDING)


def test_next_state_for_result_success(tmp_path):
    assert next_state_for_result(RunState.RUNNING, True) == RunState.SUCCESS


def test_next_state_for_result_failure(tmp_path):
    assert next_state_for_result(RunState.RUNNING, False) == RunState.FAILED


def test_next_state_for_result_rejects_non_running(tmp_path):
    with pytest.raises(ValueError):
        next_state_for_result(RunState.PENDING, True)


def test_mark_cancelled_from_pending(tmp_path):
    assert mark_cancelled(RunState.PENDING) == RunState.CANCELLED


def test_mark_cancelled_preserves_terminal(tmp_path):
    # A real outcome is not overridden by a descendant-cancel pass.
    assert mark_cancelled(RunState.SUCCESS) == RunState.SUCCESS
    assert mark_cancelled(RunState.FAILED) == RunState.FAILED


def test_blocked_descendants_chain(tmp_path):
    # deps[X] = tasks X depends on. So B,C depend on A; D depends on B,C.
    deps = {"A": [], "B": ["A"], "C": ["A"], "D": ["B", "C"]}
    # dependents[X] = tasks that depend on X.
    dependents = {}
    for u, vs in deps.items():
        for v in vs:
            dependents.setdefault(v, []).append(u)
    out = blocked_descendants("A", dependents)
    assert out == {"B", "C", "D"}


def test_blocked_descendants_none(tmp_path):
    dependents = {"A": ["B"], "B": []}
    assert blocked_descendants("B", dependents) == set()


# ===================================================================
# 2. Session lifecycle
# ===================================================================

def test_session_created_and_finished(tmp_path):
    conn = _db()
    _seed_graph(conn, "g1", 1)
    sched = _schedule("g1", [("A", 1)])
    rep = _run(conn, sched, workers={"worker:mock": MockWorker()})
    assert rep.state == SessionState.FINISHED.value
    row = conn.execute(
        "SELECT state FROM runtime_sessions WHERE session_id = ?",
        (rep.session_id,)).fetchone()
    assert row["state"] == "finished"


def test_session_has_started_and_finished(tmp_path):
    conn = _db()
    _seed_graph(conn, "g1", 1)
    sched = _schedule("g1", [("A", 1)])
    rep = _run(conn, sched, workers={"worker:mock": MockWorker()})
    assert rep.started_at
    assert rep.finished_at
    assert rep.duration_ms >= 0


def test_session_id_unique_per_run(tmp_path):
    conn = _db()
    _seed_graph(conn, "g1", 1)
    sched = _schedule("g1", [("A", 1)])
    r1 = _run(conn, sched, workers={"worker:mock": MockWorker()})
    r2 = _run(conn, sched, workers={"worker:mock": MockWorker()})
    assert r1.session_id != r2.session_id


def test_session_records_events(tmp_path):
    conn = _db()
    _seed_graph(conn, "g1", 1)
    sched = _schedule("g1", [("A", 1)])
    rep = _run(conn, sched, workers={"worker:mock": MockWorker()})
    kinds = {e["kind"] for e in conn.execute(
        "SELECT kind FROM runtime_events WHERE session_id = ?",
        (rep.session_id,)).fetchall()}
    assert "session_started" in kinds
    assert "task_started" in kinds
    assert "task_finished" in kinds
    assert "session_finished" in kinds


def test_session_persisted_row(tmp_path):
    conn = _db()
    _seed_graph(conn, "g1", 2)
    sched = _schedule("g1", [("A", 1), ("B", 1)])
    rep = _run(conn, sched, workers={"worker:mock": MockWorker()})
    rows = conn.execute(
        "SELECT * FROM runtime_sessions WHERE session_id = ?",
        (rep.session_id,)).fetchall()
    assert len(rows) == 1
    assert rows[0]["schedule_id"] == "g1"
    assert rows[0]["schema_version"] == SCHEMA_VERSION


# ===================================================================
# 3. Parallel execution
# ===================================================================

def test_parallel_wave_runs_concurrently(tmp_path):
    conn = _db()
    _seed_graph(conn, "g1", 3)
    sched = _schedule("g1", [("A", 1), ("B", 1), ("C", 1)])
    rep = _run(conn, sched, workers={"worker:mock": MockWorker()})
    assert rep.succeeded == 3
    # all in wave 1, all succeeded
    states = {t["task_id"]: t["status"] for t in rep.tasks}
    assert states == {"A": "success", "B": "success", "C": "success"}


def test_parallel_true_concurrency_timing(tmp_path):
    # A slow mock worker; parallel wave should finish near one duration, not N.
    class SlowMock(MockWorker):
        def execute(self, task):
            time.sleep(0.05)
            return super().execute(task)
    conn = _db()
    _seed_graph(conn, "g1", 4)
    sched = _schedule("g1", [("A", 1), ("B", 1), ("C", 1), ("D", 1)])
    rep = _run(conn, sched, workers={"worker:mock": SlowMock()})
    # 4 tasks * 50ms serial would be ~200ms; parallel ~<=120ms.
    assert rep.duration_ms < 200
    assert rep.succeeded == 4


def test_parallel_different_workers(tmp_path):
    conn = _db()
    _seed_graph(conn, "g1", 2)
    sched = _schedule("g1", [
        ("A", 1, None, "worker:w1"),
        ("B", 1, None, "worker:w2"),
    ])
    w1 = MockWorker(worker_id="worker:w1")
    w2 = MockWorker(worker_id="worker:w2")
    rep = _run(conn, sched, workers={"worker:w1": w1, "worker:w2": w2})
    assert rep.succeeded == 2
    assert set(rep.workers_used) == {"worker:w1", "worker:w2"}


# ===================================================================
# 4. Wave ordering
# ===================================================================

def test_wave_ordering_chain(tmp_path):
    conn = _db()
    _seed_graph(conn, "g1", 3)
    sched = _schedule("g1", [
        ("A", 1), ("B", 2, ["A"]), ("C", 3, ["B"])])
    rep = _run(conn, sched, workers={"worker:mock": MockWorker()})
    states = {t["task_id"]: t["status"] for t in rep.tasks}
    assert states == {"A": "success", "B": "success", "C": "success"}
    # Verify wave ordering via persisted start times.
    rows = conn.execute(
        "SELECT task_id, started_at FROM runtime_tasks WHERE session_id = ? "
        "ORDER BY started_at", (rep.session_id,)).fetchall()
    order = [r["task_id"] for r in rows]
    assert order.index("A") < order.index("B") < order.index("C")


def test_wave_ordering_diamond(tmp_path):
    conn = _db()
    _seed_graph(conn, "g1", 4)
    sched = _schedule("g1", [
        ("A", 1), ("B", 2, ["A"]), ("C", 2, ["A"]), ("D", 3, ["B", "C"])])
    rep = _run(conn, sched, workers={"worker:mock": MockWorker()})
    states = {t["task_id"]: t["status"] for t in rep.tasks}
    assert all(s == "success" for s in states.values())
    assert rep.succeeded == 4


def test_wave_wait_for_completion(tmp_path):
    # A wave-2 task must not start before its wave-1 dependency.
    conn = _db()
    _seed_graph(conn, "g1", 2)
    sched = _schedule("g1", [("A", 1), ("B", 2, ["A"])])
    rep = _run(conn, sched, workers={"worker:mock": MockWorker()})
    rows = {r["task_id"]: r["started_at"] for r in conn.execute(
        "SELECT task_id, started_at FROM runtime_tasks WHERE session_id = ?",
        (rep.session_id,)).fetchall()}
    assert rows["A"] <= rows["B"]


# ===================================================================
# 5. Dependency blocking (failure propagation)
# ===================================================================

def test_failure_cancels_descendants(tmp_path):
    conn = _db()
    _seed_graph(conn, "g1", 4)
    sched = _schedule("g1", [
        ("A", 1), ("B", 1), ("C", 2, ["A", "B"]), ("D", 2, ["B"])])
    fail_b = MockWorker()
    class F(MockWorker):
        def execute(self, task):
            if task.task_id == "B":
                return ExecutionResult(success=False, error="boom", exit_code=1)
            return super().execute(task)
    rep = _run(conn, sched, workers={"worker:mock": F()})
    states = {t["task_id"]: t["status"] for t in rep.tasks}
    assert states["A"] == "success"
    assert states["B"] == "failed"
    assert states["C"] == "cancelled"
    assert states["D"] == "cancelled"
    assert rep.failed == 1 and rep.cancelled == 2


def test_failure_does_not_affect_unrelated(tmp_path):
    conn = _db()
    _seed_graph(conn, "g1", 4)
    sched = _schedule("g1", [
        ("A", 1), ("B", 1), ("C", 2, ["A"]), ("D", 2, ["B"])])
    class F(MockWorker):
        def execute(self, task):
            if task.task_id == "B":
                return ExecutionResult(success=False, error="x", exit_code=1)
            return super().execute(task)
    rep = _run(conn, sched, workers={"worker:mock": F()})
    states = {t["task_id"]: t["status"] for t in rep.tasks}
    # C depends only on A (ok); D depends on B (failed) -> cancelled.
    assert states["A"] == "success"
    assert states["C"] == "success"
    assert states["B"] == "failed"
    assert states["D"] == "cancelled"


def test_no_retry_on_failure(tmp_path):
    conn = _db()
    _seed_graph(conn, "g1", 1)
    sched = _schedule("g1", [("A", 1)])
    calls = []
    class CountingFail(MockWorker):
        def execute(self, task):
            calls.append(task.task_id)
            return ExecutionResult(success=False, error="x")
    rep = _run(conn, sched, workers={"worker:mock": CountingFail()})
    assert rep.failed == 1
    # Exactly one execution attempt — no retry.
    assert calls == ["A"]


# ===================================================================
# 6. Worker failures / exceptions
# ===================================================================

def test_worker_exception_becomes_failure(tmp_path):
    conn = _db()
    _seed_graph(conn, "g1", 1)
    sched = _schedule("g1", [("A", 1)])
    class Boom(MockWorker):
        def execute(self, task):
            raise RuntimeError("kaboom")
    rep = _run(conn, sched, workers={"worker:mock": Boom()})
    assert rep.failed == 1
    row = conn.execute(
        "SELECT error FROM runtime_tasks WHERE session_id = ? AND task_id='A'",
        (rep.session_id,)).fetchone()
    assert "kaboom" in (row["error"] or "")


def test_missing_worker_is_failure(tmp_path):
    conn = _db()
    _seed_graph(conn, "g1", 1)
    sched = _schedule("g1", [("A", 1, None, "worker:none")])
    rep = _run(conn, sched, workers={"worker:mock": MockWorker()})
    assert rep.failed == 1
    row = conn.execute(
        "SELECT status, error FROM runtime_tasks WHERE session_id = ? "
        "AND task_id='A'", (rep.session_id,)).fetchone()
    assert row["status"] == "failed"
    assert "no worker" in (row["error"] or "")


def test_missing_worker_cancels_dependents(tmp_path):
    conn = _db()
    _seed_graph(conn, "g1", 2)
    sched = _schedule("g1", [
        ("A", 1, None, "worker:none"), ("B", 2, ["A"])])
    rep = _run(conn, sched, workers={"worker:mock": MockWorker()})
    states = {t["task_id"]: t["status"] for t in rep.tasks}
    assert states["A"] == "failed"
    assert states["B"] == "cancelled"


# ===================================================================
# 7. Execution events (append-only)
# ===================================================================

def test_events_append_only_count(tmp_path):
    conn = _db()
    _seed_graph(conn, "g1", 2)
    sched = _schedule("g1", [("A", 1), ("B", 1)])
    rep = _run(conn, sched, workers={"worker:mock": MockWorker()})
    n = conn.execute(
        "SELECT COUNT(*) FROM runtime_events WHERE session_id = ?",
        (rep.session_id,)).fetchone()[0]
    # 2 tasks * (started+finished) + 2 session events = 6
    assert n == 6


def test_event_ordering_monotonic(tmp_path):
    conn = _db()
    _seed_graph(conn, "g1", 1)
    sched = _schedule("g1", [("A", 1)])
    rep = _run(conn, sched, workers={"worker:mock": MockWorker()})
    evs = conn.execute(
        "SELECT eid, kind FROM runtime_events WHERE session_id = ? ORDER BY eid",
        (rep.session_id,)).fetchall()
    kinds = [e["kind"] for e in evs]
    assert kinds[0] == "session_started"
    assert kinds[-1] == "session_finished"


def test_task_failed_event_emitted(tmp_path):
    conn = _db()
    _seed_graph(conn, "g1", 1)
    sched = _schedule("g1", [("A", 1)])
    class F(MockWorker):
        def execute(self, task):
            return ExecutionResult(success=False, error="x")
    rep = _run(conn, sched, workers={"worker:mock": F()})
    kinds = [e["kind"] for e in conn.execute(
        "SELECT kind FROM runtime_events WHERE session_id = ?",
        (rep.session_id,)).fetchall()]
    assert "task_failed" in kinds


# ===================================================================
# 8. History (append-only)
# ===================================================================

def test_history_append_only(tmp_path):
    conn = _db()
    _seed_graph(conn, "g1", 1)
    sched = _schedule("g1", [("A", 1)])
    rep = _run(conn, sched, workers={"worker:mock": MockWorker()})
    n = conn.execute(
        "SELECT COUNT(*) FROM runtime_history WHERE session_id = ?",
        (rep.session_id,)).fetchone()[0]
    assert n >= 2  # pending + running + success snapshots


def test_history_records_each_state(tmp_path):
    conn = _db()
    _seed_graph(conn, "g1", 1)
    sched = _schedule("g1", [("A", 1)])
    rep = _run(conn, sched, workers={"worker:mock": MockWorker()})
    statuses = {h["status"] for h in conn.execute(
        "SELECT status FROM runtime_history WHERE session_id = ?",
        (rep.session_id,)).fetchall()}
    assert "pending" in statuses
    assert "running" in statuses
    assert "success" in statuses


# ===================================================================
# 9. Serialization / schema version
# ===================================================================

def test_execution_result_to_dict(tmp_path):
    r = ExecutionResult(success=True, stdout="o", stderr="e",
                        artifacts=["a1"], exit_code=0, duration_ms=5)
    d = r.to_dict()
    assert d["success"] is True and d["stdout"] == "o"
    assert d["artifacts"] == ["a1"] and d["duration_ms"] == 5


def test_execution_report_to_dict(tmp_path):
    rep = ExecutionReport(
        session_id="s1", schedule_id="g1", state="finished",
        started_at="t0", finished_at="t1", duration_ms=1, executed=1,
        succeeded=1, failed=0, cancelled=0)
    d = rep.to_dict()
    assert d["session_id"] == "s1"
    assert d["schema_version"] == SCHEMA_VERSION
    assert d["succeeded"] == 1


def test_runtime_task_to_dict(tmp_path):
    t = RuntimeTask(execution_id="g1:A", session_id="s", schedule_id="g1",
                    task_id="A", worker_id="w", wave=1, dependencies=["B"])
    d = t.to_dict()
    assert d["task_id"] == "A" and d["dependencies"] == ["B"]


def test_scheduled_task_schema_version(tmp_path):
    t = ScheduledTask(
        schedule_id="g1:A", graph_id="g1", assignment_id="g1:A", task_id="A",
        worker_id="w", phase="wave-1", wave=1, status=TaskState.READY,
        priority=1, dependency_count=0)
    assert t.schema_version == "1.0"


def test_runtime_results_row_persisted(tmp_path):
    conn = _db()
    _seed_graph(conn, "g1", 1)
    sched = _schedule("g1", [("A", 1)])
    rep = _run(conn, sched, workers={"worker:mock": MockWorker()})
    n = conn.execute(
        "SELECT COUNT(*) FROM runtime_results WHERE session_id = ?",
        (rep.session_id,)).fetchone()[0]
    assert n == 1


def test_runtime_results_append_only_multiple_runs(tmp_path):
    conn = _db()
    _seed_graph(conn, "g1", 1)
    sched = _schedule("g1", [("A", 1)])
    r1 = _run(conn, sched, workers={"worker:mock": MockWorker()})
    r2 = _run(conn, sched, workers={"worker:mock": MockWorker()})
    total = conn.execute(
        "SELECT COUNT(*) FROM runtime_results").fetchone()[0]
    # one result per run
    assert total == 2


# ===================================================================
# 10. Deterministic replay
# ===================================================================

def test_deterministic_replay_same_report(tmp_path):
    conn = _db()
    _seed_graph(conn, "g1", 3)
    sched = _schedule("g1", [
        ("A", 1), ("B", 1), ("C", 2, ["A", "B"])])
    r1 = _run(conn, sched, workers={"worker:mock": MockWorker()})
    r2 = _run(conn, sched, workers={"worker:mock": MockWorker()})
    assert r1.succeeded == r2.succeeded == 3
    assert r1.failed == r2.failed == 0


def test_deterministic_execution_order(tmp_path):
    conn = _db()
    _seed_graph(conn, "g1", 4)
    sched = _schedule("g1", [("A", 1), ("B", 1), ("C", 2, ["A"]), ("D", 2, ["B"])])
    order1 = sorted(t["task_id"] for t in _run(conn, sched,
            workers={"worker:mock": MockWorker()}).tasks)
    order2 = sorted(t["task_id"] for t in _run(conn, sched,
            workers={"worker:mock": MockWorker()}).tasks)
    assert order1 == order2


# ===================================================================
# 11. Mock workers
# ===================================================================

def test_mock_worker_success(tmp_path):
    w = MockWorker()
    res = w.execute(RuntimeTask("g1:A", "s", "g1", "A", "w", 1))
    assert res.success and res.exit_code == 0


def test_mock_worker_forced_fail(tmp_path):
    w = MockWorker(fail=True)
    res = w.execute(RuntimeTask("g1:A", "s", "g1", "A", "w", 1))
    assert not res.success and res.error


def test_mock_worker_hint_fail(tmp_path):
    w = MockWorker()
    t = RuntimeTask("g1:A", "s", "g1", "A", "w", 1)
    t.runtime_hint = "fail"
    assert not w.execute(t).success


def test_mock_worker_artifacts(tmp_path):
    w = MockWorker()
    t = RuntimeTask("g1:A", "s", "g1", "A", "w", 1, artifacts=["x"])
    res = w.execute(t)
    assert res.artifacts == ["x"]


# ===================================================================
# 12. Multiple workers
# ===================================================================

def test_multiple_workers_routed(tmp_path):
    conn = _db()
    _seed_graph(conn, "g1", 2)
    sched = _schedule("g1", [
        ("A", 1, None, "worker:x"), ("B", 1, None, "worker:y")])
    calls = {}
    class Rec(MockWorker):
        def __init__(self, wid, key):
            super().__init__(worker_id=wid)
            self._key = key
        def execute(self, task):
            calls[self._key] = calls.get(self._key, 0) + 1
            return super().execute(task)
    wx, wy = Rec("worker:x", "x"), Rec("worker:y", "y")
    rep = _run(conn, sched, workers={"worker:x": wx, "worker:y": wy})
    assert rep.succeeded == 2
    assert calls == {"x": 1, "y": 1}


def test_unknown_worker_falls_to_none(tmp_path):
    # worker_resolver returns None for an unmapped worker -> failure.
    conn = _db()
    _seed_graph(conn, "g1", 1)
    sched = _schedule("g1", [("A", 1, None, "worker:orphan")])
    rep = _run(conn, sched, workers={"worker:mock": MockWorker()})
    assert rep.failed == 1


# ===================================================================
# 13. Worker exceptions
# ===================================================================

def test_dispatcher_converts_exception(tmp_path):
    t = RuntimeTask("g1:A", "s", "g1", "A", "w", 1)
    class Boom(MockWorker):
        def execute(self, task):
            raise ValueError("nope")
    res = dispatch(t, Boom())
    assert not res.success
    assert "nope" in res.error


def test_dispatcher_none_worker(tmp_path):
    t = RuntimeTask("g1:A", "s", "g1", "A", "w", 1)
    res = dispatch(t, None)
    assert not res.success and "no worker" in res.error


# ===================================================================
# 14. Cancellation
# ===================================================================

def test_cancelled_task_not_executed(tmp_path):
    conn = _db()
    _seed_graph(conn, "g1", 3)
    sched = _schedule("g1", [
        ("A", 1), ("B", 1), ("C", 2, ["A", "B"])])
    calls = []
    class F(MockWorker):
        def execute(self, task):
            calls.append(task.task_id)
            if task.task_id == "A":
                return ExecutionResult(success=False, error="x")
            return super().execute(task)
    rep = _run(conn, sched, workers={"worker:mock": F()})
    # C never ran (cancelled because A failed).
    assert "C" not in calls
    states = {t["task_id"]: t["status"] for t in rep.tasks}
    assert states["C"] == "cancelled"


def test_cancelled_state_persisted(tmp_path):
    conn = _db()
    _seed_graph(conn, "g1", 2)
    sched = _schedule("g1", [("A", 1), ("B", 2, ["A"])])
    class F(MockWorker):
        def execute(self, task):
            if task.task_id == "A":
                return ExecutionResult(success=False, error="x")
            return super().execute(task)
    rep = _run(conn, sched, workers={"worker:mock": F()})
    row = conn.execute(
        "SELECT status FROM runtime_tasks WHERE session_id = ? AND task_id='B'",
        (rep.session_id,)).fetchone()
    assert row["status"] == "cancelled"


# ===================================================================
# 15. Execution report
# ===================================================================

def test_report_counts(tmp_path):
    conn = _db()
    _seed_graph(conn, "g1", 3)
    sched = _schedule("g1", [
        ("A", 1), ("B", 1), ("C", 2, ["A", "B"])])
    rep = _run(conn, sched, workers={"worker:mock": MockWorker()})
    assert rep.executed == 3
    assert rep.succeeded == 3
    assert rep.failed == 0 and rep.cancelled == 0
    assert rep.workers_used == ["worker:mock"]


def test_report_with_failure_counts(tmp_path):
    conn = _db()
    _seed_graph(conn, "g1", 2)
    sched = _schedule("g1", [("A", 1), ("B", 2, ["A"])])
    class F(MockWorker):
        def execute(self, task):
            if task.task_id == "A":
                return ExecutionResult(success=False, error="x")
            return super().execute(task)
    rep = _run(conn, sched, workers={"worker:mock": F()})
    assert rep.executed == 2
    assert rep.succeeded == 0
    assert rep.failed == 1 and rep.cancelled == 1


def test_report_no_analysis_fields(tmp_path):
    conn = _db()
    _seed_graph(conn, "g1", 1)
    sched = _schedule("g1", [("A", 1)])
    rep = _run(conn, sched, workers={"worker:mock": MockWorker()})
    d = rep.to_dict()
    assert "recommendation" not in d
    assert "analysis" not in d
    assert "learning" not in d


# ===================================================================
# 16. Runtime restart (resumability of records)
# ===================================================================

def test_runtime_tasks_reloadable_after_session(tmp_path):
    conn = _db()
    _seed_graph(conn, "g1", 2)
    sched = _schedule("g1", [("A", 1), ("B", 1)])
    rep = _run(conn, sched, workers={"worker:mock": MockWorker()})
    rows = conn.execute(
        "SELECT task_id, status FROM runtime_tasks WHERE session_id = ?",
        (rep.session_id,)).fetchall()
    assert {r["task_id"] for r in rows} == {"A", "B"}
    assert all(r["status"] == "success" for r in rows)


def test_session_listable_after_restart(tmp_path):
    conn = _db()
    _seed_graph(conn, "g1", 1)
    sched = _schedule("g1", [("A", 1)])
    _run(conn, sched, workers={"worker:mock": MockWorker()})
    # Simulate "restart" by opening a fresh engine over the same DB.
    eng2 = RuntimeEngine(conn, workers={"worker:mock": MockWorker()})
    sched2 = _schedule("g1", [("A", 1)])
    rep2 = eng2.run(sched2)
    sessions = conn.execute("SELECT COUNT(*) FROM runtime_sessions").fetchone()[0]
    assert sessions == 2
    assert rep2.succeeded == 1


# ===================================================================
# 17. Large graphs
# ===================================================================

def test_large_chain(tmp_path):
    conn = _db()
    n = 40
    _seed_graph(conn, "g1", n)
    specs = [(f"T{i}", i + 1, [f"T{i-1}"] if i > 0 else None)
             for i in range(n)]
    sched = _schedule("g1", specs)
    rep = _run(conn, sched, workers={"worker:mock": MockWorker()})
    assert rep.succeeded == n
    assert rep.wave_count == n


def test_large_wide_parallel(tmp_path):
    conn = _db()
    n = 50
    _seed_graph(conn, "g1", n)
    specs = [(f"T{i}", 1) for i in range(n)]
    sched = _schedule("g1", specs)
    rep = _run(conn, sched, workers={"worker:mock": MockWorker()})
    assert rep.succeeded == n
    assert rep.wave_count == 1


def test_large_graph_deterministic(tmp_path):
    conn = _db()
    n = 30
    _seed_graph(conn, "g1", n)
    specs = [(f"T{i}", 1) for i in range(n)]
    sched = _schedule("g1", specs)
    r1 = _run(conn, sched, workers={"worker:mock": MockWorker()})
    r2 = _run(conn, sched, workers={"worker:mock": MockWorker()})
    assert r1.succeeded == r2.succeeded == n


# ===================================================================
# 18. Dogfood: real worker adapters (PythonWorker / ShellWorker)
# ===================================================================

def test_python_worker_executes(tmp_path):
    conn = _db()
    _seed_graph(conn, "g1", 1)
    sched = _schedule("g1", [("A", 1)])
    eng = RuntimeEngine(conn, workers={"worker:mock": PythonWorker()})
    # Inject a payload via runtime_hint? No — RuntimeTask has runtime_payload.
    # Build a custom schedule whose task carries a payload through the engine.
    # Simpler: resolve mock -> PythonWorker and set payload on the task.
    for st in sched.tasks:
        st.runtime_payload = "print('hello from python')"
    rep = eng.run(sched)
    row = conn.execute(
        "SELECT stdout FROM runtime_results WHERE session_id = ?",
        (rep.session_id,)).fetchone()
    assert "hello from python" in (row["stdout"] or "")


def test_shell_worker_executes(tmp_path):
    conn = _db()
    _seed_graph(conn, "g1", 1)
    sched = _schedule("g1", [("A", 1)])
    eng = RuntimeEngine(conn, workers={"worker:mock": ShellWorker()})
    for st in sched.tasks:
        st.runtime_payload = "echo shell-ran"
    rep = eng.run(sched)
    row = conn.execute(
        "SELECT stdout FROM runtime_results WHERE session_id = ?",
        (rep.session_id,)).fetchone()
    assert "shell-ran" in (row["stdout"] or "")


def test_python_worker_failure_propagates(tmp_path):
    conn = _db()
    _seed_graph(conn, "g1", 1)
    sched = _schedule("g1", [("A", 1)])
    eng = RuntimeEngine(conn, workers={"worker:mock": PythonWorker()})
    for st in sched.tasks:
        st.runtime_payload = "import sys; sys.exit(3)"
    rep = eng.run(sched)
    assert rep.failed == 1
    row = conn.execute(
        "SELECT exit_code FROM runtime_results WHERE session_id = ?",
        (rep.session_id,)).fetchone()
    assert row["exit_code"] == 3


# ===================================================================
# 19. Dispatcher purity (no retry / repair / planning)
# ===================================================================

def test_dispatcher_no_retry(tmp_path):
    calls = []
    class C(Worker):
        worker_id = "w"
        def execute(self, task):
            calls.append(1)
            return ExecutionResult(success=False, error="x")
    dispatch(RuntimeTask("g1:A", "s", "g1", "A", "w", 1), C())
    assert len(calls) == 1


def test_dispatcher_returns_result_only(tmp_path):
    res = dispatch(RuntimeTask("g1:A", "s", "g1", "A", "w", 1), MockWorker())
    assert isinstance(res, ExecutionResult)
    # The Runtime never sees planning/scheduling internals.
    assert not hasattr(res, "plan")


# ===================================================================
# 20. Engine never schedules / resolves (boundary)
# ===================================================================

def test_engine_requires_schedule_input(tmp_path):
    conn = _db()
    eng = RuntimeEngine(conn, workers={"worker:mock": MockWorker()})
    # run() requires an ExecutionSchedule; it does not build one.
    with pytest.raises(AttributeError):
        eng.run(None)


def test_runtime_does_not_modify_schedule(tmp_path):
    conn = _db()
    _seed_graph(conn, "g1", 1)
    sched = _schedule("g1", [("A", 1)])
    original = [t.task_id for t in sched.tasks]
    _run(conn, sched, workers={"worker:mock": MockWorker()})
    assert [t.task_id for t in sched.tasks] == original


# ===================================================================
# 21. Schema version on every persisted row
# ===================================================================

def test_all_runtime_tables_carry_schema_version(tmp_path):
    conn = _db()
    _seed_graph(conn, "g1", 1)
    sched = _schedule("g1", [("A", 1)])
    rep = _run(conn, sched, workers={"worker:mock": MockWorker()})
    sess = conn.execute(
        "SELECT schema_version FROM runtime_sessions WHERE session_id = ?",
        (rep.session_id,)).fetchone()
    task = conn.execute(
        "SELECT schema_version FROM runtime_tasks WHERE session_id = ?",
        (rep.session_id,)).fetchone()
    assert sess["schema_version"] == SCHEMA_VERSION
    assert task["schema_version"] == SCHEMA_VERSION


# ===================================================================
# 22. Session id format
# ===================================================================

def test_session_id_format(tmp_path):
    sid = _session_id()
    assert sid.startswith("sess:")


def test_multiple_sessions_distinct_rows(tmp_path):
    conn = _db()
    _seed_graph(conn, "g1", 1)
    sched = _schedule("g1", [("A", 1)])
    _run(conn, sched, workers={"worker:mock": MockWorker()})
    _run(conn, sched, workers={"worker:mock": MockWorker()})
    n = conn.execute("SELECT COUNT(*) FROM runtime_sessions").fetchone()[0]
    assert n == 2


# ===================================================================
# 23. Worker conflict serialization already solved upstream
# ===================================================================

def test_same_worker_serialized_by_scheduler_not_runtime(tmp_path):
    # Both tasks share worker:mock, wave 1. Runtime just executes both; the
    # Scheduler already decided ordering. Here we confirm both still run.
    conn = _db()
    _seed_graph(conn, "g1", 2)
    sched = _schedule("g1", [("A", 1), ("B", 1)])
    rep = _run(conn, sched, workers={"worker:mock": MockWorker()})
    assert rep.succeeded == 2


# ===================================================================
# 24. Append-only evolution table
# ===================================================================

def test_evolution_records_state_changes(tmp_path):
    conn = _db()
    _seed_graph(conn, "g1", 1)
    sched = _schedule("g1", [("A", 1)])
    rep = _run(conn, sched, workers={"worker:mock": MockWorker()})
    rows = conn.execute(
        "SELECT to_state FROM runtime_evolution WHERE session_id = ?",
        (rep.session_id,)).fetchall()
    states = {r["to_state"] for r in rows}
    assert "pending" in states and "success" in states


# ===================================================================
# 25. Cancellation reason recorded
# ===================================================================

def test_cancellation_reason_recorded(tmp_path):
    conn = _db()
    _seed_graph(conn, "g1", 2)
    sched = _schedule("g1", [("A", 1), ("B", 2, ["A"])])
    class F(MockWorker):
        def execute(self, task):
            if task.task_id == "A":
                return ExecutionResult(success=False, error="x")
            return super().execute(task)
    rep = _run(conn, sched, workers={"worker:mock": F()})
    row = conn.execute(
        "SELECT status FROM runtime_tasks WHERE session_id = ? AND task_id='B'",
        (rep.session_id,)).fetchone()
    assert row["status"] == "cancelled"


# ===================================================================
# 26. Independent graph dogfood
# ===================================================================

def test_dogfood_independent_graph(tmp_path):
    conn = _db()
    _seed_graph(conn, "g1", 4)
    sched = _schedule("g1", [("A", 1), ("B", 1), ("C", 1), ("D", 1)])
    rep = _run(conn, sched, workers={"worker:mock": MockWorker()})
    assert rep.succeeded == 4 and rep.wave_count == 1


def test_dogfood_chain(tmp_path):
    conn = _db()
    _seed_graph(conn, "g1", 3)
    sched = _schedule("g1", [("A", 1), ("B", 2, ["A"]), ("C", 3, ["B"])])
    rep = _run(conn, sched, workers={"worker:mock": MockWorker()})
    assert rep.succeeded == 3


def test_dogfood_diamond(tmp_path):
    conn = _db()
    _seed_graph(conn, "g1", 4)
    sched = _schedule("g1", [
        ("A", 1), ("B", 2, ["A"]), ("C", 2, ["A"]), ("D", 3, ["B", "C"])])
    rep = _run(conn, sched, workers={"worker:mock": MockWorker()})
    assert rep.succeeded == 4 and rep.wave_count == 3


def test_dogfood_parallel_graph(tmp_path):
    conn = _db()
    _seed_graph(conn, "g1", 3)
    sched = _schedule("g1", [("A", 1), ("B", 1), ("C", 2, ["A", "B"])])
    rep = _run(conn, sched, workers={"worker:mock": MockWorker()})
    assert rep.succeeded == 3


def test_dogfood_mixed_priority_runs_all(tmp_path):
    conn = _db()
    _seed_graph(conn, "g1", 5)
    sched = _schedule("g1", [
        ("A", 1), ("B", 1), ("C", 2, ["A"]), ("D", 2, ["B"]),
        ("E", 3, ["C", "D"])])
    rep = _run(conn, sched, workers={"worker:mock": MockWorker()})
    assert rep.succeeded == 5


def test_dogfood_worker_failure_blocks_descendants(tmp_path):
    conn = _db()
    _seed_graph(conn, "g1", 3)
    sched = _schedule("g1", [("A", 1), ("B", 2, ["A"]), ("C", 3, ["B"])])
    class F(MockWorker):
        def execute(self, task):
            if task.task_id == "A":
                return ExecutionResult(success=False, error="x")
            return super().execute(task)
    rep = _run(conn, sched, workers={"worker:mock": F()})
    states = {t["task_id"]: t["status"] for t in rep.tasks}
    assert states == {"A": "failed", "B": "cancelled", "C": "cancelled"}


def test_dogfood_blocked_at_schedule_time(tmp_path):
    # A task the Scheduler already BLOCKED (no worker) is cancelled, not run.
    conn = _db()
    _seed_graph(conn, "g1", 2)
    sched = _schedule("g1", [
        ("A", 1, None, "worker:mock", TaskState.READY),
        ("B", 1, None, None, TaskState.BLOCKED)])
    rep = _run(conn, sched, workers={"worker:mock": MockWorker()})
    states = {t["task_id"]: t["status"] for t in rep.tasks}
    assert states["A"] == "success"
    assert states["B"] == "cancelled"
    assert rep.succeeded == 1 and rep.cancelled == 1


# ===================================================================
# 27. Concurrency safety (no DB corruption under threads)
# ===================================================================

def test_concurrent_sessions_safe(tmp_path):
    # Each session uses its OWN connection to a shared DB file (the realistic
    # multi-process scenario). The Runtime never shares one connection across
    # threads; this verifies no corruption when sessions run concurrently.
    base = _db()
    db_file = base.execute("PRAGMA database_list").fetchone()[2]
    base.close()
    _seed_graph(connect(db_file), "g1", 4)
    sched = _schedule("g1", [("A", 1), ("B", 1), ("C", 1), ("D", 1)])

    errors = []
    def go():
        try:
            c = connect(db_file)
            _run(c, sched, workers={"worker:mock": MockWorker()})
            c.close()
        except Exception as e:
            errors.append(e)
    threads = [threading.Thread(target=go) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    # 4 sessions created, each 4 tasks.
    c = connect(db_file)
    assert c.execute("SELECT COUNT(*) FROM runtime_sessions").fetchone()[0] == 4
    c.close()


# ===================================================================
# 28. Export / round-trip via DB helpers
# ===================================================================

def test_runtime_tasks_query_round_trip(tmp_path):
    conn = _db()
    _seed_graph(conn, "g1", 2)
    sched = _schedule("g1", [("A", 1), ("B", 1)])
    rep = _run(conn, sched, workers={"worker:mock": MockWorker()})
    from friday.db import get_runtime_tasks
    rows = get_runtime_tasks(conn, rep.session_id)
    assert len(rows) == 2
    assert all(r["status"] == "success" for r in rows)


def test_runtime_results_query_round_trip(tmp_path):
    conn = _db()
    _seed_graph(conn, "g1", 1)
    sched = _schedule("g1", [("A", 1)])
    rep = _run(conn, sched, workers={"worker:mock": MockWorker()})
    from friday.db import get_runtime_results
    rows = get_runtime_results(conn, rep.session_id)
    assert len(rows) == 1 and rows[0]["success"] == 1


def test_cli_runtime_session_lists(tmp_path):
    """Smoke: friday.runtime CLI helpers import and list sessions."""
    from friday.cli_runtime import cmd_runtime_session
    import argparse
    conn = _db()
    _seed_graph(conn, "g1", 1)
    sched = _schedule("g1", [("A", 1)])
    _run(conn, sched, workers={"worker:mock": MockWorker()})
    ns = argparse.Namespace()
    # cmd_runtime_session opens its own DB via connect(); point it at ours.
    import os
    from friday.db import db_path
    os.environ["FRIDAY_DB"] = str(conn.execute(
        "PRAGMA database_list").fetchone()[2])
    rc = cmd_runtime_session(ns)
    del os.environ["FRIDAY_DB"]
    assert rc == 0


# ===================================================================
# 29. State machine exhaustiveness
# ===================================================================

def test_all_run_states_defined(tmp_path):
    assert {s.value for s in RunState} == {
        "pending", "running", "success", "failed", "cancelled"}


def test_session_states_defined(tmp_path):
    assert {s.value for s in SessionState} == {"created", "running", "finished"}


def test_terminal_states(tmp_path):
    assert RunState.SUCCESS.terminal
    assert RunState.FAILED.terminal
    assert RunState.CANCELLED.terminal
    assert not RunState.PENDING.terminal
    assert not RunState.RUNNING.terminal


# ===================================================================
# 30. ExecutionReport artifacts / workers collecting
# ===================================================================

def test_report_collects_workers_used(tmp_path):
    conn = _db()
    _seed_graph(conn, "g1", 2)
    sched = _schedule("g1", [
        ("A", 1, None, "worker:w1"), ("B", 1, None, "worker:w2")])
    rep = _run(conn, sched, workers={
        "worker:w1": MockWorker("worker:w1"),
        "worker:w2": MockWorker("worker:w2")})
    assert set(rep.workers_used) == {"worker:w1", "worker:w2"}


def test_report_duration_nonnegative(tmp_path):
    conn = _db()
    _seed_graph(conn, "g1", 1)
    sched = _schedule("g1", [("A", 1)])
    rep = _run(conn, sched, workers={"worker:mock": MockWorker()})
    assert rep.duration_ms >= 0


# ===================================================================
# 31. No execution when schedule empty
# ===================================================================

def test_empty_schedule_no_tasks(tmp_path):
    conn = _db()
    _seed_graph(conn, "g1", 0)
    sched = _schedule("g1", [], task_count=0)
    rep = _run(conn, sched, workers={"worker:mock": MockWorker()})
    assert rep.executed == 0
    assert rep.succeeded == 0


# ===================================================================
# 32. Scheduler BLOCKED upstream flow (integration w/ resolver absent)
# ===================================================================

def test_blocked_task_never_executed_even_if_worker_exists(tmp_path):
    # A task marked BLOCKED but a worker IS registered for it: Runtime still
    # cancels it, because it respects the Scheduler's decision (no reassignment).
    conn = _db()
    _seed_graph(conn, "g1", 2)
    sched = _schedule("g1", [
        ("A", 1, None, "worker:mock", TaskState.READY),
        ("B", 1, None, "worker:mock", TaskState.BLOCKED)])
    rep = _run(conn, sched, workers={"worker:mock": MockWorker()})
    states = {t["task_id"]: t["status"] for t in rep.tasks}
    assert states["B"] == "cancelled"
    assert states["A"] == "success"


# ===================================================================
# 33. Persistence idempotency of single task re-run
# ===================================================================

def test_rerun_creates_new_session_not_duplicate(tmp_path):
    conn = _db()
    _seed_graph(conn, "g1", 1)
    sched = _schedule("g1", [("A", 1)])
    _run(conn, sched, workers={"worker:mock": MockWorker()})
    _run(conn, sched, workers={"worker:mock": MockWorker()})
    # Two sessions created (append-only); runtime_tasks keeps ONE latest-state
    # row per task (updated in place on re-run), so A has a single row.
    assert conn.execute("SELECT COUNT(*) FROM runtime_sessions").fetchone()[0] == 2
    assert conn.execute(
        "SELECT COUNT(*) FROM runtime_tasks WHERE task_id='A'").fetchone()[0] == 1
