import shutil
from friday.runtime.discovery import discover, DiscoveryResult


def test_discovery_marks_missing_binary_unavailable():
    res = discover([{"worker_id": "worker:x",
                     "requirements": ["definitely-not-a-real-binary-xyz"]}])
    assert isinstance(res, DiscoveryResult)
    assert "worker:x" in res.unavailable
    assert "definitely-not-a-real-binary-xyz" in res.missing_deps["worker:x"]


def test_discovery_available_when_binary_present(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda b: "/usr/bin/claude" if b == "claude" else None)
    res = discover([{"worker_id": "worker:claude", "requirements": ["claude"]}])
    assert "worker:claude" in res.available


from friday.db import connect
from friday.worker.engine import WorkerRegistry


def test_sync_availability_updates_only_availability():
    conn = connect(":memory:")
    reg = WorkerRegistry(conn)
    reg.register_from_manifest({
        "name": "Claude Code", "kind": "cli", "implementation": "cli",
        "provider": "anthropic", "origin": "external",
        "id": "worker:claude",
        "capabilities": ["Refactoring"], "requirements": ["claude"],
        "supported_task_types": ["refactor"], "supported_plan_types": ["feature"]})
    from friday.runtime.discovery import DiscoveryResult
    reg.sync_availability(DiscoveryResult(
        available=[], unavailable=["worker:claude"], missing_deps={"worker:claude": ["claude"]}))
    w = reg.worker_by_name("Claude Code")
    assert w is not None
    assert w.availability == "unavailable"
