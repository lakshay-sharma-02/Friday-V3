"""Worker Registry Engine (Milestone 9.2).

WRITE-ONLY catalog of worker capability profiles. It registers, validates,
exports, and reports workers. It NEVER executes, schedules, selects, or runs
anything. The future Capability Resolver (M9.3) consumes the catalog; this
engine only answers "what capabilities exist?".

Deterministic registration (no LLM, no randomness):
- Built-in workers ship pre-defined (the spec's minimum set, provider-agnostic).
- Custom workers register from JSON manifests (CLI `worker register`).
- Re-registering the same id REPLACES the live row, appends a worker_history
  snapshot, and logs the version. Append-only history never mutates.
- Unknown capabilities / languages / task-types / plan-types are rejected, never
  stored — no hallucinated capabilities.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import List, Optional

from ..db import (
    atomic,
    WorkerHistoryRow,
    WorkerRow,
    WorkerVersionRow,
    get_all_workers,
    get_worker,
    get_worker_by_name,
    insert_worker,
    insert_worker_history,
    insert_worker_version,
    update_worker_status,
    update_worker_availability,
    update_worker_version,
    worker_history_for,
    worker_versions_for,
    workers_with_capability,
)
from .models import (
    Worker,
    WorkerKind,
    KIND_LLM,
    KIND_CLI,
    KIND_FUNCTION,
    KIND_AGENT,
    KIND_TOOL,
    KIND_SERVICE,
    is_valid_capability,
    validate_capabilities,
    validate_languages,
    validate_plan_types,
    validate_task_types,
    SCHEMA_VERSION,
)


class RegistryError(Exception):
    """Raised on invalid registration input (deterministic, never an LLM)."""


@dataclass
class RegistrationResult:
    """Outcome of a registration call."""

    created: int = 0
    updated: int = 0
    rejected: List[str] = None  # rejected capability/field notices
    events: int = 0

    def __post_init__(self):
        if self.rejected is None:
            self.rejected = []

    def to_text(self) -> str:
        lines = [
            "Worker Registry",
            f"Created: {self.created}",
            f"Updated: {self.updated}",
            f"History events: {self.events}",
        ]
        if self.rejected:
            lines.append("Rejected (not stored):")
            for r in self.rejected:
                lines.append(f"  - {r}")
        lines.append("Done.")
        return "\n".join(lines) + "\n"


class WorkerRegistry:
    """Catalog of worker capability profiles. READ+WRITE, append-only history."""

    def __init__(self, conn) -> None:
        self.conn = conn

    # --- READ (never mutate) -------------------------------------------------

    def all_workers(self) -> List[Worker]:
        return [Worker.from_row(r) for r in get_all_workers(self.conn)]

    def active_workers(self) -> List[Worker]:
        return [w for w in self.all_workers() if w.status == "active"]

    def worker_by_id(self, wid: str) -> Optional[Worker]:
        row = get_worker(self.conn, wid)
        return Worker.from_row(row) if row else None

    def worker_by_name(self, name: str) -> Optional[Worker]:
        row = get_worker_by_name(self.conn, name)
        return Worker.from_row(row) if row else None

    def workers_for_capability(self, capability: str) -> List[Worker]:
        """Capability Resolver hook: every worker exposing `capability`."""
        rows = workers_with_capability(self.conn, capability)
        return [Worker.from_row(r) for r in rows]

    def history(self, wid: str) -> List[WorkerHistoryRow]:
        return worker_history_for(self.conn, wid)

    def versions(self, wid: str) -> List[WorkerVersionRow]:
        return worker_versions_for(self.conn, wid)

    def count(self) -> int:
        from ..db import count_workers
        return count_workers(self.conn)

    def export_json(self) -> dict:
        """Export the entire registry as deterministic JSON (metadata only)."""
        workers = [w.to_json() for w in self.all_workers()]
        return {
            "registry_version": "1.0",
            "worker_count": len(workers),
            "workers": workers,
        }

    # --- WRITE ---------------------------------------------------------------

    def register(self, worker: Worker, registered_at: Optional[str] = None,
                 changelog: Optional[str] = None) -> RegistrationResult:
        """Register or update ONE worker. Idempotent on id (worker:<name>).

        Validates the closed vocabularies; rejects unknown capabilities/languages/
        task-types/plan-types (recorded in result.rejected, never stored). Records
        the prior version in worker_history and logs the version (append-only).
        """
        if registered_at is None:
            from .models import now_iso
            registered_at = now_iso()

        # --- deterministic validation (no LLM) ---
        rejected: List[str] = []
        caps = validate_capabilities(worker.capabilities)
        for raw in worker.capabilities:
            if raw and raw.strip() and not is_valid_capability(raw):
                rejected.append(f"capability: {raw}")
        langs = validate_languages(worker.supported_languages)
        for raw in worker.supported_languages:
            if raw and raw.strip() and raw not in langs:
                rejected.append(f"language: {raw}")
        ttypes = validate_task_types(worker.supported_task_types)
        for raw in worker.supported_task_types:
            if raw and raw.strip() and raw not in ttypes:
                rejected.append(f"task_type: {raw}")
        ptypes = validate_plan_types(worker.supported_plan_types)
        for raw in worker.supported_plan_types:
            if raw and raw.strip() and raw not in ptypes:
                rejected.append(f"plan_type: {raw}")

        wid = worker.id or worker._generate_id()
        prev = self.worker_by_id(wid)

        # Build the validated worker (unknown fields dropped).
        if prev is None:
            worker.id = wid
            worker.capabilities = caps
            worker.supported_languages = langs
            worker.supported_task_types = ttypes
            worker.supported_plan_types = ptypes
            worker.created_at = registered_at
            worker.updated_at = registered_at
            created, updated = 1, 0
            event_type = "registered"
        else:
            worker.id = wid
            worker.capabilities = caps
            worker.supported_languages = langs
            worker.supported_task_types = ttypes
            worker.supported_plan_types = ptypes
            worker.created_at = prev.created_at
            worker.updated_at = registered_at
            created, updated = 0, 1
            # Bump version on material change so history stays meaningful.
            if (prev.capabilities != caps or prev.supported_languages != langs
                    or prev.supported_task_types != ttypes
                    or prev.supported_plan_types != ptypes
                    or prev.limitations != worker.limitations
                    or prev.status != worker.status):
                worker.version = _bump_version(prev.version)
                event_type = "updated"
            else:
                worker.version = prev.version
                event_type = "reregistered"

        row = worker.to_row()
        insert_worker(self.conn, row)

        # Append-only snapshot.
        insert_worker_history(self.conn, [WorkerHistoryRow(
            registered_at=registered_at,
            worker_id=wid,
            name=worker.name,
            kind=worker.kind.value,
            version=worker.version,
            status=worker.status,
            capabilities=",".join(caps),
            limitations=",".join(worker.limitations),
            event_type=event_type,
            note=changelog,
        )])
        # Version log (appended once per distinct version).
        insert_worker_version(self.conn, [WorkerVersionRow(
            worker_id=wid,
            version=worker.version,
            registered_at=registered_at,
            changelog=changelog or _version_note(prev, event_type),
        )])

        return RegistrationResult(
            created=created, updated=updated, rejected=rejected, events=1)

    def register_builtins(self) -> RegistrationResult:
        """Register every built-in worker (idempotent)."""
        res = RegistrationResult()
        for w in BUILTIN_WORKERS:
            r = self.register(w)
            res.created += r.created
            res.updated += r.updated
            res.events += r.events
            res.rejected.extend(r.rejected)
        return res

    def register_from_manifest(self, manifest: dict) -> RegistrationResult:
        """Register a worker described by a JSON manifest (CLI `worker register`).
        Unknown fields are ignored; only the spec'd metadata is stored."""
        declared = manifest.get("schema_version")
        if declared is not None and declared != SCHEMA_VERSION:
            # Law 24: an incompatible manifest contract must fail cleanly.
            raise RegistryError(
                f"manifest schema_version {declared!r} != current {SCHEMA_VERSION!r}")
        worker = _worker_from_manifest(manifest)
        return self.register(worker)

    def register_external(self, discovery=None) -> int:
        """Register the declared external AI adapters from their manifests.
        Idempotent: re-registering the same worker id REPLACES the row (per
        register()'s upsert semantics). Returns count registered."""
        created = 0
        for m in _EXTERNAL_MANIFESTS:
            res = self.register_from_manifest(dict(m))
            created += res.created
        if discovery is not None:
            self.sync_availability(discovery)
        return created

    def sync_availability(self, discovery) -> int:
        """Update ONLY the availability column from a DiscoveryResult. Workers are
        already registered; this synchronizes runtime state without touching
        static metadata."""
        # Duck-type check: accept any object with .available / .unavailable attrs.
        available = getattr(discovery, "available", None)
        unavailable = getattr(discovery, "unavailable", None)
        if available is None or unavailable is None:
            raise TypeError("sync_availability expects an object with 'available' "
                            "and 'unavailable' list attributes")
        updated = 0
        for wid in unavailable:
            w = self.worker_by_id(wid)
            if w is not None and w.availability != "unavailable":
                update_worker_availability(self.conn, wid, "unavailable")
                updated += 1
        for wid in available:
            w = self.worker_by_id(wid)
            if w is not None and w.availability != "available":
                update_worker_availability(self.conn, wid, "available")
                updated += 1
        return updated

    def enable(self, name: str) -> bool:
        """Enable a worker (live-row status mutation; history preserved)."""
        w = self.worker_by_name(name)
        if w is None:
            return False
        update_worker_status(self.conn, w.id, "active")
        self._record_event(w, "enabled")
        return True

    def disable(self, name: str) -> bool:
        """Disable a worker (live-row status mutation; history preserved)."""
        w = self.worker_by_name(name)
        if w is None:
            return False
        update_worker_status(self.conn, w.id, "disabled")
        self._record_event(w, "disabled")
        return True

    def upgrade_version(self, name: str, version: str) -> bool:
        """Advance a worker's version (live-row mutation; version log appended)."""
        w = self.worker_by_name(name)
        if w is None:
            return False
        update_worker_version(self.conn, w.id, version)
        insert_worker_version(self.conn, [WorkerVersionRow(
            worker_id=w.id, version=version,
            registered_at=_now(), changelog=f"version set to {version}")])
        self._record_event(w, "version_updated")
        return True

    # --- internals -----------------------------------------------------------

    def _record_event(self, w: Worker, event_type: str) -> None:
        from .models import now_iso
        insert_worker_history(self.conn, [WorkerHistoryRow(
            registered_at=now_iso(),
            worker_id=w.id,
            name=w.name,
            kind=w.kind.value,
            version=w.version,
            status=w.status,
            capabilities=",".join(w.capabilities),
            limitations=",".join(w.limitations),
            event_type=event_type,
            note=None,
        )])


def _now() -> str:
    from .models import now_iso
    return now_iso()


def _version_note(prev: Optional[Worker], event_type: str) -> str:
    if prev is None:
        return "initial registration"
    return f"{event_type} (was v{prev.version})"


def ensure_runtime_bootstrapped(conn) -> int:
    """Idempotently seed the Worker Registry so a fresh database is immediately
    executable. Registers the built-in capability profiles and the external AI
    adapter manifests. Both paths are upserts on worker id, so re-running never
    duplicates rows (history/version rows are append-only and cheap).

    Fast + safe: only performs work when the registry is empty, but re-running
    after a partial registration still completes the set. Returns the number of
    workers present after bootstrapping.
    """
    reg = WorkerRegistry(conn)
    if reg.count() == 0:
        reg.register_builtins()
    # External AI adapter manifests are always reconciled (idempotent upsert),
    # so a built-ins-only DB still gains its AI executors on first execution.
    reg.register_external()
    return reg.count()


def _bump_version(v: str) -> str:
    """Deterministic semantic-version patch bump (no randomness)."""
    parts = (v or "1.0.0").split(".")
    nums = [p for p in parts if p.isdigit()]
    while len(nums) < 3:
        nums.append("0")
    nums = nums[:3]
    nums[2] = str(int(nums[2]) + 1)
    return ".".join(nums)


def _worker_from_manifest(m: dict) -> Worker:
    """Build a Worker from a JSON manifest. Manifest carries METADATA ONLY —
    no execution fields. Missing fields default deterministically."""
    name = (m.get("name") or "").strip()
    if not name:
        raise RegistryError("manifest missing required field: name")
    raw_kind = (m.get("kind") or "tool")
    kind = WorkerKind.from_str(raw_kind)
    return Worker(
        name=name,
        kind=kind,
        description=m.get("description", ""),
        capabilities=list(m.get("capabilities", []) or []),
        supported_languages=list(m.get("supported_languages", []) or []),
        supported_task_types=list(m.get("supported_task_types", []) or []),
        supported_plan_types=list(m.get("supported_plan_types", []) or []),
        limitations=list(m.get("limitations", []) or []),
        estimated_speed=m.get("estimated_speed", "unknown"),
        estimated_cost=m.get("estimated_cost", "unknown"),
        context_window=int(m.get("context_window", 0) or 0),
        parallelism=int(m.get("parallelism", 1) or 1),
        requires_network=bool(m.get("requires_network", False)),
        requires_filesystem=bool(m.get("requires_filesystem", False)),
        requires_git=bool(m.get("requires_git", False)),
        requires_python=bool(m.get("requires_python", False)),
        requires_shell=bool(m.get("requires_shell", False)),
        confidence=m.get("confidence", "medium"),
        version=m.get("version", "1.0.0"),
        status=m.get("status", "active"),
        id=m.get("worker_id") or m.get("id"),
    )


# ---------------------------------------------------------------------------
# Built-in workers (the spec's minimum deterministic set). Provider-agnostic:
# every built-in is a generic capability profile, not a special-cased provider.
# ---------------------------------------------------------------------------

def _builtin_worker(
    name, kind, description, capabilities, languages=(), task_types=(),
    plan_types=(), limitations=(), speed="unknown", cost="unknown",
    context_window=0, parallelism=1, network=False, filesystem=False,
    git=False, python=False, shell=False, confidence="medium",
    worker_id=None,
) -> Worker:
    return Worker(
        name=name, kind=WorkerKind.from_str(kind), description=description,
        capabilities=list(capabilities), supported_languages=list(languages),
        supported_task_types=list(task_types),
        supported_plan_types=list(plan_types),
        limitations=list(limitations), estimated_speed=speed,
        estimated_cost=cost, context_window=context_window,
        parallelism=parallelism, requires_network=network,
        requires_filesystem=filesystem, requires_git=git,
        requires_python=python, requires_shell=shell, confidence=confidence,
        id=worker_id,
    )


BUILTIN_WORKERS: List[Worker] = [
    _builtin_worker(
        "Claude", KIND_LLM, "Frontier reasoning + large-context coding LLM.",
        ["Reasoning", "Architecture", "Large Context", "Code Review",
         "Static Analysis", "Refactoring", "Documentation", "Research",
         "Planning", "Python", "TypeScript", "Rust", "SQL"],
        languages=["Python", "TypeScript", "Rust", "SQL", "Go", "JavaScript"],
        task_types=["analysis", "design", "implementation", "review",
                    "refactor", "documentation", "research", "verification",
                    "planning", "infrastructure"],
        plan_types=["feature", "architecture", "refactor", "research",
                    "infrastructure", "documentation", "testing"],
        limitations=["Huge repository rewrites", "Very long-running builds"],
        speed="medium", cost="high", context_window=200000,
        parallelism=1, network=True, filesystem=True, git=True, python=True,
        shell=True, confidence="high", worker_id="worker:claude llm",
    ),
    _builtin_worker(
        "Codex", KIND_LLM, "Code-specialized LLM for implementation tasks.",
        ["Python", "TypeScript", "Rust", "SQL", "Refactoring",
         "Static Analysis", "Code Review", "Testing", "Backend", "Frontend"],
        languages=["Python", "TypeScript", "Rust", "SQL", "Go", "JavaScript",
                   "C", "C++"],
        task_types=["implementation", "refactor", "testing", "review",
                    "verification", "migration"],
        plan_types=["feature", "bug_fix", "refactor", "migration", "testing"],
        limitations=["Large architectural redesign", "Open-ended research"],
        speed="fast", cost="medium", context_window=128000,
        parallelism=1, network=True, filesystem=True, git=True, python=True,
        shell=True, confidence="high", worker_id="worker:codex llm",
    ),
    _builtin_worker(
        "Gemini", KIND_LLM, "Long-context multimodal LLM.",
        ["Large Context", "Reasoning", "Research", "Documentation",
         "Architecture", "Python", "TypeScript", "SQL"],
        languages=["Python", "TypeScript", "SQL", "Java", "Go"],
        task_types=["analysis", "research", "documentation", "design",
                    "implementation", "verification"],
        plan_types=["research", "architecture", "feature", "documentation"],
        limitations=["Precise refactors of unfamiliar codebases"],
        speed="medium", cost="medium", context_window=1000000,
        parallelism=1, network=True, filesystem=True, git=True, python=True,
        shell=False, confidence="medium", worker_id="worker:gemini llm",
    ),
    _builtin_worker(
        "GPT", KIND_LLM, "General-purpose LLM for reasoning and drafting.",
        ["Reasoning", "Documentation", "Research", "Planning", "Python",
         "TypeScript"],
        languages=["Python", "TypeScript", "SQL", "JavaScript"],
        task_types=["analysis", "documentation", "research", "planning",
                    "design"],
        plan_types=["feature", "research", "documentation", "commercial"],
        limitations=["Large-scale code refactors", "Infrastructure changes"],
        speed="fast", cost="medium", context_window=128000,
        parallelism=1, network=True, filesystem=False, git=False,
        python=False, shell=False, confidence="medium", worker_id="worker:gpt llm",
    ),
    _builtin_worker(
        "OpenRouter", KIND_SERVICE,
        "Model-routing gateway to many remote LLMs.",
        ["Reasoning", "Python", "TypeScript", "SQL", "Large Context"],
        languages=["Python", "TypeScript", "SQL", "Go", "Rust"],
        task_types=["analysis", "implementation", "research", "design"],
        plan_types=["feature", "research", "architecture"],
        limitations=["Depends on upstream provider availability"],
        speed="medium", cost="low", context_window=200000,
        parallelism=4, network=True, filesystem=False, git=False,
        python=False, shell=False, confidence="medium",
        worker_id="worker:openrouter llm",
    ),
    _builtin_worker(
        "Python", KIND_FUNCTION,
        "Local Python runtime for scripts, analysis, and tooling.",
        ["Python", "Testing", "Static Analysis", "File Editing",
         "Benchmarking", "Backend"],
        languages=["Python"],
        task_types=["implementation", "testing", "configuration", "cleanup",
                    "verification", "analysis", "design"],
        plan_types=["feature", "bug_fix", "testing", "migration",
                    "optimization", "maintenance", "architecture"],
        limitations=["Documentation generation", "Architecture reasoning"],
        speed="fast", cost="low", context_window=0, parallelism=1,
        network=False, filesystem=True, git=False, python=True, shell=False,
        confidence="high",
    ),
    _builtin_worker(
        "Shell", KIND_CLI, "Local shell for command execution and ops.",
        ["Shell Commands", "Git Operations", "Infrastructure", "Configuration",
         "File Editing", "Research", "Architecture"],
        languages=["Shell", "Bash"],
        task_types=["infrastructure", "configuration", "deployment",
                    "cleanup", "implementation", "analysis", "design"],
        plan_types=["infrastructure", "release", "migration", "maintenance",
                    "architecture", "research"],
        limitations=["Code generation", "Comprehension of unfamiliar code"],
        speed="fast", cost="low", context_window=0, parallelism=1,
        network=False, filesystem=True, git=True, python=False, shell=True,
        confidence="high",
    ),
    _builtin_worker(
        "Git", KIND_TOOL, "Version-control operations across repositories.",
        ["Git Operations", "Infrastructure", "File Editing"],
        languages=[], task_types=["infrastructure", "migration", "cleanup",
                                  "configuration"],
        plan_types=["migration", "release", "maintenance", "infrastructure"],
        limitations=["Non-VCS filesystem changes", "Code comprehension"],
        speed="fast", cost="low", context_window=0, parallelism=1,
        network=False, filesystem=True, git=True, python=False, shell=True,
        confidence="high",
    ),
    _builtin_worker(
        "Filesystem", KIND_TOOL, "Local file read/write/move operations.",
        ["File Editing"],
        languages=[], task_types=["implementation", "cleanup", "configuration",
                                  "documentation"],
        plan_types=["feature", "bug_fix", "refactor", "maintenance"],
        limitations=["No network access", "No execution semantics"],
        speed="fast", cost="low", context_window=0, parallelism=1,
        network=False, filesystem=True, git=False, python=False, shell=False,
        confidence="high",
    ),
    _builtin_worker(
        "Search", KIND_SERVICE,
        "Codebase / web search across repositories and the internet.",
        ["Research", "Static Analysis", "Code Review"],
        languages=[], task_types=["analysis", "research", "review"],
        plan_types=["research", "architecture", "feature"],
        limitations=["No write access", "No execution"],
        speed="fast", cost="low", context_window=0, parallelism=1,
        network=True, filesystem=False, git=False, python=False, shell=False,
        confidence="medium", worker_id="worker:search llm",
    ),
    _builtin_worker(
        "Local LLM", KIND_LLM, "Self-hosted model for private/offline work.",
        ["Reasoning", "Python", "Documentation", "Research"],
        languages=["Python", "TypeScript"],
        task_types=["analysis", "documentation", "research", "implementation"],
        plan_types=["feature", "research", "documentation"],
        limitations=["Smaller context", "Weaker code competence"],
        speed="slow", cost="low", context_window=16000,
        parallelism=1, network=False, filesystem=True, git=True, python=True,
        shell=False, confidence="medium", worker_id="worker:local llm",
    ),
    # --- Execution workers (real adapters in runtime/workers.py) ---------
    # Kept out of the LLM cluster so the Capability Resolver ranks them first
    # for their capability (fast/low). `worker:python` also carries "Testing"
    # and is the Resolver's pick for Testing tasks (see test_resolver).
    _builtin_worker(
        "Documentation", KIND_FUNCTION,
        "Writes README / Markdown / architecture / changelog docs locally.",
        ["Documentation"],
        languages=["Markdown"],
        task_types=["documentation", "implementation", "cleanup"],
        plan_types=["documentation", "feature", "maintenance"],
        limitations=["No code execution", "No repo analysis"],
        speed="fast", cost="low", context_window=0, parallelism=1,
        network=False, filesystem=True, git=False, python=False, shell=False,
        confidence="high",
    ),
    _builtin_worker(
        "Testing", KIND_FUNCTION,
        "Runs the test framework (pytest) and reports results locally.",
        ["Testing", "Python", "Static Analysis"],
        languages=["Python"],
        task_types=["testing", "verification", "implementation", "cleanup"],
        plan_types=["testing", "feature", "bug_fix", "maintenance"],
        limitations=["No production deployment", "No code generation"],
        speed="fast", cost="low", context_window=0, parallelism=1,
        network=False, filesystem=True, git=False, python=True, shell=False,
        confidence="high",
    ),
]


_EXTERNAL_MANIFESTS = [
    {"worker_id": "worker:claude", "name": "Claude Code", "implementation": "cli",
     "provider": "anthropic", "origin": "external",
     "capabilities": ["Refactoring", "Documentation", "Architecture Review",
                      "Testing", "Frontend", "Backend", "Reasoning", "Research",
                      "Architecture", "Planning", "Python", "Infrastructure",
                      "Configuration"],
     "requirements": ["claude"],
     "supported_task_types": ["refactor", "documentation", "review", "testing",
                              "implementation", "analysis", "design",
                              "research", "verification", "planning",
                              "infrastructure", "configuration", "deployment"],
     "supported_plan_types": ["feature", "architecture", "refactor",
                               "research", "documentation", "infrastructure"],
     "estimated_speed": "fast", "estimated_cost": "medium",
     "confidence": "high"},
    {"worker_id": "worker:codex", "name": "Codex CLI", "implementation": "cli",
     "provider": "openai", "origin": "external",
     "capabilities": ["Refactoring", "Testing", "Reasoning", "Research",
                      "Architecture", "Frontend", "Backend", "Python",
                      "Infrastructure", "Configuration"],
     "requirements": ["codex"],
     "supported_task_types": ["refactor", "testing", "implementation", "analysis",
                              "design", "research", "verification",
                              "infrastructure", "configuration", "deployment"],
     "supported_plan_types": ["feature", "architecture", "refactor",
                              "infrastructure"]},
    {"worker_id": "worker:gemini", "name": "Gemini CLI", "implementation": "cli",
     "provider": "google", "origin": "external",
     "capabilities": ["Research", "Large Context", "Reasoning", "Architecture",
                      "Documentation"], "requirements": ["gemini"],
     "supported_task_types": ["research", "analysis", "design",
                              "documentation"],
     "supported_plan_types": ["research", "architecture", "feature"]},
    {"worker_id": "worker:opencode", "name": "OpenCode", "implementation": "cli",
     "provider": "local", "origin": "external",
     "capabilities": ["Refactoring", "Reasoning", "Architecture"],
     "requirements": ["opencode"],
     "supported_task_types": ["refactor", "implementation", "analysis", "design"],
     "supported_plan_types": ["feature", "architecture", "refactor"]},
    {"worker_id": "worker:aider", "name": "Aider", "implementation": "cli",
     "provider": "local", "origin": "external",
     "capabilities": ["Refactoring", "Documentation"], "requirements": ["aider"],
     "supported_task_types": ["refactor", "documentation"], "supported_plan_types": ["feature"]},
    {"worker_id": "worker:deepseek", "name": "DeepSeek", "implementation": "api",
     "provider": "deepseek", "origin": "external",
     "capabilities": ["Reasoning"], "requirements": ["DEEPSEEK_API_KEY"],
     "supported_task_types": ["research"], "supported_plan_types": ["research"]},
]
