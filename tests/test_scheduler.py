"""Task Scheduler regression tests (Milestone 9.4).

50+ tests covering: topological ordering, cycle detection, diamond graph,
independent graph, parallel waves, worker conflict serialization, critical path,
dependency depth, priority ordering, blocked tasks, disabled worker, missing
assignments, stable output, append-only history, schema version, round-trip
serialization, export, idempotency, large graph, single task, empty graph.

The Scheduler NEVER executes — these tests only verify ordering/state math.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from friday.db import connect, now_iso
from friday.worker.engine import WorkerRegistry
from friday.resolver.engine import CapabilityResolver
from friday.scheduler.models import (
    ExecutionSchedule,
    SCHEMA_VERSION,
    ScheduledTask,
    TaskState,
)
from friday.scheduler.scheduler import (
    build_schedule,
    compute_priority,
    compute_waves,
    detect_cycle,
    serialize_worker_conflicts,
)
from friday.scheduler.state import compute_initial_state
from friday.scheduler.timeline import (
    build_timeline,
    critical_path_status,
    max_parallelism,
    order_tasks,
    wave_summary,
)
from friday.scheduler.engine import (
    CycleDetectedError,
    InvalidGraphError,
    MissingAssignmentError,
    TaskScheduler,
)

from friday.worker.models import Worker, WorkerKind


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _db(tmp_path: Path | None = None) -> sqlite3.Connection:
    import tempfile
    if tmp_path is None:
        tmp_path = Path(tempfile.mkdtemp())
    return connect(tmp_path / "scheduler_test.db")


def _register_builtins(conn: sqlite3.Connection) -> WorkerRegistry:
    reg = WorkerRegistry(conn)
    reg.register_builtins()
    return reg


def _make_worker(name, capabilities, languages=None, task_types=None,
                 plan_types=None, status="active", speed="medium", cost="medium"):
    w = Worker(
        name=name, kind=WorkerKind.LLM, capabilities=list(capabilities),
        supported_languages=languages or [], supported_task_types=task_types or [],
        supported_plan_types=plan_types or [], estimated_speed=speed,
        estimated_cost=cost, confidence="medium", status=status)
    w.id = w._generate_id()
    return w


def _seed_graph(conn, graph_id, tasks, edges=None, critical_path=None):
    """Insert a minimal task graph + tasks + edges for scheduler tests."""
    edges = edges or []
    now = now_iso()
    # Allow intentionally-dangling edges (bad-graph tests): disable FK for the
    # whole seed, restore it before returning.
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute(
        "INSERT OR REPLACE INTO plans (id, goal, plan_type, confidence, status, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?)",
        ("plan:test", "test goal", "feature", "medium", "planned", now, now))
    levels = {}
    # crude level assignment from edges: depth = longest path to root
    levels = _compute_levels(tasks, edges)
    conn.execute(
        "INSERT OR REPLACE INTO task_graphs "
        "(id, goal, plan_id, plan_type, task_count, edge_count, "
        "critical_path_length, parallel_groups, status, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (graph_id, "test goal", "plan:test", "feature", len(tasks),
         len(edges), len(critical_path or []), 0, "compiled", now, now))
    for t in tasks:
        conn.execute(
            "INSERT OR REPLACE INTO tasks "
            "(id, graph_id, plan_id, milestone_order, title, description, "
             "task_type, required_capabilities, complexity, priority, "
             "estimated_effort, dependencies, inputs, outputs, "
             "acceptance_criteria, verification, rollback, evidence, status, "
             "confidence, sequence) "
            "VALUES (?,?,?,0,?, '', 'implementation', ?, 'medium', ?, 'medium', "
            "'', '[]', '[]', '[\"done\"]', "
            "'[{\"method\":\"check\",\"detail\":\"x\"}]', "
            "'[{\"strategy\":\"undo\",\"detail\":\"x\"}]', '[]', 'pending', "
            "'medium', ?)",
            (t["id"], graph_id, "plan:test", t["id"], t["req"],
             t.get("priority", "medium"), levels.get(t["id"], 0) + 1))
    for i, e in enumerate(edges):
        conn.execute(
            "INSERT OR REPLACE INTO task_edges (id, graph_id, from_task, to_task, kind) "
            "VALUES (?,?,?,?, 'depends_on')",
            (f"{graph_id}#e{i}", graph_id, e[0], e[1]))
    conn.commit()
    conn.execute("PRAGMA foreign_keys=ON")


def _compute_levels(tasks, edges):
    """BFS longest-path depth for level assignment."""
    deps = {t["id"]: [] for t in tasks}
    for f, to in edges:
        if f in deps and to in deps:
            deps[to].append(f)
    levels = {}

    def depth(n, seen=None):
        seen = seen or set()
        if n in levels:
            return levels[n]
        if n in seen:
            return 0
        seen.add(n)
        ds = [depth(p, seen) + 1 for p in deps[n]] if deps[n] else [0]
        levels[n] = max(ds)
        return levels[n]
    for t in tasks:
        depth(t["id"])
    return levels


def _resolve(conn, graph_id):
    return CapabilityResolver(conn).resolve_graph(graph_id)


# ===================================================================
# 1. Topological ordering
# ===================================================================

def test_topological_chain_order():
    """A->B->C produces waves 1,2,3."""
    conn = _db()
    _register_builtins(conn)
    _seed_graph(conn, "g1",
                [{"id": "A", "req": "python"}, {"id": "B", "req": "python"},
                 {"id": "C", "req": "python"}],
                edges=[("A", "B"), ("B", "C")])
    _resolve(conn, "g1")
    sched = TaskScheduler(conn).schedule_graph("g1")
    waves = {t.task_id: t.wave for t in sched.schedule.tasks}
    assert waves["A"] == 1 and waves["B"] == 2 and waves["C"] == 3
    conn.close()


def test_topological_respects_edges():
    """C depends on A and B; C is in a later wave than both."""
    conn = _db()
    _register_builtins(conn)
    _seed_graph(conn, "g1",
                [{"id": "A", "req": "python"}, {"id": "B", "req": "python"},
                 {"id": "C", "req": "python"}],
                edges=[("A", "C"), ("B", "C")])
    _resolve(conn, "g1")
    sched = TaskScheduler(conn).schedule_graph("g1")
    waves = {t.task_id: t.wave for t in sched.schedule.tasks}
    assert waves["C"] > waves["A"] and waves["C"] > waves["B"]
    conn.close()


# ===================================================================
# 2. Cycle detection
# ===================================================================

def test_cycle_detected_rejects():
    conn = _db()
    _register_builtins(conn)
    _seed_graph(conn, "g1",
                [{"id": "A", "req": "python"}, {"id": "B", "req": "python"}],
                edges=[("A", "B"), ("B", "A")])
    # A cyclic graph cannot be validly resolved (frozen contract rejects it),
    # so the Scheduler must reject it directly on the raw graph.
    with pytest.raises(CycleDetectedError):
        TaskScheduler(conn).schedule_graph("g1")
    conn.close()


def test_cycle_reported_path():
    conn = _db()
    _register_builtins(conn)
    _seed_graph(conn, "g1",
                [{"id": "A", "req": "python"}, {"id": "B", "req": "python"},
                 {"id": "C", "req": "python"}],
                edges=[("A", "B"), ("B", "C"), ("C", "A")])
    try:
        TaskScheduler(conn).schedule_graph("g1")
        assert False, "should raise"
    except CycleDetectedError as e:
        assert "A" in str(e) and "C" in str(e)
    conn.close()


def test_detect_cycle_pure():
    assert detect_cycle(["A", "B", "C"],
                        [{"from": "A", "to": "B"}, {"from": "B", "to": "C"}]) is None
    cyc = detect_cycle(["A", "B"], [{"from": "A", "to": "B"}, {"from": "B", "to": "A"}])
    assert cyc is not None


def test_detect_cycle_self_loop():
    cyc = detect_cycle(["A"], [{"from": "A", "to": "A"}])
    assert cyc is not None


# ===================================================================
# 3. Diamond graph
# ===================================================================

def test_diamond_graph_waves():
    """A / B C / D  -> waves 1, 2 (B,C), 3."""
    conn = _db()
    _register_builtins(conn)
    _seed_graph(conn, "g1",
                [{"id": "A", "req": "python"}, {"id": "B", "req": "python"},
                 {"id": "C", "req": "python"}, {"id": "D", "req": "python"}],
                edges=[("A", "B"), ("A", "C"), ("B", "D"), ("C", "D")])
    _resolve(conn, "g1")
    sched = TaskScheduler(conn).schedule_graph("g1")
    waves = {t.task_id: t.wave for t in sched.schedule.tasks}
    assert waves["A"] == 1
    assert waves["B"] == waves["C"] == 2
    assert waves["D"] == 3
    conn.close()


def test_diamond_parallel_mid_wave():
    conn = _db()
    _register_builtins(conn)
    _seed_graph(conn, "g1",
                [{"id": "A", "req": "python"}, {"id": "B", "req": "python"},
                 {"id": "C", "req": "python"}, {"id": "D", "req": "python"}],
                edges=[("A", "B"), ("A", "C"), ("B", "D"), ("C", "D")])
    _resolve(conn, "g1")
    sched = TaskScheduler(conn).schedule_graph("g1")
    w2 = [t.task_id for t in sched.schedule.tasks if t.wave == 2]
    assert sorted(w2) == ["B", "C"]
    assert sched.schedule.max_parallelism >= 2
    conn.close()


# ===================================================================
# 4. Independent graph
# ===================================================================

def test_independent_tasks_same_wave():
    conn = _db()
    _register_builtins(conn)
    _seed_graph(conn, "g1",
                [{"id": "A", "req": "python"}, {"id": "B", "req": "python"},
                 {"id": "C", "req": "python"}])
    _resolve(conn, "g1")
    sched = TaskScheduler(conn).schedule_graph("g1")
    waves = {t.task_id: t.wave for t in sched.schedule.tasks}
    assert waves["A"] == waves["B"] == waves["C"] == 1
    assert sched.schedule.wave_count == 1
    conn.close()


# ===================================================================
# 5. Parallel waves
# ===================================================================

def test_parallel_waves_count():
    conn = _db()
    _register_builtins(conn)
    _seed_graph(conn, "g1",
                [{"id": "A", "req": "python"}, {"id": "B", "req": "python"},
                 {"id": "C", "req": "python"}],
                edges=[("A", "B"), ("B", "C")])
    _resolve(conn, "g1")
    sched = TaskScheduler(conn).schedule_graph("g1")
    assert sched.schedule.wave_count == 3
    ws = wave_summary(sched.schedule)
    assert [w["wave"] for w in ws] == [1, 2, 3]
    assert [w["count"] for w in ws] == [1, 1, 1]
    conn.close()


# ===================================================================
# 6. Worker conflict serialization
# ===================================================================

def test_worker_conflict_serialized():
    """Same worker on two wave-mates: serialized via estimated_start order."""
    conn = _db()
    reg = _register_builtins(conn)
    # Register a single worker ("Solo") that exclusively supports 'rust'.
    w = _make_worker("Solo", ["Rust"], task_types=["implementation"],
                     plan_types=["feature"])
    reg.register(w)
    _seed_graph(conn, "g1",
                [{"id": "A", "req": "rust"}, {"id": "B", "req": "rust"}])
    _resolve(conn, "g1")
    sched = TaskScheduler(conn).schedule_graph("g1")
    tasks = {t.task_id: t for t in sched.schedule.tasks}
    # Resolver owns assignment (builtins may win on capability match); the
    # Scheduler's job is to SERIALIZE whichever single worker both tasks share.
    assert tasks["A"].worker_id is not None
    assert tasks["A"].worker_id == tasks["B"].worker_id
    # Same worker, same wave -> estimated_start differs deterministically.
    assert tasks["A"].estimated_start != tasks["B"].estimated_start
    conn.close()


def test_worker_conflict_order_by_task_id():
    conn = _db()
    reg = _register_builtins(conn)
    w = _make_worker("Solo", ["Rust"], task_types=["implementation"],
                     plan_types=["feature"])
    reg.register(w)
    _seed_graph(conn, "g1",
                [{"id": "Z", "req": "rust"}, {"id": "A", "req": "rust"}])
    _resolve(conn, "g1")
    sched = TaskScheduler(conn).schedule_graph("g1")
    tasks = {t.task_id: t for t in sched.schedule.tasks}
    # 'A' sorts before 'Z' -> earlier estimated_start.
    assert tasks["A"].estimated_start < tasks["Z"].estimated_start
    conn.close()


# ===================================================================
# 7. Critical path
# ===================================================================

def test_critical_path_recorded():
    conn = _db()
    _register_builtins(conn)
    _seed_graph(conn, "g1",
                [{"id": "A", "req": "python"}, {"id": "B", "req": "python"},
                 {"id": "C", "req": "python"}],
                edges=[("A", "B"), ("B", "C")],
                critical_path=["A", "B", "C"])
    _resolve(conn, "g1")
    sched = TaskScheduler(conn).schedule_graph("g1")
    # Critical path is carried from the validated graph (engine-computed).
    assert set(sched.schedule.critical_path) == {"A", "B", "C"}
    assert sched.schedule.critical_path_length == 3
    conn.close()


def test_critical_path_priority_bonus():
    """Tasks on the critical path get a higher priority."""
    conn = _db()
    _register_builtins(conn)
    _seed_graph(conn, "g1",
                [{"id": "A", "req": "python"}, {"id": "B", "req": "python"},
                 {"id": "off", "req": "python"}],
                edges=[("A", "B")], critical_path=["A", "B"])
    _resolve(conn, "g1")
    sched = TaskScheduler(conn).schedule_graph("g1")
    tasks = {t.task_id: t for t in sched.schedule.tasks}
    assert tasks["A"].priority > tasks["off"].priority
    assert tasks["B"].priority > tasks["off"].priority
    conn.close()


# ===================================================================
# 8. Dependency depth
# ===================================================================

def test_dependency_depth_priority():
    conn = _db()
    _register_builtins(conn)
    _seed_graph(conn, "g1",
                [{"id": "root", "req": "python"},
                 {"id": "mid", "req": "python"},
                 {"id": "leaf", "req": "python"}],
                edges=[("root", "mid"), ("mid", "leaf")])
    _resolve(conn, "g1")
    sched = TaskScheduler(conn).schedule_graph("g1")
    tasks = {t.task_id: t for t in sched.schedule.tasks}
    assert tasks["leaf"].priority > tasks["root"].priority
    assert tasks["leaf"].dependency_count == 2
    assert tasks["root"].dependency_count == 0
    conn.close()


# ===================================================================
# 9. Priority ordering
# ===================================================================

def test_priority_order_in_timeline():
    conn = _db()
    _register_builtins(conn)
    _seed_graph(conn, "g1",
                [{"id": "A", "req": "python"}, {"id": "B", "req": "python"},
                 {"id": "C", "req": "python"}],
                edges=[("A", "B"), ("B", "C")], critical_path=["A", "B", "C"])
    _resolve(conn, "g1")
    sched = TaskScheduler(conn).schedule_graph("g1")
    # Priority reflects critical-path + dependency depth: C (deepest, on CP)
    # outranks A (root). Timeline order itself is wave-ordered (A before C).
    tasks = {t.task_id: t for t in sched.schedule.tasks}
    assert tasks["C"].priority > tasks["A"].priority
    ordered = order_tasks(sched.schedule.tasks)
    pos = {t.task_id: i for i, t in enumerate(ordered)}
    assert pos["A"] < pos["C"]  # wave order: A (wave 1) before C (wave 3)
    conn.close()


def test_priority_explicit_band():
    assert compute_priority("x", {}, [], "critical") > \
        compute_priority("x", {}, [], "low")


def test_priority_tie_break_task_id():
    conn = _db()
    _register_builtins(conn)
    _seed_graph(conn, "g1",
                [{"id": "Z", "req": "python"}, {"id": "A", "req": "python"}])
    _resolve(conn, "g1")
    sched = TaskScheduler(conn).schedule_graph("g1")
    ordered = order_tasks(sched.schedule.tasks)
    assert ordered[0].task_id == "A"  # tie -> alphabetical
    conn.close()


# ===================================================================
# 10. Blocked tasks
# ===================================================================

def test_blocked_missing_assignment():
    """A task with no resolver assignment -> schedule rejected."""
    conn = _db()
    _register_builtins(conn)
    _seed_graph(conn, "g1",
                [{"id": "A", "req": "python"}, {"id": "B", "req": "python"}])
    # Resolve only seeds assignment rows via resolve_graph; skip for B:
    _resolve(conn, "g1")
    # Delete B's assignment to simulate missing.
    conn.execute("DELETE FROM resolver_assignments WHERE task_id = 'B'")
    conn.commit()
    with pytest.raises(MissingAssignmentError):
        TaskScheduler(conn).schedule_graph("g1")
    conn.close()


def test_blocked_disabled_worker():
    """Assigned worker disabled -> task BLOCKED (never auto-reassigned)."""
    conn = _db()
    reg = _register_builtins(conn)
    _seed_graph(conn, "g1", [{"id": "A", "req": "python"}])
    _resolve(conn, "g1")
    # Disable the worker that got A (exact registry name, case-sensitive).
    from friday.resolver.engine import CapabilityResolver
    a = CapabilityResolver(conn).assignment_for_task("A")
    disabled_name = reg.worker_by_id(a.worker_id).name
    reg.disable(disabled_name)
    sched = TaskScheduler(conn).schedule_graph("g1")
    t = sched.schedule.task_by_id("A")
    assert t.status == TaskState.BLOCKED
    assert "disabled" in t.blocked_reason.lower()
    assert "A" in sched.blocked
    conn.close()


def test_not_ready_with_predecessors():
    """A task with dependencies starts NOT_READY (predecessors incomplete)."""
    conn = _db()
    _register_builtins(conn)
    _seed_graph(conn, "g1",
                [{"id": "A", "req": "python"}, {"id": "B", "req": "python"}],
                edges=[("A", "B")])
    _resolve(conn, "g1")
    sched = TaskScheduler(conn).schedule_graph("g1")
    tasks = {t.task_id: t for t in sched.schedule.tasks}
    assert tasks["B"].status == TaskState.NOT_READY
    assert tasks["A"].status == TaskState.READY
    conn.close()


def test_compute_initial_state_rules():
    class Fake: pass
    t = Fake(); t.dependencies = []
    s, r = compute_initial_state(t, "worker:x", "assigned", {"worker:x"})
    assert s == TaskState.READY and r == ""
    s, r = compute_initial_state(t, None, "unresolved", {"worker:x"})
    assert s == TaskState.BLOCKED and "assignment" in r
    t2 = Fake(); t2.dependencies = ["p"]
    s, r = compute_initial_state(t2, "worker:x", "assigned", {"worker:x"})
    assert s == TaskState.NOT_READY
    s, r = compute_initial_state(t, "worker:x", "assigned", set())
    assert s == TaskState.BLOCKED and "disabled" in r


# ===================================================================
# 11. Missing assignments
# ===================================================================

def test_missing_assignment_reports_all():
    conn = _db()
    _register_builtins(conn)
    _seed_graph(conn, "g1",
                [{"id": "A", "req": "python"}, {"id": "B", "req": "python"}])
    _resolve(conn, "g1")
    conn.execute("DELETE FROM resolver_assignments WHERE task_id = 'B'")
    conn.commit()
    try:
        TaskScheduler(conn).schedule_graph("g1")
        assert False
    except MissingAssignmentError as e:
        assert "B" in str(e)
    conn.close()


# ===================================================================
# 12. Stable output
# ===================================================================

def test_stable_output():
    conn = _db()
    _register_builtins(conn)
    _seed_graph(conn, "g1",
                [{"id": "A", "req": "python"}, {"id": "B", "req": "python"},
                 {"id": "C", "req": "python"}],
                edges=[("A", "B"), ("B", "C")])
    _resolve(conn, "g1")
    r1 = TaskScheduler(conn).schedule_graph("g1")
    r2 = TaskScheduler(conn).schedule_graph("g1")
    w1 = {t.task_id: t.wave for t in r1.schedule.tasks}
    w2 = {t.task_id: t.wave for t in r2.schedule.tasks}
    assert w1 == w2
    p1 = {t.task_id: t.priority for t in r1.schedule.tasks}
    p2 = {t.task_id: t.priority for t in r2.schedule.tasks}
    assert p1 == p2
    conn.close()


def test_idempotent_re_schedule():
    conn = _db()
    _register_builtins(conn)
    _seed_graph(conn, "g1",
                [{"id": "A", "req": "python"}, {"id": "B", "req": "python"}])
    _resolve(conn, "g1")
    TaskScheduler(conn).schedule_graph("g1")
    TaskScheduler(conn).schedule_graph("g1")
    tasks = TaskScheduler(conn).tasks("g1")
    # One row per task (UPDATE in place, not duplicate).
    assert len(tasks) == 2
    conn.close()


# ===================================================================
# 13. Append-only history
# ===================================================================

def test_history_append_only():
    conn = _db()
    _register_builtins(conn)
    _seed_graph(conn, "g1", [{"id": "A", "req": "python"}])
    _resolve(conn, "g1")
    sched = TaskScheduler(conn)
    sched.schedule_graph("g1")
    h1 = len(sched.history())
    sched.schedule_graph("g1")
    h2 = len(sched.history())
    assert h2 > h1
    conn.close()


def test_evolution_on_reblock():
    conn = _db()
    reg = _register_builtins(conn)
    _seed_graph(conn, "g1", [{"id": "A", "req": "python"}])
    _resolve(conn, "g1")
    sched = TaskScheduler(conn)
    sched.schedule_graph("g1")
    a = CapabilityResolver(conn).assignment_for_task("A")
    reg.disable(reg.worker_by_id(a.worker_id).name)
    sched.schedule_graph("g1")
    evo = sched.evolution("g1")
    assert isinstance(evo, list)  # append-only evolution exists
    conn.close()


# ===================================================================
# 14. Schema version
# ===================================================================

def test_schema_version():
    assert SCHEMA_VERSION == "1.0"


def test_scheduled_task_schema_version():
    t = ScheduledTask(
        schedule_id="g1:A", graph_id="g1", assignment_id="g1:A",
        task_id="A", worker_id="worker:x", phase="wave-1", wave=1,
        status=TaskState.READY, priority=10, dependency_count=0,
        created_at=now_iso(), updated_at=now_iso())
    assert t.to_row()["schema_version"] == SCHEMA_VERSION


# ===================================================================
# 15. Round-trip serialization
# ===================================================================

def test_scheduled_task_round_trip():
    t = ScheduledTask(
        schedule_id="g1:A", graph_id="g1", assignment_id="g1:A",
        task_id="A", worker_id="worker:x", phase="wave-1", wave=1,
        status=TaskState.READY, priority=10, dependency_count=0,
        dependencies=["p"], estimated_start=0, estimated_finish=1,
        blocked_reason="", confidence="high", selection_strategy="single",
        created_at=now_iso(), updated_at=now_iso())
    row = t.to_row()
    d = t.to_dict()
    assert d["task_id"] == "A"
    assert d["status"] == "ready"
    assert d["dependencies"] == ["p"]
    assert row["wave"] == 1


def test_execution_schedule_to_dict():
    t = ScheduledTask(
        schedule_id="g1:A", graph_id="g1", assignment_id="g1:A",
        task_id="A", worker_id="worker:x", phase="wave-1", wave=1,
        status=TaskState.READY, priority=10, dependency_count=0,
        created_at=now_iso(), updated_at=now_iso())
    s = ExecutionSchedule(
        schedule_id="g1", graph_id="g1", task_count=1, wave_count=1,
        critical_path=["A"], tasks=[t])
    d = s.to_dict()
    assert d["schedule_id"] == "g1"
    assert d["waves"] == [["A"]]
    assert d["tasks"][0]["task_id"] == "A"


def test_execution_schedule_waves():
    ts = [
        ScheduledTask(schedule_id=f"g:{i}", graph_id="g", assignment_id=f"g:{i}",
                      task_id=f"t{i}", worker_id="w", phase="x", wave=w,
                      status=TaskState.READY, priority=0, dependency_count=0)
        for i, w in enumerate([1, 2, 1, 3])]
    s = ExecutionSchedule(schedule_id="g", graph_id="g", tasks=ts, wave_count=3)
    assert s.waves() == [["t0", "t2"], ["t1"], ["t3"]]


# ===================================================================
# 16. Export
# ===================================================================

def test_export_json():
    import io, contextlib, os, tempfile
    from friday.cli_scheduler import cmd_scheduler_export
    conn = _db()
    dbpath = conn.execute("PRAGMA database_list").fetchall()
    # Point the CLI at the same temp DB.
    tmp = tempfile.mkdtemp()
    db_file = Path(tmp) / "exp.db"
    conn.close()
    conn = connect(db_file)
    os.environ["FRIDAY_DB"] = str(db_file)
    _register_builtins(conn)
    _seed_graph(conn, "g1", [{"id": "A", "req": "python"}])
    _resolve(conn, "g1")
    TaskScheduler(conn).schedule_graph("g1")
    conn.close()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cmd_scheduler_export(type("A", (), {"token": None})())
    del os.environ["FRIDAY_DB"]
    assert rc == 0
    payload = json.loads(buf.getvalue())
    assert payload["schedule_count"] == 1
    assert payload["tasks"][0]["task_id"] == "A"


def test_export_via_tasks():
    conn = _db()
    _register_builtins(conn)
    _seed_graph(conn, "g1", [{"id": "A", "req": "python"}])
    _resolve(conn, "g1")
    TaskScheduler(conn).schedule_graph("g1")
    tasks = TaskScheduler(conn).tasks("g1")
    data = json.dumps({"tasks": tasks})
    assert json.loads(data)["tasks"][0]["task_id"] == "A"
    conn.close()


# ===================================================================
# 17. Single task / empty graph
# ===================================================================

def test_single_task_wave_one():
    conn = _db()
    _register_builtins(conn)
    _seed_graph(conn, "g1", [{"id": "A", "req": "python"}])
    _resolve(conn, "g1")
    sched = TaskScheduler(conn).schedule_graph("g1")
    assert sched.schedule.wave_count == 1
    assert sched.schedule.tasks[0].status == TaskState.READY
    conn.close()


def test_empty_graph_no_tasks():
    conn = _db()
    _register_builtins(conn)
    _seed_graph(conn, "g1", [])
    sched = TaskScheduler(conn).schedule_graph("g1")
    assert sched.schedule.task_count == 0
    assert sched.schedule.wave_count == 0
    conn.close()


def test_unknown_graph_rejected():
    conn = _db()
    _register_builtins(conn)
    with pytest.raises(InvalidGraphError):
        TaskScheduler(conn).schedule_graph("nope")
    conn.close()


def test_dangling_edge_rejected():
    conn = _db()
    _register_builtins(conn)
    _seed_graph(conn, "g1", [{"id": "A", "req": "python"}],
                edges=[("A", "GHOST")])
    with pytest.raises(InvalidGraphError):
        TaskScheduler(conn).schedule_graph("g1")
    conn.close()


# ===================================================================
# 18. Large graph
# ===================================================================

def test_large_graph_deterministic():
    conn = _db()
    _register_builtins(conn)
    n = 50
    tasks = [{"id": f"T{i}", "req": "python"} for i in range(n)]
    edges = [(f"T{i}", f"T{i+1}") for i in range(n - 1)]
    _seed_graph(conn, "g1", tasks, edges=edges)
    _resolve(conn, "g1")
    r1 = TaskScheduler(conn).schedule_graph("g1")
    r2 = TaskScheduler(conn).schedule_graph("g1")
    w1 = {t.task_id: t.wave for t in r1.schedule.tasks}
    w2 = {t.task_id: t.wave for t in r2.schedule.tasks}
    assert w1 == w2
    assert r1.schedule.wave_count == n
    conn.close()


def test_large_graph_wide_parallel():
    conn = _db()
    _register_builtins(conn)
    n = 40
    tasks = [{"id": f"T{i}", "req": "python"} for i in range(n)]
    _seed_graph(conn, "g1", tasks)  # all independent
    _resolve(conn, "g1")
    sched = TaskScheduler(conn).schedule_graph("g1")
    assert sched.schedule.wave_count == 1
    assert sched.schedule.max_parallelism == n
    conn.close()


# ===================================================================
# 19. Timeline / priority helpers
# ===================================================================

def test_build_timeline_order():
    conn = _db()
    _register_builtins(conn)
    _seed_graph(conn, "g1",
                [{"id": "A", "req": "python"}, {"id": "B", "req": "python"},
                 {"id": "C", "req": "python"}],
                edges=[("A", "B"), ("B", "C")], critical_path=["A", "B", "C"])
    _resolve(conn, "g1")
    sched = TaskScheduler(conn).schedule_graph("g1")
    tl = build_timeline(sched.schedule)
    # Timeline is wave-ordered: wave-1 first. C is deepest (wave 3, on CP).
    assert [s["task_id"] for s in tl] == ["A", "B", "C"]
    assert tl[0]["wave"] == 1 and tl[-1]["wave"] == 3
    assert len(tl) == 3
    conn.close()


def test_critical_path_status_clean():
    conn = _db()
    _register_builtins(conn)
    _seed_graph(conn, "g1",
                [{"id": "A", "req": "python"}, {"id": "B", "req": "python"}],
                edges=[("A", "B")], critical_path=["A", "B"])
    _resolve(conn, "g1")
    sched = TaskScheduler(conn).schedule_graph("g1")
    cps = critical_path_status(sched.schedule)
    assert cps["blocked_on_critical_path"] == []
    conn.close()


def test_compute_waves_pure():
    waves = compute_waves(["A", "B", "C"],
                          [{"from": "A", "to": "B"}, {"from": "B", "to": "C"}],
                          {"A": 0, "B": 1, "C": 2})
    assert waves == {"A": 1, "B": 2, "C": 3}


def test_max_parallelism_pure():
    ts = [
        ScheduledTask(schedule_id=f"g:{i}", graph_id="g", assignment_id=f"g:{i}",
                      task_id=f"t{i}", worker_id="w", phase="x", wave=w,
                      status=TaskState.READY, priority=0, dependency_count=0)
        for i, w in enumerate([1, 1, 2])]
    s = ExecutionSchedule(schedule_id="g", graph_id="g", tasks=ts, wave_count=2)
    assert max_parallelism(s) == 2


def test_serialize_worker_conflicts_pure():
    ts = [
        ScheduledTask(schedule_id="g:A", graph_id="g", assignment_id="g:A",
                      task_id="A", worker_id="w", phase="x", wave=1,
                      status=TaskState.READY, priority=0, dependency_count=0),
        ScheduledTask(schedule_id="g:Z", graph_id="g", assignment_id="g:Z",
                      task_id="Z", worker_id="w", phase="x", wave=1,
                      status=TaskState.READY, priority=0, dependency_count=0),
    ]
    serialize_worker_conflicts(ts)
    by = {t.task_id: t for t in ts}
    assert by["A"].estimated_start < by["Z"].estimated_start


# ===================================================================
# 20. State machine boundaries
# ===================================================================

def test_scheduler_creates_only_initial_states():
    """Scheduler never initializes SCHEDULED/COMPLETE/FAILED/CANCELLED."""
    conn = _db()
    _register_builtins(conn)
    _seed_graph(conn, "g1",
                [{"id": "A", "req": "python"}, {"id": "B", "req": "python"}],
                edges=[("A", "B")])
    _resolve(conn, "g1")
    sched = TaskScheduler(conn).schedule_graph("g1")
    for t in sched.schedule.tasks:
        assert t.status in (TaskState.READY, TaskState.NOT_READY,
                            TaskState.BLOCKED)
    conn.close()


def test_no_execution_side_effects():
    """Scheduler does not mutate worker status or task status in the registry."""
    conn = _db()
    reg = _register_builtins(conn)
    _seed_graph(conn, "g1", [{"id": "A", "req": "python"}])
    _resolve(conn, "g1")
    before = {w.id: w.status for w in reg.active_workers()}
    TaskScheduler(conn).schedule_graph("g1")
    after = {w.id: w.status for w in reg.active_workers()}
    assert before == after
    conn.close()


# ===================================================================
# 21. Dogfood graphs
# ===================================================================

def test_dogfood_simple_chain():
    conn = _db()
    _register_builtins(conn)
    _seed_graph(conn, "g1",
                [{"id": "Implement", "req": "python"},
                 {"id": "Test", "req": "python"},
                 {"id": "Deploy", "req": "python"}],
                edges=[("Implement", "Test"), ("Test", "Deploy")])
    _resolve(conn, "g1")
    sched = TaskScheduler(conn).schedule_graph("g1")
    waves = {t.task_id: t.wave for t in sched.schedule.tasks}
    assert waves["Implement"] < waves["Test"] < waves["Deploy"]
    conn.close()


def test_dogfood_parallel_graph():
    conn = _db()
    _register_builtins(conn)
    _seed_graph(conn, "g1",
                [{"id": "A", "req": "python"}, {"id": "B", "req": "python"},
                 {"id": "C", "req": "python"}])
    _resolve(conn, "g1")
    sched = TaskScheduler(conn).schedule_graph("g1")
    assert sched.schedule.wave_count == 1
    assert sched.schedule.max_parallelism == 3
    conn.close()


def test_dogfood_diamond_graph():
    conn = _db()
    _register_builtins(conn)
    _seed_graph(conn, "g1",
                [{"id": "A", "req": "python"}, {"id": "B", "req": "python"},
                 {"id": "C", "req": "python"}, {"id": "D", "req": "python"}],
                edges=[("A", "B"), ("A", "C"), ("B", "D"), ("C", "D")])
    _resolve(conn, "g1")
    sched = TaskScheduler(conn).schedule_graph("g1")
    waves = {t.task_id: t.wave for t in sched.schedule.tasks}
    assert waves["A"] == 1 and waves["D"] == 3
    assert sched.schedule.wave_count == 3
    conn.close()


def test_dogfood_independent_graph():
    conn = _db()
    _register_builtins(conn)
    _seed_graph(conn, "g1",
                [{"id": "X", "req": "python"}, {"id": "Y", "req": "python"},
                 {"id": "Z", "req": "python"}])
    _resolve(conn, "g1")
    sched = TaskScheduler(conn).schedule_graph("g1")
    ws = wave_summary(sched.schedule)
    assert ws[0]["count"] == 3
    conn.close()


def test_dogfood_mixed_priority():
    """Deep + critical-path task outranks shallow independent task."""
    conn = _db()
    _register_builtins(conn)
    _seed_graph(conn, "g1",
                [{"id": "A", "req": "python"}, {"id": "B", "req": "python"},
                 {"id": "C", "req": "python"}, {"id": "Side", "req": "python"}],
                edges=[("A", "B"), ("B", "C")], critical_path=["A", "B", "C"])
    _resolve(conn, "g1")
    sched = TaskScheduler(conn).schedule_graph("g1")
    tasks = {t.task_id: t for t in sched.schedule.tasks}
    assert tasks["C"].priority > tasks["Side"].priority
    conn.close()
