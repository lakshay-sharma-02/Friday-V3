"""Dogfood test: Runtime Observer closes the Learning loop (Law 17).

Proves the full cycle:
  Runtime execution → Review → Observation → Knowledge

1. Set up a completed runtime session (simulated).
2. Run `friday observe` via the ObservationEngine (which runs RuntimeObserver).
3. Build Knowledge from observations.
4. Assert knowledge entries exist whose evidence traces back to the runtime
   observation facts.
5. Assert the deterministic path surfaces execution reliability info.

This follows the pattern in test_execution_dogfood.py / test_planning_dogfood.py
and tests that the observer pattern closes the loop without any new layer.
"""

from __future__ import annotations

import pytest

from friday.db import (
    connect,
    insert_observations,
    observations_all,
)
from friday.observation import (
    Confidence,
    Observation,
    ObservationEngine,
    ObserverRegistry,
    RuntimeObserver,
    now_iso,
)
from friday.knowledge import KnowledgeEngine, KnowledgeType


# ---------------------------------------------------------------------------
# Helpers: set up a realistic session with tasks, graph, and results
# ---------------------------------------------------------------------------

def _setup_runtime_session(conn) -> None:
    """Create a completed runtime session with tasks and results in the DB."""
    _now = now_iso()

    # Plan (needed for task_graphs FK)
    conn.execute(
        """INSERT OR REPLACE INTO plans
           (id, goal, plan_type, confidence, status, milestones, dependencies,
            risks, verification, rollback, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("plan:improve-docs", "Improve project documentation",
         "engineering", "medium", "planned",
         "[]", "[]", "[]", "[]", "[]", _now, _now),
    )

    # Graph
    conn.execute(
        """INSERT OR REPLACE INTO task_graphs
           (id, goal, plan_id, plan_type, task_count, edge_count,
            critical_path_length, parallel_groups, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("graph-dogfood-1", "Improve project documentation",
         "plan:improve-docs", "engineering",
         3, 2, 2, 1, "compiled", _now, _now),
    )
    # Task definitions (with required_capabilities)
    tasks_defs = [
        ("doc-task-1", "graph-dogfood-1", "Write API docs", "writing"),
        ("doc-task-2", "graph-dogfood-1", "Add examples", "writing, markdown"),
        ("doc-task-3", "graph-dogfood-1", "Generate README", "markdown"),
    ]
    for tid, gid, title, caps in tasks_defs:
        conn.execute(
            """INSERT OR REPLACE INTO tasks
               (id, graph_id, plan_id, milestone_order, title, description, task_type,
                required_capabilities, complexity, priority, estimated_effort,
                dependencies, inputs, outputs, acceptance_criteria, verification,
                rollback, evidence, status, confidence, sequence)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (tid, gid, "plan:improve-docs", 0, title, "", "general",
             caps, "medium", "medium", "medium",
             "", "[]", "[]", "[]", "[]", "[]", "[]", "pending", "medium", 0),
        )

    # Session
    conn.execute(
        """INSERT OR REPLACE INTO runtime_sessions
           (session_id, schedule_id, state, started_at, finished_at,
            schema_version, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        ("session-dogfood-1", "graph-dogfood-1", "finished",
         _now, _now, "1.0", _now, _now),
    )

    # Tasks (execution records)
    tasks_exec = [
        ("exec-doc-1", "session-dogfood-1", "graph-dogfood-1", "doc-task-1",
         "writer-1", "success"),
        ("exec-doc-2", "session-dogfood-1", "graph-dogfood-1", "doc-task-2",
         "writer-1", "success"),
        ("exec-doc-3", "session-dogfood-1", "graph-dogfood-1", "doc-task-3",
         "markdown-bot", "failed"),
    ]
    for eid, sid, sched, tid, wid, status in tasks_exec:
        conn.execute(
            """INSERT OR REPLACE INTO runtime_tasks
               (execution_id, session_id, schedule_id, task_id, worker_id,
                wave, attempt, status, started_at, finished_at,
                duration_ms, exit_code, error, schema_version, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (eid, sid, sched, tid, wid,
             1, 1, status, _now, _now, 500, 0, "", "1.0", _now, _now),
        )

    # Results (with verification info)
    results = [
        ("exec-doc-1", "session-dogfood-1", "doc-task-1", "writer-1", 1, 1),
        ("exec-doc-2", "session-dogfood-1", "doc-task-2", "writer-1", 1, 1),
        ("exec-doc-3", "session-dogfood-1", "doc-task-3", "markdown-bot", 0, 0),
    ]
    for eid, sid, tid, wid, success, vp in results:
        conn.execute(
            """INSERT INTO runtime_results
               (execution_id, session_id, task_id, worker_id, success, stdout, stderr,
                artifacts, exit_code, duration_ms, error, verification_passed,
                verification_evidence, recorded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (eid, sid, tid, wid, success,
             "", "", "[]", 0, 100, "", vp, "{}", _now),
        )

    conn.commit()


# ===========================================================================
# Tests
# ===========================================================================


def test_runtime_observer_creates_observations_from_realistic_session(tmp_path):
    """Given a realistic completed runtime session, the RuntimeObserver emits
    facts through the ObservationEngine that are persisted to the DB."""
    conn = connect(tmp_path / "dogfood.db")
    _setup_runtime_session(conn)

    reg = ObserverRegistry()
    reg.register(RuntimeObserver())
    run = ObservationEngine(reg, conn).run()

    # Observer ran and collected facts
    assert run.observers[0].name == "runtime"
    assert run.observers[0].health.healthy

    facts = {(o.subject, o.aspect): o for o in run.observers[0].observations}

    # Graph-level outcome
    assert ("graph-dogfood-1", "execution_outcome") in facts
    assert facts[("graph-dogfood-1", "execution_outcome")].value == "failed"

    # Repair required (doc-task-3 failed verification)
    assert ("graph-dogfood-1", "repair_required") in facts
    assert facts[("graph-dogfood-1", "repair_required")].value == "true"

    # Task outcomes
    assert ("doc-task-1", "task_outcome") in facts
    assert facts[("doc-task-1", "task_outcome")].value == "success"
    assert ("doc-task-3", "task_outcome") in facts
    assert facts[("doc-task-3", "task_outcome")].value == "failed"

    # Capability reliability
    assert ("writing", "capability_reliability") in facts
    assert facts[("writing", "capability_reliability")].value == "2/2 success"
    assert ("markdown", "capability_reliability") in facts
    assert facts[("markdown", "capability_reliability")].value == "1/2 success"

    # All persisted to DB
    stored = observations_all(conn)
    assert len(stored) >= len(facts)
    assert all(o.source == "runtime" for o in stored)
    conn.close()


def _add_session(conn, sid: str, schedule_id: str, tasks: list[tuple],
                  graph_goal: str = "Improve project documentation",
                  plan_id: str = "plan:improve-docs") -> None:
    """Add a runtime session with tasks and results to an existing setup."""
    _now = now_iso()
    # Session
    conn.execute(
        """INSERT OR REPLACE INTO runtime_sessions
           (session_id, schedule_id, state, started_at, finished_at,
            schema_version, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (sid, schedule_id, "finished", _now, _now, "1.0", _now, _now),
    )
    # Tasks
    for task_id, caps, status, vp in tasks:
        exec_id = f"exec-{sid}-{task_id}"
        conn.execute(
            """INSERT OR REPLACE INTO runtime_tasks
               (execution_id, session_id, schedule_id, task_id, worker_id,
                wave, attempt, status, started_at, finished_at,
                duration_ms, exit_code, error, schema_version, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (exec_id, sid, schedule_id, task_id,
             "writer-1" if "writing" in caps else "md-bot",
             1, 1, status, _now, _now, 100, 0, "", "1.0", _now, _now),
        )
        conn.execute(
            """INSERT OR REPLACE INTO runtime_results
               (execution_id, session_id, task_id, worker_id, success, stdout, stderr,
                artifacts, exit_code, duration_ms, error, verification_passed,
                verification_evidence, recorded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (exec_id, sid, task_id,
             "writer-1" if "writing" in caps else "md-bot",
             1 if status == "success" else 0,
             "", "", "[]", 0, 100, "", vp, "{}", _now),
        )
    conn.commit()


def test_runtime_observations_persisted_through_engine(tmp_path):
    """Proves the v1 Learning loop: runtime observer facts are created by
    the observer, persisted through the ObservationEngine, and stored in
    the observations table.

    Steps:
    1. Set up runtime session and run observer through the engine.
    2. Verify observations are created with correct facts and confidence.
    3. Verify observations are persisted in the DB.
    4. Verify the knowledge engine runs without error over the data.

    For v1, the key deliverable is that runtime observations enter the
    deterministic observation pipeline. Dedicated knowledge detectors
    for runtime observation types (execution_outcome, capability_reliability)
    can be added later as the knowledge layer evolves.
    """
    conn = connect(tmp_path / "loop.db")
    _setup_runtime_session(conn)

    # Step 1: Run the observer through the engine
    reg = ObserverRegistry()
    reg.register(RuntimeObserver())
    run = ObservationEngine(reg, conn).run()

    observer_result = run.observers[0]
    assert observer_result.name == "runtime"
    assert observer_result.health.healthy

    runtime_obs = observer_result.observations
    assert len(runtime_obs) > 0, "RuntimeObserver should emit observations"

    # Verify key facts
    facts = {(o.subject, o.aspect): o for o in runtime_obs}
    assert ("graph-dogfood-1", "execution_outcome") in facts
    assert ("doc-task-1", "task_outcome") in facts
    assert ("graph-dogfood-1", "repair_required") in facts
    assert ("writing", "capability_reliability") in facts
    assert facts[("writing", "capability_reliability")].confidence is Confidence.DERIVED

    # Step 2: Verify observations are persisted
    stored = observations_all(conn)
    runtime_stored = [o for o in stored if o.source == "runtime"]
    assert len(runtime_stored) >= len(runtime_obs), (
        "Runtime observations should be persisted"
    )
    assert all(o.source == "runtime" for o in runtime_stored)

    # Step 3: Knowledge engine runs without error
    knowledge_eng = KnowledgeEngine(conn)
    report = knowledge_eng.build()
    assert report is not None
    # report.total may be 0 if no detectors matched, but the build itself
    # should not raise.

    conn.close()


def test_knowledge_detectors_create_entries_from_runtime_observations(tmp_path):
    """Proves that the Knowledge detectors (detect_capability_reliability,
    detect_repair_bottlenecks) create Knowledge entries with traceable
    evidence_ids when given sufficient runtime observations.

    Pipeline:
    1. Set up 3+ runtime sessions with a shared capability.
    2. Run observe to create capability_reliability observations.
    3. Build Knowledge.
    4. Assert CAPABILITY_RELIABILITY knowledge exists with evidence_ids
       tracing back to the runtime observations.
    """
    from friday.knowledge.execution import (
        _MIN_CAPABILITY_EVIDENCE,
        _MIN_BOTTLENECK_EVIDENCE,
    )
    from friday.knowledge import get_knowledge_by_type

    conn = connect(tmp_path / "detector.db")

    # Set up the initial graph and task definitions (these define capabilities)
    _now = now_iso()
    conn.execute(
        """INSERT OR REPLACE INTO plans
           (id, goal, plan_type, confidence, status, milestones, dependencies,
            risks, verification, rollback, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("plan:test-caps", "Test capabilities",
         "engineering", "medium", "planned",
         "[]", "[]", "[]", "[]", "[]", _now, _now),
    )
    conn.execute(
        """INSERT OR REPLACE INTO task_graphs
           (id, goal, plan_id, plan_type, task_count, edge_count,
            critical_path_length, parallel_groups, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("graph-caps", "Test capabilities", "plan:test-caps",
         "engineering", 1, 0, 1, 1, "compiled", _now, _now),
    )
    # Task definitions
    conn.execute(
        """INSERT OR REPLACE INTO tasks
           (id, graph_id, plan_id, milestone_order, title, description, task_type,
            required_capabilities, complexity, priority, estimated_effort,
            dependencies, inputs, outputs, acceptance_criteria, verification,
            rollback, evidence, status, confidence, sequence)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("cap-task", "graph-caps", "plan:test-caps", 0, "Test task",
         "", "general", "writing", "medium", "medium", "medium",
         "", "[]", "[]", "[]", "[]", "[]", "[]", "pending", "medium", 0),
    )
    conn.commit()

    reg = ObserverRegistry()
    reg.register(RuntimeObserver())

    # Run N sessions where N >= _MIN_CAPABILITY_EVIDENCE so the
    # detect_capability_reliability detector meets its threshold.
    num_runs = max(_MIN_CAPABILITY_EVIDENCE, _MIN_BOTTLENECK_EVIDENCE) + 2
    for i in range(num_runs):
        sid = f"session-cap-{i}"
        status = "failed" if i % 2 == 0 else "success"  # mix failures
        vp = 0 if i % 2 == 0 else 1
        _add_session(conn, sid, "graph-caps",
                     [("cap-task", "writing", status, vp)])
        # Run observer for this session
        obs_run = ObservationEngine(reg, conn).run()
        # Check we got observations
        assert len(obs_run.observers[0].observations) > 0

    # Now build Knowledge
    knowledge_eng = KnowledgeEngine(conn)
    report = knowledge_eng.build()
    assert report.created > 0 or report.updated > 0, (
        "Knowledge pipeline should create or update entries from "
        "runtime observations"
    )

    # Assert CAPABILITY_RELIABILITY knowledge exists with evidence_ids
    cap_knowledge = get_knowledge_by_type(
        conn, KnowledgeType.CAPABILITY_RELIABILITY.value
    )
    assert len(cap_knowledge) > 0, (
        "CAPABILITY_RELIABILITY knowledge should be created"
    )
    cap_k = cap_knowledge[0]
    assert len(cap_k.evidence_ids) >= _MIN_CAPABILITY_EVIDENCE, (
        f"Knowledge should cite at least {_MIN_CAPABILITY_EVIDENCE} "
        f"observation IDs as evidence, got {len(cap_k.evidence_ids)}"
    )
    # Verify evidence_ids are actual observation IDs
    for eid in cap_k.evidence_ids:
        assert ":runtime:" in eid or eid.startswith("20"), (
            f"Evidence ID {eid} does not look like a runtime observation ID"
        )

    # Assert repair bottleneck knowledge exists
    bottleneck_knowledge = get_knowledge_by_type(
        conn, KnowledgeType.EXECUTION_BOTTLENECK.value
    )
    assert len(bottleneck_knowledge) > 0, (
        "EXECUTION_BOTTLENECK knowledge should be created"
    )
    bk = bottleneck_knowledge[0]
    assert len(bk.evidence_ids) >= _MIN_BOTTLENECK_EVIDENCE, (
        f"Bottleneck knowledge should cite at least "
        f"{_MIN_BOTTLENECK_EVIDENCE} observation IDs"
    )

    conn.close()


def test_second_observe_run_does_not_duplicate_facts(tmp_path):
    """Idempotency: running observe twice over the same sessions should not
    double-emit facts. The first run establishes a watermark; the second
    run sees no new sessions."""
    conn = connect(tmp_path / "idempotent.db")
    _setup_runtime_session(conn)

    reg = ObserverRegistry()
    reg.register(RuntimeObserver())

    # First run
    run1 = ObservationEngine(reg, conn).run()
    count1 = len(run1.observers[0].observations)
    conn.close()

    # Second run with new connection (no new sessions added)
    conn2 = connect(tmp_path / "idempotent.db")
    run2 = ObservationEngine(reg, conn2).run()
    count2 = len(run2.observers[0].observations)
    conn2.close()

    # Second run should see zero new observations (watermark prevents re-emission)
    assert count2 == 0, (
        f"Second observe run emitted {count2} facts when it should have "
        f"emitted 0. Watermark is broken."
    )


def test_new_session_observed_after_watermark(tmp_path):
    """A new session that completes after the first observe run should be
    observed on the second run."""
    conn = connect(tmp_path / "incremental.db")
    _setup_runtime_session(conn)

    reg = ObserverRegistry()
    reg.register(RuntimeObserver())

    # First run (observes session-dogfood-1)
    run1 = ObservationEngine(reg, conn).run()
    count1 = len(run1.observers[0].observations)
    assert count1 > 0

    # Add a second, newer session
    _now = now_iso()
    # Plan for the second graph
    conn.execute(
        """INSERT OR REPLACE INTO plans
           (id, goal, plan_type, confidence, status, milestones, dependencies,
            risks, verification, rollback, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("plan:fix-bug", "Fix bug #42",
         "engineering", "medium", "planned",
         "[]", "[]", "[]", "[]", "[]", _now, _now),
    )
    conn.execute(
        """INSERT OR REPLACE INTO task_graphs
           (id, goal, plan_id, plan_type, task_count, edge_count,
            critical_path_length, parallel_groups, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("graph-dogfood-2", "Fix bug #42", "plan:fix-bug",
         "engineering", 1, 0, 1, 1, "compiled", _now, _now),
    )
    conn.execute(
        """INSERT OR REPLACE INTO runtime_sessions
           (session_id, schedule_id, state, started_at, finished_at,
            schema_version, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        ("session-dogfood-2", "graph-dogfood-2", "finished",
         _now, _now, "1.0", _now, _now),
    )
    conn.execute(
        """INSERT OR REPLACE INTO runtime_tasks
           (execution_id, session_id, schedule_id, task_id, worker_id,
            wave, attempt, status, started_at, finished_at,
            duration_ms, exit_code, error, schema_version, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("exec-dogfood-2", "session-dogfood-2", "graph-dogfood-2", "fix-task-1",
         "fixer-1", 1, 1, "success", _now, _now, 200, 0, "", "1.0", _now, _now),
    )
    conn.commit()

    # Second run should observe the new session only
    run2 = ObservationEngine(reg, conn).run()
    obs2 = run2.observers[0].observations
    subjects = {o.subject for o in obs2}
    assert "graph-dogfood-2" in subjects, "New session should be observed"
    assert "graph-dogfood-1" not in subjects, "Old session should not be re-observed"

    conn.close()
