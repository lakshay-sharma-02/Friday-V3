"""Planning derivation (Milestone 9.0).

Deterministic planning stages. The engine NEVER touches observations, context,
git, READMEs, or repositories; it reads only Insights, Initiatives,
Understanding, and Knowledge (passed in by the engine). It NEVER executes,
edits files, or calls workers. It NEVER uses an LLM.

Stages (every one explainable, every one evidence-backed):

  1. Understand objective    -> classify PlanType from goal keywords
  2. Locate initiatives       -> match goal/type to active initiatives
  3. Locate insights          -> match goal to live insight titles/types
  4. Locate understanding     -> match goal to understanding subjects/types
  5. Locate knowledge         -> match goal to knowledge subjects/types
  6. Milestones               -> template per plan_type, evidence-tagged
  7. Dependencies             -> technical/initiative/knowledge/understanding
  8. Risks                    -> from insights + understanding + knowledge evo
  9. Verification             -> mandatory, method-typed, evidence-tagged
 10. Rollback                 -> mandatory, strategy-typed, evidence-tagged

The output is a STRUCTURED Plan object. Text rendering happens in models only.

All matching is deterministic keyword/type overlap. No embeddings, no vectors,
no neural scoring.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from .models import Plan, PlanType, PlanConfidence, PlanStatus


# ---------------------------------------------------------------------------
# Keyword lexicons for deterministic matching (lower-layered evidence -> plan).
# ---------------------------------------------------------------------------

_AUTH_KW = ("auth", "oauth", "login", "credential", "token", "session", "sso")
_RUST_KW = ("rust", "systems", "kernel", "crate")
_CRATE_KW = ("crate", "extract", "shared library", "reusable")
_WORKER_KW = ("worker", "orchestrat", "job", "task queue", "scheduler")
_ARCH_KW = ("architecture", "architect", "vivaha", "structure", "module")
_COMMERCIAL_KW = ("commercial", "product", "customer", "billing", "sdk")
_RESEARCH_KW = ("research", "study", "experiment", "investigate")
_BOTTLENECK_KW = ("bottleneck", "review queue", "ci flake", "blocked")
_DEBT_KW = ("debt", "duplicate", "repeated", "re-implement")
_DRIFT_KW = ("drift", "diverge", "off-track")
_LOWCONF_KW = ("weak", "uncertain", "unverified", "speculative")
_PLAN_DRIFT_KW = ("drift", "diverge", "off-track", "architecture")


# Project names whose knowledge/understanding/initiatives the plan should
# surface. Resolved once per module load from the persisted repos so a goal that
# names a project (e.g. "Add logout button to MindWell") connects to the
# evidence Friday already holds about it.
def _project_names() -> frozenset[str]:
    try:
        from ..db import connect, get_repositories
        # Connect per call (respects FRIDAY_DB / default path); caching a module
        # global connection would bind to the wrong database across sessions.
        c = connect()
        try:
            return frozenset(
                (r.name or "").lower() for r in get_repositories(c) if r.name
            )
        finally:
            c.close()
    except Exception:
        return frozenset()


def _project_hits(goal: str) -> set[str]:
    """Project-name tokens from the goal that also exist as known repos."""
    g = goal.lower()
    return {n for n in _project_names() if n and n in g}


def _matches_project(item, g: str, hits: set[str]) -> bool:
    if not hits:
        return False
    hay = " ".join([
        (getattr(item, "subject", None) or "").lower(),
        (getattr(item, "statement", None) or "").lower(),
        (getattr(item, "title", None) or "").lower(),
    ])
    return any(h in hay for h in hits)


@dataclass
class Evidence:
    """Read-only view of the lower layers the planner consumes."""

    initiatives: List = field(default_factory=list)
    insights: List = field(default_factory=list)
    understanding: List = field(default_factory=list)
    knowledge: List = field(default_factory=list)

    # id sets, populated by the engine after gathering (used for citation
    # validation so every plan references valid lower-layer ids only).
    initiatives_by_id: set = field(default_factory=set)
    insights_by_id: set = field(default_factory=set)
    understanding_by_id: set = field(default_factory=set)
    knowledge_by_id: set = field(default_factory=set)

    def _by_id(self, kind: str, iid: str):
        store = {
            "initiative": {x.id: x for x in self.initiatives if x.id},
            "insight": {x.id: x for x in self.insights if x.id},
            "understanding": {x.id: x for x in self.understanding if x.id},
            "knowledge": {x.id: x for x in self.knowledge if x.id},
        }[kind]
        return store.get(iid)


# ---------------------------------------------------------------------------
# 1. Objective
# ---------------------------------------------------------------------------

def _classify(goal: str) -> PlanType:
    return PlanType.from_goal(goal)


# ---------------------------------------------------------------------------
# 2-5. Locate lower-layer evidence (deterministic keyword + type overlap).
# ---------------------------------------------------------------------------

def _goal_tokens(goal: str) -> List[str]:
    return [t for t in (goal or "").lower().replace('"', " ").split() if t]


def _match_initiatives(ev: Evidence, goal: str) -> List[str]:
    g = goal.lower()
    hits = _project_hits(goal)
    out: List[str] = []
    for i in ev.initiatives:
        hay = " ".join([
            (i.title or "").lower(),
            (i.statement or "").lower(),
            (i.type.value if hasattr(i.type, "value") else str(i.type)).lower(),
        ])
        if any(kw in hay for kw in _AUTH_KW) and any(kw in g for kw in _AUTH_KW):
            out.append(i.id)
        elif any(kw in hay for kw in _RUST_KW) and any(kw in g for kw in _RUST_KW):
            out.append(i.id)
        elif any(kw in hay for kw in _WORKER_KW) and any(kw in g for kw in _WORKER_KW):
            out.append(i.id)
        elif any(kw in hay for kw in _ARCH_KW) and any(kw in g for kw in _ARCH_KW):
            out.append(i.id)
        elif any(kw in hay for kw in _COMMERCIAL_KW) and any(kw in g for kw in _COMMERCIAL_KW):
            out.append(i.id)
        elif _matches_project(i, g, hits):
            out.append(i.id)
        elif (i.title or "").lower() in g or (i.title or "").lower()[:6] in g:
            out.append(i.id)
    # de-dup, preserve order
    seen = set()
    return [x for x in out if not (x in seen or seen.add(x))]


def _match_insights(ev: Evidence, goal: str) -> List[str]:
    """Match live insights whose subject/type overlaps the goal. Insights are
    the 'what deserves attention' signal; a plan should reference them."""
    g = goal.lower()
    hits = _project_hits(goal)
    out: List[str] = []
    for ins in ev.insights:
        hay = " ".join([
            (ins.title or "").lower(),
            (ins.statement or "").lower(),
            (ins.type.value if hasattr(ins.type, "value") else str(ins.type)).lower(),
        ])
        if any(kw in hay for kw in _AUTH_KW) and any(kw in g for kw in _AUTH_KW):
            out.append(ins.id)
        elif any(kw in hay for kw in _RUST_KW) and any(kw in g for kw in _RUST_KW):
            out.append(ins.id)
        elif any(kw in hay for kw in _CRATE_KW) and any(kw in g for kw in _CRATE_KW):
            out.append(ins.id)
        elif any(kw in hay for kw in _WORKER_KW) and any(kw in g for kw in _WORKER_KW):
            out.append(ins.id)
        elif any(kw in hay for kw in _ARCH_KW) and any(kw in g for kw in _ARCH_KW):
            out.append(ins.id)
        elif any(kw in hay for kw in _COMMERCIAL_KW) and any(kw in g for kw in _COMMERCIAL_KW):
            out.append(ins.id)
        elif _matches_project(ins, g, hits):
            out.append(ins.id)
        elif any(kw in hay for kw in _DRIFT_KW) and any(kw in g for kw in _PLAN_DRIFT_KW):
            out.append(ins.id)
    seen = set()
    return [x for x in out if not (x in seen or seen.add(x))]


def _match_understanding(ev: Evidence, goal: str) -> List[str]:
    g = goal.lower()
    hits = _project_hits(goal)
    out: List[str] = []
    for u in ev.understanding:
        hay = " ".join([
            (u.subject or "").lower(),
            (u.statement or "").lower(),
            (u.type.value if hasattr(u.type, "value") else str(u.type)).lower(),
        ])
        if any(kw in hay for kw in _AUTH_KW) and any(kw in g for kw in _AUTH_KW):
            out.append(u.id)
        elif any(kw in hay for kw in _RUST_KW) and any(kw in g for kw in _RUST_KW):
            out.append(u.id)
        elif any(kw in hay for kw in _WORKER_KW) and any(kw in g for kw in _WORKER_KW):
            out.append(u.id)
        elif any(kw in hay for kw in _ARCH_KW) and any(kw in g for kw in _ARCH_KW):
            out.append(u.id)
        elif any(kw in hay for kw in _COMMERCIAL_KW) and any(kw in g for kw in _COMMERCIAL_KW):
            out.append(u.id)
        elif _matches_project(u, g, hits):
            out.append(u.id)
    seen = set()
    return [x for x in out if not (x in seen or seen.add(x))]


def _match_knowledge(ev: Evidence, goal: str) -> List[str]:
    g = goal.lower()
    hits = _project_hits(goal)
    out: List[str] = []
    for k in ev.knowledge:
        hay = " ".join([
            (k.subject or "").lower(),
            (k.statement or "").lower(),
            (k.type.value if hasattr(k.type, "value") else str(k.type)).lower(),
        ])
        if any(kw in hay for kw in _AUTH_KW) and any(kw in g for kw in _AUTH_KW):
            out.append(k.id)
        elif any(kw in hay for kw in _RUST_KW) and any(kw in g for kw in _RUST_KW):
            out.append(k.id)
        elif any(kw in hay for kw in _CRATE_KW) and any(kw in g for kw in _CRATE_KW):
            out.append(k.id)
        elif any(kw in hay for kw in _WORKER_KW) and any(kw in g for kw in _WORKER_KW):
            out.append(k.id)
        elif any(kw in hay for kw in _ARCH_KW) and any(kw in g for kw in _ARCH_KW):
            out.append(k.id)
        elif any(kw in hay for kw in _COMMERCIAL_KW) and any(kw in g for kw in _COMMERCIAL_KW):
            out.append(k.id)
        elif _matches_project(k, g, hits):
            out.append(k.id)
    seen = set()
    return [x for x in out if not (x in seen or seen.add(x))]


# ---------------------------------------------------------------------------
# 6. Milestones — template per plan_type, evidence-tagged.
# ---------------------------------------------------------------------------

def _milestones(ptype: PlanType, ev: Evidence, init_ids, ins_ids, u_ids, k_ids) -> List[dict]:
    """Generate ordered milestones. Each carries a deterministic title and the
    evidence kind it is grounded in (so workers know WHY a step exists)."""
    tok = "initiative" if init_ids else (
        "insight" if ins_ids else (
            "understanding" if u_ids else ("knowledge" if k_ids else "goal")))
    base = [
        {"order": 1, "title": "Investigate & scope", "detail":
         f"Confirm requirements from {tok} evidence.", "evidence": tok},
        {"order": 2, "title": "Design", "detail":
         "Produce a concrete design with interfaces.", "evidence": tok},
        {"order": 3, "title": "Implement", "detail":
         "Build the change behind a flag where possible.", "evidence": tok},
        {"order": 4, "title": "Verify", "detail":
         "Run the verification plan (tests/benchmarks/review).", "evidence": "verification"},
        {"order": 5, "title": "Document", "detail":
         "Record decisions and usage.", "evidence": tok},
        {"order": 6, "title": "Roll out & monitor", "detail":
         "Ship with rollback ready.", "evidence": "rollback"},
    ]
    # Type-specific shaping (still deterministic).
    if ptype == PlanType.FEATURE or ptype == PlanType.INTEGRATION:
        base.insert(3, {"order": 3, "title": "Backend",
                        "detail": "Implement server-side logic.", "evidence": tok})
        base.insert(4, {"order": 4, "title": "Frontend",
                        "detail": "Implement user-facing surface.", "evidence": tok})
        for m in base:
            if isinstance(m.get("order"), int) and m["order"] >= 5:
                m["order"] += 1
    elif ptype == PlanType.REFACTOR or ptype == PlanType.MIGRATION:
        base.insert(2, {"order": 2, "title": "Characterize current behavior",
                        "detail": "Lock behavior with tests before changing.", "evidence": "verification"})
        for m in base:
            if isinstance(m.get("order"), int) and m["order"] >= 3:
                m["order"] += 1
    elif ptype == PlanType.RESEARCH or ptype == PlanType.LEARNING:
        base = [
            {"order": 1, "title": "Survey", "detail": "Review existing knowledge.",
             "evidence": tok},
            {"order": 2, "title": "Hypothesis", "detail": "State the question.",
             "evidence": tok},
            {"order": 3, "title": "Prototype", "detail": "Small experiment.",
             "evidence": tok},
            {"order": 4, "title": "Evaluate", "detail": "Measure against hypothesis.",
             "evidence": "verification"},
            {"order": 5, "title": "Write-up", "detail": "Capture findings.",
             "evidence": tok},
        ]
    elif ptype == PlanType.INFRASTRUCTURE or ptype == PlanType.ARCHITECTURE:
        base.insert(1, {"order": 1, "title": "Establish boundaries",
                        "detail": "Define module/component contracts.", "evidence": tok})
        for m in base:
            if isinstance(m.get("order"), int) and m["order"] >= 2:
                m["order"] += 1
    # re-number sequentially (insertions may have shifted order ints)
    for idx, m in enumerate(base, start=1):
        m["order"] = idx
    return base


# ---------------------------------------------------------------------------
# 7. Dependencies — technical / initiative / knowledge / understanding.
# ---------------------------------------------------------------------------

def _dependencies(ev: Evidence, init_ids, k_ids, u_ids, repos) -> List[dict]:
    out: List[dict] = []
    if init_ids:
        out.append({"kind": "initiative", "target": ", ".join(init_ids),
                    "reason": "Plan advances an existing long-running initiative."})
    if k_ids:
        out.append({"kind": "knowledge", "target": ", ".join(k_ids[:5]),
                    "reason": "Plan rests on accumulated knowledge evidence."})
    if u_ids:
        out.append({"kind": "understanding", "target": ", ".join(u_ids[:5]),
                    "reason": "Plan aligns with derived engineering understanding."})
    if repos and len(repos) > 1:
        out.append({"kind": "technical", "target": ", ".join(sorted(repos)),
                    "reason": "Change spans multiple repositories; coordinate merges."})
    # de-dup by (kind, target)
    seen = set()
    res = []
    for d in out:
        key = (d["kind"], d["target"])
        if key not in seen:
            seen.add(key)
            res.append(d)
    return res


# ---------------------------------------------------------------------------
# 8. Risks — from insights, understanding, knowledge evolution. Never LLM.
# ---------------------------------------------------------------------------

def _risks(ev: Evidence, ins_ids, u_ids, k_ids) -> List[dict]:
    out: List[dict] = []
    by_id = {x.id: x for x in ev.insights if x.id}
    for iid in ins_ids:
        ins = by_id.get(iid)
        if not ins:
            continue
        t = ins.type.value if hasattr(ins.type, "value") else str(ins.type)
        if "drift" in t:
            out.append({"severity": "high", "kind": "architecture_drift",
                        "detail": ins.statement, "evidence_id": iid})
        elif "diverge" in t:
            out.append({"severity": "high", "kind": "architecture_drift",
                        "detail": ins.statement, "evidence_id": iid})
        elif "bottleneck" in t:
            out.append({"severity": "high", "kind": "repeated_bottleneck",
                        "detail": ins.statement, "evidence_id": iid})
        elif "debt" in t:
            out.append({"severity": "medium", "kind": "engineering_debt",
                        "detail": ins.statement, "evidence_id": iid})
        elif "blind_spot" in t:
            out.append({"severity": "high", "kind": "blind_spot",
                        "detail": ins.statement, "evidence_id": iid})
        elif "risk" in t or "warning" in t:
            out.append({"severity": "medium", "kind": "emerging_risk",
                        "detail": ins.statement, "evidence_id": iid})
        elif "reuse" in t or "recommendation" in t:
            out.append({"severity": "low", "kind": "repeated_implementation",
                        "detail": ins.statement, "evidence_id": iid})
    # understanding-level risk signals
    by_u = {x.id: x for x in ev.understanding if x.id}
    for uid in u_ids:
        u = by_u.get(uid)
        if not u:
            continue
        t = u.type.value if hasattr(u.type, "value") else str(u.type)
        if "weakness" in t or "risk" in t:
            out.append({"severity": "medium", "kind": "low_confidence_area",
                        "detail": u.statement, "evidence_id": uid})
        if "drift" in (u.statement or "").lower():
            out.append({"severity": "high", "kind": "architecture_drift",
                        "detail": u.statement, "evidence_id": uid})
    # knowledge-evolution risk: dormant/retired knowledge signals churn
    for k in ev.knowledge:
        if k.id in k_ids:
            st = (k.status.value if hasattr(k.status, "value") else str(k.status)).lower()
            if st in ("dormant", "retired"):
                out.append({"severity": "medium", "kind": "knowledge_churn",
                            "detail": f"Underlying knowledge '{getattr(k, 'subject', '')}' is {st}.",
                            "evidence_id": k.id})
    # de-dup by (kind, detail) so the same risk from two evidence ids is not
    # double-reported.
    seen = set()
    res = []
    for r in out:
        key = (r["kind"], r.get("detail"))
        if key not in seen:
            seen.add(key)
            res.append(r)
    return res


# ---------------------------------------------------------------------------
# 9. Verification — mandatory, method-typed, evidence-tagged.
# ---------------------------------------------------------------------------

def _verification(ptype: PlanType, ev: Evidence, k_ids) -> List[dict]:
    out: List[dict] = []
    out.append({"method": "tests", "detail":
                "Add/extend unit + integration tests covering the change."})
    if ptype in (PlanType.OPTIMIZATION, PlanType.INFRASTRUCTURE,
                 PlanType.ARCHITECTURE, PlanType.MIGRATION):
        out.append({"method": "benchmarks", "detail":
                    "Measure before/after on the affected workload."})
    out.append({"method": "static_analysis", "detail":
                "Lint + type-check + CI gates must pass."})
    out.append({"method": "review", "detail":
                "Peer review before merge; confirm against goal."})
    if ptype in (PlanType.FEATURE, PlanType.INTEGRATION, PlanType.RELEASE):
        out.append({"method": "manual_validation", "detail":
                    "Exercise the user-facing path end to end."})
    return out


# ---------------------------------------------------------------------------
# 10. Rollback — mandatory, strategy-typed, evidence-tagged.
# ---------------------------------------------------------------------------

def _rollback(ptype: PlanType, repos) -> List[dict]:
    out: List[dict] = []
    if ptype in (PlanType.FEATURE, PlanType.REFACTOR, PlanType.OPTIMIZATION,
                 PlanType.INTEGRATION, PlanType.BUG_FIX):
        out.append({"strategy": "feature_flag", "detail":
                    "Ship behind a flag; disable to revert behavior instantly."})
    out.append({"strategy": "git_revert", "detail":
                "Keep changes in small reversible commits; revert via git."})
    if ptype in (PlanType.MIGRATION, PlanType.INFRASTRUCTURE):
        out.append({"strategy": "migration_rollback", "detail":
                    "Provide a down-migration; snapshot state before applying."})
    if ptype in (PlanType.DOCUMENTATION, PlanType.MAINTENANCE, PlanType.TESTING,
                 PlanType.LEARNING):
        out.append({"strategy": "documentation_only", "detail":
                    "No runtime impact; revert the doc/test commit."})
    if repos:
        out.append({"strategy": "configuration_restore", "detail":
                    "Back up config; restore previous values if behavior regresses."})
    return out


# ---------------------------------------------------------------------------
# Confidence — derived from evidence reinforcement (never guessed).
# ---------------------------------------------------------------------------

def _confidence(ev_count: int, repos: set, risk_high: int) -> PlanConfidence:
    score = ev_count + (len(repos) - 1 if repos else 0)
    if risk_high >= 2:
        score -= 1  # many high risks lower confidence
    if score >= 5:
        return PlanConfidence.STRONG
    if score >= 2:
        return PlanConfidence.MEDIUM
    return PlanConfidence.WEAK


def _complexity(ptype: PlanType, repos: set, n_milestones: int) -> str:
    base = {
        PlanType.DOCUMENTATION: "low",
        PlanType.TESTING: "low",
        PlanType.MAINTENANCE: "low",
        PlanType.LEARNING: "low",
        PlanType.BUG_FIX: "low",
        PlanType.REFACTOR: "medium",
        PlanType.FEATURE: "medium",
        PlanType.OPTIMIZATION: "medium",
        PlanType.RESEARCH: "medium",
        PlanType.INTEGRATION: "medium",
        PlanType.COMMERCIAL: "medium",
        PlanType.RELEASE: "medium",
        PlanType.MIGRATION: "medium",
        PlanType.INFRASTRUCTURE: "high",
        PlanType.ARCHITECTURE: "high",
    }.get(ptype, "medium")
    if len(repos) > 2:
        base = "high" if base != "high" else "high"
    return base


def _effort(ptype: PlanType, n_milestones: int) -> str:
    if n_milestones >= 7:
        return "high"
    if n_milestones >= 5:
        return "medium"
    return "low"


# ---------------------------------------------------------------------------
# Orchestrator: build the STRUCTURED Plan object (no prose yet).
# ---------------------------------------------------------------------------

def plan(goal: str, ev: Evidence) -> Plan:
    """Run all 10 deterministic stages and return a STRUCTURED Plan.

    The returned object is the source of truth. Callers render it to text via
    Plan.render_text() only at the end (or not at all, for workers)."""
    ptype = _classify(goal)

    init_ids = _match_initiatives(ev, goal)
    ins_ids = _match_insights(ev, goal)
    u_ids = _match_understanding(ev, goal)
    k_ids = _match_knowledge(ev, goal)

    repos: set = set()
    for i in ev.initiatives:
        if i.id in init_ids:
            repos.update(getattr(i, "participating_repositories", []) or [])

    milestones = _milestones(ptype, ev, init_ids, ins_ids, u_ids, k_ids)
    dependencies = _dependencies(ev, init_ids, k_ids, u_ids, repos)
    risks = _risks(ev, ins_ids, u_ids, k_ids)
    verification = _verification(ptype, ev, k_ids)
    rollback = _rollback(ptype, repos)

    risk_high = sum(1 for r in risks if r.get("severity") == "high")
    conf = _confidence(len(init_ids) + len(ins_ids) + len(u_ids) + len(k_ids),
                       repos, risk_high)
    status = PlanStatus.REFINED if conf != PlanConfidence.WEAK else PlanStatus.PLANNED
    complexity = _complexity(ptype, repos, len(milestones))
    effort = _effort(ptype, len(milestones))

    return Plan(
        goal=goal,
        plan_type=ptype,
        confidence=conf,
        status=status,
        affected_initiative_ids=init_ids,
        affected_insight_ids=ins_ids,
        affected_understanding_ids=u_ids,
        affected_knowledge_ids=k_ids,
        milestones=milestones,
        dependencies=dependencies,
        risks=risks,
        verification=verification,
        rollback=rollback,
        estimated_complexity=complexity,
        estimated_effort=effort,
    )
