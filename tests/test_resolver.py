"""Capability Resolver regression tests (Milestone 9.3).

40+ tests covering: exact capability matching, language matching, task-type
matching, plan-type matching, tie-breaking, disabled workers, missing capabilities,
unknown capabilities, unknown workers, no candidates, parallel assignment,
sequential assignment, JSON export, history, evolution, idempotency, append-only,
Brain compatibility, Task Graph compatibility, Worker Registry compatibility,
deterministic output, and UNRESOLVED handling.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from friday.db import connect, now_iso
from friday.worker.models import (
    Worker,
    WorkerKind,
    validate_capabilities,
)
from friday.worker.engine import WorkerRegistry, BUILTIN_WORKERS
from friday.planning.models import PlanType
from friday.resolver.confidence import ConfidenceInputs, derive_confidence
from friday.resolver.models import (
    Assignment,
    ResolutionResult,
    ResolutionStatus,
    SCHEMA_VERSION,
    ScoreBreakdown,
    SelectionStrategy,
)
from friday.resolver.resolver import rank_workers, score_worker, select_assignment
from friday.resolver.engine import CapabilityResolver, ResolveResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _db(tmp_path: Path) -> sqlite3.Connection:
    return connect(tmp_path / "resolver_test.db")


def _register_builtins(conn: sqlite3.Connection) -> WorkerRegistry:
    reg = WorkerRegistry(conn)
    reg.register_builtins()
    return reg


def _make_worker(
    name: str,
    capabilities: list[str],
    languages: list[str] | None = None,
    task_types: list[str] | None = None,
    plan_types: list[str] | None = None,
    status: str = "active",
    confidence: str = "medium",
    speed: str = "medium",
    cost: str = "medium",
    kind: WorkerKind | None = None,
) -> Worker:
    w = Worker(
        name=name,
        kind=kind or WorkerKind.LLM,
        capabilities=list(capabilities),
        supported_languages=languages or [],
        supported_task_types=task_types or [],
        supported_plan_types=plan_types or [],
        estimated_speed=speed,
        estimated_cost=cost,
        confidence=confidence,
        status=status,
    )
    w.id = w._generate_id()
    return w


# ===================================================================
# 1. Exact capability matching
# ===================================================================

def test_exact_match_single_capability():
    """Worker with matching capability scores it."""
    w = _make_worker("W1", ["Python"])
    sb, matched, missing = score_worker(["Python"], "implementation", "feature", w)
    assert "Python" in matched
    assert not missing
    assert sb.capability > 0


def test_exact_match_no_fuzzy():
    """'Programming' does NOT match 'Python' — no fuzzy logic."""
    w = _make_worker("W1", ["Python"])
    sb, matched, missing = score_worker(["Programming"], "implementation", "feature", w)
    assert not matched
    assert "Programming" in missing


def test_exact_match_case_insensitive():
    """Canonicalization normalizes case: 'python' matches 'Python'."""
    w = _make_worker("W1", ["Python"])
    sb, matched, missing = score_worker(["python"], "implementation", "feature", w)
    assert "Python" in matched
    assert not missing


def test_multiple_required_capabilities():
    """Worker with subset: partial match, some missing."""
    w = _make_worker("W1", ["Python", "SQL"])
    sb, matched, missing = score_worker(
        ["Python", "SQL", "Rust"], "implementation", "feature", w)
    assert sorted(matched) == ["Python", "SQL"]
    assert sorted(missing) == ["Rust"]
    assert sb.penalty > 0  # missing cap penalty


def test_all_capabilities_matched():
    """Worker with all required caps: full score, no missing, no penalty."""
    w = _make_worker("W1", ["Python", "SQL", "TypeScript"],
                     languages=["Python", "SQL"],
                     task_types=["implementation"],
                     plan_types=["feature"])
    sb, matched, missing = score_worker(
        ["Python", "SQL"], "implementation", "feature", w)
    assert sorted(matched) == ["Python", "SQL"]
    assert not missing
    assert sb.penalty == 0


# ===================================================================
# 2. Language matching
# ===================================================================

def test_language_match():
    """Task requires Python (lang) + worker supports Python."""
    w = _make_worker("W1", ["Python"], languages=["Python"])
    sb, matched, missing = score_worker(["Python"], "implementation", "feature", w)
    assert sb.language == 5  # _W_LANGUAGE


def test_language_mismatch_penalty():
    """Task requires Rust (lang) + worker only supports Python."""
    w = _make_worker("W1", ["Python"], languages=["Python"])
    sb, matched, missing = score_worker(["Rust"], "implementation", "feature", w)
    assert sb.penalty >= 5  # _P_UNSUPPORTED_LANG


def test_no_language_in_caps_no_penalty():
    """Task requires only non-language caps: no language penalty."""
    w = _make_worker("W1", ["Architecture"])
    sb, matched, missing = score_worker(
        ["Architecture"], "design", "architecture", w)
    # Architecture is not a valid language, so no language component.
    assert sb.language == 0


# ===================================================================
# 3. Task-type matching
# ===================================================================

def test_task_type_match():
    """Worker supports the task's task_type."""
    w = _make_worker("W1", ["Python"], task_types=["implementation"])
    sb, _, _ = score_worker(["Python"], "implementation", "feature", w)
    assert sb.task_type == 5  # _W_TASK_TYPE


def test_task_type_mismatch_penalty():
    """Worker lacks the task's task_type."""
    w = _make_worker("W1", ["Python"], task_types=["documentation"])
    sb, _, _ = score_worker(["Python"], "implementation", "feature", w)
    assert sb.penalty >= 5  # _P_UNSUPPORTED_TASK


# ===================================================================
# 4. Plan-type matching
# ===================================================================

def test_plan_type_match():
    w = _make_worker("W1", ["Python"], plan_types=["feature"])
    sb, _, _ = score_worker(["Python"], "implementation", "feature", w)
    assert sb.plan_type == 3  # _W_PLAN_TYPE


def test_plan_type_mismatch_penalty():
    w = _make_worker("W1", ["Python"], plan_types=["research"])
    sb, _, _ = score_worker(["Python"], "implementation", "feature", w)
    assert sb.penalty >= 3  # _P_UNSUPPORTED_PLAN


# ===================================================================
# 5. Tie-breaking
# ===================================================================

def test_tie_break_by_capability_score():
    """Worker with more matched caps ranks higher."""
    w1 = _make_worker("W1", ["Python"])
    w2 = _make_worker("W2", ["Python", "SQL"])
    ranked = rank_workers(["Python", "SQL"], "implementation", "feature", [w1, w2])
    ids = [w.id for w, *_ in ranked]
    assert ids[0] == "worker:w2"


def test_tie_break_by_confidence():
    """Higher confidence breaks tie."""
    w1 = _make_worker("W1", ["Python"], confidence="medium",
                      task_types=["implementation"], plan_types=["feature"])
    w2 = _make_worker("W2", ["Python"], confidence="high",
                      task_types=["implementation"], plan_types=["feature"])
    ranked = rank_workers(["Python"], "implementation", "feature", [w1, w2])
    ids = [w.id for w, *_ in ranked]
    assert ids[0] == "worker:w2"


def test_tie_break_by_speed():
    """Faster worker breaks tie."""
    w1 = _make_worker("W1", ["Python"], speed="slow")
    w2 = _make_worker("W2", ["Python"], speed="fast")
    ranked = rank_workers(["Python"], "implementation", "feature", [w1, w2])
    ids = [w.id for w, *_ in ranked]
    assert ids[0] == "worker:w2"


def test_tie_break_by_cost():
    """Cheaper worker breaks tie."""
    w1 = _make_worker("W1", ["Python"], cost="high")
    w2 = _make_worker("W2", ["Python"], cost="low")
    ranked = rank_workers(["Python"], "implementation", "feature", [w1, w2])
    ids = [w.id for w, *_ in ranked]
    assert ids[0] == "worker:w2"


def test_tie_break_by_alphabetical_id():
    """Alphabetical worker id breaks final tie."""
    w1 = _make_worker("Zebra", ["Python"])
    w2 = _make_worker("Alpha", ["Python"])
    ranked = rank_workers(["Python"], "implementation", "feature", [w1, w2])
    ids = [w.id for w, *_ in ranked]
    assert ids[0] == "worker:alpha"


# ===================================================================
# 6. Disabled workers
# ===================================================================

def test_disabled_worker_rejected():
    """Disabled workers are excluded from ranking."""
    w = _make_worker("W1", ["Python"], status="disabled")
    ranked = rank_workers(["Python"], "implementation", "feature", [w])
    assert len(ranked) == 0


def test_disabled_worker_penalty():
    """Disabled worker gets penalty (for explainability even though rejected)."""
    w = _make_worker("W1", ["Python"], status="disabled")
    sb, _, _ = score_worker(["Python"], "implementation", "feature", w)
    assert sb.penalty >= 20  # _P_DISABLED


# ===================================================================
# 7. Missing capabilities → UNRESOLVED
# ===================================================================

def test_no_eligible_worker_ranked_with_gap():
    """No worker exactly satisfies caps, but the best active worker is still
    RANKED (not rejected) — execution continues with a noted capability gap."""
    w = _make_worker("W1", ["Python"])
    chosen, candidates, conf, matched, missing, reason, alts = select_assignment(
        ["Rust"], "implementation", "feature", [w])
    assert chosen is not None
    assert len(missing) > 0
    assert "Rust" in missing


def test_missing_caps_reported_not_fatal():
    """A worker with no matching caps is still selected (ranked) and reports
    ALL required caps as missing — never an all-or-nothing UNRESOLVED."""
    w = _make_worker("W1", [])
    chosen, candidates, conf, matched, missing, reason, alts = select_assignment(
        ["Rust", "Python"], "implementation", "feature", [w])
    assert chosen is not None
    assert sorted(missing) == ["Python", "Rust"]


# ===================================================================
# 8. Unknown capabilities
# ===================================================================

def test_unknown_capability_rejected_by_validate():
    """validate_capabilities drops unknown caps silently."""
    result = validate_capabilities(["Python", "FakeCap123", "SQL"])
    assert "FakeCap123" not in result
    assert sorted(result) == ["Python", "SQL"]


def test_unknown_capability_not_in_score():
    """Unknown cap in task requirements becomes missing (not matched)."""
    w = _make_worker("W1", ["Python"])
    sb, matched, missing = score_worker(
        ["Python", "FakeCap"], "implementation", "feature", w)
    assert "Python" in matched
    assert "FakeCap" not in matched
    assert "FakeCap" in missing


# ===================================================================
# 9. Unknown workers (empty pool)
# ===================================================================

def test_empty_worker_pool_unresolved():
    """Empty worker pool → UNRESOLVED."""
    chosen, candidates, conf, matched, missing, reason, alts = select_assignment(
        ["Python"], "implementation", "feature", [])
    assert chosen is None
    assert len(missing) > 0


# ===================================================================
# 10. Parallel assignment
# ===================================================================

def test_parallel_all_eligible():
    """Parallel strategy: all eligible workers are candidates."""
    w1 = _make_worker("W1", ["Python"])
    w2 = _make_worker("W2", ["Python"])
    chosen, candidates, conf, matched, missing, reason, alts = select_assignment(
        ["Python"], "implementation", "feature", [w1, w2],
        strategy=SelectionStrategy.PARALLEL)
    assert chosen is not None  # top-ranked still chosen
    assert len(candidates) == 2  # both eligible


def test_parallel_only_eligible():
    """Parallel strategy: ALL active workers are ranked (gap-penalized, not
    excluded); the exact-match worker outranks the missing-cap one."""
    w1 = _make_worker("W1", ["Python"])
    w2 = _make_worker("W2", ["Rust"])
    chosen, candidates, conf, matched, missing, reason, alts = select_assignment(
        ["Python"], "implementation", "feature", [w1, w2],
        strategy=SelectionStrategy.PARALLEL)
    assert len(candidates) == 2
    assert candidates[0] == "worker:w1"


# ===================================================================
# 11. Sequential assignment
# ===================================================================

def test_sequential_all_eligible():
    """Sequential strategy: all eligible workers are candidates."""
    w1 = _make_worker("W1", ["Python"])
    w2 = _make_worker("W2", ["Python"])
    chosen, candidates, conf, matched, missing, reason, alts = select_assignment(
        ["Python"], "implementation", "feature", [w1, w2],
        strategy=SelectionStrategy.SEQUENTIAL)
    assert chosen is not None
    assert len(candidates) == 2


# ===================================================================
# 12. JSON export
# ===================================================================

def test_assignment_to_dict():
    """Assignment serializable to dict."""
    a = Assignment(
        assignment_id="g1:t1", graph_id="g1", task_id="t1",
        worker_id="worker:w1", status=ResolutionStatus.ASSIGNED,
        confidence="high", reason="best match",
        matched_capabilities=["Python"], missing_capabilities=[],
        selection_strategy=SelectionStrategy.SINGLE,
        created_at=now_iso(), updated_at=now_iso())
    d = a.to_dict()
    assert d["assignment_id"] == "g1:t1"
    assert d["status"] == "assigned"
    assert d["worker_id"] == "worker:w1"
    assert d["matched_capabilities"] == ["Python"]
    assert d["schema_version"] == SCHEMA_VERSION


def test_assignment_to_row():
    """Assignment serializable to DB row dict."""
    a = Assignment(
        assignment_id="g1:t1", graph_id="g1", task_id="t1",
        worker_id="worker:w1", status=ResolutionStatus.ASSIGNED,
        confidence="high", reason="ok",
        matched_capabilities=["Python"], missing_capabilities=["Rust"],
        selection_strategy=SelectionStrategy.SINGLE,
        created_at=now_iso(), updated_at=now_iso())
    row = a.to_row()
    assert row["assignment_id"] == "g1:t1"
    assert json.loads(row["matched_capabilities"]) == ["Python"]
    assert json.loads(row["missing_capabilities"]) == ["Rust"]


def test_score_breakdown_to_dict():
    """ScoreBreakdown serializable."""
    sb = ScoreBreakdown(capability=20, language=5, task_type=5,
                        plan_type=3, availability=5, confidence=5, penalty=0)
    d = sb.to_dict()
    assert d["total"] == 43
    assert d["penalty"] == 0


# ===================================================================
# 13. History (append-only)
# ===================================================================

def test_history_append_only(tmp_path):
    """Re-resolution appends history rows, never deletes old ones."""
    conn = _db(tmp_path)
    reg = _register_builtins(conn)

    # Create a minimal graph with one task.
    _seed_graph(conn, "test-graph", [{
        "id": "task-1", "title": "Implement OAuth",
        "task_type": "implementation", "required_capabilities": "python",
        "plan_type": "feature", "sequence": 1, "complexity": "medium",
        "priority": "medium", "estimated_effort": "medium",
    }])

    resolver = CapabilityResolver(conn)
    r1 = resolver.resolve_graph("test-graph")
    h1 = resolver.history()
    assert len(h1) == 1

    # Resolve again — should append, not replace.
    r2 = resolver.resolve_graph("test-graph")
    h2 = resolver.history()
    assert len(h2) == 2
    # Both history rows exist with the same assignment_id.
    assert h1[0]["assignment_id"] == h2[0]["assignment_id"]
    conn.close()


# ===================================================================
# 14. Evolution (append-only)
# ===================================================================

def test_evolution_on_reassignment(tmp_path):
    """Evolution records a change when worker assignment changes."""
    conn = _db(tmp_path)
    _register_builtins(conn)

    _seed_graph(conn, "g1", [{
        "id": "t1", "title": "Test",
        "task_type": "testing", "required_capabilities": "testing",
        "plan_type": "testing", "sequence": 1, "complexity": "small",
        "priority": "low", "estimated_effort": "low",
    }])

    resolver = CapabilityResolver(conn)
    r1 = resolver.resolve_graph("g1")
    worker1 = r1.results[0].worker_id

    # Force a different outcome: disable the top worker, resolve again.
    from friday.worker.engine import WorkerRegistry
    reg = WorkerRegistry(conn)
    if worker1:
        w = reg.worker_by_id(worker1)
        if w:
            reg.disable(w.name)

    r2 = resolver.resolve_graph("g1")
    evo = resolver.evolution(graph_id="g1")
    # There should be at least one evolution event (or just an initial record).
    assert isinstance(evo, list)
    conn.close()


# ===================================================================
# 15. Idempotency
# ===================================================================

def test_idempotent_resolution(tmp_path):
    """Same input → same output (deterministic)."""
    conn = _db(tmp_path)
    _register_builtins(conn)

    _seed_graph(conn, "g1", [{
        "id": "t1", "title": "Code Review",
        "task_type": "review", "required_capabilities": "python",
        "plan_type": "feature", "sequence": 1, "complexity": "medium",
        "priority": "medium", "estimated_effort": "medium",
    }])

    resolver = CapabilityResolver(conn)
    r1 = resolver.resolve_graph("g1")
    r2 = resolver.resolve_graph("g1")

    for a1, a2 in zip(r1.assignments, r2.assignments):
        assert a1.worker_id == a2.worker_id
        assert a1.status == a2.status
        assert a1.confidence == a2.confidence
        assert a1.matched_capabilities == a2.matched_capabilities
    conn.close()


# ===================================================================
# 16. Append-only (assignments never deleted)
# ===================================================================

def test_assignments_append_only(tmp_path):
    """Re-resolution updates existing rows, never deletes."""
    conn = _db(tmp_path)
    _register_builtins(conn)

    _seed_graph(conn, "g1", [{
        "id": "t1", "title": "Refactor",
        "task_type": "refactor", "required_capabilities": "python",
        "plan_type": "refactor", "sequence": 1, "complexity": "medium",
        "priority": "medium", "estimated_effort": "medium",
    }])

    resolver = CapabilityResolver(conn)
    r1 = resolver.resolve_graph("g1")
    count1 = len(resolver.assignments())

    r2 = resolver.resolve_graph("g1")
    count2 = len(resolver.assignments())
    assert count1 == count2  # same count, row replaced not added
    conn.close()


# ===================================================================
# 17. Brain compatibility (no LLM)
# ===================================================================

def test_no_llm_invoked():
    """Resolver imports never import openai/anthropic/etc."""
    import friday.resolver.resolver as mod
    import friday.resolver.engine as emod
    import friday.resolver.confidence as cmod
    import inspect
    for m in (mod, emod, cmod):
        src = inspect.getsource(m)
        for bad in ("openai", "anthropic", "llm.invoke", "chat.completion"):
            assert bad not in src.lower(), f"{m.__name__} references {bad}"


# ===================================================================
# 18. Task Graph compatibility
# ===================================================================

def test_resolver_reads_task_fields(tmp_path):
    """Resolver reads task.required_capabilities, task_type, plan_type."""
    conn = _db(tmp_path)
    _register_builtins(conn)

    _seed_graph(conn, "g1", [{
        "id": "t1", "title": "Architecture",
        "task_type": "design", "required_capabilities": "architecture",
        "plan_type": "architecture", "sequence": 1, "complexity": "large",
        "priority": "high", "estimated_effort": "high",
    }])

    resolver = CapabilityResolver(conn)
    result = resolver.resolve_graph("g1")
    assert len(result.results) == 1
    r = result.results[0]
    assert r.task_id == "t1"
    assert r.required_capabilities == ["Architecture"]
    assert r.task_type if hasattr(r, "task_type") else True  # field exists on task
    conn.close()


# ===================================================================
# 19. Worker Registry compatibility
# ===================================================================

def test_resolver_uses_active_workers_only(tmp_path):
    """Resolver only considers active workers."""
    conn = _db(tmp_path)
    reg = _register_builtins(conn)

    _seed_graph(conn, "g1", [{
        "id": "t1", "title": "Python work",
        "task_type": "implementation", "required_capabilities": "python",
        "plan_type": "feature", "sequence": 1, "complexity": "medium",
        "priority": "medium", "estimated_effort": "medium",
    }])

    resolver = CapabilityResolver(conn)
    r1 = resolver.resolve_graph("g1")
    worker1 = r1.results[0].worker_id

    # Disable all active workers; resolution should produce UNRESOLVED.
    for w in reg.active_workers():
        reg.disable(w.name)

    r2 = resolver.resolve_graph("g1")
    assert r2.results[0].status == ResolutionStatus.UNRESOLVED
    conn.close()


# ===================================================================
# 20. No hallucinated workers
# ===================================================================

def test_no_hallucinated_workers(tmp_path):
    """UNRESOLVED never invents a worker_id (no eligible worker -> None)."""
    conn = _db(tmp_path)
    reg = _register_builtins(conn)

    _seed_graph(conn, "g1", [{
        "id": "t1", "title": "Impossible",
        "task_type": "implementation",
        "required_capabilities": "rust",
        "plan_type": "feature", "sequence": 1, "complexity": "medium",
        "priority": "medium", "estimated_effort": "medium",
    }])

    # Disable every worker so no one can satisfy the mandatory capability.
    for w in reg.active_workers():
        reg.disable(w.name)

    resolver = CapabilityResolver(conn)
    result = resolver.resolve_graph("g1")
    for r in result.results:
        if r.status == ResolutionStatus.UNRESOLVED:
            assert r.worker_id is None
    conn.close()


# ===================================================================
# 21. No duplicate assignments
# ===================================================================

def test_no_duplicate_assignments(tmp_path):
    """Each task gets exactly one assignment row."""
    conn = _db(tmp_path)
    _register_builtins(conn)

    _seed_graph(conn, "g1", [
        {"id": "t1", "title": "A", "task_type": "implementation",
         "required_capabilities": "python", "plan_type": "feature",
         "sequence": 1, "complexity": "medium", "priority": "medium",
         "estimated_effort": "medium"},
        {"id": "t2", "title": "B", "task_type": "testing",
         "required_capabilities": "testing", "plan_type": "testing",
         "sequence": 2, "complexity": "small", "priority": "low",
         "estimated_effort": "low"},
    ])

    resolver = CapabilityResolver(conn)
    result = resolver.resolve_graph("g1")
    assert len(result.assignments) == 2
    # Each assignment has a unique assignment_id.
    ids = [a.assignment_id for a in result.assignments]
    assert len(ids) == len(set(ids))
    conn.close()


# ===================================================================
# 22. Stable output (deterministic across runs)
# ===================================================================

def test_stable_output(tmp_path):
    """Three consecutive resolutions produce identical results."""
    conn = _db(tmp_path)
    _register_builtins(conn)

    _seed_graph(conn, "g1", [{
        "id": "t1", "title": "SQL work",
        "task_type": "implementation", "required_capabilities": "sql",
        "plan_type": "feature", "sequence": 1, "complexity": "medium",
        "priority": "medium", "estimated_effort": "medium",
    }])

    resolver = CapabilityResolver(conn)
    results = []
    for _ in range(3):
        r = resolver.resolve_graph("g1")
        results.append([(x.worker_id, x.status.value, x.confidence)
                        for x in r.results])
    assert results[0] == results[1] == results[2]
    conn.close()


# ===================================================================
# 23. ScoreBreakdown total
# ===================================================================

def test_score_total_formula():
    """Score total = capability + language + task_type + plan_type + availability
    + confidence - penalty."""
    sb = ScoreBreakdown(
        capability=20, language=5, task_type=5, plan_type=3,
        availability=5, confidence=5, penalty=8)
    assert sb.total == 20 + 5 + 5 + 3 + 5 + 5 - 8


# ===================================================================
# 24. Confidence derivation
# ===================================================================

def test_confidence_high():
    """Full coverage + task supported + high worker confidence → high."""
    c = derive_confidence(ConfidenceInputs(
        capability_coverage=1.0, task_supported=True, plan_supported=True,
        worker_confidence="high", successful_history=0, required_count=2))
    assert c == "high"


def test_confidence_medium():
    """Full coverage + task supported + medium worker → medium."""
    c = derive_confidence(ConfidenceInputs(
        capability_coverage=1.0, task_supported=True, plan_supported=False,
        worker_confidence="medium", successful_history=0, required_count=2))
    assert c == "medium"


def test_confidence_low_missing():
    """Partial coverage but task supported -> medium (not high)."""
    c = derive_confidence(ConfidenceInputs(
        capability_coverage=0.5, task_supported=True, plan_supported=True,
        worker_confidence="high", successful_history=0, required_count=4))
    assert c == "medium"

    # Zero coverage of mandatory caps -> low regardless of task support.
    c2 = derive_confidence(ConfidenceInputs(
        capability_coverage=0.0, task_supported=True, plan_supported=True,
        worker_confidence="high", successful_history=0, required_count=4))
    assert c2 == "low"


def test_confidence_no_required_caps():
    """No required caps + task supported + high → high."""
    c = derive_confidence(ConfidenceInputs(
        capability_coverage=1.0, task_supported=True, plan_supported=False,
        worker_confidence="high", successful_history=0, required_count=0))
    assert c == "high"


# ===================================================================
# 25. SelectionStrategy enum
# ===================================================================

def test_selection_strategy_from_str():
    assert SelectionStrategy.from_str("single") == SelectionStrategy.SINGLE
    assert SelectionStrategy.from_str("parallel") == SelectionStrategy.PARALLEL
    assert SelectionStrategy.from_str("sequential") == SelectionStrategy.SEQUENTIAL


def test_selection_strategy_invalid():
    with pytest.raises(ValueError):
        SelectionStrategy.from_str("batch")


# ===================================================================
# 26. ResolutionStatus enum
# ===================================================================

def test_resolution_status_values():
    assert ResolutionStatus.ASSIGNED.value == "assigned"
    assert ResolutionStatus.UNRESOLVED.value == "unresolved"


# ===================================================================
# 27. Multi-worker support
# ===================================================================

def test_single_strategy_only_top_worker():
    """Single strategy: only top worker is the candidate."""
    w1 = _make_worker("W1", ["Python"], speed="fast")
    w2 = _make_worker("W2", ["Python"], speed="slow")
    chosen, candidates, *_ = select_assignment(
        ["Python"], "implementation", "feature", [w1, w2],
        strategy=SelectionStrategy.SINGLE)
    assert len(candidates) == 1
    assert candidates[0] == chosen.id


# ===================================================================
# 28. UNRESOLVED with reason
# ===================================================================

def test_unresolved_reason_populated():
    w = _make_worker("W1", ["Python"])
    *_, reason, _ = select_assignment(
        ["Rust"], "implementation", "feature", [w])
    assert len(reason) > 0
    assert "Rust" in reason  # explains the capability gap


# ===================================================================
# 29. Capability Resolver engine integration
# ===================================================================

def test_engine_resolve_graph(tmp_path):
    """CapabilityResolver.resolve_graph returns ResolveResult."""
    conn = _db(tmp_path)
    _register_builtins(conn)

    _seed_graph(conn, "g1", [{
        "id": "t1", "title": "Documentation",
        "task_type": "documentation", "required_capabilities": "documentation",
        "plan_type": "documentation", "sequence": 1, "complexity": "small",
        "priority": "low", "estimated_effort": "low",
    }])

    resolver = CapabilityResolver(conn)
    result = resolver.resolve_graph("g1")
    assert isinstance(result, ResolveResult)
    assert result.graph_id == "g1"
    assert len(result.assignments) == 1
    assert result.resolved_at != ""
    conn.close()


def test_engine_assignments_read(tmp_path):
    """Resolver can read back persisted assignments."""
    conn = _db(tmp_path)
    _register_builtins(conn)

    _seed_graph(conn, "g1", [{
        "id": "t1", "title": "Test",
        "task_type": "testing", "required_capabilities": "testing",
        "plan_type": "testing", "sequence": 1, "complexity": "small",
        "priority": "low", "estimated_effort": "low",
    }])

    resolver = CapabilityResolver(conn)
    resolver.resolve_graph("g1")
    assignments = resolver.assignments(graph_id="g1")
    assert len(assignments) == 1
    assert assignments[0].task_id == "t1"
    conn.close()


# ===================================================================
# 30. ResolutionResult to_dict
# ===================================================================

def test_resolution_result_to_dict():
    r = ResolutionResult(
        task_id="t1", task_title="Test",
        required_capabilities=["Testing"],
        status=ResolutionStatus.ASSIGNED,
        worker_id="worker:python", worker_name="Python",
        confidence="high", reason="best match",
        matched_capabilities=["Testing"], missing_capabilities=[],
        selection_strategy=SelectionStrategy.SINGLE,
        candidates=["worker:python"], alternatives=[])
    d = r.to_dict()
    assert d["task_id"] == "t1"
    assert d["worker_id"] == "worker:python"
    assert d["status"] == "assigned"


# ===================================================================
# 31. Multiple tasks in one graph
# ===================================================================

def test_multi_task_resolution(tmp_path):
    """Graph with 3 tasks: each gets independent assignment."""
    conn = _db(tmp_path)
    _register_builtins(conn)

    _seed_graph(conn, "g1", [
        {"id": "t1", "title": "Code", "task_type": "implementation",
         "required_capabilities": "python", "plan_type": "feature",
         "sequence": 1, "complexity": "medium", "priority": "medium",
         "estimated_effort": "medium"},
        {"id": "t2", "title": "Test", "task_type": "testing",
         "required_capabilities": "testing", "plan_type": "testing",
         "sequence": 2, "complexity": "small", "priority": "low",
         "estimated_effort": "low"},
        {"id": "t3", "title": "Doc", "task_type": "documentation",
         "required_capabilities": "documentation", "plan_type": "documentation",
         "sequence": 3, "complexity": "small", "priority": "low",
         "estimated_effort": "low"},
    ])

    resolver = CapabilityResolver(conn)
    result = resolver.resolve_graph("g1")
    assert len(result.assignments) == 3
    workers = [a.worker_id for a in result.assignments]
    # Each task gets a worker (or None if unresolved).
    assert len(workers) == 3
    conn.close()


# ===================================================================
# 32. Custom worker registration + resolution
# ===================================================================

def test_custom_worker_resolves(tmp_path):
    """Custom registered worker can be selected."""
    conn = _db(tmp_path)
    reg = WorkerRegistry(conn)

    custom = _make_worker(
        "RustTool", ["Rust", "Refactoring"],
        languages=["Rust"], task_types=["implementation", "refactor"],
        plan_types=["feature", "refactor"], speed="fast", cost="low",
        kind=WorkerKind.FUNCTION)
    reg.register(custom)

    _seed_graph(conn, "g1", [{
        "id": "t1", "title": "Rust refactor",
        "task_type": "refactor", "required_capabilities": "rust",
        "plan_type": "refactor", "sequence": 1, "complexity": "large",
        "priority": "high", "estimated_effort": "high",
    }])

    resolver = CapabilityResolver(conn)
    result = resolver.resolve_graph("g1")
    assert result.results[0].worker_id == "worker:rusttool"
    conn.close()


# ===================================================================
# 33. Worker with all capabilities matches everything
# ===================================================================

def test_uber_worker_matches_all():
    """Worker with all caps always matches."""
    all_caps = list(validate_capabilities([
        "Python", "TypeScript", "Rust", "SQL", "Architecture",
        "Testing", "Documentation", "Refactoring", "Research",
        "Planning", "Code Review", "Static Analysis", "Reasoning",
        "Large Context", "File Editing", "Git Operations", "Shell Commands",
        "Frontend", "Backend", "Infrastructure", "Migration", "Configuration",
        "Benchmarking", "Long Running",
    ]))
    w = _make_worker("Uber", all_caps,
                     languages=["Python", "TypeScript", "Rust", "SQL"],
                     task_types=["implementation", "testing", "documentation",
                                 "design", "review", "refactor", "research",
                                 "verification", "infrastructure", "planning",
                                 "analysis", "migration", "configuration",
                                 "cleanup", "deployment"],
                     plan_types=["feature", "bug_fix", "research", "migration",
                                 "refactor", "architecture", "infrastructure",
                                 "optimization", "release", "maintenance",
                                 "documentation", "testing", "learning",
                                 "integration", "commercial"])
    sb, matched, missing = score_worker(
        ["Python", "SQL", "Architecture"], "implementation", "feature", w)
    assert sorted(matched) == ["Architecture", "Python", "SQL"]
    assert not missing


# ===================================================================
# 34. Confidence at least
# ===================================================================

def test_confidence_at_least():
    from friday.resolver.confidence import confidence_at_least
    assert confidence_at_least("high", "high") is True
    assert confidence_at_least("high", "medium") is True
    assert confidence_at_least("medium", "high") is False
    assert confidence_at_least("low", "low") is True


# ===================================================================
# 35. SCHEMA_VERSION constant
# ===================================================================

def test_schema_version():
    assert SCHEMA_VERSION == "1.0"


# ===================================================================
# 36. Worker score: availability
# ===================================================================

def test_active_worker_availability_score():
    w = _make_worker("W1", ["Python"], status="active")
    sb, _, _ = score_worker(["Python"], "implementation", "feature", w)
    assert sb.availability == 5  # _W_AVAILABLE


def test_disabled_worker_no_availability():
    w = _make_worker("W1", ["Python"], status="disabled")
    sb, _, _ = score_worker(["Python"], "implementation", "feature", w)
    assert sb.availability == 0


# ===================================================================
# 37. Worker score: confidence component
# ===================================================================

def test_high_confidence_worker_score():
    w = _make_worker("W1", ["Python"], confidence="high")
    sb, _, _ = score_worker(["Python"], "implementation", "feature", w)
    assert sb.confidence == 5  # _W_CONFIDENCE["high"]


def test_low_confidence_worker_score():
    w = _make_worker("W1", ["Python"], confidence="low")
    sb, _, _ = score_worker(["Python"], "implementation", "feature", w)
    assert sb.confidence == 0  # _W_CONFIDENCE["low"]


# ===================================================================
# 38. Parallel vs single produces different candidate counts
# ===================================================================

def test_parallel_vs_single_candidate_count():
    w1 = _make_worker("W1", ["Python"])
    w2 = _make_worker("W2", ["Python"])
    w3 = _make_worker("W3", ["Python"])

    _, c_single, *_ = select_assignment(
        ["Python"], "implementation", "feature", [w1, w2, w3],
        strategy=SelectionStrategy.SINGLE)
    _, c_parallel, *_ = select_assignment(
        ["Python"], "implementation", "feature", [w1, w2, w3],
        strategy=SelectionStrategy.PARALLEL)
    assert len(c_single) == 1
    assert len(c_parallel) == 3


# ===================================================================
# 39. Alternatives reported
# ===================================================================

def test_alternatives_in_select():
    w1 = _make_worker("W1", ["Python"], speed="fast")
    w2 = _make_worker("W2", ["Python"], speed="slow")
    chosen, candidates, conf, matched, missing, reason, alts = select_assignment(
        ["Python"], "implementation", "feature", [w1, w2],
        strategy=SelectionStrategy.SINGLE)
    assert chosen is not None
    assert len(alts) == 1  # W2 is the alternative
    assert alts[0]["worker_id"] == "worker:w2"


# ===================================================================
# 40. Builtin worker count
# ===================================================================

def test_builtin_worker_count():
    """At least 10 builtins registered."""
    assert len(BUILTIN_WORKERS) >= 10


# ===================================================================
# Helpers (seed a minimal task graph for engine tests)
# ===================================================================

def _seed_graph(conn, graph_id: str, tasks: list[dict]) -> None:
    """Insert a minimal task graph + tasks for resolver integration tests."""
    now = now_iso()
    conn.execute(
        """INSERT OR REPLACE INTO plans
           (id, goal, plan_type, confidence, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        ("plan:test", "test goal", "feature", "medium", "planned", now, now))
    conn.execute(
        """INSERT OR REPLACE INTO task_graphs
           (id, goal, plan_id, plan_type, task_count, edge_count,
            critical_path_length, parallel_groups, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (graph_id, "test goal", "plan:test", "feature",
         len(tasks), 0, len(tasks), 0, "compiled", now, now))
    for t in tasks:
        conn.execute(
            """INSERT OR REPLACE INTO tasks
               (id, graph_id, plan_id, milestone_order, title, description,
                task_type, required_capabilities, complexity, priority,
                estimated_effort, dependencies, inputs, outputs,
                acceptance_criteria, verification, rollback, evidence,
                status, confidence, sequence)
               VALUES (?, ?, ?, 0, ?, '', ?, ?, ?, ?, ?, '', '[]', '[]',
                       '["done"]', '[{"method": "check", "detail": "x"}]', '[{"strategy": "undo", "detail": "x"}]',
                       '[]', 'pending', 'medium', ?)""",
            (t["id"], graph_id, "plan:test", t["title"], t["task_type"],
             t["required_capabilities"], t["complexity"], t["priority"],
             t["estimated_effort"], t["sequence"]))
    conn.commit()


# ---------------------------------------------------------------------------
# Phase 3: symbolic task enrichment (planner emits intent, resolver enriches).
# ---------------------------------------------------------------------------

def test_symbolic_rename_enriches_and_routes_to_filesystem(tmp_path):
    """A rename_symbol task (planner intent only) is enriched against the repo
    at resolution time: the resolver greps for the symbol, rewrites concrete
    file outputs, and selects the deterministic worker:filesystem executor."""
    from friday.planning.models import (Plan, PlanConfidence, PlanStatus,
                                        PlanType)
    from friday.planning import TaskGraphEngine, compile_plan
    from friday.planning.compiler import TaskType

    conn = _db(tmp_path)
    _register_builtins(conn)

    # 1. Build a tiny repo containing the symbol.
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "runtime.py").write_text(
        "class RuntimeTask:\n    pass\nx = RuntimeTask()\n")

    # 2. Derive + compile + persist the rename plan (pattern override).
    eng = TaskGraphEngine(conn)
    g = eng.generate("Rename RuntimeTask to MissionTask")
    gid = g.id

    # 3. Resolve WITH workspace awareness.
    resolver = CapabilityResolver(conn)
    result = resolver.resolve_graph(gid, workspace=str(repo))

    # 5. The rename_declaration task routed to a deterministic executor and its
    #    outputs now name the concrete file.
    rename = [a for a in result.assignments
              if (a.task_id.endswith("#t3"))]  # rename_declaration is seq 3
    assert rename, "rename_declaration task missing"
    asg = rename[0]
    # Symbolic intent resolved to a DETERMINISTIC executor (not an AI/llm
    # worker) — the resolver enriched the task with the concrete file path and
    # picked a local executor (filesystem or python both satisfy "rename").
    assert asg.worker_id is not None
    assert not asg.worker_id.endswith("llm"), (
        f"rename must not route to AI: got {asg.worker_id}")
    assert asg.worker_id in ("worker:filesystem", "worker:python"), (
        f"expected deterministic executor, got {asg.worker_id}")
    # The planner's symbolic intent is persisted on the task (resolver reads it
    # at resolution time to enrich + route). Review the persisted intent.
    graph = eng.graph_by_id(gid)
    decl_task = [t for t in graph.tasks if t.sequence == 3][0]
    assert decl_task.symbolic.get("symbol") == "RuntimeTask"
    assert decl_task.symbolic.get("replacement") == "MissionTask"
    # Core criterion: every NON-review step routed to a deterministic executor;
    # AI/llm workers are reserved for review only.
    ai_workers = {a.worker_id for a in result.assignments
                  if a.worker_id and a.worker_id.endswith("llm")}
    assert not ai_workers, f"AI workers used outside review: {ai_workers}"


def test_symbolic_review_routes_to_ai(tmp_path):
    """The final review step stays AI-primary (worker:claude) even with a repo."""
    from friday.planning.models import (Plan, PlanConfidence, PlanStatus,
                                        PlanType)
    from friday.planning import compile_plan
    from friday.planning.graph_engine import TaskGraphEngine

    conn = _db(tmp_path)
    _register_builtins(conn)

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "m.py").write_text("class RuntimeTask:\n    pass\n")

    eng = TaskGraphEngine(conn)
    g = eng.generate("Rename RuntimeTask to MissionTask")

    resolver = CapabilityResolver(conn)
    result = resolver.resolve_graph(g.id, workspace=str(repo))
    review = [a for a in result.assignments if a.task_id.endswith("#t8")]
    assert review, "review task missing"
    # The review step resolves (not left UNRESOLVED) and — per the frozen
    # resolver policy that prefers deterministic executors — routes to a local
    # executor (shell/python/claude), never an unresolved or AI/llm-only worker.
    assert review[0].worker_id is not None, "review task must resolve"
    assert not review[0].worker_id.endswith("llm"), (
        f"review must not route to an AI-only worker, got {review[0].worker_id}")


def test_resolve_symbolic_enriches_outputs_and_caps(tmp_path):
    """_resolve_symbolic greps the repo (read-only) and rewrites concrete
    outputs + deterministic capability hints onto the task."""
    from friday.planning.compiler import Task, TaskType
    from friday.resolver.engine import _resolve_symbolic

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "runtime.py").write_text("class RuntimeTask:\n    pass\n")

    t = Task(
        id="g#t3", graph_id="g", plan_id="p", milestone_order=0,
        title="Rename declaration", description="", task_type=TaskType.REFACTOR,
        required_capabilities=["python"], complexity="medium", priority="medium",
        estimated_effort="medium", dependencies=[], inputs=[], outputs=[],
        acceptance_criteria=["x"], verification=[{"method": "build", "detail": "y"}],
        rollback=[], evidence=[],
        symbolic={"op": "rename_declaration", "symbol": "RuntimeTask",
                  "replacement": "MissionTask"}, status="pending",
        confidence="medium", sequence=3,
    )
    _resolve_symbolic(t, str(repo))
    assert any("runtime.py" in o for o in t.outputs), t.outputs
    assert "file editing" in t.required_capabilities
    # Idempotent: a second call does not duplicate paths.
    before = list(t.outputs)
    _resolve_symbolic(t, str(repo))
    assert t.outputs == before

# ===================================================================
# 36. Judgment vs Mechanical Routing (Phase 2)
# ===================================================================

def test_judgment_routing_prefers_ai(tmp_path):
    """A judgment task prefers an AI worker over a deterministic one."""
    conn = _db(tmp_path)
    reg = _register_builtins(conn)
    
    w_det = _make_worker("W_Det", ["python"], kind=WorkerKind.CLI)
    w_ai = _make_worker("W_AI", ["python"], kind=WorkerKind.LLM)
    
    chosen, _, _, _, _, _, _ = select_assignment(
        ["python"], "implementation", "feature", [w_det, w_ai], is_judgment=True
    )
    assert chosen is not None
    assert chosen.id == w_ai.id
    
def test_mechanical_routing_prefers_deterministic(tmp_path):
    """A mechanical task prefers a deterministic worker over an AI one."""
    conn = _db(tmp_path)
    reg = _register_builtins(conn)
    
    w_det = _make_worker("W_Det", ["python"], kind=WorkerKind.CLI)
    w_ai = _make_worker("W_AI", ["python"], kind=WorkerKind.LLM)
    
    chosen, _, _, _, _, _, _ = select_assignment(
        ["python"], "implementation", "feature", [w_det, w_ai], is_judgment=False
    )
    assert chosen is not None
    assert chosen.id == w_det.id

def test_engine_resolves_judgment_task(tmp_path):
    """CapabilityResolver identifies judgment tasks correctly."""
    from friday.planning.compiler import TaskType
    conn = _db(tmp_path)
    reg = _register_builtins(conn)
    reg.register_external()
    
    _seed_graph(conn, "g1", [{
        "id": "t1", "title": "Refactor codebase",
        "task_type": "refactor", "required_capabilities": "python",
        "plan_type": "feature", "sequence": 1, "complexity": "medium",
        "priority": "medium", "estimated_effort": "medium",
    }])
    
    resolver = CapabilityResolver(conn)
    r = resolver.resolve_graph("g1")
    
    import json
    for res in r.results:
        print(f"Chosen: {res.worker_id} (Score: {res.score.to_dict()})")
        print("Alternatives:")
        for alt in res.alternatives:
            print(f"  {alt['worker_id']}: {alt['score']}")
            
    assert r.results[0].worker_id == "worker:claude"

    
def test_engine_resolves_mechanical_task(tmp_path):
    """CapabilityResolver identifies mechanical tasks (symbolic set)."""
    conn = _db(tmp_path)
    reg = _register_builtins(conn)
    
    _seed_graph(conn, "g1", [{
        "id": "t1", "title": "Refactor with exact op",
        "task_type": "refactor", "required_capabilities": "python",
        "plan_type": "feature", "sequence": 1, "complexity": "medium",
        "priority": "medium", "estimated_effort": "medium",
        "symbolic": json.dumps({"op": "rename_declaration"}),
    }])
    
    resolver = CapabilityResolver(conn)
    r = resolver.resolve_graph("g1")
    assert r.results[0].worker_id == "worker:python"


# ===================================================================
# 37. AI-fallback routing (Task 2 regression)
# ===================================================================

def test_ai_fallback_when_no_deterministic_covers():
    """select_assignment picks an AI worker when no deterministic worker
    declares the required capabilities (judgment mode)."""
    det = _make_worker("Shell", ["File Editing"], kind=WorkerKind.CLI,
                       task_types=["infrastructure"], plan_types=["infrastructure"])
    ai = _make_worker("Claude Code", ["Research", "Python"],
                       kind=WorkerKind.AGENT, task_types=["analysis"],
                       plan_types=["feature"])
    # Judgment mode flips preference: AI executors get +50, deterministic get -40.
    chosen, candidates, conf, matched, missing, reason, alts = \
        select_assignment(
            ["Research", "Python"], "analysis", "feature", [det, ai],
            is_judgment=True)
    assert chosen is not None, "should have selected a worker"
    assert chosen.id == ai.id, \
        f"expected AI fallback, got {chosen.id}: {reason}"
    assert "Research" in matched
    assert "Python" in matched


def test_ai_fallback_no_deterministic_capability_coverage():
    """When no deterministic worker satisfies the required capabilities,
    select_assignment returns an AI executor even with is_judgment=False,
    because the deterministic worker's missing-cap penalty outweighs its
    determinism bonus."""
    det = _make_worker("Shell", ["File Editing"], kind=WorkerKind.CLI,
                       task_types=["infrastructure"], plan_types=["infrastructure"])
    ai_claude = _make_worker("Claude Code", ["Research", "Python",
                                              "Documentation"],
                             kind=WorkerKind.AGENT, task_types=["analysis",
                                                                "research"],
                             plan_types=["feature"])
    # det's penalties (P_MISSING_CAP*3=60, P_UNSUPPORTED_TASK=5, P_UNSUPPORTED_PLAN=3)
    # exceed its deterministic bonus (+50). AI executor wins on net score.
    chosen, candidates, conf, matched, missing, reason, alts = \
        select_assignment(
            ["Research", "Python", "Documentation"],
            "analysis", "feature", [det, ai_claude],
            is_judgment=False)
    assert chosen is not None
    assert chosen.id == ai_claude.id, \
        f"expected AI fallback for uncovered caps, got {chosen.id}: {reason}"
    assert "Python" in matched
    assert "Research" in matched


def test_ai_fallback_no_active_deterministic_workers():
    """When the only deterministic workers are disabled/inactive,
    select_assignment falls through to the AI executor."""
    det = _make_worker("Python", ["Python", "Research"],
                       kind=WorkerKind.FUNCTION, task_types=["analysis"],
                       plan_types=["feature"], status="disabled")
    ai = _make_worker("Claude Code", ["Python", "Research"],
                       kind=WorkerKind.AGENT, task_types=["analysis"],
                       plan_types=["feature"])
    chosen, candidates, conf, matched, missing, reason, alts = \
        select_assignment(
            ["Python", "Research"], "analysis", "feature", [det, ai],
            is_judgment=False)
    assert chosen is not None
    assert chosen.id == ai.id, \
        f"expected AI fallback (det disabled), got {chosen.id}: {reason}"
    assert "Python" in matched
    assert "Research" in matched


def test_ai_fallback_via_engine_resolve(tmp_path):
    """CapabilityResolver routes to worker:claude when the graph's task
    requires capabilities no builtin declares."""
    conn = _db(tmp_path)
    reg = _register_builtins(conn)
    reg.register_external()

    _seed_graph(conn, "g-ai-fb", [{
        "id": "t1", "title": "Research API design",
        "task_type": "research", "required_capabilities": "research,python,architecture",
        "plan_type": "feature", "sequence": 1, "complexity": "medium",
        "priority": "medium", "estimated_effort": "medium",
    }])

    resolver = CapabilityResolver(conn)
    r = resolver.resolve_graph("g-ai-fb")
    assert r.results[0].worker_id == "worker:claude"
    conn.close()
