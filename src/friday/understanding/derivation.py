"""Understanding derivation rules (Milestone 8.3).

Transforms accumulated KNOWLEDGE (plus knowledge-evolution events) into durable
engineering UNDERSTANDING. This layer NEVER reads observations, context, git, or
READMEs. It NEVER calls an LLM. Every candidate cites the knowledge ids that
produced it, so understanding is fully traceable to lower layers.

Two detector families:
  - per-subject detectors: one understanding per subject, driven by the SET of
    knowledge TYPES that back that subject (cross-source reinforcement).
  - global detectors: require relating multiple subjects (shift, convergence,
    divergence, investment trend).

Confidence is computed by the engine from the cited knowledge (see confidence.py)
— detectors only decide *whether* a thesis is supported and *which* knowledge
backs it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

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

# Keyword lexicons for content-based detectors (subject + statement, lowercased).
_COMMERCIAL = {
    "commercial", "business", "market", "revenue", "customer", "product",
    "saas", "startup", "client", "monetiz", "profit", "venture", "funding",
}
_RESEARCH = {
    "research", "experiment", "r&d", "paper", "study", "prototype",
    "investigation", "hypothesis", "novel", "academic", "thesis",
}
_SYSTEMS = {
    "kernel", "filesystem", "operating system", "os ", "systems", "driver",
    "embedded", "firmware", "runtime", "compiler", "memory", "scheduler",
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

    def strongest(self):
        order = {"strong": 3, "medium": 2, "weak": 1}
        return max(self.knowledge, key=lambda k: order.get(k.confidence.value, 0))

    @property
    def text(self) -> str:
        return (self.subject + " " + " ".join(k.statement for k in self.knowledge)).lower()


def detect(knowledge: List, evolution_events: Optional[List] = None) -> List[Candidate]:
    """Run every detector. Returns candidate understandings (pre-confidence)."""
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

    # --- per-subject detectors ------------------------------------------------
    for subj in subjects:
        idx = index[subj]
        candidates.extend(_per_subject(idx))

    # --- content-based per-subject detectors ---------------------------------
    for subj in subjects:
        idx = index[subj]
        candidates.extend(_content_based(idx))

    # --- global (multi-subject) detectors ------------------------------------
    candidates.extend(_investment_trend(index))
    candidates.extend(_technology_shift(index))
    candidates.extend(_project_convergence(index))
    candidates.extend(_project_divergence(index, contradicted))

    # --- contradiction-driven detectors --------------------------------------
    candidates.extend(_risk_and_blind_spot(index, contradicted, knowledge))

    return candidates


def _build_index(knowledge: List) -> Dict[str, _SubjectIndex]:
    out: Dict[str, _SubjectIndex] = {}
    for k in knowledge:
        key = (k.subject or "").strip().lower()
        if not key:
            continue
        out.setdefault(key, _SubjectIndex(subject=key)).knowledge.append(k)
    return out


# ---------------------------------------------------------------------------
# Per-subject detectors
# ---------------------------------------------------------------------------


def _per_subject(idx: _SubjectIndex) -> List[Candidate]:
    out: List[Candidate] = []
    ids = [k.id for k in idx.knowledge if k.id]

    # ENGINEERING_DIRECTION — an area that is both invested in AND trending/directed.
    if idx.has(_KT["invest"], _KT["direction"]) and (
        idx.has(_KT["trend"], _KT["direction"])
    ):
        out.append(Candidate(
            UnderstandingType.ENGINEERING_DIRECTION,
            idx.subject,
            f"Engineering direction is converging toward {idx.subject}.",
            ids,
        ))

    # TECHNOLOGY_PREFERENCE — explicit preference or strong investment.
    if idx.has(_KT["pref"]) or (
        idx.has(_KT["invest"])
        and any(k.confidence.value == "strong" for k in idx.by_type(_KT["invest"]))
    ):
        out.append(Candidate(
            UnderstandingType.TECHNOLOGY_PREFERENCE,
            idx.subject,
            f"{idx.subject} is becoming the preferred choice where it applies.",
            ids,
        ))

    # EMERGING_EXPERTISE — strong/growing investment or increasing trend.
    if idx.has(_KT["invest"], _KT["trend"]):
        out.append(Candidate(
            UnderstandingType.EMERGING_EXPERTISE,
            idx.subject,
            f"Emerging expertise is forming around {idx.subject}.",
            ids,
        ))

    # SKILL_DEVELOPMENT — investment signals skill building (not just usage).
    if idx.has(_KT["invest"]):
        out.append(Candidate(
            UnderstandingType.SKILL_DEVELOPMENT,
            idx.subject,
            f"Skills are being developed around {idx.subject}.",
            ids,
        ))

    # ENGINEERING_PHILOSOPHY — preference or recurring pattern of approach.
    if idx.has(_KT["pref"], _KT["pattern"]):
        out.append(Candidate(
            UnderstandingType.ENGINEERING_PHILOSOPHY,
            idx.subject,
            f"An engineering philosophy favoring {idx.subject} is emerging.",
            ids,
        ))

    # ARCHITECTURAL_STYLE — architecture knowledge.
    if idx.has(_KT["parch"], _KT["pstack"]):
        out.append(Candidate(
            UnderstandingType.ARCHITECTURAL_STYLE,
            idx.subject,
            f"The architectural style of {idx.subject} is stabilizing.",
            ids,
        ))

    # ENGINEERING_IDENTITY — project identity / stable direction self-view.
    if idx.has(_KT["pidentity"], _KT["direction"]):
        out.append(Candidate(
            UnderstandingType.ENGINEERING_IDENTITY,
            idx.subject,
            f"A stable engineering identity around {idx.subject} is forming.",
            ids,
        ))

    # LONG_TERM_INITIATIVE — sustained direction across evolution.
    if idx.has(_KT["direction"]) and idx.has(_KT["evol"], _KT["invest"]):
        out.append(Candidate(
            UnderstandingType.LONG_TERM_INITIATIVE,
            idx.subject,
            f"A long-term initiative centered on {idx.subject} is taking shape.",
            ids,
        ))

    # ENGINEERING_HABIT — explicit habit knowledge.
    if idx.has(_KT["habit"]):
        out.append(Candidate(
            UnderstandingType.ENGINEERING_HABIT,
            idx.subject,
            f"A recurring engineering habit around {idx.subject} is established.",
            ids,
        ))

    # ENGINEERING_STRENGTH — strong, stable knowledge marks a real strength.
    if any(k.confidence.value == "strong" for k in idx.knowledge):
        out.append(Candidate(
            UnderstandingType.ENGINEERING_STRENGTH,
            idx.subject,
            f"A clear engineering strength in {idx.subject} is evident.",
            ids,
        ))

    # ENGINEERING_WEAKNESS — weak knowledge / recurring bottleneck.
    if idx.has(_KT["bottleneck"]) or all(
        k.confidence.value == "weak" for k in idx.knowledge
    ):
        out.append(Candidate(
            UnderstandingType.ENGINEERING_WEAKNESS,
            idx.subject,
            f"A recurring weakness around {idx.subject} is appearing.",
            ids,
        ))

    return out


def _content_based(idx: _SubjectIndex) -> List[Candidate]:
    out: List[Candidate] = []
    ids = [k.id for k in idx.knowledge if k.id]
    text = idx.text

    if any(w in text for w in _COMMERCIAL):
        out.append(Candidate(
            UnderstandingType.COMMERCIAL_DIRECTION,
            idx.subject,
            f"Commercial engineering effort around {idx.subject} is becoming dominant.",
            ids,
        ))

    if any(w in text for w in _RESEARCH):
        out.append(Candidate(
            UnderstandingType.RESEARCH_DIRECTION,
            idx.subject,
            f"Research activity around {idx.subject} is emerging as a direction.",
            ids,
        ))

    return out


# ---------------------------------------------------------------------------
# Global (multi-subject) detectors
# ---------------------------------------------------------------------------


def _investment_trend(index: Dict[str, _SubjectIndex]) -> List[Candidate]:
    """One understanding aggregating the set of invested subjects (>=2)."""
    invested = [
        s for s, idx in index.items()
        if idx.has(_KT["invest"]) and s not in _SYSTEMS  # systems handled by direction
    ]
    if len(invested) < 2:
        return []
    ids = [k.id for s in invested for k in index[s].knowledge if k.id]
    subjs = ", ".join(sorted(invested))
    return [Candidate(
        UnderstandingType.INVESTMENT_TREND,
        "portfolio",
        f"Investment is trending toward: {subjs}.",
        ids,
    )]


def _technology_shift(index: Dict[str, _SubjectIndex]) -> List[Candidate]:
    """Detect a shift: one subject trending down while another trends up.

    Subject is the pair "dec<->inc" so the deterministic id is stable.
    """
    up, down = [], []
    for s, idx in index.items():
        for k in idx.by_type(_KT["trend"]):
            v = (k.statement or "").lower()
            if "increasing" in v or "emerging" in v:
                up.append(s)
            elif "decreasing" in v or "dormant" in v:
                down.append(s)
    out: List[Candidate] = []
    for d in down:
        for u in up:
            if d == u:
                continue
            ids = [k.id for s in (d, u) for k in index[s].knowledge if k.id]
            subj = f"{d}<->{u}"
            out.append(Candidate(
                UnderstandingType.TECHNOLOGY_SHIFT,
                subj,
                f"Technology is shifting away from {d} toward {u}.",
                ids,
            ))
    return out


def _project_convergence(index: Dict[str, _SubjectIndex]) -> List[Candidate]:
    """Projects that relate / integrate / co-evolve are converging."""
    out: List[Candidate] = []
    for s, idx in index.items():
        if idx.has(_KT["rel"], _KT["pinteg"], _KT["evol"]):
            ids = [k.id for k in idx.knowledge if k.id]
            out.append(Candidate(
                UnderstandingType.PROJECT_CONVERGENCE,
                s,
                f"Projects touching {s} are converging into an engineering ecosystem.",
                ids,
            ))
    return out


def _project_divergence(
    index: Dict[str, _SubjectIndex], contradicted: Set[str]
) -> List[Candidate]:
    """Subjects whose knowledge was contradicted/retired are diverging from the
    prior thesis. Only emit for knowledge that actually carries contradiction
    evidence — never from idle time."""
    out: List[Candidate] = []
    for s, idx in index.items():
        if any(k.id in contradicted for k in idx.knowledge):
            ids = [k.id for k in idx.knowledge if k.id in contradicted]
            out.append(Candidate(
                UnderstandingType.PROJECT_DIVERGENCE,
                s,
                f"Engineering effort around {s} is diverging from its earlier direction.",
                ids,
            ))
    return out


def _risk_and_blind_spot(
    index: Dict[str, _SubjectIndex], contradicted: Set[str], knowledge: List
) -> List[Candidate]:
    out: List[Candidate] = []
    for s, idx in index.items():
        # ENGINEERING_RISK — recurring bottlenecks, or contradicted effort.
        if idx.has(_KT["bottleneck"]) or any(k.id in contradicted for k in idx.knowledge):
            ids = [k.id for k in idx.knowledge if k.id]
            out.append(Candidate(
                UnderstandingType.ENGINEERING_RISK,
                s,
                f"An engineering risk is accumulating around {s}.",
                ids,
            ))
        # ENGINEERING_BLIND_SPOT — effort that was contradicted/retired: a belief
        # that did not hold, i.e. a blind spot in prior direction.
        if any(k.id in contradicted for k in idx.knowledge):
            ids = [k.id for k in idx.knowledge if k.id in contradicted]
            out.append(Candidate(
                UnderstandingType.ENGINEERING_BLIND_SPOT,
                s,
                f"A blind spot around {s} is now visible (earlier direction contradicted).",
                ids,
            ))
    return out
