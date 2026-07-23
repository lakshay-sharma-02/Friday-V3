"""Engineering-pattern planner (Phase 3).

Deterministic decomposition of software-engineering missions into typed
*symbolic* tasks. Pure: takes only (goal, plan), never reads a repository,
never calls a worker, never uses an LLM. This is the "WHAT" layer.

Each emitted task is SYMBOLIC — it names the engineering operation and the
symbol/module it targets, never a concrete file path. The Resolver later
enriches symbolic tasks with repository-specific information (which files
contain the symbol, whether imports need updating) before selecting an
executor. Planner = intent, Resolver = repo knowledge, Executor = work.

The compiler consults :func:`classify` BEFORE its generic milestone expansion.
A recognized pattern OVERRIDES the whole graph with the explicit engineering
workflow from the Phase 3 spec (locate -> refs -> edit -> format -> test ->
review). Unrecognized goals fall through to the frozen generic behaviour.

Task types are chosen so the Resolver routes each step to a DETERMINISTIC
executor (filesystem/git/python/testing/shell); only `review` is AI-primary.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

from .models import Plan

# Engineering operation kinds the Resolver understands. These are the symbolic
# op tokens — the contract between planner and resolver.
OP_LOCATE = "locate_symbol"
OP_FIND_REFS = "find_references"
OP_RENAME_DECL = "rename_declaration"
OP_RENAME_IMPORTS = "rename_imports"
OP_UPDATE_IMPORTS = "update_imports"
OP_UPDATE_REFS = "update_references"
OP_IDENTIFY_BOUNDARY = "identify_boundary"
OP_CREATE_MODULE = "create_module"
OP_MOVE_CODE = "move_code"
OP_REMOVE_DUPES = "remove_duplicates"
OP_LOCATE_TARGET = "locate_target"
OP_IDENTIFY_POINTS = "identify_insertion_points"
OP_MODIFY = "modify_implementation"
OP_REPRODUCE = "reproduce_failure"
OP_IDENTIFY_COMPONENT = "identify_component"
OP_IDENTIFY_UNUSED = "identify_unused"
OP_VERIFY_REFS = "verify_references"
OP_REMOVE_SAFE = "remove_safely"
OP_FORMAT = "run_formatter"
OP_TEST = "run_tests"
OP_REGRESSION = "run_regression_tests"
OP_VERIFY = "verify_fix"
OP_REVIEW = "review_changes"


@dataclass
class SymbolicTask:
    """One typed engineering operation. SYMBOLIC: no file paths.

    `task_type` drives Resolver -> executor selection. `symbolic` carries the
    op + target so the Resolver can resolve it against the repo later.
    """

    op: str
    task_type: str
    title: str
    symbolic: dict = field(default_factory=dict)
    # task_type -> (verification method, detail) for explicit success contracts.
    verification: List[dict] = field(default_factory=list)
    acceptance_criteria: List[str] = field(default_factory=list)
    parallel_next: bool = False  # run in parallel with the next task if True


@dataclass
class PatternPlan:
    """A classified engineering pattern: ordered symbolic tasks + metadata."""

    name: str
    intent: str
    tasks: List[SymbolicTask]


# ---------------------------------------------------------------------------
# Deterministic goal classification (no LLM, regex/keyword only).
# ---------------------------------------------------------------------------

# Capability hints and verify methods (consolidated in vocabulary.py).
from ..vocabulary import PATTERN_CAP_HINTS, PATTERN_VERIFY_METHODS

# Pattern regexes (consolidated in vocabulary.py).
from ..vocabulary import (
    PATTERN_RENAME as _RENAME,
    PATTERN_EXTRACT as _EXTRACT,
    PATTERN_REFACTOR as _REFActor,
    PATTERN_FEATURE as _FEATURE,
    PATTERN_BUGFIX as _BUGFIX,
    PATTERN_MAINTENANCE as _MAINTENANCE,
    PATTERN_MAINTENANCE_TARGET as _MAINTENANCE_TARGET,
)


def _cap(type_: str) -> List[str]:
    """Capability hints that help bias toward a deterministic executor.

    Uses ONLY the frozen capability vocabulary shared with graph_schema
    validation (python/testing/configuration/...). The Resolver's repo
    enrichment adds worker-side hints (file editing/shell commands/...) at
    resolution time. Kept in sync with compiler._cap_for_symbolic.
    """
    return PATTERN_CAP_HINTS.get(type_, [])


def _sym(op: str, task_type: str, title: str, sym: dict,
         verify: str, accept: str, parallel_next: bool = False) -> SymbolicTask:
    return SymbolicTask(
        op=op, task_type=task_type, title=title, symbolic=sym,
        verification=[{"method": _verify_method(task_type), "detail": verify}],
        acceptance_criteria=[accept], parallel_next=parallel_next,
    )


def _verify_method(task_type: str) -> str:
    return PATTERN_VERIFY_METHODS.get(task_type, "check")


# ---------------------------------------------------------------------------
# Pattern builders. Each returns an ordered list of SymbolicTask.
# ---------------------------------------------------------------------------

def _rename_tasks(symbol: str, replacement: str) -> List[SymbolicTask]:
    sym = {"op": OP_RENAME_DECL, "symbol": symbol, "replacement": replacement}
    return [
        _sym(OP_LOCATE, "analysis", f"Locate symbol '{symbol}'",
             {"op": OP_LOCATE, "symbol": symbol},
             "Symbol definition found in the repository.",
             f"'{symbol}' definition located."),
        _sym(OP_FIND_REFS, "analysis", f"Find all references to '{symbol}'",
             {"op": OP_FIND_REFS, "symbol": symbol},
             "Complete reference list collected (grep across repo).",
             f"All usages of '{symbol}' enumerated.", parallel_next=True),
        _sym(OP_RENAME_DECL, "refactor", f"Rename declaration of '{symbol}' to '{replacement}'",
             {**sym},
             "Declaration renamed; old name no longer defined.",
             f"'{symbol}' declaration renamed to '{replacement}'."),
        _sym(OP_RENAME_IMPORTS, "refactor", f"Rename imports of '{symbol}'",
             {"op": OP_RENAME_IMPORTS, "symbol": symbol, "replacement": replacement},
             "Import statements updated; no stale imports remain.",
             f"All imports of '{symbol}' point to '{replacement}'.", parallel_next=True),
        _sym(OP_UPDATE_REFS, "refactor", f"Update remaining references to '{symbol}'",
             {"op": OP_UPDATE_REFS, "symbol": symbol, "replacement": replacement},
             "No remaining references to the old symbol name.",
             f"Every usage of '{symbol}' replaced with '{replacement}'."),
        _sym(OP_FORMAT, "configuration", "Run formatter on changed files",
             {"op": OP_FORMAT},
             "Formatter exits successfully (0).",
             "Changed files conform to formatting rules."),
        _sym(OP_TEST, "testing", "Run tests after rename",
             {"op": OP_TEST},
             "All required tests pass.",
             "Test suite green after the rename."),
        _sym(OP_REVIEW, "review", "Review rename changes",
             {"op": OP_REVIEW},
             "No critical issues reported.",
             "Diff reviewed; rename is behaviour-preserving."),
    ]


def _extract_tasks(goal: str, module: Optional[str]) -> List[SymbolicTask]:
    mod = module or "extracted_module"
    return [
        _sym(OP_IDENTIFY_BOUNDARY, "analysis", f"Identify extraction boundary in: {goal}",
             {"op": OP_IDENTIFY_BOUNDARY, "goal": goal},
             "Boundary (functions/classes to move) clearly identified.",
             "Extraction scope defined."),
        _sym(OP_CREATE_MODULE, "implementation", f"Create new module '{mod}'",
             {"op": OP_CREATE_MODULE, "module": mod},
             "New module file created and importable.",
             f"'{mod}' exists and is empty/structured."),
        _sym(OP_MOVE_CODE, "refactor", f"Move functions into '{mod}'",
             {"op": OP_MOVE_CODE, "module": mod},
             "Moved code compiles; behaviour preserved.",
             "Target functions relocated to new module."),
        _sym(OP_UPDATE_IMPORTS, "refactor", "Update imports to new module",
             {"op": "update_imports", "module": mod},
             "All references resolve via the new module.",
             "Callers import from the new module.", parallel_next=True),
        _sym(OP_REMOVE_DUPES, "cleanup", "Remove duplicated code left behind",
             {"op": OP_REMOVE_DUPES},
             "No duplicate definitions remain.",
             "Source of truth is the new module only."),
        _sym(OP_FORMAT, "configuration", "Run formatter on changed files",
             {"op": OP_FORMAT},
             "Formatter exits successfully (0).",
             "Changed files conform to formatting rules."),
        _sym(OP_TEST, "testing", "Run tests after extraction",
             {"op": OP_TEST},
             "All required tests pass.",
             "Test suite green after extraction."),
        _sym(OP_REVIEW, "review", "Review extraction changes",
             {"op": OP_REVIEW},
             "No critical issues reported.",
             "Extraction reviewed; behaviour preserved."),
    ]


def _refactor_tasks(goal: str) -> List[SymbolicTask]:
    return [
        _sym(OP_IDENTIFY_BOUNDARY, "analysis", f"Identify responsibilities to split: {goal}",
             {"op": OP_IDENTIFY_BOUNDARY, "goal": goal},
             "Responsibilities enumerated and grouped.",
             "Refactor scope defined."),
        _sym(OP_CREATE_MODULE, "implementation", "Create new component modules",
             {"op": OP_CREATE_MODULE, "goal": goal},
             "New modules created and importable.",
             "Target modules exist."),
        _sym(OP_MOVE_CODE, "refactor", "Move code into new components",
             {"op": OP_MOVE_CODE, "goal": goal},
             "Moved code compiles; behaviour preserved.",
             "Code relocated to new components."),
        _sym(OP_UPDATE_IMPORTS, "refactor", "Update imports across callers",
             {"op": "update_imports", "goal": goal},
             "All references resolve via new components.",
             "Callers import from new components.", parallel_next=True),
        _sym(OP_TEST, "testing", "Run characterization tests",
             {"op": OP_TEST},
             "Characterization tests pass (behaviour preserved).",
             "Behaviour unchanged after refactor."),
        _sym(OP_FORMAT, "configuration", "Run formatter on changed files",
             {"op": OP_FORMAT},
             "Formatter exits successfully (0).",
             "Changed files conform to formatting rules."),
        _sym(OP_REVIEW, "review", "Review refactor changes",
             {"op": OP_REVIEW},
             "No critical issues reported.",
             "Refactor reviewed; behaviour preserved."),
    ]


def _feature_tasks(goal: str, target: Optional[str]) -> List[SymbolicTask]:
    tgt = target or "target component"
    return [
        _sym(OP_LOCATE_TARGET, "analysis", f"Locate '{tgt}' in codebase",
             {"op": OP_LOCATE_TARGET, "target": tgt, "goal": goal},
             "Target located; entry points known.",
             f"'{tgt}' located."),
        _sym(OP_IDENTIFY_POINTS, "analysis", f"Identify insertion points in '{tgt}'",
             {"op": OP_IDENTIFY_POINTS, "target": tgt},
             "Insertion points enumerated.",
             "Where the change goes is decided.", parallel_next=True),
        _sym(OP_MODIFY, "implementation", f"Modify implementation of '{tgt}'",
             {"op": OP_MODIFY, "target": tgt, "goal": goal},
             "Change applied; code compiles.",
             f"'{tgt}' modified per goal."),
        _sym(OP_FORMAT, "configuration", "Run formatter on changed files",
             {"op": OP_FORMAT},
             "Formatter exits successfully (0).",
             "Changed files conform to formatting rules."),
        _sym(OP_TEST, "testing", "Run tests for the feature",
             {"op": OP_TEST},
             "All required tests pass.",
             "Test suite green after the change."),
        _sym(OP_REVIEW, "review", "Review feature changes",
             {"op": OP_REVIEW},
             "No critical issues reported.",
             "Change reviewed."),
    ]


def _bugfix_tasks(goal: str) -> List[SymbolicTask]:
    return [
        _sym(OP_REPRODUCE, "analysis", f"Reproduce failure: {goal}",
             {"op": OP_REPRODUCE, "goal": goal},
             "Failure reproduced by a test or command.",
             "Reproduction established."),
        _sym(OP_IDENTIFY_COMPONENT, "analysis", "Identify failing component",
             {"op": OP_IDENTIFY_COMPONENT, "goal": goal},
             "Root-cause component isolated.",
             "Failing component identified.", parallel_next=True),
        _sym(OP_MODIFY, "implementation", "Modify implementation to fix defect",
             {"op": OP_MODIFY, "goal": goal},
             "Fix applied; code compiles.",
             "Defect corrected."),
        _sym(OP_REGRESSION, "testing", "Run regression tests",
             {"op": OP_REGRESSION},
             "Regression tests pass.",
             "No regressions; original failure resolved."),
        _sym(OP_VERIFY, "verification", "Verify fix against acceptance",
             {"op": OP_VERIFY},
             "Acceptance criteria met.",
             "Fix verified end to end."),
        _sym(OP_REVIEW, "review", "Review bugfix changes",
             {"op": OP_REVIEW},
             "No critical issues reported.",
             "Fix reviewed."),
    ]


def _maintenance_tasks(goal: str, symbol: Optional[str] = None) -> List[SymbolicTask]:
    sym = {"op": OP_REMOVE_SAFE}
    if symbol:
        sym["symbol"] = symbol
    return [
        _sym(OP_IDENTIFY_UNUSED, "analysis", f"Identify unused symbols: {goal}",
             {"op": OP_IDENTIFY_UNUSED, "goal": goal,
              **({"symbol": symbol} if symbol else {})},
             "Unused symbols enumerated.",
             "Dead-code candidates listed."),
        _sym(OP_VERIFY_REFS, "analysis", "Verify no live references remain",
             {"op": OP_VERIFY_REFS},
             "Confirmed no live references (grep).",
             "Removal is safe.", parallel_next=True),
        _sym(OP_REMOVE_SAFE, "cleanup", "Remove dead code safely",
             sym,
             "Symbols removed; code compiles.",
             "Dead code deleted."),
        _sym(OP_FORMAT, "configuration", "Run formatter on changed files",
             {"op": OP_FORMAT},
             "Formatter exits successfully (0).",
             "Changed files conform to formatting rules."),
        _sym(OP_TEST, "testing", "Run tests after cleanup",
             {"op": OP_TEST},
             "All required tests pass.",
             "Test suite green after cleanup."),
        _sym(OP_REVIEW, "review", "Review maintenance changes",
             {"op": OP_REVIEW},
             "No critical issues reported.",
             "Cleanup reviewed; no regressions."),
    ]


# ---------------------------------------------------------------------------
# Public classifier.
# ---------------------------------------------------------------------------

def classify(goal: str, plan: Optional[Plan] = None) -> Optional[PatternPlan]:
    """Return a PatternPlan for a recognized engineering goal, else None.

    Pure + deterministic. Order matters: rename/extract/refactor are more
    specific than generic add/fix/remove, so they are tested first.
    """
    g = (goal or "").strip()
    if not g:
        return None

    m = _RENAME.search(g)
    if m:
        symbol, replacement = m.group(1), m.group(2)
        return PatternPlan(
            name="rename", intent=f"rename {symbol} to {replacement}",
            tasks=_rename_tasks(symbol, replacement))

    # Maintenance before the generic feature/fix fall-through so a goal like
    # "Remove DEAD_FN" is captured with its concrete symbol.
    m = _MAINTENANCE_TARGET.search(g)
    if m and _MAINTENANCE.search(g):
        symbol = m.group(1)
        return PatternPlan(
            name="maintenance", intent=f"remove {symbol}",
            tasks=_maintenance_tasks(goal, symbol))

    if _EXTRACT.search(g):
        # Try to pull a module name: "extract X into Y" -> Y is the module.
        mod = None
        mm = re.search(r"into\s+(?:a\s+)?(?:new\s+)?(\w+)", g, re.IGNORECASE)
        if mm:
            mod = mm.group(1)
        return PatternPlan(
            name="extract", intent=f"extract: {g}",
            tasks=_extract_tasks(g, mod))

    if _REFActor.search(g):
        return PatternPlan(
            name="refactor", intent=f"refactor: {g}",
            tasks=_refactor_tasks(g))

    if _BUGFIX.search(g):
        return PatternPlan(
            name="bugfix", intent=f"fix: {g}",
            tasks=_bugfix_tasks(g))

    if _MAINTENANCE.search(g):
        return PatternPlan(
            name="maintenance", intent=f"maintenance: {g}",
            tasks=_maintenance_tasks(g))

    if _FEATURE.search(g):
        m = _FEATURE.search(g)
        target = m.group(1) if m else None
        return PatternPlan(
            name="feature", intent=f"add: {g}",
            tasks=_feature_tasks(g, target))

    return None
