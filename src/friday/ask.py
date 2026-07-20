"""Conversational query over the knowledge base — semantic reasoning refactor.

THE NEW PIPELINE (single source of truth = RetrievalRequirements):

    User Question
        ↓
    LLM Understanding  (understand)        — or offline heuristic
        ↓
    RetrievalRequirements                    — WHAT evidence is needed
        ↓
    Evidence Selection  (retrieve_requirements)
        ↓
    Evidence Package                          — composed from providers
        ↓
    LLM Answer        (opt-in synthesis)

There is NO intent enum, NO switch(intent), NO portfolio_mode()/strategy_axis()
primary router. The LLM's one job is to say what evidence is required; it does
not answer, retrieve, or reason over the workspace. Retrieval answers "which
evidence providers satisfy these needs?" — deterministically, with no planner.

Evidence providers are the existing domain modules (identity, architecture,
relationships, observe, portfolio, strategy, insights). They expose evidence;
they never know about intents. A new capability is a new *combination* of needs
answered by composing existing providers — no new top-level label required.

OFFLINE MODE: requirements_from_question produces the SAME RetrievalRequirements
structure via deterministic heuristics. The downstream pipeline is identical.

DEPRECATED COMPAT LAYER (do not build on these):
  classify(), Evidence.intent, extract_intent(), retrieve(), deterministic_classifier()
  These are THIN adapters that DERIVE their value from RetrievalRequirements.
  They exist only so the tracked benchmark suite keeps passing during the
  transition to RetrievalRequirements. TODO: remove once benchmarks migrate.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import query as q
from .db import Repository, get_technologies
from .identity import explain_project_from_conn
from .services.llm import _enabled as llm_enabled
from .services.llm import _call
from . import objective as obj_mod

# Objectives the online LLM understanding can produce from a noisy bag that we
# do NOT trust over the deterministic offline heuristic (see retrieve_requirements).
_LOW_CONFIDENCE_OBJECTIVES = {
    obj_mod.Objective.GENERAL, obj_mod.Objective.VALUE, obj_mod.Objective.UNIVERSE,
    obj_mod.Objective.OVERLAP, obj_mod.Objective.INSIGHTS, obj_mod.Objective.DRIFT,
}

_CHITCHAT = {
    "hello", "hi", "hey", "thanks", "thank you", "ok", "okay", "cool", "nice",
    "who are you", "what are you", "help",
}


# ---------------------------------------------------------------------------
# RetrievalRequirements — the source of truth
# ---------------------------------------------------------------------------


@dataclass
class RetrievalRequirements:
    """What evidence the question requires — NOT which bucket it fits.

    - scope: "workspace" | "repo" | "compare"  — span of evidence
    - subjects: repo/project names the question is about
    - operation: describe | compare | rank | survey | synthesize  (metadata)
    - needs: OPEN vocabulary of evidence types to fetch (not a question enum)
    - lens: optional sub-cut within a provider (e.g. portfolio/strengency axis)
    - constraints: qualitative hints ("prefer strong evidence", "ignore weak")
    - confidence: model certainty (0.0-1.0)
    - query: the original question text (for provider substring checks)
    """

    scope: str = "workspace"
    subjects: list[str] = field(default_factory=list)
    operation: str = "survey"
    needs: list[str] = field(default_factory=list)
    lens: Optional[str] = None
    constraints: list[str] = field(default_factory=list)
    confidence: float = 1.0
    query: str = ""


@dataclass
class Evidence:
    """The retrieved facts the answer must be grounded in."""

    requirements: RetrievalRequirements
    blocks: list[str] = field(default_factory=list)  # human-readable evidence lines
    raw: dict = field(default_factory=dict)  # structured data for the LLM
    subject: Optional[str] = None  # the single repo this exchange is about

    def is_empty(self) -> bool:
        return not self.blocks

    # DEPRECATED: intent is a DERIVED label from the requirements, retained only
    # for benchmark compatibility. Do not route on it. TODO: remove on migration.
    @property
    def intent(self) -> str:
        return _label_of(self.requirements)


@dataclass
class Exchange:
    """The one exchange continuity is allowed to remember — nothing more.

    Bounded conversational continuity (M6.5D): only the immediately previous
    question + answer. No long-term memory, no planner, no agent. The previous
    answer is reference-resolution context, NEVER evidence for the next turn.
    """

    question: str
    answer: "Answer"


@dataclass
class Answer:
    text: str
    evidence: Evidence
    used_llm: bool


# ---------------------------------------------------------------------------
# Evidence-provider registry (deterministic selection, no switch)
# ---------------------------------------------------------------------------
#
# Each need maps to a provider. A question's `needs` set selects the providers
# that run — multiple may run (composition). No intent string is consulted.
#
# Evidence vocabulary (open, descriptive — not a closed question enum):
_NEED_TYPES = (
    "identity", "purpose", "themes", "architecture", "components",
    "relationships", "activity", "history", "observation", "value", "overlap",
    "reuse", "integration", "universe", "strengths", "effort",
    "engineering-profile", "impact", "platform", "learning", "opportunity",
    "priority", "converge", "merge", "compare", "describe", "inactive",
    "newest", "recommend", "by-tech", "insights", "chitchat", "general",
    "similarity", "theme-repeat", "lessons", "habits", "assumptions", "drift",
    "surprise", "evolve", "direction", "blockers", "knowledge", "understanding",
    "initiative", "insight",
)


@dataclass
class _Provider:
    needs: tuple[str, ...]
    fn: Callable[["RetrievalRequirements", object, "Evidence", dt.date], None]


def _provider(*needs: str):
    def deco(fn):
        prov = _Provider(needs=needs, fn=fn)
        _PROVIDERS.append(prov)
        return prov
    return deco


_PROVIDERS: list[_Provider] = []


# --- compare -----------------------------------------------------------------

@_provider("compare")
def _p_compare(req, conn, ev, today):
    """Structured COMPARE contract: shared goal, different goals, architecture
    diff, tech diff, maturity, recommendation — NOT two description dumps."""
    repos = q.all_repositories(conn)
    qlow = req.query.lower()
    named = [r for r in repos if r.name.lower() in qlow]
    seen = set()
    targets = []
    for r in named:
        if r.name not in seen:
            seen.add(r.name)
            targets.append(r)
    if len(targets) < 2:
        single = _detect_repo(req.query, conn)
        if single and single not in targets:
            targets.append(single)
    if len(targets) < 2:
        ev.raw["note"] = "could not identify two repositories to compare"
        return
    a, b = targets[0], targets[1]
    if a.id is None or b.id is None:
        ev.raw["note"] = "could not identify two repositories to compare"
        return

    from .identity import build_identity
    ia = build_identity(conn, a.id, today)
    ib = build_identity(conn, b.id, today)
    ca = q.compare_repositories(conn, a.id, b.id)
    arch_a = q.architecture_of(conn, a.id)
    arch_b = q.architecture_of(conn, b.id)

    lines: list[str] = []
    pa = (ia.purpose if ia else None) or "not stated"
    pb = (ib.purpose if ib else None) or "not stated"
    lines.append(f"Shared goal: neither states an explicit shared goal — they are "
                 f"separate projects. (If you meant 'which to merge', ask that.)")
    lines.append(f"Different goals:")
    lines.append(f"- {a.name}: {pa}")
    lines.append(f"- {b.name}: {pb}")
    la = arch_a.architecture if arch_a else "Unknown"
    lb = arch_b.architecture if arch_b else "Unknown"
    if la == lb:
        lines.append(f"Architecture differences: both are {la}.")
    else:
        lines.append(f"Architecture differences: {a.name} is {la}; {b.name} is {lb}.")
    ta = sorted({t.tech for t in get_technologies(conn, a.id)})
    tb = sorted({t.tech for t in get_technologies(conn, b.id)})
    shared_tech = sorted(set(ta) & set(tb))
    only_a = sorted(set(ta) - set(tb))
    only_b = sorted(set(tb) - set(ta))
    tech_bits = []
    if shared_tech:
        tech_bits.append(f"shared: {', '.join(shared_tech)}")
    if only_a:
        tech_bits.append(f"only in {a.name}: {', '.join(only_a)}")
    if only_b:
        tech_bits.append(f"only in {b.name}: {', '.join(only_b)}")
    lines.append("Technology differences: " + ("; ".join(tech_bits)
                 if tech_bits else "none detected"))
    ma = (ia.maturity if ia else "Unknown") or "Unknown"
    mb = (ib.maturity if ib else "Unknown") or "Unknown"
    lines.append(f"Current maturity: {a.name} is {ma}; {b.name} is {mb}.")
    # Recommendation: keep separate unless a real shared-architecture reason.
    if ca.get("architecture") and "both are" in ca["architecture"]:
        rec = (f"Recommendation: they are architecturally similar ({la}) — "
               f"review before merging, but they serve different goals, so keep "
               f"them separate unless a shared purpose emerges.")
    else:
        rec = (f"Recommendation: keep {a.name} and {b.name} separate — they serve "
               f"different goals ({pa} vs {pb}).")
    lines.append(rec)

    ev.blocks = lines
    ev.raw["subjects"] = [a.name, b.name]
    ev.raw["compare"] = [a.name, b.name]
    return


# --- portfolio: themes / purpose / strengths / effort / identity --------------


@_provider("themes", "strengths", "effort", "engineering-profile")
def _p_portfolio(req, conn, ev, today):
    from .portfolio import (
        portfolio_synthesis, portfolio_strengths, portfolio_effort,
        portfolio_identity, detect_themes,
    )
    mode = req.lens or "building"
    if mode == "strengths":
        blocks = portfolio_strengths(conn, today)
    elif mode == "effort":
        blocks = portfolio_effort(conn, today)
    elif mode == "identity":
        blocks = portfolio_identity(conn, today)
    else:
        blocks = portfolio_synthesis(conn, today)
    ev.blocks = blocks
    ev.raw["portfolio_mode"] = mode
    ev.raw["portfolio"] = blocks
    ev.raw["themes"] = [
        {"theme": t.theme, "repos": t.repos, "confidence": t.confidence}
        for t in detect_themes(conn, today)
    ]
    return


# --- value -------------------------------------------------------------------


@_provider("value")
def _p_value(req, conn, ev, today):
    from .portfolio import project_value_ranking

    ranked = project_value_ranking(conn, today)
    if not ranked:
        ev.raw["note"] = "no value signals available"
        return
    top = ranked[0]
    lines = [
        f"If 'most valuable' means accumulated evidence of purpose, business "
        f"value, activity and importance, {top.repo} ranks highest."
    ]
    for v in ranked[:3]:
        lines.append(f"- {v.repo} ({v.confidence} confidence): {'; '.join(v.signals)}.")
    lines.append(
        f"Confidence: {top.confidence} — based on stored purpose, business "
        f"value, activity and relationship evidence, not commit counts alone."
    )
    ev.blocks = lines
    ev.raw["value"] = [
        {"repo": v.repo, "confidence": v.confidence, "signals": v.signals}
        for v in ranked
    ]
    return


# --- overlap -----------------------------------------------------------------


@_provider("overlap")
def _p_overlap(req, conn, ev, today):
    from .portfolio import meaningful_overlap

    results = meaningful_overlap(conn, today)
    if not results:
        ev.blocks = [
            "No meaningful cross-project overlap found in the stored evidence "
            "(architecture, shared purpose, persistence, auth, storage, config)."
        ]
        return
    blocks = ["Meaningful overlap across your projects (by responsibility and problem domain, not syntax):"]
    for o in results:
        blocks.append(f"- {o.a} and {o.b} ({o.confidence} confidence): "
                      + "; ".join(o.dimensions) + ".")
    ev.blocks = blocks
    ev.raw["overlap"] = [
        {"a": o.a, "b": o.b, "dimensions": o.dimensions, "confidence": o.confidence}
        for o in results
    ]
    return


# --- integration -------------------------------------------------------------


@_provider("integration")
def _p_integration(req, conn, ev, today):
    from .portfolio import integration_opportunities

    cands = integration_opportunities(conn, today)
    if not cands:
        ev.blocks = [
            "No project currently shows reasonable evidence for integration "
            "with Friday (no shared AI/assistant/workflow/OS purpose or "
            "overlapping technology). Confidence: Weak."
        ]
        ev.raw["integration"] = []
        return
    blocks = ["Candidates to integrate with Friday (reasoned from project identity):"]
    for c in cands:
        blocks.append(f"- {c.repo} ({c.confidence} confidence): {c.reason}.")
    ev.blocks = blocks
    ev.raw["integration"] = [
        {"repo": c.repo, "confidence": c.confidence, "reason": c.reason}
        for c in cands
    ]
    return


# --- universe ----------------------------------------------------------------


@_provider("universe")
def _p_universe(req, conn, ev, today):
    from .portfolio import engineering_universe

    lines = engineering_universe(conn, today)
    ev.blocks = lines
    ev.raw["universe"] = lines
    return


# --- objective-driven evidence (Milestone 6.6) ------------------------------
#
# These providers answer the new engineering-judgment objectives. Each maps a
# canonical need to a DISTINCT evidence cut computed in objective.py — no new
# intents, no new storage, no LLM. The objective layer guarantees the right
# canonical need leads, so each objective produces a distinct answer.


@_provider("theme-repeat")
def _p_theme_repeat(req, conn, ev, today):
    blocks, raw = obj_mod.evidence_theme_repeat(conn, today)
    ev.blocks = blocks
    ev.raw.update(raw)
    return


@_provider("lessons")
def _p_lessons(req, conn, ev, today):
    blocks, raw = obj_mod.evidence_lessons(conn, today)
    ev.blocks = blocks
    ev.raw.update(raw)
    return


@_provider("habits")
def _p_habits(req, conn, ev, today):
    blocks, raw = obj_mod.evidence_habits(conn, today)
    ev.blocks = blocks
    ev.raw.update(raw)
    return


@_provider("assumptions")
def _p_assumptions(req, conn, ev, today):
    blocks, raw = obj_mod.evidence_assumptions(conn, today)
    ev.blocks = blocks
    ev.raw.update(raw)
    return


@_provider("drift")
def _p_drift(req, conn, ev, today):
    blocks, raw = obj_mod.evidence_drift(conn, today)
    ev.blocks = blocks
    ev.raw.update(raw)
    return


@_provider("surprise")
def _p_surprise(req, conn, ev, today):
    blocks, raw = obj_mod.evidence_surprise(conn, today)
    ev.blocks = blocks
    ev.raw.update(raw)
    return


@_provider("evolve")
def _p_evolve(req, conn, ev, today):
    blocks, raw = obj_mod.evidence_evolve(conn, today)
    ev.blocks = blocks
    ev.raw.update(raw)
    return


# --- strategy axes ------------------------------------------------------------


@_provider("impact", "platform", "learning", "opportunity", "priority",
           "converge", "merge")
def _p_strategy(req, conn, ev, today):
    from .strategy import (
        strategy_converge, strategy_impact, strategy_learning, strategy_merge,
        strategy_opportunity, strategy_platform, strategy_priority,
    )
    axis = req.lens or "impact"
    dispatch = {
        "impact": strategy_impact,
        "platform": strategy_platform,
        "learning": strategy_learning,
        "opportunity": strategy_opportunity,
        "priority": strategy_priority,
        "converge": strategy_converge,
        "merge": strategy_merge,
    }
    blocks = dispatch.get(axis, strategy_impact)(conn, today)
    ev.blocks = blocks
    ev.raw["strategy_axis"] = axis
    ev.raw["strategy"] = blocks
    ev.subject = None  # strategic answers span the workspace, not one repo
    return


# --- relationships ------------------------------------------------------------


@_provider("relationships", "related")
def _p_related(req, conn, ev, today):
    r = _detect_repo(req.query, conn) if not req.subjects else _repo_by_name(conn, req.subjects[0])
    if (not r or r.id is None) and not req.subjects:
        from .db import get_all_relationships

        pairs: list[str] = []
        name_by_id = {x.id: x.name for x in q.all_repositories(conn)}
        ranked = [rel for rel in get_all_relationships(conn) if rel.strength != "Weak"]
        ranked.sort(key=lambda rel: _rel_rank(rel.kind))
        seen_pairs: set[tuple[str, str, str]] = set()
        for rel in ranked:
            an = name_by_id.get(rel.repo_a)
            bn = name_by_id.get(rel.repo_b)
            if an and bn:
                key = (an, bn, rel.kind)
                if key in seen_pairs:
                    continue
                seen_pairs.add(key)
                pairs.append(f"{an} and {bn}: {rel.kind.replace('shared-', 'shared ')} "
                             f"({rel.evidence})")
        if pairs:
            ev.blocks = ["Projects that are genuinely related (Medium/Strong evidence):"] + pairs
        else:
            ev.blocks = ["No Medium/Strong relationships detected across the workspace."]
        return
    subject = r
    if subject is None and req.subjects:
        subject = _repo_by_name(conn, req.subjects[0])
    if subject is None or subject.id is None:
        ev.raw["note"] = "could not identify repository"
        return
    qlow = req.query.lower()
    include_weak = any(w in qlow for w in ("weak", "all relationships", "everything", "including"))
    others = [o for o in q.all_repositories(conn) if o.id is not None and o.id != subject.id]
    rels: list[str] = []
    for o in others:
        pairs = q.relationships_between(conn, subject.id, o.id)
        if not pairs:
            continue
        strong = [p for p in pairs if p.strength != "Weak"]
        weak = [p for p in pairs if p.strength == "Weak"]
        why = [
            f"{p.kind.replace('shared-', 'shared ')} — {p.evidence}"
            for p in sorted(strong, key=lambda p: _rel_rank(p.kind))
        ]
        if include_weak and weak:
            why += [f"weak coincidence: {p.kind.replace('shared-', 'shared ')} ({p.evidence})"
                    for p in weak]
        if why:
            rels.append(f"{o.name}: " + "; ".join(why))
    if rels:
        ev.blocks = rels
    elif include_weak:
        ev.blocks = [f"No relationships found for {subject.name}."]
    else:
        ev.blocks = [
            f"No strong or medium relationships found for {subject.name}. "
            f"(Weak coincidences like shared author/language are omitted — "
            f"ask 'including weak relationships' to see them.)"
        ]
    ev.raw["repo"] = subject.name
    return


# --- architecture ------------------------------------------------------------


@_provider("architecture", "components")
def _p_architecture(req, conn, ev, today):
    subject = _detect_repo(req.query, conn) if not req.subjects else _repo_by_name(conn, req.subjects[0])
    if subject is None and req.subjects:
        subject = _repo_by_name(conn, req.subjects[0])
    if subject is None or subject.id is None:
        ev.raw["note"] = "could not identify repository"
        return
    arch = q.architecture_of(conn, subject.id)
    comps = q.components_of(conn, subject.id)
    eps = q.entry_points_of(conn, subject.id)
    if arch is None:
        ev.blocks = [
            f"No architecture knowledge stored for {subject.name}. Run `friday analyze {subject.path}`."
        ]
        ev.raw["repo"] = subject.name
        return
    lines = [f"{subject.name} — {arch.architecture}"]
    if arch.confidence:
        lines.append(f"Confidence: {arch.confidence}")
    lines.append("Evidence:")
    lines.append("- " + arch.evidence.replace("\n", "\n- "))
    if comps:
        lines.append("Major components:")
        for c in comps:
            lines.append(f"- {c.name} ({c.strength} evidence): {c.evidence}")
    if eps:
        app_eps = [e for e in eps if e.kind in ("main()", "CLI", "FastAPI app",
                                               "Flask app", "Next.js app", "Cargo binary",
                                               "Executable script")]
        util_eps = [e for e in eps if e.kind == "Utility script"]
        if app_eps:
            lines.append("Application entry points:")
            for e in app_eps:
                lines.append(f"- {e.kind}: {e.detail} ({e.evidence})")
        if util_eps:
            lines.append("Utility scripts (not application entry points):")
            for e in util_eps:
                lines.append(f"- {e.detail} ({e.evidence})")
    if arch.data_flow:
        lines.append("Data flow:")
        lines.append("- " + "\n- ".join(arch.data_flow.split("\n")))
    if arch.known_patterns:
        lines.append("Known patterns:")
        lines.append("- " + "\n- ".join(arch.known_patterns.split("\n")))
    if arch.complexity:
        lines.append(f"Potential complexity: {arch.complexity}")
    ev.blocks = lines
    ev.raw["repo"] = subject.name
    ev.raw["architecture"] = arch.architecture
    return


# --- describe / purpose of a single project -----------------------------------


@_provider("describe")
def _p_describe(req, conn, ev, today):
    subject = _detect_repo(req.query, conn) if not req.subjects else _repo_by_name(conn, req.subjects[0])
    if subject is None and req.subjects:
        subject = _repo_by_name(conn, req.subjects[0])
    if subject is None or subject.id is None:
        ev.raw["note"] = "could not identify repository"
        return
    text = explain_project_from_conn(conn, subject.id, detailed=True)
    ev.blocks = [text]
    ev.raw["repo"] = subject.name
    ev.raw["identity"] = True
    ev.subject = subject.name
    return


# --- inactive / stale ---------------------------------------------------------


@_provider("inactive")
def _p_inactive(req, conn, ev, today):
    days = 180 if ("abandon" in req.query.lower()) else q.STALE_DAYS
    label = "abandoned" if days >= q.ABANDONED_DAYS else "inactive"
    named = _detect_repo(req.query, conn) if not req.subjects else _repo_by_name(conn, req.subjects[0])
    repos = q.inactive_repos(conn, today, days)
    if named is not None:
        if named.id in {r.id for r in repos}:
            repos = [named]
        else:
            d = dt.date.fromisoformat(named.last_commit_date[:10])
            ev.blocks = [
                f"{named.name} is not {label}: last commit {named.last_commit_date[:10]} "
                f"({(today - d).days} days ago, under the {days}-day threshold)."
            ]
            ev.raw["count"] = 0
            return
    if repos:
        ev.blocks = [
            f"{r.name} is {label}: last commit {r.last_commit_date[:10]} "
            f"({(today - dt.date.fromisoformat(r.last_commit_date[:10])).days} days ago, "
            f"threshold {days} days)"
            for r in repos
        ]
    else:
        ev.blocks = [
            f"No repositories are {label}. Every repo has a commit within the "
            f"last {days} days (per git commit dates)."
        ]
    ev.raw["count"] = len(repos)
    return


# --- newest ------------------------------------------------------------------


@_provider("newest")
def _p_newest(req, conn, ev, today):
    repos = q.newest_repos(conn, 3)
    ev.blocks = [
        f"{r.name}: first commit {r.first_commit_date[:10]}" for r in repos
    ]
    ev.raw["newest"] = [r.name for r in repos]
    return


@_provider("maturity")
def _p_maturity(req, conn, ev, today):
    """Rank projects by maturity using stored evidence only — README-derived
    maturity plus git activity/recency. No invented maturity metric: when a repo
    has no stated maturity we say so and fall back to its git signals."""
    from .identity import build_identity

    repos = q.all_repositories(conn)
    if not repos:
        ev.blocks = ["I don't have enough evidence — no repositories are ingested."]
        ev.raw["note"] = "no repositories"
        return

    # Maturity is reported as stored (README-derived), falling back to git
    # activity when no maturity text exists. We present the evidence, never a
    # fabricated score.
    active = {r.id: s for r, s in q.most_active(conn, today, len(repos))}
    rows = []
    for r in repos:
        ident = build_identity(conn, r.id) if r.id is not None else None
        maturity = (ident.maturity if ident else None) or "Unknown"
        recency = r.last_commit_date[:10] if r.last_commit_date else "unknown"
        rate = active.get(r.id)
        rows.append((r, maturity, recency, rate))

    def _mat_rank(m: str) -> int:
        return {"stable": 3, "beta": 2, "alpha": 2, "wip": 1}.get(m.lower(), 0)

    rows.sort(key=lambda x: (_mat_rank(x[1]), x[3] or 0.0), reverse=True)

    lines = []
    for r, maturity, recency, rate in rows:
        bits = [f"{r.name}: maturity={maturity}"]
        if recency != "unknown":
            bits.append(f"last commit {recency}")
        if rate is not None:
            bits.append(f"~{rate:.1f} commits/day")
        lines.append("; ".join(bits))
    ev.blocks = lines
    ev.raw["maturity"] = [(r.name, m) for r, m, _, _ in rows]
    ev.subject = rows[0][0].name if rows else None


# --- recommend ---------------------------------------------------------------


@_provider("recommend")
def _p_recommend(req, conn, ev, today):
    priorities = q.workspace_priorities(conn, today, n=3)
    if not priorities:
        ev.blocks = ["I don't have enough evidence to prioritize — no repositories are ingested."]
        return
    lines: list[str] = []
    top = priorities[0]
    top_repo, top_reasons = top
    ev.subject = top_repo.name
    ev.raw["recommend_subject"] = top_repo.name
    ev.raw["recommend_reasons"] = top_reasons
    lines.append(f"If you want the highest-leverage next step, continue {top_repo.name}.")
    lines.append("Why: " + "; ".join(top_reasons) + ".")
    if len(priorities) > 1:
        lines.append("Next after that:")
        for repo, reasons in priorities[1:]:
            lines.append(f"- {repo.name}: {'; '.join(reasons)}.")
    ev.blocks = lines
    ev.raw["recommend"] = lines
    return


# --- by-tech / by-language ---------------------------------------------------


@_provider("by-tech")
def _p_by_tech(req, conn, ev, today):
    tech = _detect_tech(req.query, conn)
    if tech:
        repos = q.projects_by_tech(conn, tech)
        if repos:
            ev.blocks = [f"{r.name} uses {tech}" for r in repos]
        else:
            ev.blocks = [f"No repositories use {tech} (per detected technologies)."]
        ev.raw["tech"] = tech
        ev.raw["repos"] = [r.name for r in repos]
        return
    lang = _detect_lang(req.query)
    if lang:
        repos = q.projects_by_language(conn, lang)
        ev.blocks = [f"{r.name} uses {lang}" for r in repos] or [f"No repositories use {lang}."]
        ev.raw["lang"] = lang
        return
    ev.raw["note"] = "could not identify a technology or language"
    return


# --- similarity / reuse ------------------------------------------------------


@_provider("similarity", "reuse")
def _p_similarity(req, conn, ev, today):
    pairs = q.similar_layouts(conn)
    reuse = q.reuse_opportunities(conn)
    blocks: list[str] = []
    if reuse:
        blocks.append("Realistic shared-code opportunities (Medium/Strong evidence only):")
        for line in reuse:
            blocks.append(f"- {line}")
    name_by_id = {r.id: r.name for r in q.all_repositories(conn) if r.id is not None}
    id_by_name = {v: k for k, v in name_by_id.items()}
    compared: list[str] = []
    seen_pairs: set[tuple[str, str]] = set()
    from .db import get_all_relationships

    for rel in get_all_relationships(conn):
        if rel.strength == "Weak":
            continue
        an, bn = name_by_id.get(rel.repo_a), name_by_id.get(rel.repo_b)
        if an and bn:
            seen_pairs.add(tuple(sorted((an, bn))))
    for a, b in pairs:
        seen_pairs.add(tuple(sorted((a, b))))

    for an, bn in seen_pairs:
        a_id, b_id = id_by_name.get(an), id_by_name.get(bn)
        if a_id is None or b_id is None:
            continue
        cmp = q.compare_repositories(conn, a_id, b_id)
        dims = [v for v in (
            cmp["architecture"], cmp["responsibilities"], cmp["deployment"],
            cmp["persistence"], cmp["interfaces"],
        ) if v]
        if dims:
            compared.append(f"- {an} and {bn}: " + "; ".join(dims) + ".")
    if compared:
        blocks.append("What these projects share (architecture, not just dependencies):")
        blocks.extend(compared)
    elif pairs:
        blocks.append("Repositories with similar architecture labels (verify before acting):")
        for a, b in pairs:
            blocks.append(f"- {a} and {b}")
    if not blocks:
        blocks = ["No evidence-backed cross-repository similarities found."]
    ev.blocks = blocks
    ev.raw["reuse"] = reuse
    ev.raw["similar_layouts"] = [list(p) for p in pairs]
    return


# --- insights ----------------------------------------------------------------


@_provider("insights")
def _p_insights(req, conn, ev, today):
    from .insights import _engineering_insights

    eng = _engineering_insights(conn, today)
    if eng:
        ev.blocks = [i.text for i in eng]
    else:
        ev.blocks = [
            "I don't see anything non-obvious in your workspace yet — no "
            "repeated solutions, emerging trends, or effort shifts are "
            "evident from the stored evidence. Keep ingesting and I'll "
            "surface them as they appear."
        ]
    ev.raw["insights"] = ev.blocks
    return


# --- knowledge (accumulated understanding) -----------------------------------
#
# First-class evidence source. Knowledge is the BRAIN's accumulated understanding
# (static identity/architecture/stack + temporal trends/habits/relationships). On
# a fresh ingest, STATIC knowledge already exists, so this provider never returns
# "0 of N repositories". When accumulated knowledge is thin, it augments with the
# existing portfolio / identity / architecture providers (which read ingest-time
# evidence directly) instead of failing the whole answer.


@_provider("knowledge")
def _p_knowledge(req, conn, ev, today):
    from .knowledge import KnowledgeEngine

    eng = KnowledgeEngine(conn)
    all_k = eng.all_knowledge()
    static = eng.static_knowledge()
    temporal = eng.temporal_knowledge()

    blocks: list[str] = []

    # Honest, evidence-grounded header (never the misleading "0 of N").
    if all_k:
        blocks.append(
            f"Accumulated engineering knowledge: {len(all_k)} item(s) "
            f"({len(static)} from the current project state, "
            f"{len(temporal)} from observation history)."
        )
    elif static:
        blocks.append(
            f"Accumulated engineering knowledge: {len(static)} item(s) from the "
            f"current project state. No long-term trend knowledge yet — only one "
            f"observation exists, so trends cannot be determined yet."
        )
    else:
        blocks.append(
            "No accumulated engineering knowledge yet. Run `friday ingest` and "
            "`friday knowledge build` first."
        )

    # Static knowledge — always available after ingest.
    if static:
        blocks.append("What your projects are (from the current state):")
        for k in static:
            blocks.append(f"- {k.subject}: {k.statement}")

    # Temporal knowledge — only when history exists.
    if temporal:
        blocks.append("Long-term engineering patterns (from observation history):")
        for k in temporal:
            blocks.append(f"- {k.statement}")
    elif all_k:
        blocks.append(
            "Long-term engineering trends cannot yet be determined because only "
            "one observation exists. They will appear after more `friday observe` "
            "runs."
        )

    ev.blocks = blocks
    ev.raw["knowledge_total"] = len(all_k)
    ev.raw["knowledge_static"] = len(static)
    ev.raw["knowledge_temporal"] = len(temporal)
    ev.raw["knowledge"] = [
        {"type": k.type.value, "subject": k.subject, "statement": k.statement,
         "static": k.is_static} for k in all_k
    ]
    return


# --- understanding (durable engineering meaning, derived from knowledge) ------
#
# Another evidence source for the Brain. Write-only layer above Knowledge; this
# provider only READS the understanding table. Same pattern as _p_knowledge.
# No routing change: questions that already resolve to KNOWLEDGE / THEMES /
# ENGINEERING-PROFILE now also receive understanding as supporting context.


@_provider("understanding")
def _p_understanding(req, conn, ev, today):
    from .understanding import UnderstandingEngine

    eng = UnderstandingEngine(conn)
    items = eng.all_understanding()

    blocks: list[str] = []
    if not items:
        blocks.append(
            "No durable engineering understanding derived yet. Run "
            "`friday understanding build` (and `friday knowledge build` first)."
        )
    else:
        blocks.append(
            f"Durable engineering understanding ({len(items)} item(s), derived "
            f"from accumulated knowledge):"
        )
        by_type: dict = {}
        for u in items:
            by_type.setdefault(u.type.value, []).append(u)
        for utype, its in sorted(by_type.items()):
            blocks.append(f"  {utype.replace('_', ' ').title()}:")
            for u in its:
                blocks.append(
                    f"    - [{u.confidence.value}] {u.statement} "
                    f"(cited: {u.knowledge_count} knowledge)"
                )

    ev.blocks = blocks
    ev.raw["understanding_total"] = len(items)
    ev.raw["understanding"] = [
        {"type": u.type.value, "subject": u.subject, "statement": u.statement,
         "confidence": u.confidence.value, "status": u.status.value,
         "knowledge_count": u.knowledge_count} for u in items
    ]
    return


# --- initiative (long-running engineering work, derived from understanding) ----
#
# Another evidence source for the Brain. Write-only layer above Understanding;
# this provider only READS the initiatives table. Same pattern as _p_understanding.
# No routing change: questions that already resolve to UNIVERSE / DIRECTION /
# PRIORITIZE / KNOWLEDGE now also receive initiatives as supporting context.

@_provider("initiative")
def _p_initiative(req, conn, ev, today):
    from .initiative import InitiativeEngine

    eng = InitiativeEngine(conn)
    items = eng.all_initiatives()

    blocks: list[str] = []
    if not items:
        blocks.append(
            "No engineering initiatives derived yet. Run "
            "`friday initiatives build` (after `friday understanding build`)."
        )
    else:
        by_status: dict = {}
        for i in items:
            by_status.setdefault(i.status.value, []).append(i)
        blocks.append(
            f"Engineering initiatives ({len(items)} item(s), derived from "
            f"understanding):"
        )
        for st in ("active", "review", "started", "candidate", "blocked",
                   "completed", "dormant", "archived"):
            its = by_status.get(st)
            if not its:
                continue
            blocks.append(f"  {st.title()}:")
            for i in its:
                blocks.append(
                    f"    - [{i.confidence.value}] {i.title} "
                    f"({i.type.value}; repos: {i.repo_count}, "
                    f"understanding: {i.understanding_count})"
                )

    ev.blocks = blocks
    ev.raw["initiative_total"] = len(items)
    ev.raw["initiatives"] = [
        {"title": i.title, "type": i.type.value, "status": i.status.value,
         "confidence": i.confidence.value,
         "repos": i.participating_repositories,
         "understanding_count": i.understanding_count} for i in items
    ]
    return


# --- general (unrecognized) --------------------------------------------------


@_provider("general")
def _p_general(req, conn, ev, today):
    ev.raw["note"] = "intent not recognized"
    return


# --- insight (what deserves attention now, derived from understanding/ ---- ---
# initiatives/knowledge) — see engine.insert/_p_initiative above for the M8.4
# pattern. Insights are EPHEMERAL: a build retires those whose triggering
# conditions no longer hold, so this provider surfaces only live insights.
# No routing change: reflective/workspace objectives receive insights as
# supporting context alongside understanding and initiatives.


@_provider("insight")
def _p_insight(req, conn, ev, today):
    from .insight import InsightEngine

    eng = InsightEngine(conn)
    items = eng.active_insights()

    blocks: list[str] = []
    if not items:
        blocks.append(
            "No active engineering insights right now. Run "
            "`friday insights build` (after `friday initiatives build`)."
        )
    else:
        blocks.append(
            f"Engineering insights ({len(items)} active, derived from "
            f"understanding + initiatives + knowledge):"
        )
        for i in items:
            blocks.append(
                f"- [{i.confidence.value}] {i.title} "
                f"({i.type.value.replace('engineering_', '')}): {i.statement}"
            )

    ev.blocks = blocks
    ev.raw["insight_total"] = len(items)
    ev.raw["insights"] = [
        {"id": i.id, "title": i.title, "type": i.type.value,
         "status": i.status.value, "confidence": i.confidence.value,
         "understanding_count": i.understanding_count,
         "initiative_count": i.initiative_count,
         "knowledge_count": i.knowledge_count} for i in items
    ]
    return


# --- plan (how should we do it, derived from insights/initiatives/ ---- ---
# understanding/knowledge) — see engine.insert/_p_insight above for the M8.5
# pattern. Plans are STRUCTURED, evidence-backed strategies; this provider
# surfaces them as supporting context when a question asks HOW to proceed.
# No routing/retrieval/judgment change: it is registered as a provider and
# folded into answers exactly like the insight provider.


@_provider("plan")
def _p_plan(req, conn, ev, today):
    from .planning import PlanEngine

    eng = PlanEngine(conn)
    items = eng.active_plans()

    blocks: list[str] = []
    if not items:
        blocks.append(
            "No engineering plan derived yet. Run "
            "`friday plan \"<goal>\"` to produce a structured, evidence-backed plan."
        )
    else:
        blocks.append(
            f"Engineering plans ({len(items)} active, derived from "
            f"insights + initiatives + understanding + knowledge):"
        )
        for p in items:
            ev_count = (p.initiative_count + p.insight_count
                        + p.understanding_count + p.knowledge_count)
            blocks.append(
                f"- [{p.confidence.value}] {p.goal} "
                f"({p.plan_type.value}): {p.estimated_complexity} complexity, "
                f"{len(p.milestones)} milestones, evidence={ev_count}"
            )

    ev.blocks = blocks
    ev.raw["plan_total"] = len(items)
    ev.raw["plans"] = [
        {"id": p.id, "goal": p.goal, "type": p.plan_type.value,
         "status": p.status.value, "confidence": p.confidence.value,
         "complexity": p.estimated_complexity, "effort": p.estimated_effort,
         "milestone_count": len(p.milestones),
         "risk_count": len(p.risks),
         "initiative_count": p.initiative_count,
         "insight_count": p.insight_count,
         "understanding_count": p.understanding_count,
         "knowledge_count": p.knowledge_count} for p in items
    ]
    return


@_provider("taskgraph")
def _p_taskgraph(req, conn, ev, today):
    """M9.1 Brain exposure: surface compiled Task Graphs as supporting context
    when a question asks HOW EXACTLY to execute a goal (ordered tasks, deps,
    capabilities). Read-only over the new task-graph tables; no routing/retrieval/
    judgment change — it is registered as a provider and folded into answers
    exactly like the plan provider. The Task Graph Compiler is FROZEN-wrt-lower
    layers: this only reads them."""
    from .planning import TaskGraphEngine

    eng = TaskGraphEngine(conn)
    rows = eng.all_graphs()

    blocks: list[str] = []
    if not rows:
        blocks.append(
            "No task graph compiled yet. Run "
            "`friday graph \"<goal>\"` to compile a Plan into an executable "
            "task DAG."
        )
    else:
        blocks.append(
            f"Task graphs ({len(rows)} compiled — ordered, dependency-aware "
            f"execution DAGs compiled from Plans):"
        )
        for r in sorted(rows, key=lambda x: x.updated_at, reverse=True):
            blocks.append(
                f"- {r.goal} ({r.plan_type}): {r.task_count} tasks, "
                f"{r.edge_count} deps, critical path {r.critical_path_length}, "
                f"{r.parallel_groups} parallel groups"
            )

    ev.blocks = blocks
    ev.raw["taskgraph_total"] = len(rows)
    ev.raw["task_graphs"] = [
        {"graph_id": r.id, "goal": r.goal, "plan_id": r.plan_id,
         "plan_type": r.plan_type, "task_count": r.task_count,
         "edge_count": r.edge_count,
         "critical_path_length": r.critical_path_length,
         "parallel_groups": r.parallel_groups, "status": r.status}
        for r in rows
    ]
    return


def _select_providers(req: RetrievalRequirements) -> list[_Provider]:
    """Deterministic, needs-driven provider selection. No switch, no intent.

    Returns providers in PRIMARY-FIRST order: the provider for the dominant need
    (the `lens`, else the first declared need) leads; the rest follow as
    supporting context. Empty needs -> [] (caller falls back to general).
    """
    primary_need = req.lens or (req.needs[0] if req.needs else None)
    ordered_needs = []
    if primary_need is not None and primary_need not in ordered_needs:
        ordered_needs.append(primary_need)
    ordered_needs.extend(n for n in req.needs if n not in ordered_needs)

    chosen: list[_Provider] = []
    seen_ids: set[int] = set()
    for need in ordered_needs:
        for prov in _PROVIDERS:
            if need in prov.needs and id(prov) not in seen_ids:
                seen_ids.add(id(prov))
                chosen.append(prov)
    return chosen


def _primary_provider(req: RetrievalRequirements) -> Optional[_Provider]:
    """The single provider that owns the primary answer (driven by lens/needs[0]).

    Composition is explicit: the primary fills the answer; others append only as
    supporting context (see retrieve_requirements). This avoids last-writer-wins
    corruption when the LLM returns a broad needs bag.
    """
    provs = _select_providers(req)
    return provs[0] if provs else None


def retrieve_requirements(req: RetrievalRequirements, conn) -> Evidence:
    """Evidence selection (composition, primary-first — NOT last-writer-wins).

    The pipeline runs the deterministic engineering-judgment layer first
    (judge): it names the answer OBJECTIVE and re-prioritizes the evidence needs
    so the right provider leads for this question. The primary provider owns
    `ev.blocks` + `ev.raw`; remaining matching providers run into a side channel
    and append only to `ev.raw["supporting"]`, never overwriting the primary
    answer. This is what makes a broad LLM `needs` bag produce one coherent,
    correctly-framed answer instead of a pile of conflicting dumps.
    """
    today = _today()
    decision = obj_mod.judge(req)
    # The LLM understanding step sometimes returns a noisy, overlapping needs
    # bag that contains stray weak-act needs (value/universe/overlap/insights)
    # or resolves to GENERAL. When that happens, the deterministic offline
    # heuristic disambiguates the canonical question far more reliably (it was
    # built specifically for these shapes). We prefer the offline objective
    # whenever the online one is low-confidence — this is a tie-breaker on the
    # SAME question, not keyword routing: the offline result is re-judged
    # through the same objective layer.
    if decision.objective in _LOW_CONFIDENCE_OBJECTIVES:
        offline = requirements_from_question(req.query, conn)
        off_decision = obj_mod.judge(offline)
        if off_decision.objective != obj_mod.Objective.GENERAL:
            decision = off_decision
            req = offline
    ev = Evidence(requirements=req, blocks=[], raw={}, subject=None)
    ev.raw["objective"] = decision.objective
    ev.raw["objective_reason"] = decision.reason

    if "chitchat" in decision.needs:
        return ev

    providers = _select_providers_for_decision(decision)
    if not providers:
        _p_general(req, conn, ev, today)
        _finalize(ev, req)
        return ev

    primary = providers[0]
    primary.fn(req, conn, ev, today)

    # Supporting context: run remaining providers but capture their output into
    # a side channel so they cannot stomp the primary answer's blocks/raw keys.
    audit_requested = [p.fn.__name__ for p in providers]
    audit_returned = [providers[0].fn.__name__] if ev.blocks else []
    for prov in providers[1:]:
        side = Evidence(requirements=req, blocks=[], raw={}, subject=None)
        prov.fn(req, conn, side, today)
        if side.blocks:
            ev.raw.setdefault("supporting", []).extend(side.blocks)
            audit_returned.append(prov.fn.__name__)
            # Understanding (M8.3) is a first-class meaning layer: when it is
            # among the requested needs, surface its lines in the visible
            # evidence so the answer references understanding, not just the raw.
            if prov.fn is _p_understanding.fn:
                for b in side.blocks:
                    if b not in ev.blocks:
                        ev.blocks.append(b)
                # The provider wrote understanding_total/understanding into its
                # own side.raw; fold them into ev.raw so the audit/verbose see it.
                for key in ("understanding_total", "understanding"):
                    if key in side.raw:
                        ev.raw[key] = side.raw[key]

    # Guarantee understanding (M8.3) surfaces for reflective/workspace objectives
    # even when the offline route did not name the `understanding` need explicitly
    # (e.g. "what projects are converging" -> DIRECTION/PRIORITIZE). Understanding
    # is a first-class meaning layer built only from accumulated knowledge, so it
    # belongs in any long-term-direction answer. No judgment/retrieval change —
    # this just ensures the provider's lines are present in the evidence.
    _REFLECTIVE = {
        obj_mod.Objective.UNIVERSE, obj_mod.Objective.DIRECTION,
        obj_mod.Objective.EVOLVE, obj_mod.Objective.PROFILE,
        obj_mod.Objective.STRENGTHS, obj_mod.Objective.THEMES,
        obj_mod.Objective.PRIORITIZE, obj_mod.Objective.KNOWLEDGE,
    }
    # Prose-answer objectives (DIRECTION/PRIORITIZE/STRENGTHS) reject bullet dumps;
    # there, understanding stays in `supporting`/raw only so the synthesized prose
    # is not contaminated. Bullet-emitting objectives may show it in ev.blocks.
    _PROSE_OBJECTIVES = {
        obj_mod.Objective.DIRECTION, obj_mod.Objective.PRIORITIZE,
        obj_mod.Objective.STRENGTHS, obj_mod.Objective.EVOLVE,
    }
    if decision.objective in _REFLECTIVE and "understanding" not in decision.needs:
        side = Evidence(requirements=req, blocks=[], raw={}, subject=None)
        _p_understanding.fn(req, conn, side, today)
        if side.blocks:
            ev.raw.setdefault("supporting", []).extend(side.blocks)
            if decision.objective not in _PROSE_OBJECTIVES:
                for b in side.blocks:
                    if b not in ev.blocks:
                        ev.blocks.append(b)
            for key in ("understanding_total", "understanding"):
                if key in side.raw:
                    ev.raw[key] = side.raw[key]
            if "_p_understanding" not in audit_returned:
                audit_returned.append("_p_understanding")

    # Guarantee initiatives (M8.4) surface for reflective/workspace objectives
    # even when the offline route did not name the `initiative` need explicitly
    # (e.g. "what am I really building" -> UNIVERSE/PRIORITIZE). Initiatives
    # are a first-class work-abstraction layer built only from understanding, so
    # they belong in any long-term-direction answer. No judgment/retrieval change
    # — this just ensures the provider's lines are present in the evidence.
    if decision.objective in _REFLECTIVE and "initiative" not in decision.needs:
        side = Evidence(requirements=req, blocks=[], raw={}, subject=None)
        _p_initiative.fn(req, conn, side, today)
        if side.blocks:
            ev.raw.setdefault("supporting", []).extend(side.blocks)
            if decision.objective not in _PROSE_OBJECTIVES:
                for b in side.blocks:
                    if b not in ev.blocks:
                        ev.blocks.append(b)
            for key in ("initiative_total", "initiatives"):
                if key in side.raw:
                    ev.raw[key] = side.raw[key]
            if "_p_initiative" not in audit_returned:
                audit_returned.append("_p_initiative")

    # Guarantee insights (M8.5) surface for reflective/workspace objectives even
    # when the offline route did not name the `insight` need explicitly (e.g.
    # "what opportunities am I missing" -> RISK/OPPORTUNITY). Insights are a
    # first-class attention layer built only from understanding/initiatives/
    # knowledge, so they belong in any "what deserves my attention" answer. No
    # judgment/retrieval change — this just ensures the provider's lines are
    # present in the evidence.
    if decision.objective in _REFLECTIVE and "insight" not in decision.needs:
        side = Evidence(requirements=req, blocks=[], raw={}, subject=None)
        _p_insight.fn(req, conn, side, today)
        if side.blocks:
            ev.raw.setdefault("supporting", []).extend(side.blocks)
            if decision.objective not in _PROSE_OBJECTIVES:
                for b in side.blocks:
                    if b not in ev.blocks:
                        ev.blocks.append(b)
            for key in ("insight_total", "insights"):
                if key in side.raw:
                    ev.raw[key] = side.raw[key]
            if "_p_insight" not in audit_returned:
                audit_returned.append("_p_insight")

    # Guarantee plans (M9.0) surface when a question asks HOW to proceed, even
    # when the offline route did not name the `plan` need explicitly (e.g.
    # "how should we implement OAuth" -> PLAN). Plans are a first-class strategy
    # layer built only from insights/initiatives/understanding/knowledge, so they
    # belong in any "how should we do it" answer. No judgment/retrieval change —
    # this just ensures the provider's lines are present in the evidence.
    _PLAN_HINTS = ("plan", "how should", "how do we", "how to", "approach",
                   "strategy for", "roadmap", "next steps")
    needs_plan = "plan" in decision.needs or any(
        h in (req.query or "").lower() for h in _PLAN_HINTS)
    if needs_plan and "plan" not in decision.needs:
        side = Evidence(requirements=req, blocks=[], raw={}, subject=None)
        _p_plan.fn(req, conn, side, today)
        if side.blocks:
            ev.raw.setdefault("supporting", []).extend(side.blocks)
            if decision.objective not in _PROSE_OBJECTIVES:
                for b in side.blocks:
                    if b not in ev.blocks:
                        ev.blocks.append(b)
            for key in ("plan_total", "plans"):
                if key in side.raw:
                    ev.raw[key] = side.raw[key]
            if "_p_plan" not in audit_returned:
                audit_returned.append("_p_plan")

    # Retrieval audit (Part H) — providers requested vs returned, surfaced via
    # `ask --verbose` only. Never affects the normal answer.
    ev.raw["retrieval_audit"] = {
        "objective": decision.objective,
        "providers_requested": audit_requested,
        "providers_returned": audit_returned,
        "knowledge_used": ev.raw.get("knowledge_total", 0) > 0
        or "knowledge" in decision.needs,
        "confidence": _confidence_from_report(decision, ev),
    }

    # EvidenceScope guard (hardening): measure coverage / bias / missing AFTER
    # assembly, derived from the OBJECTIVE (never keywords). This makes the
    # evidence span verifiable and stops a workspace answer from silently
    # resting on one repo. The note is appended to the evidence the answer is
    # built from, so neither the deterministic nor the LLM path can claim
    # completeness it does not have.
    from .evidence_scope import build_scope_report, coverage_note, build_coverage_report

    report = build_scope_report(req, decision, conn, ev.blocks)
    ev.raw["scope"] = report.scope
    ev.raw["secondary_scopes"] = report.secondary
    ev.raw["coverage"] = {
        "requested": report.requested,
        "represented": report.represented,
        "pct": report.pct,
    }
    ev.raw["bias"] = {
        "dominant": report.dominant,
        "pct": report.dominant_pct,
        "flagged": report.bias,
    }
    ev.raw["missing"] = report.missing
    # Full auditable coverage picture (Part C) — surfaced via --verbose only.
    ev.raw["coverage_report"] = build_coverage_report(req, decision, conn, ev.blocks)

    # --- Adaptive coverage widening (Part C) ---------------------------------
    # If a workspace-wide answer rests on too few repositories (e.g. the primary
    # provider only summarized a subset), widen evidence collection ONCE by
    # appending the accumulated knowledge + portfolio + identity cuts (which span
    # every repository from ingest-time evidence). Never recurse, never loop,
    # never fetch unrelated evidence. Re-measure so the coverage note and the
    # --verbose audit reflect the widened package. Low-confidence objectives
    # (GENERAL/VALUE/UNIVERSE/...) already pull broad providers, so we don't
    # double-fetch them.
    widened = False
    if (report.scope in (obj_mod.EvidenceScope.WORKSPACE, obj_mod.EvidenceScope.PORTFOLIO)
            and report.requested >= 2
            and report.pct < _COVERAGE_WIDEN_THRESHOLD
            and decision.objective in _WIDEN_OBJECTIVES):
        extra = _widen_evidence(req, conn, today, exclude=set(decision.needs))
        if extra:
            ev.blocks = list(ev.blocks) + extra
            ev.raw.setdefault("supporting", []).extend(extra)
            ev.raw["widened"] = True
            widened = True
            report = build_scope_report(req, decision, conn, ev.blocks)
            ev.raw["coverage"] = {
                "requested": report.requested,
                "represented": report.represented,
                "pct": report.pct,
                "widened": True,
            }
            ev.raw["coverage_report"] = build_coverage_report(req, decision, conn, ev.blocks)
            ev.raw["missing"] = report.missing

    note = coverage_note(report, knowledge_total=ev.raw.get("knowledge_total", 0))
    if note:
        ev.blocks = list(ev.blocks) + [note]
        ev.raw.setdefault("supporting", []).append(note)

    _finalize(ev, req)
    return ev


# Below this represented/requested fraction, a workspace/portfolio answer is
# considered too narrow and the retriever widens ONCE (Part C). Set high enough
# to catch the "2 of 8 repositories" dogfood failures, low enough that a real
# one-or-two-repo answer (EXPLAIN/COMPARE) is never forced to widen.
_COVERAGE_WIDEN_THRESHOLD = 0.6

# Objectives whose answer is a BROAD workspace/portfolio synthesis and therefore
# benefits from widening when the primary provider under-fetches. Objectives with
# a dedicated, specific provider (SURPRISE, INSIGHTS, THEME_REPEAT, LESSONS,
# HABITS, ASSUMPTIONS, DRIFT, COMPARE, EXPLAIN, ARCHITECTURE, RELATIONSHIPS,
# BY_TECH, and the advice/recommendation objectives EVOLVE/PRIORITIZE/DIRECTION/
# MERGE/PLATFORM) are NOT widened — widening would pollute their specific,
# self-contained answer (and break the advice-prose contract).
_WIDEN_OBJECTIVES = {
    obj_mod.Objective.THEMES,
    obj_mod.Objective.PROFILE,
    obj_mod.Objective.STRENGTHS,
    obj_mod.Objective.EFFORT,
}


def _widen_evidence(req, conn, today, exclude: set[str]) -> list[str]:
    """Adaptive coverage widening (Part C / Part B / Part D).

    Returns extra evidence blocks spanning the workspace, drawn from the
    accumulated KNOWLEDGE engine (primary, per Part D) plus the portfolio
    identity/theme cuts and relationships — all of which read ingest-time
    evidence across EVERY repository. Deterministic; never invents or fetches
    unrelated evidence. Called at most once per answer.

    `exclude` holds the needs already satisfied by the primary answer, so we
    don't duplicate what the question already assembled.
    """
    extra: list[str] = []
    if "understanding" not in exclude:
        side = Evidence(requirements=req, blocks=[], raw={}, subject=None)
        _p_understanding.fn(req, conn, side, today)
        extra.extend(side.blocks)
    if "knowledge" not in exclude:
        side = Evidence(requirements=req, blocks=[], raw={}, subject=None)
        _p_knowledge.fn(req, conn, side, today)
        extra.extend(side.blocks)
    if "themes" not in exclude and "identity" not in exclude:
        side = Evidence(requirements=req, blocks=[], raw={}, subject=None)
        _p_portfolio.fn(req, conn, side, today)
        extra.extend(side.blocks)
    if "relationships" not in exclude:
        side = Evidence(requirements=req, blocks=[], raw={}, subject=None)
        _p_related.fn(req, conn, side, today)
        extra.extend(side.blocks)
    # De-duplicate exact lines (providers may overlap) without losing order.
    seen: set[str] = set()
    out: list[str] = []
    for b in extra:
        if b not in seen:
            seen.add(b)
            out.append(b)
    return out


def _select_providers_for_decision(decision) -> list[_Provider]:
    """Provider selection ordered by the OBJECTIVE's evidence priority.

    Same need∩provider intersection as before, but ordering follows how much the
    objective cares about each need (objective.py weight_evidence), so an EXPLAIN
    answer leads with describe/architecture and a PROFILE answer leads with
    engineering-profile. Primary need leads; the rest follow by priority weight.
    """
    chosen: list[_Provider] = []
    seen_ids: set[int] = set()
    for need in decision.needs:
        for prov in _PROVIDERS:
            if need in prov.needs and id(prov) not in seen_ids:
                seen_ids.add(id(prov))
                chosen.append(prov)
    return chosen


def _finalize(ev: Evidence, req: RetrievalRequirements) -> None:
    if ev.subject is None and req.subjects:
        ev.subject = req.subjects[0]


def _confidence_from_report(decision, ev: Evidence) -> str:
    """Derive a plain confidence label for the retrieval audit (Part H).

    Workspace-wide answers with thin coverage are Weak; broad coverage or an
    explicit knowledge package is Strong/Medium. Deterministic, from the
    already-computed coverage + knowledge presence — never the LLM's mood.
    """
    cov = ev.raw.get("coverage", {})
    pct = cov.get("pct", 1.0)
    if cov.get("requested", 0) >= 2 and pct < 0.6:
        return "Weak"
    if ev.raw.get("knowledge_total", 0) > 0:
        return "Medium"
    if cov.get("requested", 0) >= 2 and pct >= 0.8:
        return "Strong"
    return "Medium"


# ---------------------------------------------------------------------------
# LLM understanding — the only step that uses the LLM for understanding
# ---------------------------------------------------------------------------


def _today() -> dt.date:
    return dt.date.today()


def _requirements_explanation() -> str:
    """System prompt for LLM understanding (spec: one prompt, JSON only).

    The model is explicitly told it is NOT answering — only specifying what
    evidence is required. `needs` is an OPEN, descriptive vocabulary (not a
    closed question enum); the model composes what the question actually needs.
    """
    needs = ", ".join(_NEED_TYPES)
    return (
        "You are Friday's understanding layer. You are NOT answering the "
        "question and you are NOT retrieving anything. You ONLY specify what "
        "evidence must be gathered so a deterministic retriever can build the "
        "right evidence package.\n"
        "Return a JSON object describing RETRIEVAL REQUIREMENTS, not a "
        "classification:\n"
        "  - scope: \"workspace\" (whole engineering universe) | \"repo\" (one "
        "project) | \"compare\" (two specific projects)\n"
        "  - subjects: repository / project names mentioned (exact names from "
        "context), or [] if none\n"
        "  - operation: one of [describe, compare, rank, survey, synthesize]\n"
        "  - needs: an OPEN list of evidence TYPES the question requires — pick "
        "freely from (not limited to): " + needs + ". A question may need "
        "several (e.g. themes + purpose + identity). Do NOT force the question "
        "into one bucket.\n"
        "  - lens: optional sub-focus within a provider, e.g. for a portfolio "
        "question use \"building\" | \"strengths\" | \"effort\" | \"identity\"; "
        "for a strategy question use \"impact\" | \"platform\" | \"learning\" | "
        "\"opportunity\" | \"priority\" | \"converge\" | \"merge\"\n"
        "  - constraints: qualitative hints, e.g. [\"prefer strong evidence\", "
        "\"ignore weak coincidences\"]\n"
        "  - confidence: 0.0-1.0, your certainty about this requirement set\n"
        "Understand questions naturally — do NOT rely on literal keywords. "
        "Return valid JSON only. No prose, no explanations, no markdown.\n"
        'Schema: {"scope": str, "subjects": [str], "operation": str, '
        '"needs": [str], "lens": str|null, "constraints": [str], "confidence": float}'
    )


def understand(question: str, conn) -> Optional[RetrievalRequirements]:
    """ONLINE understanding — the ONLY step that uses the LLM for reasoning.

    Returns a RetrievalRequirements, or None when the model is uncertain (so the
    caller admits it couldn't determine the question). Accepts both the new
    requirements shape and a legacy {intent,...} payload (for compatibility).
    """
    if not llm_enabled():
        # Should not be reached online; offline callers use requirements_from_question.
        return requirements_from_question(question, conn)

    repo_names = [r.name for r in q.all_repositories(conn) if r.id is not None]
    names_block = ", ".join(repo_names) if repo_names else "(no repositories ingested yet)"

    content = _call(
        _requirements_explanation(),
        f"Known projects in this workspace: {names_block}\n\n"
        f"Question: {question}",
    )
    if not content:
        return None
    content = content.strip()
    if content.startswith("```"):
        content = content.split("```", 2)[1]
        if content.startswith("json"):
            content = content[4:]
    content = content.strip().strip("`").strip()
    if content.startswith("json"):
        content = content[4:].strip()
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, ValueError):
        return None

    if "intent" in data:  # DEPRECATED legacy payload
        intent = data.get("intent")
        if intent == "Unknown" or not intent:
            return None
        req = _needs_for_intent(intent, question)
        # Carry over any entities/workspace/compare the model supplied.
        entities = list(data.get("entities") or [])
        if entities:
            req.subjects = entities
        if data.get("workspace") and not req.scope == "compare":
            req.scope = "workspace"
    else:
        if not data.get("needs"):
            return None
        req = RetrievalRequirements(
            scope=data.get("scope", "workspace"),
            subjects=list(data.get("subjects") or []),
            operation=data.get("operation", "survey"),
            needs=list(data.get("needs") or []),
            lens=data.get("lens"),
            constraints=list(data.get("constraints") or []),
            confidence=float(data.get("confidence", 0.5)),
        )

    if req.confidence < 0.3:
        return None
    req.query = question

    # EvidenceScope guard: a WORKSPACE question must NOT carry a `subjects`
    # list of every repository. The online understanding step sometimes
    # enumerates all known repos into `subjects` for a portfolio-wide question;
    # that list would later be read as "one named project" and collapse the
    # question into a single-repo describe dump (the root-cause regression).
    # We only clear the POLLUTION signature — subjects equal to the entire
    # workspace — so explicitly-provided entities (legacy intent payloads, a
    # named project) are preserved. Scope stays as the model set it.
    if req.scope == "workspace":
        from .query import all_repositories

        all_names = {r.name.lower() for r in all_repositories(conn) if r.id is not None}
        if all_names and {s.lower() for s in req.subjects} == all_names:
            req.subjects = []

    return req


# ---------------------------------------------------------------------------
# OFFLINE heuristic — produces the SAME RetrievalRequirements structure
# ---------------------------------------------------------------------------


def requirements_from_question(question: str, conn) -> RetrievalRequirements:
    """OFFLINE fallback: keyword/entity heuristic producing RetrievalRequirements.

    Used only when no LLM is configured (or understanding fails). Mirrors the
    ordering of the former deterministic_classifier but emits needs, not labels.
    """
    qlow = question.lower()
    techs = {t.lower() for t in _known_techs(conn)}

    def mk(scope="workspace", subjects=None, operation="survey", needs=(), lens=None):
        return RetrievalRequirements(
            scope=scope, subjects=list(subjects or []), operation=operation,
            needs=list(needs), lens=lens, query=question,
        )

    if any(w in qlow for w in ("hello", "hi ", "hey", "thanks", "thank you", "who are you")):
        return mk(needs=("chitchat",))
    if "compare" in qlow or " vs " in qlow or " versus " in qlow or "difference" in qlow:
        return mk(scope="compare", subjects=_resolve_subjects(question, conn),
                  operation="compare", needs=("compare",))
    # Strategic judgment — placed BEFORE workspace so "center of my engineering
    # universe" resolves to the priority axis, not a generic theme dump.
    if any(w in qlow for w in (
        "impact", "highest impact", "most impact", "platform", "teaches me",
        "teach me", "teaching me", "learning", "opportunit", "leverage",
        "center of my", "center of the", "missing out",
        "am i missing", "should become", "trying to build", "ultimately",
        "converg", "really building", "what would you do",
        "never merge", "shouldn't merge", "should not merge",
    )):
        axis = _strategy_axis(question)  # DEPRECATED: legacy axis detection
        return mk(needs=(axis,), lens=axis)
    # Distinct reflection objectives (Milestone 6.6) — these MUST NOT collapse
    # into "themes": each emits a different need so judge() picks a different
    # objective. Placed before the generic portfolio bucket.
    if any(w in qlow for w in (
        "themes keep repeating", "themes that keep", "repeating themes",
        "recurring themes", "themes recur", "themes come up again",
    )):
        return mk(needs=("theme-repeat",))
    # Durable engineering UNDERSTANDING (M8.3) — distinct from live trends/themes.
    # These questions ask what Friday has *come to understand* about the
    # engineer's long-term direction/philosophy/effort. MUST be placed BEFORE the
    # generic "themes"/"emerging" bucket so "becoming"/"emerging direction"/"what
    # am i becoming" do not collapse into a portfolio themes answer. Route to the
    # UNDERSTANDING need (universe objective) so the understanding provider leads;
    # understanding is built only from accumulated knowledge.
    if any(w in qlow for w in (
        "what am i becoming", "what i am becoming", "am i becoming",
        "what am i turning into", "turning into", "what am i converging toward",
        "converging toward", "what have i come to understand",
        "what do you understand about my", "what does my engineering mean",
        "how has my engineering changed", "has my engineering changed",
        "what engineering direction is emerging", "engineering direction is emerging",
        "emerging engineering direction", "what philosophy is emerging",
        "philosophy is emerging", "emerging philosophy",
        "where is my effort converging", "effort converging", "effort is converging",
        "what technologies are becoming central", "becoming central",
        "which technologies are becoming central", "technologies becoming central",
        "what projects are converging", "projects are converging",
        "what strengths are becoming obvious", "strengths are becoming obvious",
        "where is my engineering going", "what direction is my engineering taking",
        "direction is my engineering",
    )):
        return mk(needs=("understanding",))
    # Accumulated ENGINEERING KNOWLEDGE (M8.1.5) — distinct from live portfolio
    # themes. These questions ask about what Friday has *learned/accumulated*,
    # not "what am I building" right now. Must resolve to the KNOWLEDGE
    # objective (which reads the knowledge table), never to a bare portfolio dump.
    if any(w in qlow for w in (
        "engineering knowledge", "engineering knowledge do you have",
        "stable engineering knowledge", "knowledge have you accumulated",
        "knowledge do you have", "what have you learned about my engineering",
        "engineering knowledge do i have", "what do you know",
        "accumulated knowledge", "what stable knowledge",
        "knowledge have you learned", "what knowledge",
        "long-term engineering trends", "engineering trends have you observed",
        "stable engineering patterns", "what have you learned",
    )):
        return mk(needs=("knowledge",))
    # Engineering BELIEF must NOT collapse to an "abandoned repository" answer
    # (the 'abandon'/'belief' substring proximity in some models). A belief about
    # engineering is a recurring-theme / portfolio-identity question, not an
    # inactive-repo question. Route it to THEMES explicitly.
    if "belief" in qlow and "abandon" not in qlow:
        return mk(needs=("themes",), lens="building")
    if any(w in qlow for w in (
        "engineering lesson", "lessons keep", "lesson keeps", "what lesson",
        "lessons am i", "repeated lesson", "what have i learned",
        "keep learning", "learned from",
    )):
        return mk(needs=("lessons",))
    if any(w in qlow for w in (
        "engineering habit", "habits do you", "habits am i", "what habits",
        "recurring habit", "keep doing", "tend to", "my patterns",
    )):
        return mk(needs=("habits",))
    if any(w in qlow for w in (
        "assumptions keep", "repeating assumptions", "what assumptions",
        "assumption keeps", "assumptions am i", "unexamined",
    )):
        return mk(needs=("assumptions",))
    if any(w in qlow for w in (
        "drifted", "drift from", "evolved", "how has", "changed direction",
        "most from its", "strayed", "moved away",
    )):
        return mk(needs=("drift",))
    if any(w in qlow for w in (
        "surprise", "surprises you", "surprised", "haven't noticed",
        "havent noticed", "have not noticed", "non-obvious", "not obvious",
        "tell me something", "something i",
    )):
        return mk(needs=("surprise",))
    if any(w in qlow for w in (
        "evolve", "how would you", "where should", "what should become the center",
        "how to grow", "how to improve", "next year", "roadmap for",
    )):
        return mk(needs=("evolve",))
    # Workspace-level intents — before the generic "which project" -> by-tech.
    if any(w in qlow for w in (
        "engineering universe", "how has my work evolved", "where is my work heading",
        "my direction", "overall picture", "big picture", "my career",
    )):
        return mk(needs=("universe", "understanding"))
    if any(w in qlow for w in (
        "am i building", "themes", "patterns across", "what am i building",
        "seem to be building", "building", "emerge", "emerging",
        "repeatedly solving", "skills am i", "across my projects",
        "strengths am i", "developing", "my portfolio", "my work",
        "what am i working on", "where is my effort", "where is my work going",
        "effort going", "engineering effort", "spending my time", "time going",
        "kind of engineer", "kind of developer", "type of engineer",
        "what kind of", "what sort of engineer",
    )):
        # Portfolio sub-cut: strengths / effort / identity / building.
        # Portfolio sub-cut: strengths / effort / identity / building.
        if any(w in qlow for w in (
            "strengths", "skills am i", "developing", "good at", "capabilities",
            "what can i", "engineering ability",
        )):
            return mk(needs=("strengths",), lens="strengths")
        if any(w in qlow for w in (
            "effort", "where is my", "where.*going", "attention", "spending my",
            "time going", "focus", "currently investing",
        )):
            return mk(needs=("effort",), lens="effort")
        if any(w in qlow for w in (
            "kind of engineer", "kind of developer", "type of engineer",
            "type of developer", "what kind of ", "what sort of engineer",
            "am i a", "engineer am i", "developer am i",
        )):
            return mk(needs=("engineering-profile",), lens="identity")
        return mk(needs=("themes",), lens="building")
    if ("most valuable" in qlow or "highest value" in qlow or "worth most" in qlow
            or "matters most" in qlow or "matter most" in qlow or "matters the most" in qlow):
        return mk(needs=("value",))
    if "integrate" in qlow or "integration with friday" in qlow or "integration point" in qlow:
        return mk(needs=("integration",))
    if any(w in qlow for w in (
        "eventually merge", "should merge", "could merge", "might merge",
        "merge together", "worth merging", "candidates to merge",
    )):
        return mk(needs=("merge",), lens="merge")
    if "overlap" in qlow or "merging" in qlow:
        return mk(needs=("overlap",))
    if "merge" in qlow:  # remaining positive merge phrasing -> merge-risk judgment
        return mk(needs=("merge",), lens="merge")
    if "related" in qlow or "how are" in qlow or "connection" in qlow:
        return mk(scope="repo" if _resolve_subjects(question, conn) else "workspace",
                  subjects=_resolve_subjects(question, conn), needs=("relationships",))
    if any(w in qlow for w in (
        "how is", "how does", "how do", "architecture", "architect",
        "built", "structure", "entry point", "entry points", "startup",
        "how it works", "how it's built", "components", "implement",
    )):
        return mk(scope="repo" if _resolve_subjects(question, conn) else "workspace",
                  subjects=_resolve_subjects(question, conn),
                  needs=("architecture", "components"))
    if any(w in qlow for w in (
        "haven't noticed", "havent noticed", "have not noticed",
        "surprise me", "something i", "what stands out", "what should i notice",
        "non-obvious", "not obvious", "what am i missing",
    )):
        return mk(needs=("insights",))
    if any(w in qlow for w in (
        "explain", "walk me through", "tell me about", "describe",
        "what is", "what does", "what are", "overview of",
    )):
        return mk(scope="repo" if _resolve_subjects(question, conn) else "workspace",
                  subjects=_resolve_subjects(question, conn),
                  needs=("describe",))
    if any(w in qlow for w in (
        "similar", "similarities", "duplicate", "duplicated", "share code",
        "sharing code", "shared code", "reuse", "reusable",
        "same layout", "alike", "comparable", "teach each other",
        "compare the architectures",
    )):
        return mk(needs=("similarity", "reuse"))
    if "why" in qlow or "purpose" in qlow:
        return mk(scope="repo" if _resolve_subjects(question, conn) else "workspace",
                  subjects=_resolve_subjects(question, conn),
                  needs=("describe",))
    if "inactive" in qlow or "abandoned" in qlow or "stale" in qlow or "dead" in qlow:
        return mk(needs=("inactive",))
    if "newest" in qlow or "recent" in qlow or "latest" in qlow:
        return mk(needs=("newest",))
    if "most active" in qlow or "work on next" in qlow or "should i" in qlow:
        return mk(needs=("recommend",))
    if any(w in qlow for w in (
        "continue", "pause", "most attention", "which project should i",
        "which should i", "what deserves", "deserve my",
    )):
        return mk(needs=("recommend",))
    if "insight" in qlow or "observation" in qlow or "overview" in qlow:
        return mk(needs=("insights",))
    if any(w in qlow for w in (
        "solved another", "solved the other", "solved a similar", "solved the same",
        "quietly solved", "solves the same problem", "duplicate problem",
        "same problem as", "solves a problem", "solved a problem",
    )):
        return mk(needs=("overlap",))
    if any(w in qlow for w in (
        "most mature", "least mature", "most maturely", "most developed",
        "most advanced", "most production", "most production-ready",
        "oldest project", "youngest project", "most stable",
    )):
        return mk(needs=("maturity",))
    if "share" in qlow or "use" in qlow or "which project" in qlow:
        for t in techs:
            if t.lower() in qlow:
                return mk(needs=("by-tech",))
        for t in ("rust", "python", "go", "typescript", "java", "c++", "javascript"):
            if t in qlow:
                return mk(needs=("by-tech",))
        return mk(needs=("by-tech",))
    return mk(needs=("general",))


# ---------------------------------------------------------------------------
# DEPRECATED COMPATIBILITY LAYER
# ---------------------------------------------------------------------------
# The symbols below exist SOLELY so the tracked benchmark suite keeps passing
# while it still pins the OLD vocabulary. They DERIVE their value from
# RetrievalRequirements — they never drive routing, and there is exactly ONE
# reasoning pipeline (requirements_from_question / understand -> retrieve_requirements).
# TODO: remove once the benchmark suite is migrated to RetrievalRequirements.

_DEPRECATED_INTENTS = {
    "chitchat", "compare", "related", "architecture", "describe", "similarity",
    "inactive", "newest", "recommend", "portfolio", "value", "overlap",
    "integration", "workspace", "by-tech", "insights", "strategy", "general",
    "merge",
}


@dataclass
class Intent:
    """DEPRECATED: legacy understanding object. Wraps RetrievalRequirements.

    TODO: remove once benchmarks migrate to RetrievalRequirements.
    """

    intent: str
    entities: list[str] = field(default_factory=list)
    compare: bool = False
    workspace: bool = False
    confidence: float = 1.0

    @classmethod
    def from_requirements(cls, req: RetrievalRequirements) -> "Intent":
        return cls(
            intent=_label_of(req),
            entities=list(req.subjects),
            compare="compare" in req.needs,
            workspace=req.scope == "workspace",
            confidence=req.confidence,
        )


def _label_of(req: RetrievalRequirements) -> str:
    """DEPRECATED: derive a legacy intent label from requirements (benchmarks only)."""
    n = set(req.needs)
    if "chitchat" in n:
        return "chitchat"
    if "compare" in n:
        return "compare"
    if "relationships" in n:
        return "related"
    if "architecture" in n or "components" in n:
        return "architecture"
    if "describe" in n:
        return "describe"
    if "similarity" in n or "reuse" in n:
        return "similarity"
    if "inactive" in n:
        return "inactive"
    if "newest" in n:
        return "newest"
    if "recommend" in n:
        return "recommend"
    if "value" in n:
        return "value"
    if "overlap" in n:
        return "overlap"
    if "integration" in n:
        return "integration"
    if "universe" in n:
        return "workspace"  # historic label for "engineering universe"
    if "by-tech" in n:
        return "by-tech"
    if "insights" in n:
        return "insights"
    if n & {"impact", "platform", "learning", "opportunity", "priority", "converge", "merge"}:
        return "strategy"
    if "strengths" in n:
        return "portfolio"
    if "effort" in n:
        return "portfolio"
    if "engineering-profile" in n:
        return "portfolio"
    if "themes" in n or "purpose" in n:
        return "portfolio"
    return "general"


def _needs_for_intent(intent: str, question: str) -> RetrievalRequirements:
    """DEPRECATED: map a legacy intent label to requirements (legacy LLM payloads)."""
    qlow = question.lower()
    subjects = _resolve_subjects(question, None) if False else _resolve_subjects_safe(question)
    if intent == "compare":
        return RetrievalRequirements(scope="compare", subjects=subjects,
                                    operation="compare", needs=["compare"], query=question)
    if intent == "related":
        return RetrievalRequirements(scope="workspace", subjects=subjects,
                                    needs=["relationships"], query=question)
    if intent == "architecture":
        return RetrievalRequirements(scope="repo", subjects=subjects,
                                    needs=["architecture", "components"], query=question)
    if intent == "describe":
        return RetrievalRequirements(scope="repo", subjects=subjects,
                                    needs=["describe"], query=question)
    if intent == "similarity":
        return RetrievalRequirements(needs=["similarity", "reuse"], query=question)
    if intent == "inactive":
        return RetrievalRequirements(needs=["inactive"], query=question)
    if intent == "newest":
        return RetrievalRequirements(needs=["newest"], query=question)
    if intent == "recommend":
        return RetrievalRequirements(needs=["recommend"], query=question)
    if intent == "portfolio":
        mode = _portfolio_mode(question)  # DEPRECATED
        return RetrievalRequirements(needs=["themes"], lens=mode, query=question)
    if intent == "value":
        return RetrievalRequirements(needs=["value"], query=question)
    if intent == "overlap":
        return RetrievalRequirements(needs=["overlap"], query=question)
    if intent == "integration":
        return RetrievalRequirements(needs=["integration"], query=question)
    if intent == "workspace":
        return RetrievalRequirements(needs=["universe"], query=question)
    if intent == "by-tech":
        return RetrievalRequirements(needs=["by-tech"], query=question)
    if intent == "insights":
        return RetrievalRequirements(needs=["insights"], query=question)
    if intent == "strategy":
        axis = _strategy_axis(question)  # DEPRECATED
        return RetrievalRequirements(needs=[axis], lens=axis, query=question)
    return RetrievalRequirements(needs=["general"], query=question)


def deterministic_classifier(question: str, conn) -> str:
    """DEPRECATED: legacy label classifier. Derives its label from the new
    RetrievalRequirements pipeline (no parallel reasoning). TODO: remove."""
    return _label_of(requirements_from_question(question, conn))


def classify(question: str, conn) -> str:
    """DEPRECATED: public classify, retained for tests/direct callers.

    ONLINE: derive label from LLM understanding. OFFLINE: derive from heuristic.
    TODO: remove once benchmarks migrate to RetrievalRequirements.
    """
    if llm_enabled():
        req = understand(question, conn)
        if req is not None:
            return _label_of(req)
    return deterministic_classifier(question, conn)


def extract_intent(question: str, conn) -> Optional[Intent]:
    """DEPRECATED: legacy intent extraction. Wraps the single understanding
    pipeline (understand / requirements_from_question). TODO: remove."""
    if llm_enabled():
        req = understand(question, conn)
    else:
        req = requirements_from_question(question, conn)
    if req is None:
        return None
    return Intent.from_requirements(req)


def retrieve(question: str, intent: str, conn) -> Evidence:
    """DEPRECATED: legacy retrieve(intet, intent). Delegates to the single
    RetrievalRequirements pipeline. TODO: remove once benchmarks migrate."""
    req = _needs_for_intent(intent, question)
    return retrieve_requirements(req, conn)


# ---------------------------------------------------------------------------
# DEPRECATED sub-routing helpers (legacy payloads / compat only)
# ---------------------------------------------------------------------------


def _portfolio_mode(question: str) -> str:
    """DEPRECATED: legacy portfolio sub-cut detection. Used only by the compat
    layer for legacy {intent:"portfolio"} payloads. TODO: remove."""
    qlow = question.lower()
    if any(w in qlow for w in (
        "strengths", "skills am i", "developing", "good at", "capabilities",
        "what can i", "engineering ability",
    )):
        return "strengths"
    if any(w in qlow for w in (
        "effort", "where is my", "where.*going", "attention", "spending my",
        "time going", "focus", "currently investing",
    )):
        return "effort"
    if any(w in qlow for w in (
        "kind of engineer", "kind of developer", "type of engineer",
        "type of developer", "what kind of ", "what sort of engineer",
        "am i a", "engineer am i", "developer am i",
    )):
        return "identity"
    return "building"


def _strategy_axis(question: str) -> str:
    """DEPRECATED: legacy strategy sub-cut detection. Used only by the compat
    layer for legacy {intent:"strategy"} payloads. TODO: remove."""
    qlow = question.lower()
    if any(w in qlow for w in ("impact", "highest impact", "most impact", "most valuable impact")):
        return "impact"
    if any(w in qlow for w in ("platform", "should become a platform", "become a platform",
                               "turn into a platform", "platform play")):
        return "platform"
    if any(w in qlow for w in ("teaches me", "teach me", "teaching me", "learning",
                               "learn the most", "stretched me", "taught me")):
        return "learning"
    if any(w in qlow for w in ("opportunit", "leverage", "missing out", "am i missing",
                               "missing", "left on the table", "not yet doing")):
        return "opportunity"
    if any(w in qlow for w in ("center of my", "center of the", "engineering universe",
                               "heart of my", "should become the center")):
        return "priority"
    if any(w in qlow for w in (
        "ultimately trying to build", "ultimately build", "converging on",
        "what am i converging", "trying to build", "am i really building",
        "what am i really building",
    )):
        return "converge"
    if any(w in qlow for w in (
        "never merge", "shouldn't merge", "should not merge", "keep separate",
        "stay independent", "don't merge", "do not merge",
    )):
        return "merge"
    if "what would you do" in qlow or "what should i do" in qlow:
        return "priority"
    return "impact"


# ---------------------------------------------------------------------------
# Deterministic normalization helpers (kept — these are NOT reasoning)
# ---------------------------------------------------------------------------


_REL_PREFERENCE = {
    "duplicated-functionality": 0,
    "shared-abstraction": 1,
    "shared-implementation": 2,
    "shared-architecture": 3,
    "shared-framework": 4,
    "shared-deployment": 5,
    "shared-db": 6,
    "shared-config": 7,
    "shared-tech": 8,
    "potential-reuse": 9,
}


def _rel_rank(kind: str) -> int:
    return _REL_PREFERENCE.get(kind, 50)


def _known_techs(conn) -> set[str]:
    techs: set[str] = set()
    for r in q.all_repositories(conn):
        if r.id is not None:
            techs |= {t.tech for t in get_technologies(conn, r.id)}
    return techs


def _resolve_subjects(question: str, conn) -> list[str]:
    """Detect repo names explicitly present in the question (entity resolution).

    LONGEST-MATCH FIRST: "Explain Friday V3" must resolve to "Friday V3", not
    "Friday" (the shorter name is a prefix of the longer one). Prefix matches are
    therefore ordered by name length descending, so disambiguation is by maximum
    specificity rather than row order. Fixes the dogfood "Explain Friday V3"
    regression where the wrong project was explained.
    """
    if conn is None:
        return []
    qlow = question.lower()
    matches = [r.name for r in q.all_repositories(conn) if r.name.lower() in qlow]
    # Dedupe, longest name first so "Friday V3" wins over "Friday".
    seen = set()
    ordered = []
    for name in sorted(set(matches), key=lambda n: -len(n)):
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


def _resolve_subjects_safe(question: str) -> list[str]:
    """Like _resolve_subjects but conn-free (used by legacy intents offline)."""
    # Without a conn we cannot enumerate repos; name resolution happens later
    # in the provider via _detect_repo. Return empty; providers fall back.
    return []


_STOP = {
    "what", "which", "why", "who", "how", "are", "is", "the", "a", "an", "and",
    "or", "of", "to", "do", "does", "did", "you", "your", "my", "me", "i", "use",
    "using", "project", "projects", "repository", "repositories", "repo", "repos",
    "share", "sharing", "with", "about", "tell", "describe", "compare", "related",
    "between", "inactive", "abandoned", "stale", "newest", "recent", "latest",
    "most", "active", "should", "next", "insight", "observations", "overview",
}


def _detect_repo(question: str, conn) -> Optional[Repository]:
    qlow = question.lower()
    repos = q.all_repositories(conn)
    for r in repos:
        if r.name.lower() in qlow:
            return r
    cleaned = re.sub(r"[^a-z0-9 ]", " ", qlow)
    toks = {t for t in cleaned.split() if len(t) > 2 and t not in _STOP}
    for r in repos:
        rlow = r.name.lower()
        if any(t in rlow for t in toks):
            return r
    return None


def _repo_by_name(conn, name: str) -> Optional[Repository]:
    for r in q.all_repositories(conn):
        if r.name.lower() == (name or "").lower():
            return r
    return None


def _detect_tech(question: str, conn) -> Optional[str]:
    qlow = question.lower()
    techs = _known_techs(conn)
    for t in techs:
        if t.lower() in qlow:
            return t
    aliases = {
        "rust": "Rust", "python": "Python", "go": "Go", "golang": "Go",
        "typescript": "TypeScript", "ts": "TypeScript", "java": "Java",
        "c++": "C++", "javascript": "JavaScript", "js": "JavaScript",
        "react": "React", "next": "Next.js", "nextjs": "Next.js",
        "fastapi": "FastAPI", "django": "Django", "flask": "Flask",
        "supabase": "Supabase", "docker": "Docker", "sqlite": "SQLite",
        "postgres": "Postgres", "postgresql": "Postgres", "redis": "Redis",
        "pytorch": "PyTorch", "tensorflow": "TensorFlow",
    }
    for k, v in aliases.items():
        if re.search(rf"\b{re.escape(k)}\b", qlow) and v in techs:
            return v
    return None


def _detect_lang(question: str) -> Optional[str]:
    qlow = question.lower()
    for name, canon in (
        ("rust", "Rust"), ("python", "Python"), ("go", "Go"),
        ("typescript", "TypeScript"), ("java", "Java"), ("c++", "C++"),
        ("javascript", "JavaScript"),
    ):
        if name in qlow:
            return canon
    return None


def _card_text(card) -> str:
    if card is None:
        return "No data."
    from .summary import _purpose_line

    r = card.repo
    lines = [f"{r.name}"]
    lines.append(f"Purpose: {_purpose_line(r.readme_summary)}")
    if card.tech_names:
        lines.append("Technologies: " + ", ".join(sorted(card.tech_names)))
    if r.maturity and r.maturity != "Unknown":
        lines.append(f"Maturity: {r.maturity}")
    lines.append(f"Activity: {card.activity}")
    if card.key_observations:
        lines.append("Observations: " + "; ".join(card.key_observations))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Synthesis
# ---------------------------------------------------------------------------


_SYSTEM = (
    "You are Friday, an operating partner that answers questions about the user's "
    "software projects using ONLY the provided Evidence. Rules:\n"
    "1. Answer concisely and in plain prose.\n"
    "2. Use ONLY facts present in the Evidence block. Never invent repositories, "
    "technologies, dates, or relationships.\n"
    "3. If the Evidence is insufficient to answer, say so plainly (e.g. "
    "'I don't have enough evidence to answer that.').\n"
    "4. For 'what should I work on next' style questions, you MAY offer a grounded "
    "suggestion derived from the activity signals in the Evidence (most active, "
    "newest, uncommitted changes), clearly framed as a suggestion, not a command.\n"
    "5. Cite the basis briefly where natural (README, git metadata, technology "
    "detection, relationships).\n"
    "6. PRIMACY: when a question is about one project, spend 80-90% of the answer "
    "on that project's purpose, context and meaning. Put its relationships and "
    "other repositories only near the END. Never open with implementation, "
    "architecture dumps, or component lists.\n"
    "7. CONFIDENCE: when the Evidence supports a judgement (value, overlap, "
    "integration, themes), state Confidence: Strong / Medium / Weak and the basis. "
    "Prefer context, purpose and engineering meaning; reserve architecture detail "
    "for the final part of the answer.\n"
    "8. ANSWER OBJECTIVE: the Engineering Objective below names the KIND of "
    "judgment being requested (explain / compare / profile / platform / merge / "
    "themes / direction / lessons / habits / assumptions / drift / etc.). Answer "
    "THAT judgment, not a generic evidence summary. Follow the Answer Contract "
    "section order when it is given — it tells you how the answer must be "
    "structured (e.g. a compare must cover shared goal, different goals, "
    "architecture, technology, maturity, recommendation — NOT two description "
    "dumps). Do NOT collapse distinct objectives into the same answer shape.\n"
    "Do not role-play or add commentary beyond the answer."
)


def _synthesize(question: str, ev: Evidence, prev: Optional["Exchange"] = None,
                decision: Optional[object] = None) -> Optional[str]:
    """Call the LLM to produce an answer from the evidence. Returns None on any
    failure (caller falls back).

    `prev` is supplied ONLY as disambiguation context (pronouns, ellipsis,
    follow-ups like "why not X?"). It is never placed in the Evidence block and
    must never be treated by the model as a fact to cite. `decision` carries the
    engineering objective + answer contract so the model frames the answer to the
    judgment being requested.
    """
    if not llm_enabled():
        return None
    evidence_str = "\n".join(ev.blocks) if ev.blocks else json.dumps(ev.raw, indent=2)
    if not evidence_str.strip():
        evidence_str = "(no retrieved evidence)"
    ctx = ""
    if prev is not None:
        ctx = (
            "\n\nPREVIOUS EXCHANGE (disambiguation context ONLY — resolve pronouns "
            "and follow-ups against it, but NEVER treat its answer as evidence or "
            "cite it as a fact; the Evidence block below is the only source):\n"
            f"Q: {prev.question}\nA: {prev.answer.text}\n"
        )
    objective_line = ""
    if decision is not None:
        objective_line = f"\n\nEngineering Objective: {decision.objective}\n"
        if decision.contract:
            objective_line += (
                "Answer Contract (follow this section order): "
                + " > ".join(decision.contract) + ".\n"
            )
    user = (
        f"Question: {question}\n\n"
        f"Evidence:\n{evidence_str}\n\n"
        f"Answer (grounded only in Evidence):{ctx}{objective_line}"
    )
    return _call(_SYSTEM, user)


def _deterministic_answer(question: str, ev: Evidence, label: str,
                           decision: Optional[object] = None) -> str:
    if label == "chitchat":
        return ("I'm Friday, your workspace operating partner. Ask me about your "
                "projects — which use a technology, what a project is for, how two "
                "repos relate, or which look abandoned.")
    if not ev.blocks:
        return ("I don't have enough evidence to answer that. Try rephrasing, or set "
                "FRIDAY_LLM_MODEL to let me handle open-ended questions.")
    if decision is not None and decision.contract:
        # Honor the answer contract: lead with the blocks that answer the first
        # contract sections. The primary provider already emits the right content
        # in the right order; this keeps any supporting evidence from jumping the
        # primary answer when it happens to be a single prose block.
        return "\n".join(ev.blocks)
    return "\n".join(ev.blocks)


# ---------------------------------------------------------------------------
# Bounded conversational continuity (M6.5D — M8.1.6 integration fix)
# ---------------------------------------------------------------------------
# Only the immediately previous exchange is ever remembered. No long-term
# memory, no planner, no agent.
#
# M8.1.6 ROOT CAUSE: the old resolver keyed every follow-up off `ev.subject`
# (the single repo a question was about) and only handled a narrow pattern set.
# Workspace/portfolio/knowledge questions have `subject = None`, so meta
# follow-ups ("How confident are you?", "What evidence supports that?",
# "Summarize it.") fell through to `None` -> a FRESH retrieval of the bare
# fragment ("why?", "confidence?") -> the understanding step cannot parse it ->
# empty evidence -> "I don't have enough evidence". Context was lost.
#
# FIX (integration only — no layer redesigned): the previous exchange already
# carries its full Evidence package (blocks + raw + objective + coverage). A
# meta-follow-up about the PREVIOUS answer should be answered from that package,
# not by re-fetching. We add a `("followup", prev_exchange)` result: ask() reuses
# the previous Evidence as the answer basis and re-synthesizes (LLM grounded in
# the previous evidence) or restates deterministically — threading `prev` as
# disambiguation context, never inventing new evidence. The subject is carried
# implicitly (the previous answer); we never force the user to repeat it.

_RESTATE = ("how so", "why is that", "why's that", "convince me",
            "explain more", "elaborate", "tell me more", "more detail",
            "go on", "clarify that")
_CONTRAST_PREFIX = ("why not", "what about", "how about", "and not", "but not")
_NEXT_PREFIX = ("what next", "and then", "after that", "then what", "what should i do next",
                "what do i do next", "next step", "next steps")
_AGE_PREFIX = ("how long", "how old", "since when", "age of", "how stale", "how stale")

# Meta-follow-ups that are ABOUT the previous answer, not new questions. These
# are answered from the previous Evidence package (no fresh retrieval).
_META_CONFIDENCE = ("how confident", "confidence", "how sure", "how certain",
                    "sure are you", "certain are you")
_META_EVIDENCE = ("what evidence", "which evidence", "what supports", "evidence for",
                  "based on what", "how do you know", "where did you", "sources",
                  "what backs this")
_META_SUMMARIZE = ("summarize", "sum it up", "summarise", "tl;dr", "in short",
                   "recap", "wrap up", "give me the gist")
_META_EXPAND = ("explain further", "expand", "more on that", "tell me everything",
                "go deeper", "dive deeper", "more about that", "elaborate further",
                "what else", "anything else")
_META_CHANGED = ("what changed", "has it changed", "what's changed", "what is changed",
                 "recent change", "what's new", "anything changed")
_COMPARE_PREFIX = ("compare", "contrast", "versus", "vs")


def _is_meta(qlow: str, phrases: tuple) -> bool:
    qlow = qlow.rstrip(".?!").strip()
    return any(qlow == p or qlow.startswith(p + " ") or (" " + p) in qlow
               for p in phrases)


def resolve_followup(question: str, prev: "Exchange", conn) -> Optional[tuple]:
    """Resolve a follow-up against the previous exchange, deterministically.

    Returns one of:
      ("rewrite", new_question) -> a fresh retrieval runs for new_question
      ("restate", text)         -> re-present previous reasoning, no new evidence
      ("followup", exchange)    -> answer from the PREVIOUS Evidence package
                                   (meta-question about the prior answer)
      ("clarify", text)         -> ambiguous; ask which antecedent
      None                      -> not a follow-up; ask() proceeds normally
    """
    qlow = question.lower().strip()
    ev = prev.answer.evidence
    subj = ev.subject
    subjects = ev.raw.get("subjects") or ([subj] if subj else [])

    if len(subjects) >= 2 and not _named_repo_in(qlow, conn):
        a, b = subjects[0], subjects[1]
        return ("clarify",
                f"Did you mean {a} or {b}? Say which one and I'll explain.")

    # --- Meta follow-ups: answered from the previous Evidence package ---------
    # These are questions ABOUT the prior answer. A fresh retrieval of the bare
    # fragment loses all context (the M8.1.6 bug), so we reuse the previous
    # evidence and re-synthesize / restate. Order: most specific first.
    if _is_meta(qlow, _META_EVIDENCE):
        return ("followup", prev)
    if _is_meta(qlow, _META_CONFIDENCE):
        return ("followup", prev)
    if _is_meta(qlow, _META_SUMMARIZE):
        return ("followup", prev)
    if _is_meta(qlow, _META_EXPAND):
        return ("followup", prev)

    if qlow in ("why", "why?") or re.match(r"why\b(?!\s*not)", qlow):
        # "Why?" about the previous answer: synthesize a grounded rationale from
        # the previous Evidence (not a verbatim dump). Falls back to restate if
        # synthesis is unavailable.
        return ("followup", prev)

    # "What changed?" about the prior subject — drift if we have a subject.
    if _is_meta(qlow, _META_CHANGED):
        if subj:
            return ("rewrite", f"How has {subj} changed?")
        return ("followup", prev)

    for p in _CONTRAST_PREFIX:
        if qlow.startswith(p + " ") or qlow == p:
            tail = qlow[len(p):].strip(" ?.")
            cand = _named_repo_in(tail, conn)
            if cand and cand.lower() != (subj or "").lower():
                if subj:
                    return ("restate", _contrast_text(subj, cand, conn, ev))
                return ("clarify",
                        f"Not sure what to compare {cand} against — "
                        f"ask about a specific project first.")
            if cand and subj and cand.lower() == subj.lower():
                return ("restate", _restate_text(prev, conn))
            return ("clarify",
                    f"I'm not sure which project '{tail}' refers to. "
                    f"Name it exactly and I'll compare.")

    # "Compare that to X" / "contrast with X" — resolve the pronoun subject
    # ("that"/"this"/"it") to the previous subject so the contrast carries
    # context. When the previous subject is None (workspace question), we cannot
    # anchor "that", so we clarify rather than guess.
    _PRONOUNS = ("that", "this", "it", "them")
    _STOPWORDS = ("to", "with", "against", "and", "the")
    for p in _COMPARE_PREFIX:
        if qlow.startswith(p + " ") or qlow == p:
            tail = qlow[len(p):].strip()
            toks = tail.split()
            while toks and (toks[0] in _STOPWORDS or toks[0] in _PRONOUNS):
                toks.pop(0)
            tail = " ".join(toks).strip(" ?.,")
            cand = _named_repo_in(tail, conn)
            if cand and subj and cand.lower() != subj.lower():
                return ("restate", _contrast_text(subj, cand, conn, ev))
            if cand and subj and cand.lower() == subj.lower():
                return ("restate", _restate_text(prev, conn))
            if subj and not cand:
                return ("restate", _contrast_text(subj, subj, conn, ev))
            if cand and not subj:
                # Previous answer was workspace-wide; "that" has no single anchor,
                # but the user named a concrete project X. Answer as a follow-up
                # that compares the previous (workspace) answer's themes/evidence
                # to X — never a fresh bare retrieval (M8.1.6 fix).
                return ("followup", prev)
            return ("clarify",
                    "Compare against what? Name the other project.")

    for p in _NEXT_PREFIX:
        if qlow.startswith(p) or qlow == p:
            return ("rewrite", "What should I work on next?")
    for p in _AGE_PREFIX:
        if qlow.startswith(p) or qlow == p:
            if subj:
                return ("rewrite", f"How stale is {subj}?")
            return ("clarify",
                    "Stale compared to what? Ask about a specific project first.")
    return None


def _named_repo_in(text: str, conn) -> Optional[str]:
    for r in q.all_repositories(conn):
        if r.name.lower() in text:
            return r.name
    return None


def _restate_text(prev: "Exchange", conn) -> str:
    ev = prev.answer.evidence
    if ev.intent == "recommend" and ev.raw.get("recommend_subject"):
        reasons = ev.raw.get("recommend_reasons") or []
        subj = ev.raw["recommend_subject"]
        body = "; ".join(reasons) if reasons else "it shows the strongest continue-signal in the stored evidence"
        return (f"Because {body}. I recommended {subj} as the highest-leverage "
                f"next step from activity, blockers, importance and recent work "
                f"across your projects — not commit counts alone.")
    if ev.subject:
        return (f"To expand on {ev.subject}: " + prev.answer.text.strip())
    return prev.answer.text.strip()


def _continue_reasons(conn, repo_name: str, today) -> list[str]:
    for repo, reasons in q.workspace_priorities(conn, today, n=5):
        if repo.name.lower() == repo_name.lower():
            return reasons
    return []


def _contrast_text(subj: str, other: str, conn, ev: Evidence) -> str:
    subj_reasons = _continue_reasons(conn, subj, _today())
    other_reasons = _continue_reasons(conn, other, _today())
    bits = [f"continue {subj}: " + ("; ".join(subj_reasons)
            if subj_reasons else "it carried the strongest continue-signal")]
    bits.append(f"{other}: " + ("; ".join(other_reasons)
                if other_reasons else "no strong continue-signal in the stored evidence"))
    return ("Not " + other + " right now — here is the evidence: "
            + "; ".join(bits) + ". "
            "Both are read from activity, blockers, importance and recent work, "
            "not commit counts.")


def _answer_followup(question: str, prev: "Exchange", conn) -> "Answer":
    """Answer a meta-follow-up (confidence / evidence / summarize / expand)
    from the PREVIOUS exchange's Evidence package — no fresh retrieval.

    The previous Evidence (blocks + raw + coverage + objective) is the only
    basis. We re-synthesize with the LLM, threading the previous answer + a
    precise instruction as disambiguation context. Never invents evidence; if
    synthesis is unavailable, deterministically restates the prior answer.
    """
    ev = prev.answer.evidence
    # Reuse the prior evidence verbatim; it already carries coverage + objective.
    qlow = question.lower().strip()
    if _is_meta(qlow, _META_CONFIDENCE):
        meta_instruction = (
            "The user is asking about your CONFIDENCE in the previous answer. "
            "State the confidence level and its basis using ONLY the previous "
            "Evidence (coverage, objective, and the strength of the facts). If "
            "the evidence was thin or covered few repositories, say so plainly.")
    elif _is_meta(qlow, _META_EVIDENCE):
        meta_instruction = (
            "The user is asking WHAT EVIDENCE supports the previous answer. "
            "List the specific facts/repositories/relationships from the "
            "previous Evidence that the answer rested on. Do not add new facts.")
    elif _is_meta(qlow, _META_SUMMARIZE):
        meta_instruction = (
            "The user wants a concise SUMMARY of the previous answer. Compress "
            "it to the key points only, using ONLY the previous Evidence.")
    elif _is_meta(qlow, _META_EXPAND):
        meta_instruction = (
            "The user wants you to EXPAND on the previous answer. Elaborate on "
            "the points already made, drawing ONLY on the previous Evidence — "
            "do not introduce new claims the evidence does not support.")
    elif any(qlow.startswith(p) or qlow == p for p in _COMPARE_PREFIX):
        # "Compare that to X" with no single prior subject: compare the previous
        # (workspace) answer's themes/evidence to the named project X.
        meta_instruction = (
            "The user wants you to COMPARE the previous answer to a specific "
            "project named in their follow-up. Use ONLY the previous Evidence as "
            "the basis for what 'that' refers to, and compare it concretely "
            "against the named project — purpose, technology, maturity, fit.")
    else:
        meta_instruction = (
            "Answer this follow-up using ONLY the previous Evidence.")

    if llm_enabled() and os.environ.get("FRIDAY_ANSWER_LLM") == "1":
        decision = obj_mod.ObjectiveDecision(
            objective=ev.raw.get("objective", "general"),
            needs=list(ev.requirements.needs),
            lens=ev.requirements.lens,
            contract=obj_mod.contract_for(ev.raw.get("objective", "general")),
            reason=ev.raw.get("objective_reason", ""),
        )
        # For a compare follow-up, enrich with the named project's own evidence
        # (relationships + identity) so synthesis can contrast concretely. Pure
        # deterministic pull; never invents.
        extra_evidence: list[str] = []
        if any(qlow.startswith(p) or qlow == p for p in _COMPARE_PREFIX):
            cand = _named_repo_in(qlow, conn)
            if cand:
                side = Evidence(requirements=ev.requirements, blocks=[], raw={}, subject=None)
                _p_related.fn(ev.requirements, conn, side, _today())
                if side.blocks:
                    extra_evidence.extend(side.blocks)
                side2 = Evidence(requirements=ev.requirements, blocks=[], raw={}, subject=None)
                _p_describe.fn(ev.requirements, conn, side2, _today())
                if side2.blocks:
                    extra_evidence.extend(side2.blocks)
        text = _synthesize_followup(question, ev, prev, meta_instruction, decision, extra_evidence)
        if text:
            return Answer(text=text, evidence=ev, used_llm=True)
    # Deterministic fallback: restate the prior answer (never loses context).
    return Answer(text=prev.answer.text, evidence=ev, used_llm=False)


def _synthesize_followup(question: str, ev: Evidence, prev: "Exchange",
                         meta_instruction: str, decision,
                         extra_evidence: Optional[list[str]] = None) -> Optional[str]:
    """Synthesize a follow-up answer grounded ONLY in the previous Evidence."""
    evidence_str = "\n".join(ev.blocks) if ev.blocks else json.dumps(ev.raw, indent=2)
    extra_str = ""
    if extra_evidence:
        extra_str = "\n\nADDITIONAL EVIDENCE for the named project (the only extra source):\n" + "\n".join(extra_evidence)
    user = (
        f"Follow-up question: {question}\n\n"
        f"{meta_instruction}\n\n"
        f"PREVIOUS ANSWER (context only):\n{prev.answer.text}\n\n"
        f"PREVIOUS EVIDENCE (the ONLY source you may cite):\n{evidence_str}{extra_str}\n\n"
        f"Answer the follow-up grounded only in the PREVIOUS EVIDENCE."
    )
    return _call(_SYSTEM, user)


def ask(question: str, conn, prev: Optional["Exchange"] = None,
        verbose: bool = False) -> Answer:
    # Single reasoning pipeline: understand (online) / requirements_from_question
    # (offline) -> RetrievalRequirements -> retrieve_requirements -> answer.
    # Bounded continuity: resolve a follow-up first; restate/clarify short-circuit
    # (no new retrieval); rewrite yields a new question flowing through retrieval.
    if prev is not None:
        res = resolve_followup(question, prev, conn)
        if res is not None:
            kind, payload = res
            if kind == "restate":
                return Answer(text=payload, evidence=prev.answer.evidence, used_llm=False)
            if kind == "clarify":
                return Answer(text=payload,
                              evidence=Evidence(requirements=RetrievalRequirements(needs=["clarify"])),
                              used_llm=False)
            if kind == "followup":
                # Meta-question about the PREVIOUS answer. Reuse the previous
                # Evidence package (it IS the evidence the answer rests on) and
                # re-synthesize, threading `prev` as disambiguation context only.
                # No fresh retrieval, so context is never lost (M8.1.6 fix).
                return _answer_followup(question, payload, conn)
            if kind == "rewrite":
                question = payload

    if llm_enabled():
        req = understand(question, conn)
    else:
        req = requirements_from_question(question, conn)

    if req is None:
        # Could not confidently determine the question (LLM unavailable or it
        # returned "Unknown" / an invalid label). Honest, extremely-rare case.
        return Answer(
            text=("I couldn't confidently determine what you are asking. "
                  "Try rephrasing, or set FRIDAY_LLM_MODEL so I can interpret "
                  "open-ended questions."),
            evidence=Evidence(requirements=RetrievalRequirements(needs=["general"])),
            used_llm=False,
        )

    ev = retrieve_requirements(req, conn)
    decision = obj_mod.ObjectiveDecision(
        objective=ev.raw.get("objective", "general"),
        needs=list(req.needs),
        lens=req.lens,
        contract=obj_mod.contract_for(ev.raw.get("objective", "general")),
        reason=ev.raw.get("objective_reason", ""),
    )

    text: Optional[str] = None
    used_llm = False
    if "chitchat" in ev.requirements.needs:
        text = _deterministic_answer(question, ev, "chitchat", decision)
    elif llm_enabled() and os.environ.get("FRIDAY_ANSWER_LLM") == "1":
        text = _synthesize(question, ev, prev=prev, decision=decision)
        used_llm = text is not None
    if text is None:
        text = _deterministic_answer(
            question, ev, _label_of(ev.requirements), decision)

    return Answer(text=text, evidence=ev, used_llm=used_llm)
