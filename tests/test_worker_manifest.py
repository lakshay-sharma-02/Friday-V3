# tests/test_worker_manifest.py
import pytest
from dataclasses import FrozenInstanceError

from friday.worker.models import WorkerManifest, VerificationResult, Worker, WorkerKind


def test_worker_availability_persisted():
    from friday.db import connect, get_worker_by_name, insert_worker
    conn = connect(":memory:")
    w = Worker(name="PersistTest", kind=WorkerKind.TOOL,
               capabilities=["Shell Commands"], availability="unavailable",
               manifest_ref="manifest:claude")
    insert_worker(conn, w.to_row())
    row = get_worker_by_name(conn, "PersistTest")
    assert row is not None
    assert row.availability == "unavailable"
    assert row.manifest_ref == "manifest:claude"
    # round-trip via from_row
    w2 = Worker.from_row(row)
    assert w2.availability == "unavailable"
    assert w2.manifest_ref == "manifest:claude"


def test_manifest_is_frozen():
    m = WorkerManifest(
        name="Claude Code", implementation="cli", provider="anthropic",
        origin="external", capabilities=["Refactoring", "Documentation"],
        requirements=["claude"], supported_task_types=["refactor", "documentation"],
        supported_plan_types=["feature"])
    with pytest.raises(FrozenInstanceError):
        m.name = "x"


def test_manifest_validates_capabilities():
    m = WorkerManifest(
        name="Claude Code", implementation="cli", provider="anthropic",
        origin="external", capabilities=["Refactoring", "BogusCap"],
        requirements=["claude"], supported_task_types=["refactor"],
        supported_plan_types=["feature"])
    assert "Refactoring" in m.capabilities
    assert "BogusCap" not in m.capabilities  # closed vocabulary rejects


def test_verification_result_shape():
    v = VerificationResult(passed=True, reason="exit 0")
    assert v.passed is True and v.reason == "exit 0"
