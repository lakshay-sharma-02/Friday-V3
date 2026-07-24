"""Milestone — Runtime Observer (Law 17 / Learning Loop) tests.

Deterministic tests for the RuntimeObserver: it is a PURE READER of already-
persisted runtime/review tables and emits Observation facts that plug into the
frozen Observation Engine. No execution, no LLM, no side effects.

Coverage: graph-level/task-level outcomes, repair detection, capability
reliability (DERIVED), watermark (no re-emission), empty state, observer
registration, health, end-to-end through the Observation Engine.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from friday.db import (
    ObservationRow,
    connect,
    insert_observations,
    latest_observations,
    observations_all,
)
from friday.observation import (
    Confidence,
    Observation,
    ObservationEngine,
    ObserverRegistry,
    RuntimeObserver,
    default_registry,
)


# ---------------------------------------------------------------------------
# Helpers: populate runtime tables directly (read-only observer, so we
# can set up the data without going through the full runtime engine).
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _past(seconds: int = 3600) -> str:
    return datetime.now(timezone.utc).timestamp() - seconds


def _insert_plan(conn, plan_id: str = "plan:improve-readme",
                  goal: str = "Improve the README") -> None:
    conn.execute(
        """INSERT OR REPLACE INTO plans
           (id, goal, plan_type, confidence, status, milestones, dependencies,
            risks, verification, rollback, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (plan_id, goal, "engineering", "medium", "planned",
         "[]", "[]", "[]", "[]", "[]",
         _now(), _now()),
    )
    conn.commit()


def _insert_graph(conn, gid: str = "graph-1", goal: str = "Improve the README",
                  status: str = "compiled") -> None:
    plan_id = f"plan:{goal.lower().replace(' ', '-')}"
    _insert_plan(conn, plan_id, goal)
    conn.execute(
        """INSERT OR REPLACE INTO task_graphs
           (id, goal, plan_id, plan_type, task_count, edge_count,
            critical_path_length, parallel_groups, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (gid, goal, plan_id, "engineering", 0, 0, 0, 0,
         status, _now(), _now()),
    )
    conn.commit()


def _insert_session(conn, sid: str = "session-1",
                    schedule_id: str = "graph-1",
                    state: str = "finished",
                    started_at: str | None = None,
                    finished_at: str | None = None) -> None:
    if started_at is None:
        started_at = _now()
    if finished_at is None:
        finished_at = _now()
    conn.execute(
        """INSERT OR REPLACE INTO runtime_sessions
           (session_id, schedule_id, state, started_at, finished_at,
            schema_version, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (sid, schedule_id, state, started_at, finished_at,
         "1.0", started_at, finished_at),
    )
    conn.commit()


def _insert_task(conn, execution_id: str, session_id: str = "session-1",
                 task_id: str = "task-1", worker_id: str = "worker-1",
                 status: str = "success",
                 schedule_id: str = "graph-1") -> None:
    conn.execute(
        """INSERT OR REPLACE INTO runtime_tasks
           (execution_id, session_id, schedule_id, task_id, worker_id,
            wave, attempt, status, started_at, finished_at,
            duration_ms, exit_code, error, schema_version, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (execution_id, session_id, schedule_id, task_id, worker_id,
         1, 1, status, _now(), _now(), 100, 0, "", "1.0", _now(), _now()),
    )
    conn.commit()


def _insert_result(conn, execution_id: str, session_id: str = "session-1",
                   task_id: str = "task-1", worker_id: str = "worker-1",
                   success: int = 1, verification_passed: int | None = 1) -> None:
    conn.execute(
        """INSERT INTO runtime_results
           (execution_id, session_id, task_id, worker_id, success, stdout, stderr,
            artifacts, exit_code, duration_ms, error, verification_passed,
            verification_evidence, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (execution_id, session_id, task_id, worker_id, success,
         "", "", "[]", 0, 100, "", verification_passed, "{}", _now()),
    )
    conn.commit()


def _insert_task_def(conn, task_id: str = "task-1",
                     graph_id: str = "graph-1",
                     title: str = "Write documentation",
                     capabilities: str = "writing",
                     plan_id: str = "plan:improve-the-readme") -> None:
    conn.execute(
        """INSERT OR REPLACE INTO tasks
           (id, graph_id, plan_id, milestone_order, title, description, task_type,
            required_capabilities, complexity, priority, estimated_effort,
            dependencies, inputs, outputs, acceptance_criteria, verification,
            rollback, evidence, status, confidence, sequence)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (task_id, graph_id, plan_id, 0, title, "", "general",
         capabilities, "medium", "medium", "medium",
         "", "[]", "[]", "[]", "[]", "[]", "[]", "pending", "medium", 0),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    """In-memory SQLite database with runtime tables set up."""
    conn = connect(tmp_path / "test.db")
    yield conn
    conn.close()


@pytest.fixture
def observer():
    return RuntimeObserver()


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

def test_health_healthy_when_runtime_tables_exist(db, observer):
    h = observer.health(db)
    assert h.healthy is True
    assert h.status.value == "healthy"


def test_health_healthy_with_sessions(db, observer):
    _insert_graph(db)
    _insert_session(db)
    h = observer.health(db)
    assert h.healthy is True


# ---------------------------------------------------------------------------
# Empty state
# ---------------------------------------------------------------------------

def test_collect_empty_when_no_sessions(db, observer):
    obs = observer.collect(db)
    assert obs == []


def test_collect_empty_when_session_not_finished(db, observer):
    _insert_graph(db)
    _insert_session(db, state="running", finished_at=None)
    obs = observer.collect(db)
    assert obs == []


def test_collect_empty_when_no_finished_at(db, observer):
    """When a session has no finished_at, it should be skipped even if
    state is 'finished'. We must use raw SQL here because the helper
    replaces None finished_at with _now()."""
    _insert_graph(db)
    _n = _now()
    db.execute(
        """INSERT OR REPLACE INTO runtime_sessions
           (session_id, schedule_id, state, started_at, finished_at,
            schema_version, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        ("session-1", "graph-1", "finished", _n, None, "1.0", _n, _n),
    )
    db.commit()
    obs = observer.collect(db)
    assert obs == []


# ---------------------------------------------------------------------------
# Successful session
# ---------------------------------------------------------------------------

def test_collect_graph_outcome_success(db, observer):
    _insert_graph(db)
    _insert_session(db)
    _insert_task(db, execution_id="exec-1", task_id="task-1", status="success")
    obs = observer.collect(db)
    # Should emit: execution_outcome, repair_required, task_outcome
    assert len(obs) >= 3
    facts = {(o.subject, o.aspect): o for o in obs}

    # Graph-level outcome
    assert ("graph-1", "execution_outcome") in facts
    assert facts[("graph-1", "execution_outcome")].value == "success"
    assert facts[("graph-1", "execution_outcome")].confidence is Confidence.OBSERVED

    # Repair required (no failures, so false)
    assert ("graph-1", "repair_required") in facts
    assert facts[("graph-1", "repair_required")].value == "false"

    # Task-level outcome
    assert ("task-1", "task_outcome") in facts
    assert facts[("task-1", "task_outcome")].value == "success"
    assert facts[("task-1", "task_outcome")].confidence is Confidence.OBSERVED


def test_collect_source_is_runtime(db, observer):
    _insert_graph(db)
    _insert_session(db)
    _insert_task(db, execution_id="exec-1", task_id="task-1", status="success")
    obs = observer.collect(db)
    assert all(o.source == "runtime" for o in obs)


# ---------------------------------------------------------------------------
# Failed session
# ---------------------------------------------------------------------------

def test_collect_graph_outcome_failed(db, observer):
    _insert_graph(db)
    _insert_session(db)
    _insert_task(db, execution_id="exec-1", task_id="task-1", status="failed")
    obs = {(o.subject, o.aspect): o for o in observer.collect(db)}
    assert obs[("graph-1", "execution_outcome")].value == "failed"
    assert obs[("task-1", "task_outcome")].value == "failed"
    assert obs[("graph-1", "repair_required")].value == "true"


# ---------------------------------------------------------------------------
# Mixed outcomes
# ---------------------------------------------------------------------------

def test_collect_mixed_success_failure(db, observer):
    _insert_graph(db)
    _insert_session(db)
    _insert_task(db, execution_id="exec-1", task_id="task-1", status="success")
    _insert_task(db, execution_id="exec-2", task_id="task-2", status="failed")
    obs = {(o.subject, o.aspect): o for o in observer.collect(db)}
    assert obs[("graph-1", "execution_outcome")].value == "failed"
    assert obs[("task-1", "task_outcome")].value == "success"
    assert obs[("task-2", "task_outcome")].value == "failed"


# ---------------------------------------------------------------------------
# Cancelled session
# ---------------------------------------------------------------------------

def test_collect_graph_outcome_cancelled(db, observer):
    _insert_graph(db)
    _insert_session(db)
    _insert_task(db, execution_id="exec-1", task_id="task-1", status="cancelled")
    obs = {(o.subject, o.aspect): o for o in observer.collect(db)}
    assert obs[("graph-1", "execution_outcome")].value == "cancelled"


# ---------------------------------------------------------------------------
# Capability reliability (DERIVED)
# ---------------------------------------------------------------------------

def test_capability_reliability_emitted(db, observer):
    _insert_graph(db)
    _insert_session(db)
    _insert_task_def(db, task_id="task-1", capabilities="writing")
    _insert_task(db, execution_id="exec-1", task_id="task-1", status="success")
    obs = {(o.subject, o.aspect): o for o in observer.collect(db)}
    assert ("writing", "capability_reliability") in obs
    assert obs[("writing", "capability_reliability")].value == "1/1 success"
    assert obs[("writing", "capability_reliability")].confidence is Confidence.DERIVED


def test_capability_reliability_multiple_tasks(db, observer):
    _insert_graph(db)
    _insert_session(db)
    _insert_task_def(db, task_id="task-1", capabilities="writing")
    _insert_task_def(db, task_id="task-2", capabilities="writing")
    _insert_task(db, execution_id="exec-1", task_id="task-1", status="success")
    _insert_task(db, execution_id="exec-2", task_id="task-2", status="failed")
    obs = {(o.subject, o.aspect): o for o in observer.collect(db)}
    assert ("writing", "capability_reliability") in obs
    assert obs[("writing", "capability_reliability")].value == "1/2 success"


def test_capability_reliability_multiple_caps(db, observer):
    _insert_graph(db)
    _insert_session(db)
    _insert_task_def(db, task_id="task-1", capabilities="writing, git")
    _insert_task(db, execution_id="exec-1", task_id="task-1", status="success")
    obs = {(o.subject, o.aspect): o for o in observer.collect(db)}
    assert ("writing", "capability_reliability") in obs
    assert ("git", "capability_reliability") in obs


# ---------------------------------------------------------------------------
# Repair detection (verification_passed = 0)
# ---------------------------------------------------------------------------

def test_repair_detected_from_verification(db, observer):
    _insert_graph(db)
    _insert_session(db)
    _insert_task(db, execution_id="exec-1", task_id="task-1", status="success")
    _insert_result(db, execution_id="exec-1", verification_passed=0)
    obs = {(o.subject, o.aspect): o for o in observer.collect(db)}
    assert obs[("graph-1", "repair_required")].value == "true"


# ---------------------------------------------------------------------------
# Watermark / idempotency
# ---------------------------------------------------------------------------

def test_idempotent_second_run_no_new_sessions(db, observer):
    """Running collect() twice on the same data should return an empty list
    the second time (watermark prevents re-emission through the engine)."""
    _insert_graph(db)
    _insert_session(db)
    _insert_task(db, execution_id="exec-1", task_id="task-1", status="success")

    # First collection
    first = observer.collect(db)
    assert len(first) >= 3

    # Simulate what the engine does: persist the observations so the
    # watermark query sees them.
    from friday.db import insert_observations
    insert_observations(db, [o.to_row() for o in first])

    # Second collection with same data should return empty
    second = observer.collect(db)
    assert second == []


def test_race_condition_backdated_finished_at_still_observed(db, observer):
    """Reproduce the exact race condition from the timestamp-based watermark:
    a session whose finished_at is EARLIER than a previously-observed session's
    timestamp but was UNOBSERVED should still be observed on the next run.

    Under the old timestamp-based approach (approach b), if:
      - Run 1 observes session-A (finished_at=T1). Watermark set to T_obs1.
      - Session-B is inserted with finished_at=T0 where T0 < T1 < T_obs1.
      - Run 2: session-B skipped because T0 < T_obs1 (watermark). WRONG.

    Under the new session-ID cursor approach, we track session IDs, not
    timestamps. Session-B is unobserved, so it IS collected regardless
    of its finished_at timestamp relative to any observation run time.
    """
    _insert_graph(db, gid="graph-a", goal="Session A")
    _insert_graph(db, gid="graph-b", goal="Session B")

    # Session-A: finished on 2026-07-20
    _insert_session(db, sid="session-a",
                    finished_at="2026-07-20T00:00:00",
                    schedule_id="graph-a")
    _insert_task(db, execution_id="exec-a", session_id="session-a",
                 task_id="task-a", schedule_id="graph-a", status="success")

    # First run: observes session-a (the only one)
    first = observer.collect(db)
    subjects_first = {o.subject for o in first}
    assert "graph-a" in subjects_first
    insert_observations(db, [o.to_row() for o in first])

    # Now add session-B with a BACKDATED finished_at that's EARLIER than
    # session-a's finished_at. This is the race condition: under a timestamp
    # watermark, session-B (finished_at=T0 < watermark=T_obs1) would be
    # skipped. Under the session-ID cursor, it's unobserved -> collected.
    _insert_session(db, sid="session-b",
                    finished_at="2026-07-19T00:00:00",  # EARLIER than session-a!
                    schedule_id="graph-b")
    _insert_task(db, execution_id="exec-b", session_id="session-b",
                 task_id="task-b", schedule_id="graph-b", status="success")

    # Second run: session-B MUST be observed (cursor tracks IDs, not timestamps)
    second = observer.collect(db)
    subjects_second = {o.subject for o in second}
    assert "graph-b" in subjects_second, (
        "Session with backdated finished_at must still be observed "
        "under the session-ID cursor approach"
    )
    assert "graph-a" not in subjects_second, (
        "Previously observed session must NOT be re-observed"
    )


def test_new_sessions_after_watermark_collected(db, observer):
    """Sessions that finish after the watermark should still be collected."""
    _insert_graph(db, gid="graph-1", goal="Improve the README")
    _insert_graph(db, gid="graph-2", goal="Fix the tests")
    _insert_session(db, sid="session-1", finished_at="2026-07-20T00:00:00",
                    schedule_id="graph-1")
    _insert_task(db, execution_id="exec-1", session_id="session-1",
                 task_id="task-1", schedule_id="graph-1", status="success")

    # First collection
    first = observer.collect(db)
    insert_observations(db, [o.to_row() for o in first])

    # New session with finished_at AFTER the watermark (use _now() to ensure
    # it's later than the first run's observation timestamp).
    _insert_session(db, sid="session-2",
                    finished_at=_now(),
                    schedule_id="graph-2")
    _insert_task(db, execution_id="exec-2", session_id="session-2",
                 task_id="task-2", schedule_id="graph-2", status="success")

    second = observer.collect(db)
    # Should only contain facts from session-2 (graph-2)
    subjects = {o.subject for o in second}
    assert "graph-2" in subjects
    assert "graph-1" not in subjects


# ---------------------------------------------------------------------------
# Summarize
# ---------------------------------------------------------------------------

def test_summary_empty(db, observer):
    summary = observer.summarize(db)
    assert "no completed execution sessions" in summary


def test_summary_with_sessions(db, observer):
    _insert_graph(db)
    _insert_session(db)
    _insert_task(db, execution_id="exec-1", task_id="task-1", status="success")
    summary = observer.summarize(db)
    assert "completed session" in summary


# ---------------------------------------------------------------------------
# Observer registration
# ---------------------------------------------------------------------------

def test_runtime_registered_in_default_registry():
    assert "runtime" in default_registry()


def test_register_duplicate_raises():
    reg = ObserverRegistry()
    reg.register(RuntimeObserver())
    with pytest.raises(ValueError):
        reg.register(RuntimeObserver())


# ---------------------------------------------------------------------------
# Real end-to-end through the frozen Observation Engine
# ---------------------------------------------------------------------------

def test_end_to_end_through_observation_engine(db, observer, tmp_path):
    """Run the full engine with just the runtime observer and verify facts
    are persisted to the observations table."""
    # Set up a completed session
    _insert_graph(db)
    _insert_session(db)
    _insert_task(db, execution_id="exec-1", task_id="task-1", status="success")

    # Build a registry with ONLY the runtime observer
    reg = ObserverRegistry()
    reg.register(RuntimeObserver())
    run = ObservationEngine(reg, db).run()

    # Verify the run result
    assert run.observers[0].name == "runtime"
    assert run.observers[0].health.healthy

    # Verify facts were persisted
    stored = observations_all(db)
    aspects = {(o.subject, o.aspect) for o in stored}
    assert ("graph-1", "execution_outcome") in aspects
    assert ("task-1", "task_outcome") in aspects
    assert ("graph-1", "repair_required") in aspects
    assert all(o.source == "runtime" for o in stored)


def test_observation_ids_are_deterministic_and_idempotent(db, observer, tmp_path):
    """Running the engine twice over the same data produces identical
    observation ids (no duplicate facts)."""
    _insert_graph(db)
    _insert_session(db)
    _insert_task(db, execution_id="exec-1", task_id="task-1", status="success")

    reg = ObserverRegistry()
    reg.register(RuntimeObserver())
    ObservationEngine(reg, db).run()
    ids1 = {o.id for o in latest_observations(db)}

    # Re-run with identical data
    ObservationEngine(reg, db).run()
    ids2 = {o.id for o in latest_observations(db)}
    assert ids1 == ids2
