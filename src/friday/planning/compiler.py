"""Task Graph Compiler (Milestone 9.1).

Write-only layer on TOP of the Planning Engine. Compiles a STRUCTURED Plan into
a deterministic, acyclic task DAG — Friday's execution IR (the thing Workers
will later consume, never the Plan itself).

NEVER executes, edits files, calls workers, or uses an LLM. NEVER reads
observations/context/git/repositories directly — its sole input is a Plan
object. The Planning Engine is FROZEN and untouched; this layer only reads the
structured Plan it produces.

Determinism guarantees (no LLM, no embeddings, no vectors, no randomness):
- Task types are a FIXED frozen enum. Never LLM-generated.
- Capability inference is keyword/type-based (a closed capability vocabulary).
- Priority and complexity are PURE functions of graph structure + plan fields.
- Edges are derived from milestone order; the compiler REJECTS cycles.
- Each graph is idempotent on goal: recompiling REPLACES the same row and
  records the prior version in task_history (append-only). Plans are NEVER
  overloaded.

Database: dedicated tables task_graphs / tasks / task_edges / task_history /
task_evolution. See db.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from .models import Plan, now_iso
from .patterns import classify


# ---------------------------------------------------------------------------
# Fixed, frozen task-type vocabulary. NEVER LLM-generated.
# ---------------------------------------------------------------------------

class TaskType(str):
    """Executable task category. A closed, deterministic set — Workers match on
    these, never on prose. Mirrors the spec's minimum deterministic task types."""

    ANALYSIS = "analysis"
    DESIGN = "design"
    IMPLEMENTATION = "implementation"
    TESTING = "testing"
    DOCUMENTATION = "documentation"
    MIGRATION = "migration"
    REVIEW = "review"
    REFACTOR = "refactor"
    INFRASTRUCTURE = "infrastructure"
    RESEARCH = "research"
    VERIFICATION = "verification"
    DEPLOYMENT = "deployment"
    CONFIGURATION = "configuration"
    CLEANUP = "cleanup"
    PLANNING = "planning"

    _ALL = (
        ANALYSIS, DESIGN, IMPLEMENTATION, TESTING, DOCUMENTATION, MIGRATION,
        REVIEW, REFACTOR, INFRASTRUCTURE, RESEARCH, VERIFICATION, DEPLOYMENT,
        CONFIGURATION, CLEANUP, PLANNING,
    )

    @classmethod
    def from_str(cls, s: str) -> str:
        s = (s or "").strip().lower()
        for v in cls._ALL:
            if v == s:
                return v
        raise ValueError(f"{cls.__name__} has no member {s!r}")

    @classmethod
    def all(cls) -> Tuple[str, ...]:
        return cls._ALL


# Fixed capability vocabulary (NO worker names — only capabilities).
_CAP_RUST = "rust"
_CAP_PYTHON = "python"
_CAP_TS = "typescript"
_CAP_SQL = "sql"
_CAP_ARCH = "architecture"
_CAP_TEST = "testing"
_CAP_DOC = "documentation"
_CAP_FE = "frontend"
_CAP_BE = "backend"
_CAP_INFRA = "infrastructure"
_CAP_RESEARCH = "research"
_CAP_CONFIG = "configuration"

# Closed capability set, used to validate inference (no hallucinated caps).
_ALL_CAPS = {
    _CAP_RUST, _CAP_PYTHON, _CAP_TS, _CAP_SQL, _CAP_ARCH, _CAP_TEST, _CAP_DOC,
    _CAP_FE, _CAP_BE, _CAP_INFRA, _CAP_RESEARCH, _CAP_CONFIG,
}

# Language keywords scanned from the goal (deterministic, lowercased).
_LANG_KW = {
    _CAP_RUST: ("rust", "crate", "cargo"),
    _CAP_PYTHON: ("python", "django", "flask", "py"),
    _CAP_TS: ("typescript", "javascript", "frontend", "web", "react", "node"),
    _CAP_SQL: ("sql", "database", "schema", "postgres", "mysql"),
}

# Base capability per task type (always present for that type).
_BASE_CAPS: Dict[str, Tuple[str, ...]] = {
    TaskType.ANALYSIS: (_CAP_RESEARCH,),
    TaskType.RESEARCH: (_CAP_RESEARCH,),
    TaskType.DESIGN: (_CAP_ARCH,),
    TaskType.IMPLEMENTATION: (),  # resolved by title (backend/frontend) + lang
    TaskType.TESTING: (_CAP_TEST,),
    TaskType.VERIFICATION: (_CAP_TEST,),
    TaskType.DOCUMENTATION: (_CAP_DOC,),
    TaskType.MIGRATION: (_CAP_INFRA,),
    TaskType.REFACTOR: (_CAP_BE,),
    TaskType.INFRASTRUCTURE: (_CAP_INFRA,),
    TaskType.REVIEW: (_CAP_ARCH,),
    TaskType.DEPLOYMENT: (_CAP_INFRA, _CAP_CONFIG),
    TaskType.CONFIGURATION: (_CAP_CONFIG,),
    TaskType.CLEANUP: (_CAP_BE,),
    TaskType.PLANNING: (_CAP_RESEARCH,),
}


# ---------------------------------------------------------------------------
# Complexity / priority scales (closed, ordered).
# ---------------------------------------------------------------------------

_COMPLEXITY_ORDER = ("tiny", "small", "medium", "large", "very_large")
_PRIORITY_ORDER = ("low", "medium", "high", "critical")

# Base complexity per task type.
_BASE_COMPLEXITY: Dict[str, str] = {
    TaskType.ANALYSIS: "small",
    TaskType.RESEARCH: "small",
    TaskType.DOCUMENTATION: "small",
    TaskType.TESTING: "small",
    TaskType.VERIFICATION: "small",
    TaskType.PLANNING: "small",
    TaskType.CONFIGURATION: "small",
    TaskType.CLEANUP: "small",
    TaskType.DESIGN: "medium",
    TaskType.IMPLEMENTATION: "medium",
    TaskType.REFACTOR: "medium",
    TaskType.REVIEW: "medium",
    TaskType.MIGRATION: "large",
    TaskType.INFRASTRUCTURE: "large",
    TaskType.DEPLOYMENT: "large",
}


class CycleError(Exception):
    """Raised when a compiled graph would contain a cycle (rejected)."""


# ---------------------------------------------------------------------------
# Structured output: Task + TaskGraph.
# ---------------------------------------------------------------------------

@dataclass
class Task:
    """One executable node in a compiled task graph.

    No prose interpretation required by Workers: every field is structured.
    `outputs` is the only field that may legitimately be empty.
    """

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
    dependencies: List[str]          # predecessor task ids (in-edges)
    inputs: List[str]
    outputs: List[str]
    acceptance_criteria: List[str]
    verification: List[dict]
    rollback: List[dict]
    evidence: List[str]
    symbolic: dict = field(default_factory=dict)  # Phase 3: symbolic op intent
    status: str = "pending"
    confidence: str = "medium"
    sequence: int = 0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "graph_id": self.graph_id,
            "plan_id": self.plan_id,
            "milestone_order": self.milestone_order,
            "title": self.title,
            "description": self.description,
            "task_type": self.task_type,
            "required_capabilities": list(self.required_capabilities),
            "complexity": self.complexity,
            "priority": self.priority,
            "estimated_effort": self.estimated_effort,
            "dependencies": list(self.dependencies),
            "inputs": list(self.inputs),
            "outputs": list(self.outputs),
            "acceptance_criteria": list(self.acceptance_criteria),
            "verification": list(self.verification),
            "rollback": list(self.rollback),
            "evidence": list(self.evidence),
            "symbolic": dict(self.symbolic),
            "status": self.status,
            "confidence": self.confidence,
            "sequence": self.sequence,
        }


@dataclass
class TaskGraph:
    """A deterministic, acyclic task DAG compiled from one Plan."""

    id: str
    goal: str
    plan_id: str
    plan_type: str
    tasks: List[Task] = field(default_factory=list)
    edges: List[dict] = field(default_factory=list)  # {from, to, kind}
    status: str = "compiled"
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    # --- derived metrics (computed by the compiler) -------------------------
    critical_path: List[str] = field(default_factory=list)
    levels: Dict[str, int] = field(default_factory=dict)  # task_id -> topo level
    parallel_groups: int = 0
    parallel_tasks: List[str] = field(default_factory=list)

    # --- serialization ------------------------------------------------------

    def to_json(self) -> dict:
        """Worker-Engine export. Self-contained, deterministic JSON object."""
        return {
            "graph_id": self.id,
            "goal": self.goal,
            "plan_id": self.plan_id,
            "plan_type": self.plan_type,
            "generated_at": self.updated_at,
            "task_count": len(self.tasks),
            "edge_count": len(self.edges),
            "critical_path_length": len(self.critical_path),
            "critical_path": list(self.critical_path),
            "parallel_groups": self.parallel_groups,
            "parallel_tasks": list(self.parallel_tasks),
            "tasks": [t.to_dict() for t in self.tasks],
            "edges": [
                {"from": e["from"], "to": e["to"], "kind": e.get("kind", "depends_on")}
                for e in self.edges
            ],
            "metadata": {
                "compiler": "M9.1-task-graph-compiler",
                "acyclic": True,
                "schema_version": 1,
            },
        }

    def summary(self) -> str:
        lines = [
            f"Task Graph: {self.id}",
            f"Goal:       {self.goal}",
            f"From plan:  {self.plan_id} ({self.plan_type})",
            f"Status:     {self.status}",
            "",
            f"Tasks:            {len(self.tasks)}",
            f"Edges:            {len(self.edges)}",
            f"Critical path:    {len(self.critical_path)} tasks",
            f"Parallel groups:  {self.parallel_groups}",
            f"Parallel tasks:   {len(self.parallel_tasks)}",
            "",
            "Tasks (in execution order):",
        ]
        for t in sorted(self.tasks, key=lambda x: x.sequence):
            lines.append(
                f"  {t.sequence:>2}. [{t.priority:8}] {t.task_type:13} "
                f"{t.title}  (caps: {', '.join(t.required_capabilities) or '-'})"
            )
        if self.critical_path:
            cp = " -> ".join(self._title(p) for p in self.critical_path)
            lines.append("")
            lines.append(f"Critical path: {cp}")
        return "\n".join(lines) + "\n"

    def _title(self, tid: str) -> str:
        for t in self.tasks:
            if t.id == tid:
                return t.title
        return tid


# ---------------------------------------------------------------------------
# Pure helpers (deterministic, individually testable).
# ---------------------------------------------------------------------------

def _graph_id(plan: Plan) -> str:
    pid = plan.id or plan._generate_id()
    return f"taskgraph:{pid}"


def _infer_language_caps(goal: str) -> List[str]:
    g = (goal or "").lower()
    out: List[str] = []
    for cap, kws in _LANG_KW.items():
        if any(kw in g for kw in kws):
            out.append(cap)
    return out


def _infer_capabilities(milestone_title: str, task_type: str, plan: Plan) -> List[str]:
    """Deterministic capability set (subset of the fixed vocabulary)."""
    caps: List[str] = list(_BASE_CAPS.get(task_type, ()))
    title = (milestone_title or "").lower()
    if task_type == TaskType.IMPLEMENTATION:
        if "frontend" in title:
            caps.append(_CAP_FE)
        elif "backend" in title:
            caps.append(_CAP_BE)
        else:
            # Generic implementation: surface both sides when the plan is a
            # feature/integration, else backend.
            if plan.plan_type.value in ("feature", "integration"):
                caps.append(_CAP_FE)
                caps.append(_CAP_BE)
            else:
                caps.append(_CAP_BE)
    # Plan-type-driven architecture signal.
    if plan.plan_type.value in ("infrastructure", "architecture"):
        if _CAP_ARCH not in caps:
            caps.append(_CAP_ARCH)
    # Language capabilities from the goal.
    caps.extend(_infer_language_caps(plan.goal))
    # De-dup, preserve first-seen order, constrain to the vocabulary.
    seen = set()
    res: List[str] = []
    for c in caps:
        if c in _ALL_CAPS and c not in seen:
            seen.add(c)
            res.append(c)
    return res


def _complexity(task_type: str, dep_count: int, plan_complexity: str) -> str:
    """Tiny<Small<Medium<Large<VeryLarge from type + deps + plan complexity."""
    idx = _COMPLEXITY_ORDER.index(_BASE_COMPLEXITY.get(task_type, "medium"))
    if plan_complexity == "high":
        idx += 1
    if dep_count >= 3:
        idx += 1
    idx = min(idx, len(_COMPLEXITY_ORDER) - 1)
    return _COMPLEXITY_ORDER[idx]


def _priority(task_type: str, level: int, n_dependents: int,
              on_critical: bool, max_level: int) -> str:
    """Deterministic priority from dependency depth, blocking factor, and
    verification impact. Never random."""
    if task_type == TaskType.DEPLOYMENT:
        return "critical"
    if task_type == TaskType.VERIFICATION:
        return "critical"
    if task_type == TaskType.TESTING:
        return "high"
    if task_type == TaskType.DESIGN:
        return "high"
    score = level + n_dependents
    if on_critical and score >= max_level:
        return "high"
    if score >= 4:
        return "high"
    if score >= 2:
        return "medium"
    return "low"


def _acceptance_criteria(task_type: str, title: str, plan: Plan) -> List[str]:
    """What success means for this task. Never empty."""
    base = {
        TaskType.ANALYSIS: "Current state documented; gaps and unknowns identified.",
        TaskType.RESEARCH: "Question framed; findings captured and evaluated.",
        TaskType.DESIGN: "Interfaces and contracts specified and reviewed.",
        TaskType.IMPLEMENTATION: "Change implemented and builds; unit tests green.",
        TaskType.TESTING: "Tests added/extended and passing in CI.",
        TaskType.VERIFICATION: "Acceptance criteria verified against the plan goal.",
        TaskType.DOCUMENTATION: "Docs updated and linked from the relevant entry points.",
        TaskType.MIGRATION: "Data/state migrated; down-migration verified reversible.",
        TaskType.REFACTOR: "Behavior preserved (characterization tests pass).",
        TaskType.INFRASTRUCTURE: "Component live and monitored; rollback ready.",
        TaskType.REVIEW: "Review approved; all comments resolved.",
        TaskType.DEPLOYMENT: "Change shipped; health checks green post-rollout.",
        TaskType.CONFIGURATION: "Config applied; behavior validated; previous values backed up.",
        TaskType.CLEANUP: "Debris removed; no regressions introduced.",
        TaskType.PLANNING: "Plan produced and validated against evidence.",
    }.get(task_type, "Task completes and its output is verified.")
    specific = f"'{title}' satisfies the milestone it was compiled from."
    return [base, specific]


def _task_verification(task_type: str, plan: Plan) -> List[dict]:
    """Reuse the Plan's verification; each task gets task-specific verification
    on top. Never empty (plan verification is mandatory)."""
    out: List[dict] = []
    for v in plan.verification:
        out.append({"method": v.get("method", "check"),
                    "detail": v.get("detail", "")})
    # Task-type-specific check.
    specific = {
        TaskType.IMPLEMENTATION: {"method": "build", "detail": "Change compiles/links cleanly."},
        TaskType.DESIGN: {"method": "review", "detail": "Design peer-reviewed before implementation."},
        TaskType.TESTING: {"method": "tests", "detail": "New tests execute and pass."},
        TaskType.VERIFICATION: {"method": "acceptance", "detail": "Acceptance criteria met end to end."},
        TaskType.MIGRATION: {"method": "reversible", "detail": "Down-migration restores prior state."},
        TaskType.DEPLOYMENT: {"method": "health", "detail": "Post-deploy health checks green."},
    }.get(task_type)
    if specific:
        out.append(specific)
    return out


def _task_rollback(task_type: str, plan: Plan) -> List[dict]:
    """Reuse the Plan's rollback; each task gets task-specific rollback."""
    out: List[dict] = []
    for rb in plan.rollback:
        out.append({"strategy": rb.get("strategy", "revert"),
                    "detail": rb.get("detail", "")})
    specific = {
        TaskType.IMPLEMENTATION: {"strategy": "feature_flag",
                                  "detail": "Gate the change; disable to revert instantly."},
        TaskType.MIGRATION: {"strategy": "migration_rollback",
                             "detail": "Apply down-migration to revert data changes."},
        TaskType.DEPLOYMENT: {"strategy": "git_revert",
                              "detail": "Revert the deploy commit to roll back."},
    }.get(task_type)
    if specific:
        out.append(specific)
    return out or [{"strategy": "git_revert", "detail": "Revert the change via version control."}]


_INPUTS: Dict[str, List[str]] = {
    TaskType.ANALYSIS: ["Plan goal", "Prior findings"],
    TaskType.RESEARCH: ["Plan goal", "Existing knowledge"],
    TaskType.DESIGN: ["Analysis findings", "Plan goal"],
    TaskType.IMPLEMENTATION: ["Design interfaces", "Analysis findings"],
    TaskType.TESTING: ["Implementation output", "Acceptance criteria"],
    TaskType.VERIFICATION: ["Test results", "Acceptance criteria"],
    TaskType.DOCUMENTATION: ["Implemented change", "Design spec"],
    TaskType.MIGRATION: ["Current schema/state", "Down-migration plan"],
    TaskType.REFACTOR: ["Characterization tests", "Current implementation"],
    TaskType.INFRASTRUCTURE: ["Boundary contracts", "Current topology"],
    TaskType.REVIEW: ["Implemented change", "Design spec"],
    TaskType.DEPLOYMENT: ["Verified build", "Rollback plan"],
    TaskType.CONFIGURATION: ["Current config", "Target values"],
    TaskType.CLEANUP: ["Stale artifacts list"],
    TaskType.PLANNING: ["Evidence set"],
}

_OUTPUTS: Dict[str, List[str]] = {
    TaskType.ANALYSIS: ["Findings report"],
    TaskType.RESEARCH: ["Evaluation write-up"],
    TaskType.DESIGN: ["Interface/contract spec"],
    TaskType.IMPLEMENTATION: ["Working code"],
    TaskType.TESTING: ["Test report"],
    TaskType.VERIFICATION: ["Verification result"],
    TaskType.DOCUMENTATION: ["Updated docs"],
    TaskType.MIGRATION: ["Migrated state"],
    TaskType.REFACTOR: ["Refactored code"],
    TaskType.INFRASTRUCTURE: ["Provisioned component"],
    TaskType.REVIEW: ["Approved change"],
    TaskType.CONFIGURATION: ["Applied config"],
    TaskType.CLEANUP: ["Clean workspace"],
    TaskType.PLANNING: ["Plan document"],
    # Deployment intentionally leaves outputs open (release artifact varies).
    TaskType.DEPLOYMENT: [],
}


# File extensions that unambiguously name a real artifact (vs a bare word).
_ARTIFACT_EXT = {
    "py", "md", "txt", "sh", "ts", "tsx", "jsx", "js", "rs", "go", "rb", "java",
    "c", "h", "cpp", "hpp", "json", "yaml", "yml", "sql", "html", "css", "scss",
    "toml", "cfg", "ini", "lock", "env", "pdf", "csv", "xml",
}


def _expected_artifacts(task_type: str, title: str, goal: str) -> List[str]:
    """Derive the explicit, machine-checkable artifact contract for a task.

    Phase 1.5: the planner now STAMPS the expected file paths onto the task so
    the runtime verifies an explicit contract instead of guessing from prose.
    Deterministic: looks for `name.ext` tokens in the task title and the goal
    (the goal/title name the very file the mission wants). Returns [] for task
    types that are not artifact-producing (analysis, research, design, ...).
    """
    if task_type not in _CREATION_TASK_TYPES:
        return []
    out: List[str] = []
    seen = set()
    for text in (title, goal):
        for word in (text or "").split():
            word = word.strip(".,;:'\"!?()[]{}")
            if "." in word and not word.startswith(".") and not word.endswith("."):
                ext = word.rsplit(".", 1)[1].lower()
                if ext in _ARTIFACT_EXT and word not in seen:
                    seen.add(word)
                    out.append(word)
    return out


# Task types whose purpose is to PRODUCE a file on disk. For these the planner
# emits an explicit artifact path into the contract.
_CREATION_TASK_TYPES = frozenset({
    TaskType.IMPLEMENTATION, TaskType.DOCUMENTATION, TaskType.TESTING,
    TaskType.CONFIGURATION, TaskType.CLEANUP, TaskType.MIGRATION,
    TaskType.INFRASTRUCTURE, TaskType.DEPLOYMENT,
})


def _cap_for_symbolic(task_type: str) -> List[str]:
    """Capability hints for a symbolic task type (Resolver refines these).

    Must use the FROZEN capability vocabulary shared with graph_schema
    validation (python/testing/configuration/...), NOT the Resolver's
    worker-capability strings (file editing/shell commands/...). The Resolver's
    repo enrichment adds the worker-side hints at resolution time, where the
    graph schema is no longer re-validated. This keeps the compiled graph
    schema-valid while still biasing toward deterministic executors."""
    return {
        "analysis": ["python"],
        "refactor": ["python"],
        "implementation": ["python"],
        "cleanup": ["python"],
        "configuration": ["configuration"],
        "testing": ["testing"],
        "verification": ["testing"],
        # Review has no capability in the frozen vocabulary; a valid token keeps
        # the task's capability contract non-empty (the resolver routes review by
        # task_type, not capability, so this is cosmetic).
        "review": ["research"],
    }.get(task_type, [])


def _evidence_for(milestone: dict, plan: Plan) -> List[str]:
    """Ground each task in the evidence kind its milestone referenced. No
    hallucinated ids — only valid lower-layer ids the plan already cites."""
    kind = (milestone.get("evidence") or "").lower()
    if kind == "initiative":
        return list(plan.affected_initiative_ids)
    if kind == "insight":
        return list(plan.affected_insight_ids)
    if kind == "understanding":
        return list(plan.affected_understanding_ids)
    if kind == "knowledge":
        return list(plan.affected_knowledge_ids)
    return []


# ---------------------------------------------------------------------------
# Milestone -> sub-task expansion (deterministic, type-driven).
# ---------------------------------------------------------------------------

def _expand(milestone: dict, plan: Plan, ptype: str = "") -> List[dict]:
    """Expand one plan milestone into 1+ sub-task specs.

    Each spec: {task_type, title, parallel_next, symbolic, acceptance_criteria}.
    When a milestone already carries ``task_type`` / ``symbolic`` /
    ``acceptance_criteria`` (added by the LLM or trivial-pattern planner),
    these are passed through verbatim so the compiler emits a task that
    matches the milestone's intent. Otherwise, the original deterministic
    title-based mapping is used.
    """
    title = (milestone.get("title") or "").lower()
    ptype = plan.plan_type.value
    detail = milestone.get("detail", "")

    # If the milestone already has an explicit task_type (from LLM/trivial plan),
    # pass it through with the milestone's full payload.
    if milestone.get("task_type"):
        tt = milestone["task_type"]
        sym = milestone.get("symbolic", {}) or {}
        ac = milestone.get("acceptance_criteria", []) or []
        return [{
            "task_type": tt,
            "title": milestone.get("title", ""),
            "parallel_next": bool(milestone.get("parallel_next", False)),
            "symbolic": sym,
            "acceptance_criteria": ac if isinstance(ac, list) else [str(ac)],
        }]

    def spec(tt, t, par=False):
        # Propagate an explicit parallel_next hint from the milestone itself
        # (set by the template-fallback path for independent evidence subjects
        # that share the same milestone order).
        par = par or bool(milestone.get("parallel_next", False))
        return {"task_type": tt, "title": t, "parallel_next": par}

    # Feature / Integration: the FEATURE plan already carries explicit Backend
    # + Frontend milestones, so a plain "Implement" milestone is redundant and
    # must expand to NOTHING (avoids duplicate implementation tasks).
    if title == "implement" and ptype in ("feature", "integration"):
        return []
    # "Verify" milestone fans into Testing + Verification running in PARALLEL
    # (no intra-phase edge), both verifying the same change.
    if title == "verify":
        return [
            spec(TaskType.TESTING, "Run verification plan (tests/benchmarks/review)", True),
            spec(TaskType.VERIFICATION, "Verify against acceptance criteria", False),
        ]
    # "Characterize current behavior" (refactor/migration) -> Testing.
    if "characterize" in title:
        return [spec(TaskType.TESTING, "Characterize current behavior with tests")]
    # "Establish boundaries" (infra/arch) -> Design.
    if "boundaries" in title:
        return [spec(TaskType.DESIGN, "Establish module/component boundaries")]
    # "Backend" / "Frontend" explicit milestones -> Implementation. They are
    # sibling phases and run in PARALLEL (no intra-phase edge), so Backend is
    # marked parallel_next=True.
    if title == "backend":
        return [spec(TaskType.IMPLEMENTATION, "Implement backend logic", True)]
    if title == "frontend":
        return [spec(TaskType.IMPLEMENTATION, "Implement frontend surface")]
    # Research/learning milestones map to research + documentation shapes.
    if title in ("survey", "hypothesis", "prototype", "evaluate", "write-up"):
        tt = {
            "survey": TaskType.RESEARCH,
            "hypothesis": TaskType.RESEARCH,
            "prototype": TaskType.IMPLEMENTATION,
            "evaluate": TaskType.TESTING,
            "write-up": TaskType.DOCUMENTATION,
        }[title]
        return [spec(tt, milestone.get("title", title))]
    # Generic, title-driven mapping.
    mapping = {
        "investigate": TaskType.ANALYSIS,
        "scope": TaskType.ANALYSIS,
        "design": TaskType.DESIGN,
        "document": TaskType.DOCUMENTATION,
        "roll out": TaskType.DEPLOYMENT,
        "monitor": TaskType.DEPLOYMENT,
        "roll": TaskType.DEPLOYMENT,
    }
    for key, tt in mapping.items():
        if key in title:
            return [spec(tt, milestone.get("title", title))]
    # Generic "implement": a single implementation task.
    if title == "implement":
        return [spec(TaskType.IMPLEMENTATION, milestone.get("title", "Implement change"))]
    # Default: a single implementation task named after the milestone.
    return [spec(TaskType.IMPLEMENTATION, milestone.get("title", "Implement change"))]


# ---------------------------------------------------------------------------
# Graph algorithms: cycle detection, levels, critical path, parallel groups.
# ---------------------------------------------------------------------------

def _detect_cycle(edges: List[dict], task_ids: List[str]) -> bool:
    """True if the directed graph (from depends on to) contains a cycle.

    Traverses in dependency direction: an edge `from -> to` (from depends on
    to) is followed as `from -> to`, so a back-edge from a descendant to an
    ancestor is caught by the GRAY (in-stack) check.
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
        if color[t] == WHITE:
            if visit(t):
                return True
    return False


def _compute_levels(edges: List[dict], task_ids: List[str]) -> Dict[str, int]:
    """Longest-path-from-root level for each task (topological DP)."""
    preds: Dict[str, List[str]] = {t: [] for t in task_ids}
    for e in edges:
        preds.setdefault(e["from"], []).append(e["to"])
    level: Dict[str, int] = {}

    def depth(u: str, stack: Tuple[str, ...] = ()) -> int:
        if u in level:
            return level[u]
        if u in stack:  # shouldn't happen (acyclic enforced) — guard
            return 0
        if not preds.get(u):
            level[u] = 0
            return 0
        d = 1 + max(depth(p, stack + (u,)) for p in preds[u])
        level[u] = d
        return d

    for t in task_ids:
        depth(t)
    return level


def _critical_path(edges: List[dict], tasks: List[Task]) -> List[str]:
    """Longest path by node count (the critical path). Deterministic tie-break
    by lowest task sequence."""
    succ: Dict[str, List[str]] = {t.id: [] for t in tasks}
    preds: Dict[str, List[str]] = {t.id: [] for t in tasks}
    for e in edges:
        succ.setdefault(e["to"], []).append(e["from"])
        preds.setdefault(e["from"], []).append(e["to"])
    seq = {t.id: t.sequence for t in tasks}

    best: Dict[str, int] = {}
    nxt: Dict[str, Optional[str]] = {}

    def longest(u: str, stack: Tuple[str, ...] = ()) -> int:
        if u in best:
            return best[u]
        if u in stack:
            best[u] = 1
            return 1
        outs = succ.get(u, [])
        if not outs:
            best[u] = 1
            nxt[u] = None
            return 1
        cand = []
        for v in outs:
            cand.append((longest(v, stack + (u,)), v))
        # pick max length; tie-break: smallest successor sequence
        cand.sort(key=lambda x: (-x[0], seq.get(x[1], 1 << 30)))
        bl, bv = cand[0]
        best[u] = bl + 1
        nxt[u] = bv
        return best[u]

    for t in tasks:
        longest(t.id)

    # start from the root (no predecessors) with the longest chain
    roots = [t.id for t in tasks if not preds.get(t.id)]
    if not roots:  # defensive: shouldn't happen for a DAG / empty graph
        return []
    start = max(roots, key=lambda r: (best.get(r, 1), -seq.get(r, 1 << 30)))
    path: List[str] = []
    cur: Optional[str] = start
    guard = 0
    while cur is not None and guard <= len(tasks) + 1:
        path.append(cur)
        cur = nxt.get(cur)
        guard += 1
    return path


def _parallel_groups(levels: Dict[str, int]) -> Tuple[int, List[str]]:
    by_level: Dict[int, List[str]] = {}
    for tid, lv in levels.items():
        by_level.setdefault(lv, []).append(tid)
    groups = sum(1 for v in by_level.values() if len(v) >= 2)
    parallel = [tid for v in by_level.values() if len(v) >= 2 for tid in v]
    return groups, parallel


# ---------------------------------------------------------------------------
# Compiler entrypoint.
# ---------------------------------------------------------------------------

class TaskGraphCompiler:
    """Compiles a Plan into a deterministic, acyclic TaskGraph.

    Read-only over the Plan; write-only over the new task-graph tables. The
    Plan object is the ONLY input.
    """

    def compile(self, plan: Plan, generated_at: Optional[str] = None) -> TaskGraph:
        if generated_at is None:
            generated_at = now_iso()
        gid = _graph_id(plan)
        pid = plan.id or plan._generate_id()

        # Phase 3: engineering-pattern pre-pass. Only applies when the plan
        # has NO LLM-derived or trivial-pattern milestones (i.e. milestones that
        # already carry task_type/symbolic). Even then, only specific engineering
        # patterns (rename/extract/refactor/bugfix/maintenance) override the
        # template — generic patterns like "add X to Y" produce worse output
        # than the template fallback, so they are excluded.
        _has_explicit_milestones = any(
            m.get("task_type") for m in plan.milestones)
        if not _has_explicit_milestones:
            pattern = classify(plan.goal, plan)
            if pattern is not None and pattern.name not in ("feature",):
                return self._compile_pattern(gid, pid, plan, pattern, generated_at)

        milestones = sorted(plan.milestones, key=lambda m: m.get("order", 0))
        if not milestones:
            raise ValueError(f"plan {pid} has no milestones to compile")

        # 1. Expand milestones into ordered sub-task specs, assign phases.
        #    Sibling milestones (e.g. Backend/Frontend, Testing/Verification)
        #    collapse into a SINGLE shared phase so they become parallel tasks.
        phase_of: List[int] = []      # milestone order per sub-task
        specs: List[dict] = []
        _sibling_titles = {"backend", "frontend", "roll out", "monitor"}
        _verify_titles = {"verify"}
        last_phase = 0
        for idx, m in enumerate(milestones):
            order = m.get("order", 0)
            title = (m.get("title") or "").lower()
            # Siblings share the previous phase (parallel), unless first phase.
            if idx > 0:
                prev_title = (milestones[idx - 1].get("title") or "").lower()
                if (title in _sibling_titles and prev_title in _sibling_titles) \
                        or (title in _verify_titles and prev_title in _verify_titles):
                    order = last_phase
            last_phase = order
            for s in _expand(m, plan, ptype=plan.plan_type.value):
                if not s:  # redundant milestone (e.g. plain "Implement" in FEATURE)
                    continue
                specs.append(s)
                phase_of.append(order)
        # Re-number phases contiguously (0..k-1) so there are no gaps; sibling
        # collapse can leave a gap that would break inter-phase dependency edges.
        _distinct = sorted(set(phase_of))
        _rank = {v: i for i, v in enumerate(_distinct)}
        phase_of = [_rank[v] for v in phase_of]

        n = len(specs)
        tasks: List[Task] = []
        seq_to_id: List[str] = []

        # 2. Build task objects (fields except edges; deps filled after edges).
        for i, (spec, phase) in enumerate(zip(specs, phase_of), start=1):
            try:
                tt = TaskType.from_str(spec["task_type"])
            except ValueError:
                tt = TaskType.IMPLEMENTATION  # fallback for unknown task_type
            m = self._milestone_for(phase, milestones)
            title = spec["title"]
            caps = _infer_capabilities(m.get("title", ""), tt, plan)
            desc = (m.get("detail") or "")
            conf = plan.confidence.value if plan.confidence else "medium"
            # Phase 1: propagate symbolic/ac from milestone (LLM/trivial planners
            # stamp these on the milestone; the template fallback doesn't.)
            spec_sym = spec.get("symbolic") or {}
            spec_ac = spec.get("acceptance_criteria") or []
            # When the trivial/LLM planner provided explicit acceptance_criteria
            # on the spec, those override the generic ones.
            outputs = list(_OUTPUTS.get(tt, [])) + _expected_artifacts(tt, title, plan.goal)
            # If symbolic has a "path", include it in outputs for artifact check.
            if spec_sym.get("path"):
                p = spec_sym["path"]
                if p not in outputs:
                    outputs.append(p)
            t = Task(
                id=f"{gid}#t{i}",
                graph_id=gid,
                plan_id=pid,
                milestone_order=phase,
                title=title,
                description=desc,
                task_type=tt,
                required_capabilities=caps,
                complexity="medium",          # filled below (needs dep count)
                priority="medium",            # filled below (needs level)
                estimated_effort=plan.estimated_effort or "medium",
                dependencies=[],
                inputs=list(_INPUTS.get(tt, ["Plan goal"])),
                # Phase 1.5: emit the explicit artifact contract (file paths)
                # alongside the human-readable description, so the runtime can
                # verify WHAT must exist after execution. Deduplicated.
                outputs=outputs,
                acceptance_criteria=spec_ac or _acceptance_criteria(tt, title, plan),
                verification=_task_verification(tt, plan),
                rollback=_task_rollback(tt, plan),
                evidence=_evidence_for(m, plan),
                # Phase 1: propagate symbolic intent from milestone to task.
                symbolic=spec_sym,
                status="pending",
                confidence=conf,
                sequence=i,
            )
            tasks.append(t)
            seq_to_id.append(t.id)

        # 3. Build edges: inter-phase (phase p depends on phase p-1) + intra-phase
        #    (sequential chain when parallel_next is False).
        edges: List[dict] = []
        for i, (spec, phase) in enumerate(zip(specs, phase_of)):
            cur_id = seq_to_id[i]
            # intra-phase: chain to previous sub-task in same phase if sequential
            if i > 0 and phase_of[i - 1] == phase and not specs[i - 1]["parallel_next"]:
                edges.append({"from": cur_id, "to": seq_to_id[i - 1],
                              "kind": "depends_on"})
            # inter-phase: depend on every task in the immediately prior phase
            prev_ids = [seq_to_id[j] for j in range(i)
                        if phase_of[j] == phase - 1]
            for p in prev_ids:
                edges.append({"from": cur_id, "to": p, "kind": "depends_on"})

        # 4. De-duplicate edges by (from, to).
        seen = set()
        deduped = []
        for e in edges:
            key = (e["from"], e["to"])
            if key not in seen:
                seen.add(key)
                deduped.append(e)
        edges = deduped

        # 5. Cycle rejection (compiler must never emit a cycle).
        task_ids = [t.id for t in tasks]
        if _detect_cycle(edges, task_ids):
            raise CycleError(
                f"compiled graph {gid} contains a cycle; rejected")

        # 6. Levels / critical path / parallel groups.
        levels = _compute_levels(edges, task_ids)
        cpath = _critical_path(edges, tasks)
        pgroups, ptasks = _parallel_groups(levels)
        on_crit = set(cpath)
        max_level = max(levels.values()) if levels else 0

        # 7. Fill dependency ids, complexity, priority per task.
        preds: Dict[str, List[str]] = {t.id: [] for t in tasks}
        for e in edges:
            preds.setdefault(e["from"], []).append(e["to"])
        # reverse: how many tasks depend on this one (blocking factor)
        dependents: Dict[str, int] = {t.id: 0 for t in tasks}
        for e in edges:
            dependents[e["to"]] = dependents.get(e["to"], 0) + 1

        for t in tasks:
            t.dependencies = sorted(preds.get(t.id, []))
            t.complexity = _complexity(t.task_type, len(t.dependencies),
                                       plan.estimated_complexity)
            t.priority = _priority(t.task_type, levels.get(t.id, 0),
                                   dependents.get(t.id, 0),
                                   t.id in on_crit, max_level)

        g = TaskGraph(
            id=gid, goal=plan.goal, plan_id=pid, plan_type=plan.plan_type.value,
            tasks=tasks, edges=edges, status="compiled",
            created_at=generated_at, updated_at=generated_at,
            critical_path=cpath, levels=levels,
            parallel_groups=pgroups, parallel_tasks=ptasks,
        )
        return g

    def _compile_pattern(self, gid: str, pid: str, plan: Plan,
                          pattern, generated_at: str) -> TaskGraph:
        """Build a deterministic engineering task graph from a PatternPlan.

        Reuses the SAME edge/level/critical-path/priority machinery as the
        generic path — only the task source differs (symbolic tasks instead of
        expanded milestones). A pattern task that is `parallel_next` runs in
        parallel with the following task within the same linear phase.
        """
        specs = pattern.tasks
        n = len(specs)
        tasks: List[Task] = []
        seq_to_id: List[str] = []

        for i, spec in enumerate(specs, start=1):
            tt = TaskType.from_str(spec.task_type)
            # Symbolic capability hints + language caps inferred from the goal
            # (reuses the frozen language vocabulary so e.g. "Rust" still
            # surfaces on an extract/rename graph, preserving prior behaviour).
            caps = _cap_for_symbolic(spec.task_type) + _infer_language_caps(plan.goal)
            # De-dup, preserve order.
            _seen = set()
            caps = [c for c in caps if not (c in _seen or _seen.add(c))]
            desc = spec.symbolic.get("goal") or plan.goal
            t = Task(
                id=f"{gid}#t{i}",
                graph_id=gid, plan_id=pid, milestone_order=0,
                title=spec.title, description=desc, task_type=tt,
                required_capabilities=caps,
                complexity="medium", priority="medium",
                estimated_effort=plan.estimated_effort or "medium",
                dependencies=[], inputs=["Plan goal"],
                outputs=list(_OUTPUTS.get(tt, [])),
                acceptance_criteria=list(spec.acceptance_criteria),
                verification=list(spec.verification),
                rollback=_task_rollback(tt, plan),
                evidence=[],
                symbolic=dict(spec.symbolic),
                status="pending",
                confidence=(plan.confidence.value if plan.confidence else "medium"),
                sequence=i,
            )
            tasks.append(t)
            seq_to_id.append(t.id)

        # Linear chain; a `parallel_next` task gets NO edge to its successor.
        edges: List[dict] = []
        for i in range(1, n):
            if not specs[i - 1].parallel_next:
                edges.append({"from": seq_to_id[i], "to": seq_to_id[i - 1],
                              "kind": "depends_on"})
        # De-dup edges.
        seen = set()
        deduped = []
        for e in edges:
            key = (e["from"], e["to"])
            if key not in seen:
                seen.add(key)
                deduped.append(e)
        edges = deduped

        task_ids = [t.id for t in tasks]
        if _detect_cycle(edges, task_ids):
            raise CycleError(
                f"pattern graph {gid} contains a cycle; rejected")

        levels = _compute_levels(edges, task_ids)
        cpath = _critical_path(edges, tasks)
        pgroups, ptasks = _parallel_groups(levels)
        on_crit = set(cpath)
        max_level = max(levels.values()) if levels else 0

        preds: Dict[str, List[str]] = {t.id: [] for t in tasks}
        for e in edges:
            preds.setdefault(e["from"], []).append(e["to"])
        dependents: Dict[str, int] = {t.id: 0 for t in tasks}
        for e in edges:
            dependents[e["to"]] = dependents.get(e["to"], 0) + 1

        for t in tasks:
            t.dependencies = sorted(preds.get(t.id, []))
            t.complexity = _complexity(t.task_type, len(t.dependencies),
                                       plan.estimated_complexity)
            t.priority = _priority(t.task_type, levels.get(t.id, 0),
                                   dependents.get(t.id, 0),
                                   t.id in on_crit, max_level)

        return TaskGraph(
            id=gid, goal=plan.goal, plan_id=pid, plan_type=plan.plan_type.value,
            tasks=tasks, edges=edges, status="compiled",
            created_at=generated_at, updated_at=generated_at,
            critical_path=cpath, levels=levels,
            parallel_groups=pgroups, parallel_tasks=ptasks,
        )

    @staticmethod
    def _milestone_for(phase: int, milestones: List[dict]) -> dict:
        for m in milestones:
            if m.get("order", 0) == phase:
                return m
        return milestones[0] if milestones else {}


def compile_plan(plan: Plan, generated_at: Optional[str] = None) -> TaskGraph:
    """Convenience wrapper: compile one Plan into a TaskGraph."""
    return TaskGraphCompiler().compile(plan, generated_at=generated_at)
