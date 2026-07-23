"""Tests for `friday execute` orchestration glue (plan->resolve->schedule->run)."""

from __future__ import annotations

import argparse

from friday.db import connect
from friday.cli_execute import cmd_execute


class _FakeReport:
    session_id = "sess:fake"
    state = "finished"
    verification_time_ms = 0
    stopped_at = None
    stop_reason = None
    executed = 1
    succeeded = 1
    failed = 0
    cancelled = 0
    workers_used = 1
    duration_ms = 5
    tasks = [{"task_id": "t1", "status": "success", "error": ""}]


def test_execute_chains_plan_resolve_schedule_run(monkeypatch, capsys):
    # Capture the schedule the runtime was asked to run, and stub the engine
    # so no real external worker is invoked (deterministic, offline).
    captured = {}

    def _fake_run(self, schedule):
        captured["schedule_id"] = schedule.schedule_id
        captured["tasks"] = [t.task_id for t in schedule.tasks]
        return _FakeReport()

    monkeypatch.setattr(
        "friday.cli_execute.RuntimeEngine.run", _fake_run)

    conn = connect(":memory:")
    # Register a native documentation worker so the goal resolves to something
    # the (stubbed) runtime can "run".
    from friday.worker.engine import WorkerRegistry
    reg = WorkerRegistry(conn)
    reg.register_from_manifest({
        "name": "Documentation", "kind": "tool", "implementation": "native",
        "provider": "local", "origin": "builtin", "id": "worker:documentation",
        "capabilities": ["Documentation"], "requirements": [],
        "supported_task_types": ["documentation"],
        "supported_plan_types": ["documentation"]})

    args = argparse.Namespace(goal=["Improve", "the", "README"], workspace=".", yes=True)
    rc = cmd_execute(args, conn=conn)
    out = capsys.readouterr().out

    assert rc == 0
    assert captured.get("schedule_id") is not None
    assert captured["tasks"]  # at least one task scheduled
    assert "Success:" in out and "1" in out
    assert "Failed:" in out
