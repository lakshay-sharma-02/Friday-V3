"""Initiative derivation rules (Milestone 8.4).

Transforms accumulated UNDERSTANDING (plus knowledge-evolution events and
knowledge) into durable engineering INITIATIVES. This layer NEVER reads
observations, context, git, READMEs, or repositories directly. It NEVER calls
an LLM. Every candidate cites the understanding ids (and knowledge ids) that
produced it, so an initiative is fully traceable to lower layers.

Two detector families:
  - per-understanding detectors: one initiative per (semantic title, type),
    driven by the understanding's TYPE plus the knowledge TYPES that back it
    (cross-source reinforcement). Titles are SEMANTIC, never repository names.
  - global detectors: require relating multiple understandings/subjects
    (platform direction, multi-project convergence, shared infrastructure).

Confidence is computed by the engine from the cited understanding/knowledge
(see confidence.py) — detectors only decide *whether* a thesis is supported
and *which* evidence backs it.

A shared lexicon maps subjects/knowledge text to SEMANTIC initiative titles
(e.g. "auth", "oauth", "login" -> "Authentication Infrastructure"; "rust",
"kernel", "filesystem" -> "Systems Infrastructure") so initiatives stay stable
across repository renames/splits.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from .models import InitiativeType


# ---------------------------------------------------------------------------
# Semantic title resolution — the heart of "initiatives are not repositories".
# Map raw understanding subjects / knowledge text to stable initiative titles.
# ---------------------------------------------------------------------------

# (keyword -> semantic title, initiative type) ; first match wins, ordered.
_SEMANTIC_MAP = [
    # Authentication / identity
    ({"auth", "oauth", "login", "authentication", "sso", "session", "jwt",
      "credential", "token", "identity", "password"},
     "Authentication Infrastructure", InitiativeType.INFRASTRUCTURE),
    # AI routing / reasoning
    ({"router", "routing", "llm", "ai routing", "agent", "planner",
      "reasoning", "model selection"},
     "AI Routing", InitiativeType.ARCHITECTURE),
    # Knowledge
    ({"knowledge", "knowledge engine", "knowledge base", "memory", "recall"},
     "Knowledge Evolution", InitiativeType.PLATFORM),
    # Systems / low-level
    ({"rust", "kernel", "filesystem", "operating system", "embedded",
      "firmware", "runtime", "compiler", "driver", "scheduler", "memory"},
     "Systems Infrastructure", InitiativeType.INFRASTRUCTURE),
    # Frontend / UI
    ({"frontend", "ui", "react", "vue", "component", "css", "design system"},
     "Frontend Experience", InitiativeType.FEATURE),
    # Backend / API
    ({"backend", "api", "server", "service", "endpoint", "grpc", "rest"},
     "Backend Services", InitiativeType.INFRASTRUCTURE),
    # Database / storage
    ({"database", "db", "sql", "postgres", "storage", "schema", "migration"},
     "Data Layer", InitiativeType.MIGRATION),
    # Testing / quality
    ({"test", "testing", "ci", "coverage", "qa", "e2e", "regression"},
     "Test Assurance", InitiativeType.TESTING),
    # Documentation
    ({"documentation", "docs", "readme", "guide", "manual"},
     "Documentation", InitiativeType.DOCUMENTATION),
    # Deployment / release
    ({"deploy", "deployment", "release", "ship", "rollout", "docker",
      "kubernetes", "ci/cd"},
     "Deployment & Release", InitiativeType.DEPLOYMENT),
    # Commercial / product
    ({"commercial", "business", "product", "market", "revenue", "customer",
      "saas", "monetiz"},
     "Commercial Engineering", InitiativeType.COMMERCIAL),
    # Platform / ecosystem
    ({"platform", "ecosystem", "integration", "interoperab", "sdk"},
     "Platform Integration", InitiativeType.INTEGRATION),
]

# Understanding TYPE -> semantic initiative type (when no lexicon hit).
_UTYPE_TO_ITYPE = {
    "engineering_direction": InitiativeType.ARCHITECTURE,
    "engineering_philosophy": InitiativeType.ARCHITECTURE,
    "skill_development": InitiativeType.LEARNING,
    "technology_preference": InitiativeType.OPTIMIZATION,
    "technology_shift": InitiativeType.MIGRATION,
    "project_convergence": InitiativeType.INTEGRATION,
    "project_divergence": InitiativeType.REFACTOR,
    "emerging_expertise": InitiativeType.LEARNING,
    "architectural_style": InitiativeType.ARCHITECTURE,
    "engineering_identity": InitiativeType.PLATFORM,
    "long_term_initiative": InitiativeType.PLATFORM,
    "investment_trend": InitiativeType.PLATFORM,
    "commercial_direction": InitiativeType.COMMERCIAL,
    "research_direction": InitiativeType.RESEARCH,
    "engineering_habit": InitiativeType.AUTOMATION,
    "engineering_risk": InitiativeType.MAINTENANCE,
    "engineering_opportunity": InitiativeType.FEATURE,
    "engineering_blind_spot": InitiativeType.MAINTENANCE,
    "engineering_strength": InitiativeType.OPTIMIZATION,
    "engineering_weakness": InitiativeType.MAINTENANCE,
}


def _resolve_title(subject: str, text: str):
    """Return (semantic_title, initiative_type) for a subject + backing text."""
    low = (subject + " " + text).lower()
    for kws, title, itype in _SEMANTIC_MAP:
        if any(kw in low for kw in kws):
            return title, itype
    return None, None


@dataclass
class Candidate:
    """A tentative initiative before confidence aggregation + lifecycle.

    `understanding_ids` / `knowledge_ids` are the full backing sets. `repos` is
    the set of participating repositories (from the backing knowledge's evidence
    provenance, never from direct repo parsing). The engine merges candidates
    with the same (type, title) and unions their ids/repos.
    """

    type: InitiativeType
    title: str
    statement: str = ""
    understanding_ids: List[str] = field(default_factory=list)
    knowledge_ids: List[str] = field(default_factory=list)
    repos: List[str] = field(default_factory=list)

    def key(self) -> tuple:
        return (self.type, self.title)


@dataclass
class _UnderstandingIndex:
    """All understanding that shares one (normalized) subject, with knowledge."""

    subject: str
    understanding: List = field(default_factory=list)  # list[Understanding]
    knowledge: List = field(default_factory=list)  # list[Knowledge]

    @property
    def text(self) -> str:
        bits = [self.subject]
        bits += [u.statement for u in self.understanding]
        bits += [k.statement for k in self.knowledge]
        return " ".join(bits).lower()

    @property
    def repo_set(self) -> Set[str]:
        out: Set[str] = set()
        for k in self.knowledge:
            out |= {r for r in (getattr(k, "evidence_ids", []) or []) if r}
        return out


def detect(
    understanding: List,
    knowledge: List,
    evolution_events: Optional[List] = None,
) -> List[Candidate]:
    """Run every detector. Returns candidate initiatives (pre-confidence)."""
    if evolution_events is None:
        evolution_events = []

    # Index knowledge by subject for cross-reference.
    know_by_subj: Dict[str, List] = {}
    for k in knowledge:
        key = (k.subject or "").strip().lower()
        if key:
            know_by_subj.setdefault(key, []).append(k)

    # Index understanding by subject.
    und_by_subj: Dict[str, List] = {}
    for u in understanding:
        key = (u.subject or "").strip().lower()
        if key:
            und_by_subj.setdefault(key, []).append(u)

    candidates: List[Candidate] = []

    # --- per-understanding detectors (semantic titles) ----------------------
    for subj in sorted(und_by_subj):
        idx_und = und_by_subj[subj]
        idx_know = know_by_subj.get(subj, [])
        candidates.extend(_per_understanding(subj, idx_und, idx_know))

    # --- global (multi-subject) detectors ----------------------------------
    # Cross-project span is captured per-subject via repo_count; the platform
    # and shared-infrastructure detectors below combine MULTIPLE subjects.
    candidates.extend(_platform_direction(und_by_subj, know_by_subj))
    candidates.extend(_shared_infrastructure(und_by_subj, know_by_subj))

    return candidates


def _per_understanding(subj: str, und: List, know: List) -> List[Candidate]:
    out: List[Candidate] = []
    und_ids = [u.id for u in und if u.id]
    know_ids = [k.id for k in know if k.id]
    repos = sorted({r for k in know for r in (getattr(k, "evidence_ids", []) or []) if r})

    if not und_ids:
        return out

    text = (subj + " " + " ".join(u.statement for u in und)
            + " " + " ".join(k.statement for k in know))

    title, itype = _resolve_title(subj, text)
    if title is None:
        # Deterministic fallback: semantic title from the subject only (never a repo
        # name, never the understanding type — keeps titles stable across builds).
        primary = und[0]
        itype = _UTYPE_TO_ITYPE.get(primary.type.value, InitiativeType.FEATURE)
        title = f"{subj.title()} Engineering Initiative"

    out.append(Candidate(
        type=itype,
        title=title,
        statement=f"{title}: a long-running engineering effort indicated by "
                  f"{len(und_ids)} understanding(s) and {len(know_ids)} knowledge.",
        understanding_ids=und_ids,
        knowledge_ids=know_ids,
        repos=repos,
    ))
    return out


def _platform_direction(
    und_by_subj: Dict[str, List], know_by_subj: Dict[str, List]
) -> List[Candidate]:
    """One platform initiative when several subjects converge into an ecosystem."""
    convergence = [
        s for s, us in und_by_subj.items()
        if any(u.type.value == "project_convergence" for u in us)
    ]
    if len(convergence) < 2:
        return []
    und_ids = [u.id for s in convergence for u in und_by_subj[s] if u.id]
    know_ids = [k.id for s in convergence for k in know_by_subj.get(s, []) if k.id]
    repos = sorted({
        r for s in convergence for k in know_by_subj.get(s, [])
        for r in (getattr(k, "evidence_ids", []) or []) if r
    })
    return [Candidate(
        type=InitiativeType.PLATFORM,
        title="Engineering Platform",
        statement="Multiple converging engineering efforts form a single platform "
                  "initiative spanning several areas of the workspace.",
        understanding_ids=und_ids,
        knowledge_ids=know_ids,
        repos=repos,
    )]


def _shared_infrastructure(
    und_by_subj: Dict[str, List], know_by_subj: Dict[str, List]
) -> List[Candidate]:
    """Shared infrastructure initiative when several systems subjects co-occur."""
    systems_subjects = [
        s for s in und_by_subj
        if any(kw in s for kw in
               ("rust", "kernel", "filesystem", "systems", "runtime", "compiler"))
    ]
    if len(systems_subjects) < 2:
        return []
    und_ids = [u.id for s in systems_subjects for u in und_by_subj[s] if u.id]
    know_ids = [k.id for s in systems_subjects for k in know_by_subj.get(s, []) if k.id]
    repos = sorted({
        r for s in systems_subjects for k in know_by_subj.get(s, [])
        for r in (getattr(k, "evidence_ids", []) or []) if r
    })
    return [Candidate(
        type=InitiativeType.INFRASTRUCTURE,
        title="Systems Infrastructure",
        statement="Recurring systems-level effort (rust, kernel, filesystem) forms "
                  "a shared infrastructure initiative.",
        understanding_ids=und_ids,
        knowledge_ids=know_ids,
        repos=repos,
    )]
