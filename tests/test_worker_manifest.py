# tests/test_worker_manifest.py
from friday.worker.models import WorkerManifest, VerificationResult, Worker


def test_manifest_is_frozen():
    m = WorkerManifest(
        name="Claude Code", implementation="cli", provider="anthropic",
        origin="external", capabilities=["Refactoring", "Documentation"],
        requirements=["claude"], supported_task_types=["refactor", "documentation"],
        supported_plan_types=["feature"])
    try:
        m.name = "x"  # type: ignore[misc]
        assert False, "manifest must be immutable"
    except Exception:
        pass


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
