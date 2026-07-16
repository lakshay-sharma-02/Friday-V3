"""Frozen Task Graph JSON schema — the stable public interface (Milestone 9.1+).

This module is the CONTRACT between the cognitive/planning stack and every
downstream execution component (Worker Registry, Capability Resolver,
Scheduler, Runtime, Review, Repair, external integrations). It is intentionally
isolated from the compiler: it imports NOTHING from `compiler` or `engine`, so
the execution system can evolve independently while Planning and the compiler
stay frozen.

FREEZE POLICY
-------------
- `SCHEMA_VERSION` is bumped ONLY on a breaking change to this JSON shape.
- The vocabulary enumerations below are the closed, allowed value sets. The
  compiler must only ever emit values from these sets; downstream consumers
  MUST validate against them (never assume, never silently accept).
- Adding a new capability or task type is a schema change: bump the version,
  extend the enum, and add a migration note — do NOT change the field shapes
  without a version bump.
- Nothing in this module reads Planning or the compiler. It is pure
  validation/loading of the JSON object the compiler emits via
  `TaskGraph.to_json()`.

Determinism: validation is a pure function of the JSON; no I/O, no randomness,
no LLM.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Version + top-level contract
# ---------------------------------------------------------------------------

SCHEMA_VERSION: int = 1

# Top-level JSON keys required on every exported graph.
_TOP_LEVEL_REQUIRED = (
    "graph_id", "goal", "plan_id", "plan_type", "generated_at",
    "task_count", "edge_count", "critical_path_length", "critical_path",
    "parallel_groups", "parallel_tasks", "tasks", "edges", "metadata",
)

# `metadata` keys required on every exported graph.
_METADATA_REQUIRED = ("compiler", "acyclic", "schema_version")


# ---------------------------------------------------------------------------
# Closed vocabularies (mirror compiler.TaskType / capability inference exactly).
# These are the ONLY legal values downstream should accept.
# ---------------------------------------------------------------------------

# Task types — frozen, never LLM-generated (see compiler.TaskType).
TASK_TYPES = (
    "analysis", "design", "implementation", "testing", "documentation",
    "migration", "review", "refactor", "infrastructure", "research",
    "verification", "deployment", "configuration", "cleanup", "planning",
)

# Capabilities — NO worker names, only capabilities (see compiler._ALL_CAPS).
CAPABILITIES = (
    "rust", "python", "typescript", "sql", "architecture", "testing",
    "documentation", "frontend", "backend", "infrastructure", "research",
    "configuration",
)

# Priority band.
PRIORITIES = ("low", "medium", "high", "critical")

# Complexity band.
COMPLEXITIES = ("tiny", "small", "medium", "large", "very_large")

# Effort band (echoes the plan's estimated_effort; informational for workers).
EFFORTS = ("low", "medium", "high")

# Task lifecycle status (compiler emits "pending"; downstream mutates it).
TASK_STATUSES = (
    "pending", "in_progress", "blocked", "completed", "failed",
    "skipped", "cancelled",
)

# Graph-level status.
GRAPH_STATUSES = ("compiled", "scheduled", "running", "completed", "failed")

# Confidence band (echoes the plan's confidence).
CONFIDENCES = ("weak", "medium", "strong")

# Dependency edge kind.
EDGE_KINDS = ("depends_on",)

# Per-task JSON keys required on every task.
_TASK_REQUIRED = (
    "id", "graph_id", "plan_id", "milestone_order", "title", "description",
    "task_type", "required_capabilities", "complexity", "priority",
    "estimated_effort", "dependencies", "inputs", "outputs",
    "acceptance_criteria", "verification", "rollback", "evidence", "status",
    "confidence", "sequence",
)

# Verification / rollback entry keys (each is a {method/strategy, detail} dict).
_VERIFICATION_KEYS = ("method", "detail")
_ROLLBACK_KEYS = ("strategy", "detail")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class SchemaError(ValueError):
    """Raised when a Task Graph JSON violates the frozen contract."""


# ---------------------------------------------------------------------------
# Cycle detection (independent of the compiler's — keeps this module self-contained)
# ---------------------------------------------------------------------------

def _detect_cycle(edges: List[dict], task_ids: List[str]) -> bool:
    """True if the dependency graph (from depends on to) contains a cycle.

    Mirrors compiler._detect_cycle semantics exactly: an edge `from -> to`
    means `from` depends on `to`, followed in dependency direction.
    """
    adj: Dict[str, List[str]] = {t: [] for t in task_ids}
    for e in edges:
        adj.setdefault(e["from"], []).append(e["to"])
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {t: WHITE for t in task_ids}

    def visit(u: str) -> bool:
        color[u] = GRAY
        for v in adj.get(u, []):
            if color.get(v, WHITE) == GRAY:
                return True
            if color.get(v, WHITE) == WHITE and visit(v):
                return True
        color[u] = BLACK
        return False

    for t in task_ids:
        if color[t] == WHITE and visit(t):
            return True
    return False


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise SchemaError(msg)


# ---------------------------------------------------------------------------
# Validation — pure function of the JSON object
# ---------------------------------------------------------------------------

def validate_task_graph(obj: Any) -> None:
    """Validate a Task Graph JSON object against the frozen contract.

    Raises SchemaError on any violation. Does NOT mutate `obj`. Safe to call on
    untrusted input from any downstream consumer (worker engine, scheduler).
    """
    _check(isinstance(obj, dict), "task graph must be a JSON object")

    # --- top level ---------------------------------------------------------
    for key in _TOP_LEVEL_REQUIRED:
        _check(key in obj, f"missing top-level key: {key}")

    meta = obj["metadata"]
    _check(isinstance(meta, dict), "metadata must be an object")
    for k in _METADATA_REQUIRED:
        _check(k in meta, f"missing metadata key: {k}")
    _check(meta["schema_version"] == SCHEMA_VERSION,
           f"metadata.schema_version must be {SCHEMA_VERSION}")
    _check(meta.get("acyclic") is True, "metadata.acyclic must be true")

    tasks = obj["tasks"]
    edges = obj["edges"]
    _check(isinstance(tasks, list), "tasks must be a list")
    _check(isinstance(edges, list), "edges must be a list")

    # --- task count consistency --------------------------------------------
    _check(obj["task_count"] == len(tasks),
           f"task_count {obj['task_count']} != actual {len(tasks)}")
    _check(obj["edge_count"] == len(edges),
           f"edge_count {obj['edge_count']} != actual {len(edges)}")

    # --- tasks -------------------------------------------------------------
    ids: List[str] = []
    seen_ids: set = set()
    for idx, t in enumerate(tasks):
        _check(isinstance(t, dict), f"task #{idx} must be an object")
        for key in _TASK_REQUIRED:
            _check(key in t, f"task #{idx} missing key: {key}")
        tid = t["id"]
        _check(tid not in seen_ids, f"duplicate task id: {tid}")
        seen_ids.add(tid)
        ids.append(tid)
        _check(t["graph_id"] == obj["graph_id"],
               f"task {tid} graph_id {t['graph_id']} != graph {obj['graph_id']}")
        _check(t["plan_id"] == obj["plan_id"],
               f"task {tid} plan_id {t['plan_id']} != graph plan_id "
               f"{obj['plan_id']}")
        _check(t["task_type"] in TASK_TYPES,
               f"task {tid} unknown task_type: {t['task_type']}")
        _check(all(c in CAPABILITIES for c in t["required_capabilities"]),
               f"task {tid} has non-capability token in required_capabilities")
        _check(t["priority"] in PRIORITIES,
               f"task {tid} unknown priority: {t['priority']}")
        _check(t["complexity"] in COMPLEXITIES,
               f"task {tid} unknown complexity: {t['complexity']}")
        _check(t["estimated_effort"] in EFFORTS,
               f"task {tid} unknown estimated_effort: {t['estimated_effort']}")
        _check(t["status"] in TASK_STATUSES,
               f"task {tid} unknown status: {t['status']}")
        _check(t["confidence"] in CONFIDENCES,
               f"task {tid} unknown confidence: {t['confidence']}")
        for dep in t["dependencies"]:
            _check(dep in seen_ids,
                   f"task {tid} depends on unknown task: {dep}")
        _check(isinstance(t["acceptance_criteria"], list)
               and len(t["acceptance_criteria"]) > 0,
               f"task {tid} acceptance_criteria must be non-empty")
        for v in t["verification"]:
            _check(isinstance(v, dict)
                   and all(k in v for k in _VERIFICATION_KEYS),
                   f"task {tid} verification entry malformed: {v}")
        for r in t["rollback"]:
            _check(isinstance(r, dict)
                   and all(k in r for k in _ROLLBACK_KEYS),
                   f"task {tid} rollback entry malformed: {r}")

    # --- edges -------------------------------------------------------------
    edge_seen: set = set()
    for idx, e in enumerate(edges):
        _check(isinstance(e, dict), f"edge #{idx} must be an object")
        for key in ("from", "to", "kind"):
            _check(key in e, f"edge #{idx} missing key: {key}")
        _check(e["from"] in seen_ids, f"edge #{idx} from unknown task: {e['from']}")
        _check(e["to"] in seen_ids, f"edge #{idx} to unknown task: {e['to']}")
        _check(e["kind"] in EDGE_KINDS, f"edge #{idx} unknown kind: {e['kind']}")
        ekey = (e["from"], e["to"])
        _check(ekey not in edge_seen, f"duplicate edge: {ekey}")
        edge_seen.add(ekey)

    # --- graph must be acyclic (the contract guarantees a DAG) -------------
    _check(not _detect_cycle(edges, ids), "task graph must be acyclic")

    # --- critical path / parallel consistency ------------------------------
    cp = obj["critical_path"]
    _check(isinstance(cp, list), "critical_path must be a list")
    _check(obj["critical_path_length"] == len(cp),
           f"critical_path_length {obj['critical_path_length']} != "
           f"actual {len(cp)}")
    for c in cp:
        _check(c in seen_ids, f"critical_path references unknown task: {c}")
    pt = obj["parallel_tasks"]
    _check(isinstance(pt, list), "parallel_tasks must be a list")
    for p in pt:
        _check(p in seen_ids, f"parallel_tasks references unknown task: {p}")


# ---------------------------------------------------------------------------
# Loading — a typed view for downstream consumers (no compiler dependency)
# ---------------------------------------------------------------------------

@dataclass
class SchemaTask:
    id: str
    graph_id: str
    plan_id: str
    milestone_order: int
    title: str
    description: str
    task_type: str
    required_capabilities: List[str]
    complexity: str
    priority: str
    estimated_effort: str
    dependencies: List[str]
    inputs: List[str]
    outputs: List[str]
    acceptance_criteria: List[str]
    verification: List[dict]
    rollback: List[dict]
    evidence: List[str]
    status: str
    confidence: str
    sequence: int


@dataclass
class SchemaTaskGraph:
    """A validated, typed view of a Task Graph export (consumer-side)."""
    graph_id: str
    goal: str
    plan_id: str
    plan_type: str
    generated_at: str
    task_count: int
    edge_count: int
    critical_path_length: int
    critical_path: List[str]
    parallel_groups: int
    parallel_tasks: List[str]
    tasks: List[SchemaTask] = field(default_factory=list)
    edges: List[dict] = field(default_factory=list)


def load_task_graph(obj: Any) -> SchemaTaskGraph:
    """Validate then return a typed, consumer-safe view. Raises SchemaError."""
    validate_task_graph(obj)
    tasks = [
        SchemaTask(
            id=t["id"], graph_id=t["graph_id"], plan_id=t["plan_id"],
            milestone_order=t["milestone_order"], title=t["title"],
            description=t["description"], task_type=t["task_type"],
            required_capabilities=list(t["required_capabilities"]),
            complexity=t["complexity"], priority=t["priority"],
            estimated_effort=t["estimated_effort"],
            dependencies=list(t["dependencies"]), inputs=list(t["inputs"]),
            outputs=list(t["outputs"]),
            acceptance_criteria=list(t["acceptance_criteria"]),
            verification=list(t["verification"]), rollback=list(t["rollback"]),
            evidence=list(t["evidence"]), status=t["status"],
            confidence=t["confidence"], sequence=t["sequence"],
        )
        for t in obj["tasks"]
    ]
    return SchemaTaskGraph(
        graph_id=obj["graph_id"], goal=obj["goal"], plan_id=obj["plan_id"],
        plan_type=obj["plan_type"], generated_at=obj["generated_at"],
        task_count=obj["task_count"], edge_count=obj["edge_count"],
        critical_path_length=obj["critical_path_length"],
        critical_path=list(obj["critical_path"]),
        parallel_groups=obj["parallel_groups"],
        parallel_tasks=list(obj["parallel_tasks"]), tasks=tasks,
        edges=list(obj["edges"]),
    )
