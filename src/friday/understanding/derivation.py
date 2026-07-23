"""Understanding derivation rules (Milestone 8.3) — LLM-grounded rewrite.

Transforms accumulated KNOWLEDGE (plus knowledge-evolution events) into durable
engineering UNDERSTANDING via LLM synthesis over real evidence. Replaces the old
template-substitution system (11 templates × N subjects = 103 formulaic entries).

Design (mirrors synthesis.py):
  - Deterministic type detection: same knowledge-type → UnderstandingType mapping
    as the old system, so the (type, subject) dedup key stays stable and builds
    remain idempotent.
  - LLM statement generation: the actual natural-language statement is produced
    by the LLM from the raw knowledge evidence. The LLM can also decide "nothing
    notable" for a given (type, subject), which means we skip emitting an entry
    rather than manufacturing one to fill a category.
  - No LLM = zero entries. The early return in detect() skips all detectors
    (per-subject, content-based, and global) when no LLM is configured. The
    engine surfaces the "LLM not configured" status in its build output.
  - LLM failure during generation = skip that type, never fallback filler.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from ..services.llm import _call as llm_call
from ..services.llm import _enabled as llm_enabled
from .models import UnderstandingType

# Knowledge TYPE values we read. Imported lazily to avoid a hard dependency
# cycle at module import; we compare by string value only.
_KT = {
    "trend": "engineering_trend",
    "habit": "engineering_habit",
    "interest": "engineering_interest",
    "rel": "project_relationship",
    "evol": "project_evolution",
    "pref": "engineering_preference",
    "pattern": "recurring_pattern",
    "bottleneck": "recurring_bottleneck",
    "invest": "technology_investment",
    "direction": "stable_direction",
    "pidentity": "project_identity",
    "parch": "project_architecture",
    "pstack": "project_stack",
    "ptech": "portfolio_technology",
    "pinteg": "portfolio_integration",
}


@dataclass
class Candidate:
    """A tentative understanding before confidence aggregation.

    `knowledge_ids` is the full set of backing knowledge. The engine merges
    candidates with the same (type, subject) and unions their knowledge ids.
    """

    type: UnderstandingType
    subject: str
    statement: str
    knowledge_ids: List[str] = field(default_factory=list)

    def key(self) -> tuple:
        return (self.type, self.subject)


@dataclass
class _SubjectIndex:
    """All knowledge that shares one (normalized) subject."""

    subject: str
    knowledge: List = field(default_factory=list)  # list[Knowledge]

    @property
    def types(self) -> Set[str]:
        return {k.type.value for k in self.knowledge}

    def by_type(self, t: str) -> List:
        return [k for k in self.knowledge if k.type.value == t]

    def has(self, *types: str) -> bool:
        return bool(self.types & set(types))


# ---------------------------------------------------------------------------
# Deterministic type detector — maps knowledge type combinations to the
# UnderstandingTypes they support. Same logic as the old _per_subject but
# only used for type detection, never for statement generation.
# ---------------------------------------------------------------------------


def _detect_types(idx: _SubjectIndex) -> List[UnderstandingType]:
    """Detect which UnderstandingTypes are supported by this subject's knowledge.

    Fully deterministic. Same mapping as the old system but without
    emitting template strings — this only determines *which* types fire.
    """
    out: List[UnderstandingType] = []

    # ENGINEERING_DIRECTION — an area that is both invested in AND trending/directed.
    if idx.has(_KT["invest"], _KT["direction"]) and (
        idx.has(_KT["trend"], _KT["direction"])
    ):
        out.append(UnderstandingType.ENGINEERING_DIRECTION)

    # TECHNOLOGY_PREFERENCE — explicit preference or strong investment.
    if idx.has(_KT["pref"]) or (
        idx.has(_KT["invest"])
        and any(k.confidence.value == "strong" for k in idx.by_type(_KT["invest"]))
    ):
        out.append(UnderstandingType.TECHNOLOGY_PREFERENCE)

    # EMERGING_EXPERTISE — strong/growing investment or increasing trend.
    if idx.has(_KT["invest"], _KT["trend"]):
        out.append(UnderstandingType.EMERGING_EXPERTISE)

    # SKILL_DEVELOPMENT — investment signals skill building (not just usage).
    if idx.has(_KT["invest"]):
        out.append(UnderstandingType.SKILL_DEVELOPMENT)

    # ENGINEERING_PHILOSOPHY — preference or recurring pattern of approach.
    if idx.has(_KT["pref"], _KT["pattern"]):
        out.append(UnderstandingType.ENGINEERING_PHILOSOPHY)

    # ARCHITECTURAL_STYLE — architecture knowledge.
    if idx.has(_KT["parch"], _KT["pstack"]):
        out.append(UnderstandingType.ARCHITECTURAL_STYLE)

    # ENGINEERING_IDENTITY — project identity / stable direction self-view.
    if idx.has(_KT["pidentity"], _KT["direction"]):
        out.append(UnderstandingType.ENGINEERING_IDENTITY)

    # LONG_TERM_INITIATIVE — sustained direction across evolution.
    if idx.has(_KT["direction"]) and idx.has(_KT["evol"], _KT["invest"]):
        out.append(UnderstandingType.LONG_TERM_INITIATIVE)

    # ENGINEERING_HABIT — explicit habit knowledge.
    if idx.has(_KT["habit"]):
        out.append(UnderstandingType.ENGINEERING_HABIT)

    # ENGINEERING_STRENGTH — strong, stable knowledge marks a real strength.
    if any(k.confidence.value == "strong" for k in idx.knowledge):
        out.append(UnderstandingType.ENGINEERING_STRENGTH)

    # ENGINEERING_WEAKNESS — weak knowledge / recurring bottleneck.
    if idx.has(_KT["bottleneck"]) or all(
        k.confidence.value == "weak" for k in idx.knowledge
    ):
        out.append(UnderstandingType.ENGINEERING_WEAKNESS)

    # ENGINEERING_RISK — recurring bottlenecks, or contradicted effort.
    if idx.has(_KT["bottleneck"]):
        out.append(UnderstandingType.ENGINEERING_RISK)

    return out


# ---------------------------------------------------------------------------
# LLM system prompt for understanding statement generation
# ---------------------------------------------------------------------------

_UNDERSTANDING_SYSTEM = (
    "You are Friday's understanding layer. Your job is to look at a set of "
    "engineering knowledge statements about ONE subject (a repository, "
    "technology, or concept) and determine what is specifically notable about "
    "that subject for each applicable understanding category.\n\n"
    "Rules:\n"
    "1. Base your analysis ONLY on the knowledge statements provided below.\n"
    "2. For each applicable understanding type, produce ONE sentence that is "
    "specific to THIS subject — referencing what the evidence actually shows.\n"
    '3. If there is nothing notable to say for a given type, return "skip" '
    "for that type — do not manufacture a finding.\n"
    "4. An honest 'nothing notable' produces a better user experience than "
    "a generic statement that could apply to any project.\n"
    "5. Each statement must be a single sentence. Be specific: mention repo "
    "names, technologies, or patterns the evidence actually supports.\n\n"
    "Return valid JSON only:\n"
    '{"findings": [{"type": str, "statement": str|null, "skip": bool}]}\n\n'
    "  type: one of the applicable type strings provided below\n"
    "  statement: a single, specific sentence or null if skip is true\n"
    "  skip: true if nothing notable to say for this type\n\n"
    'Example: {"findings": [{"type": "architectural_style", "statement": '
    '"The architectural style of vivaha is stabilizing on a Next.js + Supabase '
    "stack, with 5 knowledge items confirming the pattern.\", \"skip\": false}]}"
)

_USER_TEMPLATE = """Knowledge evidence for "{subject}" ({type_count} applicable type(s)):

{knowledge_lines}

Applicable understanding types: {type_list}

For each type in the list above, determine if the evidence supports a specific,
non-obvious finding. If yes, produce one sentence. If the evidence is too thin
or the finding would be generic, set skip=true.
"""


# ---------------------------------------------------------------------------
# LLM call with JSON parsing — same pattern as synthesis.py
# ---------------------------------------------------------------------------


def _call_llm_for_subject(
    subject: str,
    types: List[UnderstandingType],
    idx: _SubjectIndex,
) -> List[Candidate]:
    """Call the LLM with this subject's evidence, return candidates for types
    where the LLM found something notable. When no LLM is configured or the
    call fails, return [] — honest emptiness beats template filler."""
    if not types:
        return []

    if not llm_enabled():
        return []

    knowledge_lines = []
    for k in idx.knowledge:
        confidence = getattr(k, "confidence", None)
        conf_str = getattr(confidence, "value", "unknown") if confidence is not None else "unknown"
        ktype = getattr(k, "type", None)
        ktype_str = getattr(ktype, "value", "unknown") if ktype is not None else "unknown"
        statement = getattr(k, "statement", "") or ""
        knowledge_lines.append(f"  [{ktype_str}] ({conf_str}) {k.subject}: {statement}")

    type_list = ", ".join(t.value for t in types)

    user = _USER_TEMPLATE.format(
        subject=subject,
        type_count=len(types),
        knowledge_lines="\n".join(knowledge_lines),
        type_list=type_list,
    )

    content = llm_call(_UNDERSTANDING_SYSTEM, user)
    if not content:
        return []

    # Parse JSON
    content = content.strip()
    if content.startswith("```"):
        content = content.split("```", 2)[1]
        if content.startswith("json"):
            content = content[4:]
    content = content.strip().strip("`").strip()

    try:
        data = json.loads(content)
    except (json.JSONDecodeError, ValueError):
        return []

    findings = data.get("findings", []) if isinstance(data, dict) else []
    out: List[Candidate] = []
    all_ids = [k.id for k in idx.knowledge if k.id]
    seen_types: Set[str] = set()

    for f in findings:
        t_str = f.get("type", "")
        skip = f.get("skip", False)
        statement = f.get("statement")
        if skip or not statement:
            continue
        try:
            utype = UnderstandingType.from_str(t_str)
        except ValueError:
            continue
        if utype not in types:
            continue
        seen_types.add(t_str)
        out.append(Candidate(
            type=utype,
            subject=subject,
            statement=statement,
            knowledge_ids=all_ids,
        ))

    # If LLM skipped all types, do NOT emit fallback entries. The user
    # explicitly said "An empty result is more honest than a generic paragraph"
    # and this applies equally to "Insufficient signal" fillers. If the
    # evidence supports the type but the LLM found nothing notable, go with
    # the LLM's judgment — a subject with real signal but nothing notable
    # means the evidence hasn't converged into understanding yet.
    return out


# ---------------------------------------------------------------------------
# Content-based markers (deterministic, no LLM needed for these)
# ---------------------------------------------------------------------------


_COMMERCIAL = {
    "commercial", "business", "market", "revenue", "customer", "product",
    "saas", "startup", "client", "monetiz", "profit", "venture", "funding",
}
_RESEARCH = {
    "research", "experiment", "r&d", "paper", "study", "prototype",
    "investigation", "hypothesis", "novel", "academic", "thesis",
}


def _content_based_detection(idx: _SubjectIndex) -> List[Candidate]:
    """Keyword-based content detection for commercial/research direction.

    These are pure keyword matches and produce factual statements, not
    speculative templates. They're kept deterministic because the signal
    is unambiguous: the repo's knowledge says X is commercial/research.
    """
    out: List[Candidate] = []
    ids = [k.id for k in idx.knowledge if k.id]
    text = (idx.subject + " " + " ".join(
        k.statement for k in idx.knowledge
    )).lower()

    if any(w in text for w in _COMMERCIAL):
        out.append(Candidate(
            UnderstandingType.COMMERCIAL_DIRECTION,
            idx.subject,
            f"Commercial signals detected in {idx.subject}'s knowledge.",
            ids,
        ))

    if any(w in text for w in _RESEARCH):
        out.append(Candidate(
            UnderstandingType.RESEARCH_DIRECTION,
            idx.subject,
            f"Research signals detected in {idx.subject}'s knowledge.",
            ids,
        ))

    return out


# ---------------------------------------------------------------------------
# Global (multi-subject) detectors — these aggregate across subjects and
# remain deterministic (they don't benefit from per-subject LLM generation).
# The statement is still evidence-grounded.
# ---------------------------------------------------------------------------


def _investment_trend(index: Dict[str, _SubjectIndex]) -> List[Candidate]:
    """One understanding aggregating the set of invested subjects (>=2)."""
    invested = sorted([
        s for s, idx in index.items()
        if idx.has(_KT["invest"])
    ])
    if len(invested) < 2:
        return []
    ids = [k.id for s in invested for k in index[s].knowledge if k.id]
    subjects_str = ", ".join(invested)
    return [Candidate(
        UnderstandingType.INVESTMENT_TREND,
        "portfolio",
        f"Investment is concentrated across {len(invested)} subjects: {subjects_str}.",
        ids,
    )]


def _project_convergence(index: Dict[str, _SubjectIndex]) -> List[Candidate]:
    """Projects that relate / integrate / co-evolve — factual, not judgmental.

    Each entry lists the relation types present for the subject rather than
    applying a uniform "converging" narrative. Different subjects naturally
    get different descriptions because they have different relation evidence.
    """
    out: List[Candidate] = []
    for s, idx in index.items():
        present = [t for t in (_KT["rel"], _KT["pinteg"], _KT["evol"]) if idx.has(t)]
        if not present:
            continue
        ids = [k.id for k in idx.knowledge if k.id]
        labels = {
            _KT["rel"]: "relationships", _KT["pinteg"]: "integration",
            _KT["evol"]: "co-evolution",
        }
        type_labels = ", ".join(labels[t] for t in present)
        n = len(present)
        out.append(Candidate(
            UnderstandingType.PROJECT_CONVERGENCE,
            s,
            f"{s} has {n} cross-project connection type(s): {type_labels}.",
            ids,
        ))
    return out


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def detect(knowledge: List, evolution_events: Optional[List] = None) -> List[Candidate]:
    """Run every detector. Returns candidate understandings (pre-confidence).

    Per-subject understanding requires an LLM to produce specific statements.
    Without one, no entries are produced at all — the engine surfaces
    "understanding requires an LLM — none configured" as a build note.
    """
    if not llm_enabled():
        return []
    if evolution_events is None:
        evolution_events = []
    contradicted: Set[str] = set()
    for e in evolution_events:
        if e.event_type in ("Contradicted", "Weakened", "Retired", "Split"):
            if e.knowledge_id:
                contradicted.add(e.knowledge_id)

    candidates: List[Candidate] = []
    index = _build_index(knowledge)
    subjects = sorted(index.keys())

    # --- Per-subject LLM generation ------------------------------------------
    # For each subject, detect what understanding types apply, then ask the LLM
    # to generate specific statements from the evidence.
    # Contradicted/weakened knowledge adds ENGINEERING_RISK + ENGINEERING_BLIND_SPOT
    # as applicable types — the LLM (or fallback) assesses whether the contradiction
    # is genuinely notable.
    for subj in subjects:
        idx = index[subj]
        types = _detect_types(idx)
        # Add contradiction-driven understanding types when this subject's
        # knowledge has been contradicted/weakened/retired.
        if any(k.id in contradicted for k in idx.knowledge):
            if UnderstandingType.ENGINEERING_RISK not in types:
                types.append(UnderstandingType.ENGINEERING_RISK)
            if UnderstandingType.ENGINEERING_BLIND_SPOT not in types:
                types.append(UnderstandingType.ENGINEERING_BLIND_SPOT)
        if not types:
            continue
        candidates.extend(_call_llm_for_subject(subj, types, idx))

    # --- Content-based (keyword, deterministic) ------------------------------
    for subj in subjects:
        idx = index[subj]
        candidates.extend(_content_based_detection(idx))

    # --- Global (multi-subject) detectors ------------------------------------
    #
    # _investment_trend: retained — aggregates a simple count across subjects,
    # not a formulaic judgment about each subject's state.
    candidates.extend(_investment_trend(index))
    # _project_convergence: factual multi-project relation listing.
    # Subjects with relationship/integration/evolution evidence get a factual
    # entry naming the relation types present. This is NOT the old template
    # ("Projects touching X are converging into an ecosystem") — it's a
    # descriptive count of evidence, different per subject.
    candidates.extend(_project_convergence(index))

    return candidates


def _build_index(knowledge: List) -> Dict[str, _SubjectIndex]:
    out: Dict[str, _SubjectIndex] = {}
    for k in knowledge:
        key = (k.subject or "").strip().lower()
        if not key:
            continue
        out.setdefault(key, _SubjectIndex(subject=key)).knowledge.append(k)
    return out
