"""Worker Registry models (Milestone 9.2).

A Worker is a CAPABILITY PROFILE — metadata only. NOT a running process, NOT an
API request, NOT an execution. The model stores exactly the fields the spec
lists, plus a generic `kind` so provider-agnostic workers fit one schema.

Capabilities are validated against a CLOSED deterministic vocabulary. No
free-form capabilities are ever accepted. Languages, task types, and plan types
are validated against frozen sets mirrored from the Task Graph Compiler so the
catalog and the execution IR never drift.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional

from ..db import WorkerRow


@dataclass(frozen=True)
class WorkerManifest:
    """Immutable capability declaration. Single source of truth for a worker's
    static identity. The registry row is BUILT FROM a manifest at registration
    time; a worker never mutates its own manifest at runtime."""
    name: str
    implementation: str            # native|cli|api|mcp|plugin
    provider: str                  # anthropic|openai|google|deepseek|local|friday
    origin: str                    # builtin|external|generated
    capabilities: list
    requirements: list             # PATH binaries or env vars the worker needs
    supported_task_types: list
    supported_plan_types: list
    supported_languages: list = field(default_factory=list)
    description: str = ""
    supports_workspace: bool = False
    supports_streaming: bool = False
    supports_files: bool = False
    supports_patch: bool = False
    estimated_speed: str = "unknown"
    estimated_cost: str = "unknown"
    confidence: str = "medium"
    version: str = "1.0.0"

    def __post_init__(self):
        # Closed vocabulary: capabilities must be registry-valid canonical forms.
        object.__setattr__(self, "capabilities", validate_capabilities(self.capabilities))


@dataclass
class VerificationResult:
    """Objective correctness verdict from a worker's verify() step."""
    passed: bool
    reason: str = ""


# Contract version (Law 24).
SCHEMA_VERSION = "1.0"


# ---------------------------------------------------------------------------
# Worker kinds — provider-agnostic categories. Every worker is one of these.
# ---------------------------------------------------------------------------

class WorkerKind(str, Enum):
    """Generic worker categories. No provider is special-cased."""

    LLM = "llm"
    CLI = "cli"
    FUNCTION = "function"
    AGENT = "agent"
    TOOL = "tool"
    SERVICE = "service"

    @classmethod
    def from_str(cls, s: str) -> "WorkerKind":
        s = (s or "").strip().lower()
        for k in cls:
            if k.value == s:
                return k
        raise ValueError(f"{cls.__name__} has no member {s!r}")

    @classmethod
    def all(cls) -> tuple:
        return tuple(k.value for k in cls)


KIND_LLM = WorkerKind.LLM.value
KIND_CLI = WorkerKind.CLI.value
KIND_FUNCTION = WorkerKind.FUNCTION.value
KIND_AGENT = WorkerKind.AGENT.value
KIND_TOOL = WorkerKind.TOOL.value
KIND_SERVICE = WorkerKind.SERVICE.value


# ---------------------------------------------------------------------------
# Closed deterministic capability vocabulary (spec examples, expanded).
# NO free-form capabilities. Mirrors the Task Graph Compiler's capability set
# and the spec's explicit list.
#
# Canonical forms are Capitalized (the spec's style: "Rust", "Python"). Each
# canonical form maps from its lowercase alias. A precomputed LOWER->CANON dict
# makes normalization DETERMINISTIC (frozenset iteration is not).
# ---------------------------------------------------------------------------

_CAP_CANON = (
    "Rust", "Python", "TypeScript", "SQL", "Architecture", "Frontend",
    "Backend", "Infrastructure", "Testing", "Documentation", "Migration",
    "Refactoring", "Research", "Planning", "Configuration", "Benchmarking",
    "Static Analysis", "Code Review", "Reasoning", "Large Context",
    "Long Running", "File Editing", "Git Operations", "Shell Commands",
)

# Lowercase alias -> canonical (Capitalized) form. Deterministic single source.
_CANON_MAP = {c.lower(): c for c in _CAP_CANON}

# Informal synonyms users register with, mapped to the canonical capability
# name (NO new canonical caps added, validation stays closed). Without these,
# common names like "Shell"/"Git"/"File System" were rejected entirely.
_ALIASES = {
    "shell": "Shell Commands",
    "git": "Git Operations",
    "file system": "File Editing",
    "filesystem": "File Editing",
    "file editing": "File Editing",
    "shell commands": "Shell Commands",
    "git operations": "Git Operations",
}


def all_capabilities() -> tuple:
    """The full closed capability vocabulary (canonical forms)."""
    return _CAP_CANON


def is_valid_capability(c: str) -> bool:
    """True iff `c` is in the closed vocabulary (case-insensitive)."""
    c = (c or "").strip().lower()
    return c in _CANON_MAP or c in _ALIASES


def validate_capabilities(caps: List[str]) -> List[str]:
    """Return only the canonical-valid capabilities (deduped, order-preserving).
    Unknown capabilities are rejected (never stored, never silently kept)."""
    seen = set()
    out: List[str] = []
    for c in caps:
        c = (c or "").strip()
        if not c:
            continue
        key = c.lower()
        canon = _CANON_MAP.get(key) or _ALIASES.get(key)
        if canon is None:
            continue  # rejected: no hallucinated capabilities
        if canon not in seen:
            seen.add(canon)
            out.append(canon)
    return out


# ---------------------------------------------------------------------------
# Closed language / task-type / plan-type vocabularies (validation only).
# ---------------------------------------------------------------------------

_LANGUAGES = (
    "Rust", "Python", "TypeScript", "JavaScript", "SQL", "Go", "Java",
    "C", "C++", "Ruby", "Shell", "Bash", "HTML", "CSS", "Kotlin", "Swift",
)
_LANG_SET = frozenset(x.lower() for x in _LANGUAGES)


def is_valid_language(l: str) -> bool:
    return (l or "").strip().lower() in _LANG_SET


def validate_languages(items: List[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for x in items:
        x = (x or "").strip()
        if x and x.lower() in _LANG_SET and x not in seen:
            seen.add(x)
            out.append(x)
    return out


# Task types mirror the Task Graph Compiler's frozen TaskType vocabulary.
_TASK_TYPES = (
    "analysis", "design", "implementation", "testing", "documentation",
    "migration", "review", "refactor", "infrastructure", "research",
    "verification", "deployment", "configuration", "cleanup", "planning",
)
_TASK_SET = frozenset(_TASK_TYPES)


def is_valid_task_type(t: str) -> bool:
    return (t or "").strip().lower() in _TASK_SET


def validate_task_types(items: List[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for x in items:
        x = (x or "").strip().lower()
        if x and x in _TASK_SET and x not in seen:
            seen.add(x)
            out.append(x)
    return out


# Plan types mirror PlanType.value (M9.0, FROZEN).
_PLAN_TYPES = (
    "feature", "bug_fix", "research", "migration", "refactor", "architecture",
    "infrastructure", "optimization", "release", "maintenance",
    "documentation", "testing", "learning", "integration", "commercial",
)
_PLAN_SET = frozenset(_PLAN_TYPES)


def is_valid_plan_type(p: str) -> bool:
    return (p or "").strip().lower() in _PLAN_SET


def validate_plan_types(items: List[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for x in items:
        x = (x or "").strip().lower()
        if x and x in _PLAN_SET and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Worker:
    """One worker capability profile. Metadata only — NEVER an execution.

    `id` is deterministic (`worker:<name>`), stable across re-registration so a
    re-register REPLACES the same row (idempotent) and version history accrues.
    """

    SCHEMA_VERSION = SCHEMA_VERSION

    name: str
    kind: WorkerKind
    description: str = ""
    capabilities: List[str] = field(default_factory=list)
    supported_languages: List[str] = field(default_factory=list)
    supported_task_types: List[str] = field(default_factory=list)
    supported_plan_types: List[str] = field(default_factory=list)
    limitations: List[str] = field(default_factory=list)
    estimated_speed: str = "unknown"          # e.g. 'fast' / 'medium' / 'slow'
    estimated_cost: str = "unknown"           # e.g. 'low' / 'medium' / 'high'
    context_window: int = 0
    parallelism: int = 1
    requires_network: bool = False
    requires_filesystem: bool = False
    requires_git: bool = False
    requires_python: bool = False
    requires_shell: bool = False
    confidence: str = "medium"                 # registry-assigned, deterministic
    version: str = "1.0.0"
    status: str = "active"                     # 'active' | 'disabled'
    availability: str = "available"            # available|unavailable|error
    manifest_ref: Optional[str] = None         # id of the WorkerManifest it was built from
    schema_version: str = SCHEMA_VERSION       # contract version (Law 24)
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    id: Optional[str] = None

    # --- structured helpers -------------------------------------------------

    def _generate_id(self) -> str:
        """Deterministic id from name. Stable across re-registration."""
        return f"worker:{self.name.strip().lower()}"

    def to_row(self) -> WorkerRow:
        return WorkerRow(
            id=self.id or self._generate_id(),
            name=self.name,
            kind=self.kind.value,
            description=self.description,
            capabilities=",".join(self.capabilities),
            supported_languages=",".join(self.supported_languages),
            supported_task_types=",".join(self.supported_task_types),
            supported_plan_types=",".join(self.supported_plan_types),
            limitations=",".join(self.limitations),
            estimated_speed=self.estimated_speed,
            estimated_cost=self.estimated_cost,
            context_window=self.context_window,
            parallelism=self.parallelism,
            requires_network=self.requires_network,
            requires_filesystem=self.requires_filesystem,
            requires_git=self.requires_git,
            requires_python=self.requires_python,
            requires_shell=self.requires_shell,
            confidence=self.confidence,
            version=self.version,
            status=self.status,
            schema_version=self.schema_version,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )

    @classmethod
    def from_row(cls, row: WorkerRow) -> "Worker":
        return cls(
            id=row.id,
            name=row.name,
            kind=WorkerKind.from_str(row.kind),
            description=row.description,
            capabilities=_split(row.capabilities),
            supported_languages=_split(row.supported_languages),
            supported_task_types=_split(row.supported_task_types),
            supported_plan_types=_split(row.supported_plan_types),
            limitations=_split(row.limitations),
            estimated_speed=row.estimated_speed or "unknown",
            estimated_cost=row.estimated_cost or "unknown",
            context_window=row.context_window or 0,
            parallelism=row.parallelism or 1,
            requires_network=bool(row.requires_network),
            requires_filesystem=bool(row.requires_filesystem),
            requires_git=bool(row.requires_git),
            requires_python=bool(row.requires_python),
            requires_shell=bool(row.requires_shell),
            confidence=row.confidence or "medium",
            version=row.version or "1.0.0",
            status=row.status or "active",
            schema_version=getattr(row, "schema_version", None) or SCHEMA_VERSION,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    def to_summary(self) -> str:
        """One-line registry summary (used by `friday workers`)."""
        caps = ", ".join(self.capabilities) or "-"
        return (f"  {self.name} ({self.kind.value}, {self.status}) "
                f"v{self.version} | caps: {caps}")

    def to_detail(self) -> str:
        """Full human-readable capability profile (`friday worker <name>`)."""
        def line(label, items):
            if isinstance(items, list):
                val = ", ".join(items) or "-"
            else:
                val = str(items)
            return f"  {label:18}: {val}"

        lines = [
            f"Worker: {self.name}",
            line("ID", self.id or self._generate_id()),
            line("Kind", self.kind.value),
            line("Status", self.status),
            line("Version", self.version),
            line("Confidence", self.confidence),
            line("Description", self.description or "-"),
            "",
            "Capabilities",
            "  " + (", ".join(self.capabilities) or "-"),
            "",
            "Supported Languages",
            "  " + (", ".join(self.supported_languages) or "-"),
            "",
            "Supported Task Types",
            "  " + (", ".join(self.supported_task_types) or "-"),
            "",
            "Supported Plan Types",
            "  " + (", ".join(self.supported_plan_types) or "-"),
            "",
            "Limitations",
            "  " + ("; ".join(self.limitations) or "-"),
            "",
            line("Estimated Speed", self.estimated_speed),
            line("Estimated Cost", self.estimated_cost),
            line("Context Window", self.context_window or "-"),
            line("Parallelism", self.parallelism),
            "",
            "Requirements",
            line("Network", "yes" if self.requires_network else "no"),
            line("Filesystem", "yes" if self.requires_filesystem else "no"),
            line("Git", "yes" if self.requires_git else "no"),
            line("Python", "yes" if self.requires_python else "no"),
            line("Shell", "yes" if self.requires_shell else "no"),
        ]
        return "\n".join(lines)

    def to_json(self) -> dict:
        """Export this worker as a deterministic JSON object (no execution
        fields — metadata only, matching the manifest contract)."""
        return {
            "id": self.id or self._generate_id(),
            "name": self.name,
            "kind": self.kind.value,
            "description": self.description,
            "capabilities": list(self.capabilities),
            "supported_languages": list(self.supported_languages),
            "supported_task_types": list(self.supported_task_types),
            "supported_plan_types": list(self.supported_plan_types),
            "limitations": list(self.limitations),
            "estimated_speed": self.estimated_speed,
            "estimated_cost": self.estimated_cost,
            "context_window": self.context_window,
            "parallelism": self.parallelism,
            "requires_network": self.requires_network,
            "requires_filesystem": self.requires_filesystem,
            "requires_git": self.requires_git,
            "requires_python": self.requires_python,
            "requires_shell": self.requires_shell,
            "confidence": self.confidence,
            "version": self.version,
            "status": self.status,
            "schema_version": self.schema_version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def _split(s: str) -> List[str]:
    return [x for x in (s or "").split(",") if x]
