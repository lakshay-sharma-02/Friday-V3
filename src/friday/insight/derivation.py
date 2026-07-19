"""Insight derivation rules (Milestone 8.5).

Transforms accumulated UNDERSTANDING (plus Initiatives and Knowledge) into
rare, high-value ENGINEERING INSIGHTS. This layer NEVER reads observations,
context, git, READMEs, or repositories directly. It NEVER calls an LLM. Every
candidate cites the understanding ids (and/or initiative ids and/or knowledge
ids) that produced it, so an insight is fully traceable to lower layers.

Each rule expresses a deterministic trigger -> (insight_type, semantic_title,
statement_factory). The QUALITY FILTER guarantees an insight is only emitted
when multiple independent evidence sources agree:

  - >= 2 Understanding items, OR
  - 1 Understanding + 1 Initiative, OR
  - >= 3 Knowledge items.

Single evidence never produces an insight. The engine counts qualifying
evidence and drops weak triggers.

Insights are SEMANTIC ("Extract shared Rust crates", "Authentication
subsystem") and EPHEMERAL. A rule only fires while its triggering conditions
hold; when they vanish, the engine retires the insight instead of re-emitting
it (see engine.build).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from ..understanding.models import UnderstandingType
from .models import InsightType


# ---------------------------------------------------------------------------
# Candidate — a tentative insight before confidence aggregation + lifecycle.
# ---------------------------------------------------------------------------


@dataclass
class Candidate:
    type: InsightType
    title: str
    statement: str
    understanding_ids: List[str] = field(default_factory=list)
    initiative_ids: List[str] = field(default_factory=list)
    knowledge_ids: List[str] = field(default_factory=list)
    repos: List[str] = field(default_factory=list)

    def key(self) -> tuple:
        return (self.type, self.title)

    def evidence_count(self) -> int:
        return (len(self.understanding_ids) + len(self.initiative_ids)
                + len(self.knowledge_ids))


# ---------------------------------------------------------------------------
# Rule context — the lower-layer evidence the rules read.
# ---------------------------------------------------------------------------


@dataclass
class _Ctx:
    """Indexes over the lower layers, used by every rule."""

    understandings: List  # list[Understanding]
    initiatives: List    # list[Initiative]
    knowledge: List      # list[Knowledge]

    @property
    def u_by_type(self) -> dict:
        out: dict = {}
        for u in self.understandings:
            out.setdefault(u.type.value, []).append(u)
        return out

    @property
    def u_by_subject(self) -> dict:
        out: dict = {}
        for u in self.understandings:
            out.setdefault((u.subject or "").strip().lower(), []).append(u)
        return out

    @property
    def i_by_type(self) -> dict:
        out: dict = {}
        for i in self.initiatives:
            out.setdefault(i.type.value, []).append(i)
        return out

    @property
    def k_by_subject(self) -> dict:
        out: dict = {}
        for k in self.knowledge:
            out.setdefault((k.subject or "").strip().lower(), []).append(k)
        return out


# ---------------------------------------------------------------------------
# QUALITY FILTER — rules must not emit below this evidence bar.
# ---------------------------------------------------------------------------


def _qualifies(c: Candidate) -> bool:
    """Multiple independent evidence sources must agree.

    Rule (>=2 understanding) OR (1 understanding + 1 initiative) OR
    (>=3 knowledge). Single evidence never produces an insight.
    """
    n_u = len(c.understanding_ids)
    n_i = len(c.initiative_ids)
    n_k = len(c.knowledge_ids)
    if n_u >= 2:
        return True
    if n_u >= 1 and n_i >= 1:
        return True
    if n_k >= 3:
        return True
    return False


# ---------------------------------------------------------------------------
# Helpers — collect a set of understanding ids matching a predicate, plus repo
# provenance from the underlying knowledge they cite.
# ---------------------------------------------------------------------------


def _uids_matching(ctx: _Ctx, pred) -> List[str]:
    return [u.id for u in ctx.understandings if pred(u) and u.id]


def _kids_for_understandings(ctx: _Ctx, uids: List[str]) -> List[str]:
    out: List[str] = []
    for u in ctx.understandings:
        if u.id in uids:
            out.extend(u.knowledge_ids)
    # de-dup, preserve order
    seen = set()
    res = []
    for k in out:
        if k not in seen:
            seen.add(k)
            res.append(k)
    return res


def _repos_for_understandings(ctx: _Ctx, uids: List[str]) -> List[str]:
    kids = _kids_for_understandings(ctx, uids)
    kid_set = set(kids)
    out: List[str] = []
    seen = set()
    for k in ctx.knowledge:
        if k.id in kid_set:
            for r in (getattr(k, "evidence_ids", []) or []):
                if r and r not in seen:
                    seen.add(r)
                    out.append(r)
    return out


def _repos_for_initiatives(ctx: _Ctx, iids: List[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for i in ctx.initiatives:
        if i.id in iids:
            for r in getattr(i, "participating_repositories", []) or []:
                if r and r not in seen:
                    seen.add(r)
                    out.append(r)
    return out


# ---------------------------------------------------------------------------
# Rules. Each returns a list of Candidate (pre-filter / pre-confidence).
# ---------------------------------------------------------------------------


def _rule_reuse_auth(ctx: _Ctx) -> List[Candidate]:
    """Repeated authentication + multiple repositories -> Engineering Reuse.

    Trigger: >=2 understandings referencing auth across >1 participating repo.
    """
    auth_u = [u for u in ctx.understandings
              if "auth" in (u.subject or "").lower()
              or any(k in (u.statement or "").lower()
                     for k in ("auth", "login", "oauth", "credential", "token"))]
    repos = _repos_for_understandings(ctx, [u.id for u in auth_u])
    distinct = {r for r in repos if r}
    if len(auth_u) >= 2 and len(distinct) >= 2:
        uids = [u.id for u in auth_u if u.id]
        return [Candidate(
            type=InsightType.REUSE,
            title="Reusable authentication subsystem",
            statement=("Authentication has been independently solved multiple "
                       f"times across {len(distinct)} repositories. Recommendation: "
                       "build a reusable authentication subsystem to stop "
                       "re-solving the same problem."),
            understanding_ids=uids,
            knowledge_ids=_kids_for_understandings(ctx, uids),
            repos=sorted(distinct),
        )]
    return []


def _rule_rust_reuse(ctx: _Ctx) -> List[Candidate]:
    """Repeated Rust/systems investment -> Engineering Opportunity (extract crates).

    Requires >=2 understandings on rust/systems (the quality filter on its own),
    reinforced by >=2 knowledge items. A single understanding would be below
    the bar even with knowledge, so we demand multiple independent signals.
    """
    rust_u = [u for u in ctx.understandings
              if "rust" in (u.subject or "").lower()
              or "systems" in (u.subject or "").lower()]
    rust_k = [k for k in ctx.knowledge
              if any(t in (k.subject or "").lower() for t in ("rust", "systems", "kernel"))]
    uids = [u.id for u in rust_u if u.id]
    kids = [k.id for k in rust_k if k.id]
    if len(uids) >= 2 and len(kids) >= 2:
        return [Candidate(
            type=InsightType.OPPORTUNITY,
            title="Extract shared Rust crates",
            statement=("Enough Rust/systems infrastructure has accumulated that "
                       "extracting shared crates is likely to reduce future "
                       "engineering effort."),
            understanding_ids=uids,
            knowledge_ids=kids,
            repos=_repos_for_understandings(ctx, uids)
                   + _repos_for_knowledge(ctx, kids),
        )]
    return []


def _rule_commercial_risk(ctx: _Ctx) -> List[Candidate]:
    """Commercial increasing + research decreasing -> Engineering Risk."""
    comm = ctx.u_by_type.get(UnderstandingType.COMMERCIAL_DIRECTION.value, [])
    research = ctx.u_by_type.get(UnderstandingType.RESEARCH_DIRECTION.value, [])
    comm_u = [u for u in comm if u.id]
    research_u = [u for u in research if u.id]
    # research DEcreasing: present but flagged as decreasing in statement, OR
    # present while commercial rises. We require commercial present and at
    # least one research understanding to mark displacement.
    if comm_u and research_u:
        uids = [u.id for u in comm_u + research_u]
        return [Candidate(
            type=InsightType.RISK,
            title="Commercial work displacing research",
            statement=("Commercial engineering is beginning to displace "
                       "foundational research work. Watch the long-term balance "
                       "between product pressure and capability investment."),
            understanding_ids=uids,
            knowledge_ids=_kids_for_understandings(ctx, uids),
            repos=_repos_for_understandings(ctx, uids),
        )]
    return []


def _rule_convergence(ctx: _Ctx) -> List[Candidate]:
    """Several efforts converging -> Engineering Convergence.

    Fires on >=2 PROJECT_CONVERGENCE understandings (satisfies the quality
    filter on its own) and optionally folds in a platform/integration
    initiative when one exists.
    """
    convergence_u = ctx.u_by_type.get(
        UnderstandingType.PROJECT_CONVERGENCE.value, [])
    if len(convergence_u) >= 2:
        uids = [u.id for u in convergence_u if u.id]
        platform = ctx.i_by_type.get("platform", []) + ctx.i_by_type.get(
            "integration", [])
        iids = [i.id for i in platform if i.id]
        repos = _repos_for_understandings(ctx, uids)
        if iids:
            repos = repos + _repos_for_initiatives(ctx, iids)
        return [Candidate(
            type=InsightType.CONVERGENCE,
            title="Converging engineering efforts",
            statement=("Multiple engineering efforts are converging into a shared "
                       "direction. This is an opportunity to consolidate investment "
                       "rather than let the parts drift apart."),
            understanding_ids=uids,
            initiative_ids=iids,
            knowledge_ids=_kids_for_understandings(ctx, uids),
            repos=repos,
        )]
    return []


def _rule_divergence(ctx: _Ctx) -> List[Candidate]:
    """Several divergence understandings -> Engineering Divergence."""
    div = ctx.u_by_type.get(UnderstandingType.PROJECT_DIVERGENCE.value, [])
    if len(div) >= 2:
        uids = [u.id for u in div if u.id]
        return [Candidate(
            type=InsightType.DIVERGENCE,
            title="Diverging engineering efforts",
            statement=("Several projects are diverging in direction. Consider "
                       "whether the split is intentional or a sign of uncoordinated "
                       "effort."),
            understanding_ids=uids,
            knowledge_ids=_kids_for_understandings(ctx, uids),
            repos=_repos_for_understandings(ctx, uids),
        )]
    return []


def _rule_debt(ctx: _Ctx) -> List[Candidate]:
    """Many related subjects, no shared infrastructure initiative -> Debt."""
    rel = ctx.u_by_type.get(UnderstandingType.PROJECT_CONVERGENCE.value, [])
    has_infra = any(i.type and i.type.value == "infrastructure"
                    for i in ctx.initiatives)
    if len(rel) >= 2 and not has_infra:
        uids = [u.id for u in rel if u.id]
        return [Candidate(
            type=InsightType.DEBT,
            title="Emerging engineering debt",
            statement=("Many related projects exist with no shared infrastructure "
                       "initiative to hold them together. Duplicated effort is "
                       "likely accumulating as engineering debt."),
            understanding_ids=uids,
            knowledge_ids=_kids_for_understandings(ctx, uids),
            repos=_repos_for_understandings(ctx, uids),
        )]
    return []


def _rule_blind_spot(ctx: _Ctx) -> List[Candidate]:
    """Engineering blind-spot understanding -> Engineering Blind Spot."""
    spot = ctx.u_by_type.get(UnderstandingType.ENGINEERING_BLIND_SPOT.value, [])
    if len(spot) >= 1:
        uids = [u.id for u in spot if u.id]
        # blind spot requires a second independent signal to qualify
        others = [u.id for u in ctx.understandings
                  if u.id not in uids
                  and u.type.value in (
                      UnderstandingType.ENGINEERING_WEAKNESS.value,
                      UnderstandingType.ENGINEERING_RISK.value)]
        if len(others) >= 1:
            all_u = uids + others
            return [Candidate(
                type=InsightType.BLIND_SPOT,
                title="Engineering blind spot",
                statement=("A recurring blind spot is emerging and is reinforced "
                           "by a separate weakness/risk signal. It may be the "
                           "highest-leverage thing to confront."),
                understanding_ids=all_u,
                knowledge_ids=_kids_for_understandings(ctx, all_u),
                repos=_repos_for_understandings(ctx, all_u),
            )]
    return []


def _rule_momentum(ctx: _Ctx) -> List[Candidate]:
    """Investment trend increasing + strong -> Engineering Momentum."""
    inv = [u for u in ctx.understandings
           if u.type.value == UnderstandingType.INVESTMENT_TREND.value]
    if len(inv) >= 2:
        uids = [u.id for u in inv if u.id]
        return [Candidate(
            type=InsightType.MOMENTUM,
            title="Engineering momentum building",
            statement=("Investment is trending upward across several areas. The "
                       "current engineering momentum is worth protecting and "
                       "feeding, not fragmenting."),
            understanding_ids=uids,
            knowledge_ids=_kids_for_understandings(ctx, uids),
            repos=_repos_for_understandings(ctx, uids),
        )]
    return []


def _rule_drift(ctx: _Ctx) -> List[Candidate]:
    """Drift understanding present -> Engineering Drift."""
    drift = [u for u in ctx.understandings
             if u.type.value == UnderstandingType.ENGINEERING_DIRECTION.value
             and "drift" in (u.statement or "").lower()]
    if len(drift) >= 2:
        uids = [u.id for u in drift if u.id]
        return [Candidate(
            type=InsightType.DRIFT,
            title="Engineering direction drift",
            statement=("The engineering direction shows drift across multiple "
                       "understandings. Re-anchor on the primary objective before "
                       "the spread widens."),
            understanding_ids=uids,
            knowledge_ids=_kids_for_understandings(ctx, uids),
            repos=_repos_for_understandings(ctx, uids),
        )]
    return []


def _rule_bottleneck(ctx: _Ctx) -> List[Candidate]:
    """Recurring bottleneck knowledge (>=3) -> Engineering Bottleneck."""
    bn_k = [k for k in ctx.knowledge
            if k.type and k.type.value == "recurring_bottleneck"]
    if len(bn_k) >= 3:
        kids = [k.id for k in bn_k if k.id]
        return [Candidate(
            type=InsightType.BOTTLENECK,
            title="Recurring engineering bottleneck",
            statement=("The same bottleneck keeps recurring across the workspace. "
                       "Removing it would unblock multiple efforts at once."),
            knowledge_ids=kids,
            repos=_repos_for_knowledge(ctx, kids),
        )]
    return []


def _rule_focus(ctx: _Ctx) -> List[Candidate]:
    """Single dominant initiative (1) + supporting understanding -> Focus."""
    if len(ctx.initiatives) == 1:
        i = ctx.initiatives[0]
        uids = [u.id for u in ctx.understandings if u.id]
        if uids:
            return [Candidate(
                type=InsightType.FOCUS,
                title="Primary focus area",
                statement=(f"The workspace centers on one initiative "
                           f"('{i.title}'). Concentrating effort there is the "
                           f"highest-leverage move right now."),
                understanding_ids=uids,
                initiative_ids=[i.id],
                knowledge_ids=_kids_for_understandings(ctx, uids),
                repos=_repos_for_initiatives(ctx, [i.id]),
            )]
    return []


def _rule_recommendation(ctx: _Ctx) -> List[Candidate]:
    """Repeated implementation of the same concern -> Engineering Recommendation.

    Trigger: >=2 *distinct* understandings citing the same knowledge subject.
    Two understandings with the same subject collapse into one row (subject is
    part of the understanding id), so repetition must be detected via shared
    knowledge targets instead. That signals one concern solved more than once.
    """
    by_knowledge_subject: dict = {}
    ksubj = {k.id: (k.subject or "").strip().lower() for k in ctx.knowledge}
    for u in ctx.understandings:
        if not u.id:
            continue
        for kid in getattr(u, "knowledge_ids", []) or []:
            subj = ksubj.get(kid)
            if subj:
                by_knowledge_subject.setdefault(subj, []).append(u)
    repeated = [us for us in by_knowledge_subject.values() if len(us) >= 2]
    if repeated:
        uids = [u.id for us in repeated for u in us if u.id]
        # most-cited knowledge subject wins the title
        top = max(repeated, key=len)
        top_subj = next((ksubj.get(kid) for u in top
                         for kid in getattr(u, "knowledge_ids", []) or [])
                        or ["repeated concern"], None)
        title_subj = top_subj or "repeated concern"
        return [Candidate(
            type=InsightType.RECOMMENDATION,
            title=f"Reusable solution for {title_subj}",
            statement=(f"{title_subj} has been independently implemented more than "
                       "once. Recommendation: build a reusable solution to stop "
                       "re-solving the same problem."),
            understanding_ids=uids,
            knowledge_ids=_kids_for_understandings(ctx, uids),
            repos=_repos_for_understandings(ctx, uids),
        )]
    return []


def _repos_for_knowledge(ctx: _Ctx, kids: List[str]) -> List[str]:
    kid_set = set(kids)
    out: List[str] = []
    seen = set()
    for k in ctx.knowledge:
        if k.id in kid_set:
            for r in (getattr(k, "evidence_ids", []) or []):
                if r and r not in seen:
                    seen.add(r)
                    out.append(r)
    return out


def _rule_investment(ctx: _Ctx) -> List[Candidate]:
    """Sustained investment trend -> Engineering Investment.

    Fires on >=2 INVESTMENT_TREND understandings (quality bar met alone) and
    folds in any TECHNOLOGY_INVESTMENT knowledge to show the investment is
    materializing into capability rather than merely being planned.
    """
    inv = [u for u in ctx.understandings
           if u.type.value == UnderstandingType.INVESTMENT_TREND.value]
    if len(inv) >= 2:
        uids = [u.id for u in inv if u.id]
        tech_k = [k for k in ctx.knowledge
                  if k.type and k.type.value == "technology_investment"]
        kids = [k.id for k in tech_k if k.id]
        return [Candidate(
            type=InsightType.INVESTMENT,
            title="Engineering investment paying off",
            statement=("Sustained investment across several areas is beginning to "
                       "compound into capability. Protect this investment; it is "
                       "the engine of future leverage."),
            understanding_ids=uids,
            knowledge_ids=kids,
            repos=_repos_for_understandings(ctx, uids)
                   + _repos_for_knowledge(ctx, kids),
        )]
    return []


def _rule_warning(ctx: _Ctx) -> List[Candidate]:
    """Risk signal + weakness signal -> Engineering Warning.

    Fires on >=1 ENGINEERING_RISK understanding AND >=1 ENGINEERING_WEAKNESS
    understanding: two independent signals that fragility is building *before*
    it becomes a realized risk. Distinct from BLIND_SPOT (which pairs the
    blind-spot type with a weakness) by requiring the risk type instead.
    """
    risk = [u for u in ctx.understandings
            if u.type.value == UnderstandingType.ENGINEERING_RISK.value]
    weak = [u for u in ctx.understandings
            if u.type.value == UnderstandingType.ENGINEERING_WEAKNESS.value]
    if risk and weak:
        uids = [u.id for u in risk + weak if u.id]
        return [Candidate(
            type=InsightType.WARNING,
            title="Emerging engineering warning",
            statement=("A risk signal is reinforced by a weakness signal. This is "
                       "an early warning: address it now while it is cheap, before "
                       "it escalates into a realized risk."),
            understanding_ids=uids,
            knowledge_ids=_kids_for_understandings(ctx, uids),
            repos=_repos_for_understandings(ctx, uids),
        )]
    return []


def _rule_breakthrough(ctx: _Ctx) -> List[Candidate]:
    """Emerging expertise across areas -> Engineering Breakthrough.

    Fires on >=2 EMERGING_EXPERTISE understandings (a compound capability leap)
    and optionally folds in a strength understanding to mark the new ground.
    """
    exp = [u for u in ctx.understandings
           if u.type.value == UnderstandingType.EMERGING_EXPERTISE.value]
    if len(exp) >= 2:
        uids = [u.id for u in exp if u.id]
        strength = [u for u in ctx.understandings
                    if u.type.value == UnderstandingType.ENGINEERING_STRENGTH.value]
        all_u = uids + [u.id for u in strength if u.id]
        return [Candidate(
            type=InsightType.BREAKTHROUGH,
            title="Engineering breakthrough emerging",
            statement=("Expertise is emerging across multiple areas at once. This "
                       "is a breakthrough moment: accumulated effort is converting "
                       "into new capability. Feed it."),
            understanding_ids=all_u,
            knowledge_ids=_kids_for_understandings(ctx, all_u),
            repos=_repos_for_understandings(ctx, all_u),
        )]
    return []


def _rule_efficiency(ctx: _Ctx) -> List[Candidate]:
    """Repeated patterns across the workspace -> Engineering Efficiency.

    Fires on >=3 RECURRING_PATTERN knowledge items (the quality bar for pure
    knowledge evidence). Consolidating these into shared tooling or process
    recovers engineering efficiency at every repetition.
    """
    pat_k = [k for k in ctx.knowledge
             if k.type and k.type.value == "recurring_pattern"]
    if len(pat_k) >= 3:
        kids = [k.id for k in pat_k if k.id]
        return [Candidate(
            type=InsightType.EFFICIENCY,
            title="Engineering efficiency opportunity",
            statement=("The same patterns recur across the workspace. "
                       "Standardizing or automating them would recover engineering "
                       "efficiency at every repetition."),
            knowledge_ids=kids,
            repos=_repos_for_knowledge(ctx, kids),
        )]
    return []


# Ordered rule registry. Each rule is independent and deterministic.
RULES = [
    _rule_reuse_auth,
    _rule_rust_reuse,
    _rule_commercial_risk,
    _rule_convergence,
    _rule_divergence,
    _rule_debt,
    _rule_blind_spot,
    _rule_recommendation,
    _rule_momentum,
    _rule_drift,
    _rule_bottleneck,
    _rule_focus,
    _rule_investment,
    _rule_warning,
    _rule_breakthrough,
    _rule_efficiency,
]


def detect(
    understanding: List,
    initiatives: List,
    knowledge: List,
) -> List[Candidate]:
    """Run every rule, apply the quality filter, return surviving candidates."""
    ctx = _Ctx(understandings=understanding, initiatives=initiatives,
               knowledge=knowledge)
    out: List[Candidate] = []
    for rule in RULES:
        for c in rule(ctx):
            if _qualifies(c):
                out.append(c)
    return out
