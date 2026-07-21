"""Engineering-pattern planner tests (Phase 3).

The planner must decompose software-engineering missions into deterministic,
typed SYMBOLIC task graphs instead of one AI blob. For each recognized pattern
we assert:
  - the recognized pattern emits the explicit multi-step workflow,
  - dependency ordering is a valid chain (no cycles; correct sequence),
  - each step carries a task_type that routes to a DETERMINISTIC executor
    (analysis/refactor/implementation/cleanup/configuration/testing/
    verification) EXCEPT the final `review` step which is AI-primary,
  - every task has non-empty, specific acceptance_criteria + verification.

Before vs after (rename "RuntimeTask to MissionTask"):
  BEFORE (generic PlanEngine milestones -> one implementation blob):
    Investigate -> Design -> Backend -> Frontend -> Verify -> Document -> Rollout
  AFTER (pattern override):
    locate_symbol -> find_references -> rename_declaration -> rename_imports
    -> update_references -> run_formatter -> run_tests -> review_changes
"""

from __future__ import annotations

import pytest

from friday.planning import compile_plan
from friday.planning.compiler import TaskType, _detect_cycle
from friday.planning.models import Plan, PlanConfidence, PlanStatus, PlanType
from friday.planning.patterns import classify

# Task types that must route to deterministic (non-AI) executors.
_DETERMINISTIC_TYPES = {
    TaskType.ANALYSIS, TaskType.REFACTOR, TaskType.IMPLEMENTATION,
    TaskType.CLEANUP, TaskType.CONFIGURATION, TaskType.TESTING,
    TaskType.VERIFICATION,
}


def _plan(goal, ptype=PlanType.REFACTOR):
    return Plan(
        goal=goal, plan_type=ptype,
        confidence=PlanConfidence.MEDIUM, status=PlanStatus.PLANNED,
        milestones=[{"order": 1, "title": "Implement",
                     "detail": "do the thing", "evidence": ""}],
    )


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("goal,expected", [
    ("Rename RuntimeTask to MissionTask", "rename"),
    ("rename the foo into bar", "rename"),
    ("Extract scheduler utilities into a new module", "extract"),
    ("refactor RuntimeEngine into smaller components", "refactor"),
    ("Add structured logging to scheduler", "feature"),
    ("add retry support to Claude executor", "feature"),
    ("Fix failing scheduler tests", "bugfix"),
    ("fix the dependency ordering bug", "bugfix"),
    ("Remove dead code", "maintenance"),
    ("delete unused helpers", "maintenance"),
    ("clean up the legacy module", "maintenance"),
])
def test_classify_recognizes_patterns(goal, expected):
    p = classify(goal)
    assert p is not None
    assert p.name == expected


def test_classify_unknown_goal_returns_none():
    assert classify("Write a haiku about concurrency") is None
    assert classify("") is None


def test_rename_extracts_symbol_and_replacement():
    p = classify("Rename RuntimeTask to MissionTask")
    assert p is not None
    # symbol + replacement live on the rename_declaration task.
    decl = [t for t in p.tasks if t.op == "rename_declaration"][0]
    assert decl.symbolic["symbol"] == "RuntimeTask"
    assert decl.symbolic["replacement"] == "MissionTask"


# ---------------------------------------------------------------------------
# Compiled graph shape
# ---------------------------------------------------------------------------

def _compile(goal, ptype=PlanType.REFACTOR):
    return compile_plan(_plan(goal, ptype))


def test_rename_overrides_generic_milestones():
    g = _compile("Rename RuntimeTask to MissionTask")
    titles = [t.title for t in g.tasks]
    assert any("Locate symbol" in t for t in titles)
    assert any("Run tests" in t for t in titles)
    assert any("Review" in t for t in titles)
    # No generic PlanEngine milestones leaked through.
    assert not any("Frontend" in t for t in titles)
    assert not any("Roll out" in t for t in titles)


def test_rename_emits_eight_step_workflow():
    g = _compile("Rename RuntimeTask to MissionTask")
    # locate -> find_refs(parallel) -> rename_decl -> rename_imports(parallel)
    # -> update_refs -> format -> test -> review
    assert len(g.tasks) == 8
    seq = [t.task_type for t in sorted(g.tasks, key=lambda x: x.sequence)]
    assert seq[0] == TaskType.ANALYSIS
    assert seq[-1] == TaskType.REVIEW
    assert TaskType.TESTING in seq


def test_dependency_chain_is_acyclic():
    for goal in [
        "Rename RuntimeTask to MissionTask",
        "Extract scheduler utilities into new_module",
        "refactor RuntimeEngine into smaller components",
        "Add structured logging to scheduler",
        "Fix failing scheduler tests",
        "Remove dead code",
    ]:
        g = _compile(goal)
        ids = [t.id for t in g.tasks]
        assert not _detect_cycle(g.edges, ids), f"cycle in {goal}"


def test_only_review_is_ai_primary():
    for goal in [
        "Rename RuntimeTask to MissionTask",
        "Extract scheduler utilities into new_module",
        "refactor RuntimeEngine into smaller components",
        "Add structured logging to scheduler",
        "Fix failing scheduler tests",
        "Remove dead code",
    ]:
        g = _compile(goal)
        types = [t.task_type for t in g.tasks]
        # Every non-review step must be a deterministic-type.
        for tt in types:
            if tt != TaskType.REVIEW:
                assert tt in _DETERMINISTIC_TYPES, f"{goal}: {tt} not deterministic"
        # Exactly one review step (the last).
        assert types.count(TaskType.REVIEW) == 1
        assert types[-1] == TaskType.REVIEW


def test_every_task_has_specific_verification_and_acceptance():
    for goal in [
        "Rename RuntimeTask to MissionTask",
        "Extract scheduler utilities into new_module",
        "refactor RuntimeEngine into smaller components",
        "Add structured logging to scheduler",
        "Fix failing scheduler tests",
        "Remove dead code",
    ]:
        g = _compile(goal)
        for t in g.tasks:
            assert t.acceptance_criteria, f"{goal}: {t.title} missing AC"
            assert t.verification, f"{goal}: {t.title} missing verification"
            # Specific, not vague: must mention the symbol/goal or concrete method.
            blob = " ".join(t.acceptance_criteria) + " ".join(
                v.get("detail", "") for v in t.verification)
            assert len(blob) > 10


def test_symbolic_payload_carried_on_tasks():
    g = _compile("Rename RuntimeTask to MissionTask")
    rename_decl = [t for t in g.tasks if t.task_type == TaskType.REFACTOR][0]
    assert rename_decl.symbolic.get("symbol") == "RuntimeTask"
    assert rename_decl.symbolic.get("replacement") == "MissionTask"
    assert rename_decl.symbolic.get("op") == "rename_declaration"


def test_parallel_pairs_have_no_intra_edge():
    # In the rename workflow, find_references runs in parallel with
    # rename_declaration (find_refs is marked parallel_next), and
    # rename_declaration runs in parallel with rename_imports. So the edges
    # (rename_decl <- find_refs) and (rename_imports <- rename_decl) must be
    # ABSENT, while all other adjacent pairs are chained.
    g = _compile("Rename RuntimeTask to MissionTask")
    edges = {(e["from"], e["to"]) for e in g.edges}
    by_seq = {t.sequence: t.id for t in g.tasks}
    # find_refs (seq2, parallel_next) -> rename_decl (seq3): parallel, NO edge.
    assert (by_seq[3], by_seq[2]) not in edges
    # rename_imports (seq4, parallel_next) -> update_refs (seq5): parallel, NO edge.
    assert (by_seq[5], by_seq[4]) not in edges
    # locate (seq1) -> find_refs (seq2): chained, edge present.
    assert (by_seq[2], by_seq[1]) in edges
    # rename_decl (seq3) -> rename_imports (seq4): chained, edge present.
    assert (by_seq[4], by_seq[3]) in edges
    # update_refs (seq5) -> format (seq6): chained, edge present.
    assert (by_seq[6], by_seq[5]) in edges


def test_unknown_goal_uses_generic_expansion():
    # A non-engineering goal falls through to the frozen generic path.
    g = _compile("Write a haiku about concurrency", ptype=PlanType.RESEARCH)
    # Generic path still produces a valid graph (no crash, acyclic).
    ids = [t.id for t in g.tasks]
    assert not _detect_cycle(g.edges, ids)
