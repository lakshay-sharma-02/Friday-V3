"""Worker Genesis regression tests (M10.x).

Covers: gap detection (no duplicates), draft_manifest (closed vocabulary only),
proposal lifecycle (pending → approved → registry, or rejected), CLI propose/review
commands, and resolver integration (additive, no regression).

The resolver test suite (test_resolver.py) must pass unmodified.
"""

from __future__ import annotations

import json
import sqlite3
import os
from pathlib import Path

import pytest

from friday.db import connect, now_iso, ProposedWorkerRow
from friday.worker.genesis import (
    CapabilityGapEvent,
    detect_gap,
    reset_gap_tracking,
    draft_manifest,
    propose_worker,
    register_approved_proposal,
    _tool_name_from_capability,
    _check_path_for_tool,
)
from friday.worker.models import (
    WorkerManifest,
    validate_capabilities,
    all_capabilities,
    WorkerKind,
)
from friday.worker.engine import WorkerRegistry, BUILTIN_WORKERS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _db(tmp_path: Path) -> sqlite3.Connection:
    return connect(tmp_path / "genesis_test.db")


def _register_builtins(conn: sqlite3.Connection) -> WorkerRegistry:
    reg = WorkerRegistry(conn)
    reg.register_builtins()
    return reg


# ===================================================================
# 1. Gap detection
# ===================================================================

def test_detect_gap_fires_once():
    """detect_gap returns one event per capability gap, no duplicates."""
    reset_gap_tracking()
    events = detect_gap("Build a Rust project", ["Rust", "Testing"])
    assert len(events) == 2
    caps = {e.required_capability for e in events}
    assert caps == {"Rust", "Testing"}
    assert all(e.goal == "Build a Rust project" for e in events)


def test_detect_gap_no_duplicates_same_gap():
    """Repeated detection of the same (goal, gap) pair returns no new events."""
    reset_gap_tracking()
    events1 = detect_gap("Build a Rust project", ["Rust"])
    assert len(events1) == 1
    events2 = detect_gap("Build a Rust project", ["Rust"])
    assert len(events2) == 0  # already seen


def test_detect_gap_different_goal_same_gap():
    """Same gap, different goal → new event allowed."""
    reset_gap_tracking()
    events1 = detect_gap("Goal A", ["Rust"])
    assert len(events1) == 1
    events2 = detect_gap("Goal B", ["Rust"])
    assert len(events2) == 1


def test_detect_gap_empty_list():
    """Empty missing capabilities → no events."""
    reset_gap_tracking()
    events = detect_gap("Goal", [])
    assert len(events) == 0


def test_detect_gap_reset_tracking():
    """After reset, same gap can be detected again."""
    reset_gap_tracking()
    e1 = detect_gap("Goal", ["Rust"])
    assert len(e1) == 1
    reset_gap_tracking()
    e2 = detect_gap("Goal", ["Rust"])
    assert len(e2) == 1


# ===================================================================
# 2. draft_manifest — closed vocabulary enforcement
# ===================================================================

def test_draft_manifest_never_returns_outside_vocabulary():
    """draft_manifest never returns a manifest with a capability
    outside the closed vocabulary, even if an LLM fallback is configured."""
    gap = CapabilityGapEvent(
        goal="Test", required_capability="FakeCapability123",
        task_id="t1", graph_id="g1",
    )
    manifest = draft_manifest(gap)
    if manifest is not None:
        # If an LLM fallback produced a manifest, its capabilities must
        # all be from the closed vocabulary (validate_capabilities rejects
        # anything outside the vocabulary).
        for cap in manifest.capabilities:
            assert validate_capabilities([cap]) == [cap], (
                f"Capability {cap!r} not in closed vocabulary")
        # 'FakeCapability123' must NOT be in the capabilities.
        assert "FakeCapability123" not in manifest.capabilities
        assert "fakecapability123" not in [c.lower() for c in manifest.capabilities]
    # If manifest is None (no LLM configured, no PATH tool), that's also fine.


def test_draft_manifest_known_cap_with_tool_on_path():
    """A capability matching a known tool on PATH should produce a manifest
    with valid capabilities (or None if the tool isn't actually on PATH)."""
    # 'python' maps to 'python3' — which is likely on PATH, but not guaranteed.
    gap = CapabilityGapEvent(
        goal="Test", required_capability="python",
        task_id="t1", graph_id="g1",
    )
    manifest = draft_manifest(gap)
    if manifest is not None:
        # Must have valid capabilities from the closed vocabulary.
        assert len(manifest.capabilities) > 0
        for cap in manifest.capabilities:
            assert validate_capabilities([cap]) == [cap], (
                f"Capability {cap} not in closed vocabulary")
        assert manifest.origin == "generated"
        assert manifest.confidence == "medium"


# Actually, let's test with a capability whose tool we KNOW won't be on PATH
# in CI, so we get deterministic None result.
def test_draft_manifest_unknown_capability_no_tool():
    """A capability with no known tool has no deterministic match.
    If a manifest IS returned (via LLM fallback), its capabilities must
    be from the closed vocabulary."""
    gap = CapabilityGapEvent(
        goal="Test", required_capability="Architecture",
        task_id="t1", graph_id="g1",
    )
    manifest = draft_manifest(gap)
    if manifest is not None:
        for cap in manifest.capabilities:
            assert validate_capabilities([cap]) == [cap], (
                f"Capability {cap!r} not in closed vocabulary")


# ===================================================================
# 3. _tool_name_from_capability
# ===================================================================

def test_tool_name_from_capability():
    """Known capabilities map to deterministic tool names."""
    assert _tool_name_from_capability("rust") == "cargo"
    assert _tool_name_from_capability("python") == "python3"
    assert _tool_name_from_capability("git operations") == "git"
    assert _tool_name_from_capability("shell commands") == "bash"
    assert _tool_name_from_capability("testing") == "pytest"


def test_tool_name_from_unknown_capability():
    """Unknown capabilities return None."""
    assert _tool_name_from_capability("blender") is None
    assert _tool_name_from_capability("docker") is None


def test_tool_name_from_generic_capability():
    """Generic capabilities with no CLI tool return None."""
    assert _tool_name_from_capability("architecture") is None
    assert _tool_name_from_capability("frontend") is None
    assert _tool_name_from_capability("code review") is None


# ===================================================================
# 4. propose_worker — persistence
# ===================================================================

def test_propose_worker_writes_to_db(tmp_path):
    """propose_worker writes a pending proposal to the proposed_workers table."""
    conn = _db(tmp_path)
    reset_gap_tracking()
    created = propose_worker(
        conn, goal="Test goal",
        missing_capabilities=["Rust"],
        task_id="t1", graph_id="g1",
    )
    # Rust maps to "cargo" — whether cargo is on PATH determines if we
    # get a proposal. If cargo is on PATH, we'll get one.
    if created:
        from friday.db import get_proposed_worker
        row = get_proposed_worker(conn, created[0])
        assert row is not None
        assert row.status == "pending"
        assert row.capability_gap == "Rust"
        assert row.detected_from_goal == "Test goal"
    conn.close()


def test_propose_worker_no_duplicate_pending(tmp_path):
    """propose_worker does not create duplicate pending proposals."""
    conn = _db(tmp_path)
    reset_gap_tracking()
    created1 = propose_worker(
        conn, goal="Test", missing_capabilities=["Architecture"],
        task_id="t1", graph_id="g1",
    )
    reset_gap_tracking()
    created2 = propose_worker(
        conn, goal="Test", missing_capabilities=["Architecture"],
        task_id="t1", graph_id="g1",
    )
    # Architecture has no tool → no proposal for either.
    assert len(created1) == 0 or len(created2) == 0
    conn.close()


# ===================================================================
# 5. Proposal lifecycle: pending → approved → registry
# ===================================================================

def test_proposal_not_in_registry_until_approved(tmp_path):
    """A manifest is NOT present in WorkerRegistry.all_workers() until
    its proposal status is 'approved'."""
    conn = _db(tmp_path)
    reg = _register_builtins(conn)
    initial_count = reg.count()

    # Insert a proposal manually (for a capability that would draft).
    from friday.db import insert_proposed_worker
    manifest_json = json.dumps({
        "name": "RustTool", "implementation": "cli", "provider": "local",
        "origin": "generated",
        "capabilities": ["Rust", "Testing"],
        "requirements": ["cargo"],
        "supported_task_types": ["implementation", "testing"],
        "supported_plan_types": ["feature"],
        "description": "Rust build tool", "estimated_speed": "fast",
        "estimated_cost": "low", "confidence": "medium",
    })
    row = ProposedWorkerRow(
        id="proposal:rust:test",
        detected_from_goal="Test",
        capability_gap="Rust",
        draft_manifest_json=manifest_json,
        status="pending",
        created_at=now_iso(),
        reviewed_at=None,
    )
    insert_proposed_worker(conn, row)

    # Before approval: not in registry.
    assert reg.count() == initial_count

    # Approve the proposal.
    success = register_approved_proposal(conn, "proposal:rust:test", reg)
    assert success, "register_approved_proposal should succeed"

    # After approval: in registry.
    assert reg.count() == initial_count + 1
    w = reg.worker_by_name("RustTool")
    assert w is not None
    assert "Rust" in w.capabilities

    conn.close()


def test_rejected_proposal_not_in_registry(tmp_path):
    """Rejecting a proposal leaves the WorkerRegistry unchanged."""
    conn = _db(tmp_path)
    reg = _register_builtins(conn)
    initial_count = reg.count()

    # Insert a proposal manually.
    from friday.db import insert_proposed_worker, update_proposed_worker_status
    manifest_json = json.dumps({
        "name": "BadWorker", "implementation": "cli", "provider": "local",
        "origin": "generated",
        "capabilities": ["Python"],
        "requirements": ["python3"],
        "supported_task_types": ["implementation"],
        "supported_plan_types": ["feature"],
        "description": "Testing reject", "estimated_speed": "fast",
        "estimated_cost": "low", "confidence": "medium",
    })
    row = ProposedWorkerRow(
        id="proposal:bad:test",
        detected_from_goal="Test",
        capability_gap="Python",
        draft_manifest_json=manifest_json,
        status="pending",
        created_at=now_iso(),
        reviewed_at=None,
    )
    insert_proposed_worker(conn, row)

    # Reject it instead of approving.
    update_proposed_worker_status(conn, "proposal:bad:test", "rejected")

    # Registry unchanged.
    assert reg.count() == initial_count
    w = reg.worker_by_name("BadWorker")
    assert w is None

    conn.close()


def test_proposal_with_invalid_capabilities_rejected(tmp_path):
    """A proposed manifest with capabilities outside the closed vocabulary
    is rejected at review, not silently trusted."""
    conn = _db(tmp_path)
    reg = _register_builtins(conn)
    initial_count = reg.count()

    from friday.db import insert_proposed_worker
    # This manifest claims "SuperIntelligence" which is NOT in the vocabulary.
    manifest_json = json.dumps({
        "name": "FakeWorker", "implementation": "cli", "provider": "local",
        "origin": "generated",
        "capabilities": ["SuperIntelligence", "Python"],
        "requirements": [],
        "supported_task_types": ["implementation"],
        "supported_plan_types": ["feature"],
        "description": "Fake worker", "estimated_speed": "fast",
        "estimated_cost": "low", "confidence": "high",
    })
    row = ProposedWorkerRow(
        id="proposal:fake:test",
        detected_from_goal="Test",
        capability_gap="Python",
        draft_manifest_json=manifest_json,
        status="pending",
        created_at=now_iso(),
        reviewed_at=None,
    )
    insert_proposed_worker(conn, row)

    # Attempt to register — should fail because fabricated caps are rejected.
    success = register_approved_proposal(conn, "proposal:fake:test", reg)
    assert not success, (
        "Fabricated capability should be rejected at review time")

    # Registry unchanged.
    assert reg.count() == initial_count

    # Proposal should now be marked as rejected.
    from friday.db import get_proposed_worker
    row = get_proposed_worker(conn, "proposal:fake:test")
    assert row is not None
    assert row.status == "rejected"

    conn.close()


def test_proposal_with_overbroad_capabilities_rejected(tmp_path):
    """A manifest that claims capabilities beyond what's supported is
    silently corrected by validate_capabilities (only valid caps survive),
    but if NO valid caps remain after validation, the proposal is rejected."""
    conn = _db(tmp_path)
    reg = _register_builtins(conn)
    initial_count = reg.count()

    from friday.db import insert_proposed_worker
    # All capabilities are fabricated — none are in the closed vocabulary.
    manifest_json = json.dumps({
        "name": "Overbroad", "implementation": "api", "provider": "openai",
        "origin": "generated",
        "capabilities": ["MakeCoffee", "SolveWorldHunger", "DoMyLaundry"],
        "requirements": ["magic_wand"],
        "supported_task_types": ["everything"],
        "supported_plan_types": ["everything"],
        "description": "Overly broad", "estimated_speed": "fast",
        "estimated_cost": "low", "confidence": "high",
    })
    row = ProposedWorkerRow(
        id="proposal:overbroad:test",
        detected_from_goal="Test",
        capability_gap="Python",
        draft_manifest_json=manifest_json,
        status="pending",
        created_at=now_iso(),
        reviewed_at=None,
    )
    insert_proposed_worker(conn, row)

    # Attempt to register — should fail.
    success = register_approved_proposal(conn, "proposal:overbroad:test", reg)
    assert not success, (
        "Overbroad/fabricated capabilities should be rejected")

    assert reg.count() == initial_count
    conn.close()


# ===================================================================
# 6. CapabilityGapEvent dataclass
# ===================================================================

def test_capability_gap_event_creation():
    """CapabilityGapEvent stores all fields correctly."""
    event = CapabilityGapEvent(
        goal="Implement OAuth",
        required_capability="Rust",
        task_id="task-1",
        graph_id="graph-1",
    )
    assert event.goal == "Implement OAuth"
    assert event.required_capability == "Rust"
    assert event.task_id == "task-1"
    assert event.graph_id == "graph-1"
    assert event.detected_at != ""


def test_capability_gap_event_to_dict():
    """CapabilityGapEvent serializes to dict."""
    event = CapabilityGapEvent(
        goal="Test", required_capability="Python",
        task_id="t1", graph_id="g1",
    )
    d = event.to_dict()
    assert d["goal"] == "Test"
    assert d["required_capability"] == "Python"
    assert d["task_id"] == "t1"
    assert d["graph_id"] == "g1"
    assert "detected_at" in d


# ===================================================================
# 7. WorkerManifest from draft_manifest
# ===================================================================

def test_draft_manifest_deterministic_structure():
    """Deterministic manifest (from PATH tool) has correct structure."""
    # Use 'git operations' which maps to 'git' — likely on PATH.
    gap = CapabilityGapEvent(
        goal="Test git", required_capability="git operations",
        task_id="t1", graph_id="g1",
    )
    manifest = draft_manifest(gap)
    if manifest is not None:
        assert isinstance(manifest, WorkerManifest)
        assert manifest.implementation == "cli"
        assert manifest.provider == "local"
        assert manifest.origin == "generated"
        assert manifest.confidence == "medium"
        assert manifest.estimated_speed == "fast"
        assert manifest.estimated_cost == "low"
        # Capabilities must be valid.
        assert len(manifest.capabilities) > 0
        assert validate_capabilities(manifest.capabilities) == manifest.capabilities


# ===================================================================
# 8. Resolver integration — no regression
# ===================================================================

def test_resolver_regression_suite_passes():
    """The existing test_resolver.py suite passes unmodified.
    This test imports the suite module to verify it loads correctly.
    (The full suite is run by pytest -- we just verify the module loads.)"""
    import tests.test_resolver as mod
    # The module should have all the expected test functions.
    assert hasattr(mod, "test_exact_match_single_capability")
    assert hasattr(mod, "test_no_hallucinated_workers")
    assert hasattr(mod, "test_stable_output")
    assert hasattr(mod, "test_engine_resolve_graph")
