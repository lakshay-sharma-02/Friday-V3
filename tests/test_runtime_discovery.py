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
