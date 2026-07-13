"""Strategic judgment — Milestone 6.5B + 6.5C.

Each strategic question uses a DISTINCT reasoning axis over already-persisted
evidence (6.5B). The answer is SYNTHESIZED, not dumped (6.5C): one prose thesis
that (1) answers the question, (2) explains why, (3) cites the evidence, and
(4) states Confidence. No bullet inventories, no technology/architecture dumps
unless directly relevant. No LLM planning, no new storage, nothing invented —
every clause is backed by a stored fact.

Axes (deliberately disjoint so one ranking cannot answer several questions):
  impact     -> USER VALUE          (stated value, shipped maturity, adoption)
  platform   -> REUSABLE CAPABILITY (shared components / abstractions)
  learning   -> ENGINEERING COMPLEXITY (what stretched the engineer)
  opportunity-> LEVERAGE           (what unlocks other work / is under-exploited)
  priority   -> CURRENT BLOCKERS    (what is urgent / stalled / moving now)
  converge   -> SYNTHESIS          (what the body of work is converging toward)
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Optional

from . import query as q
from .db import get_all_relationships
from .portfolio import _repo_facts, _langs, detect_themes, engineering_universe


_STRONG = "Strong"
_MEDIUM = "Medium"
_WEAK = "Weak"


@dataclass
class Judgment:
    """An engineering opinion (M6.5E): recommendation + reasoning + evidence + confidence.

    Every strategic question returns one of these instead of a bare ranking. If
    the stored evidence is insufficient for an honest opinion, `recommendation`
    says so plainly, `evidence` is empty, and `confidence` is "Insufficient".
    """

    axis: str
    recommendation: str
    reasoning: str
    evidence: list[str] = field(default_factory=list)
    confidence: str = "Medium"  # Strong | Medium | Weak | Insufficient

    def render(self) -> str:
        if not self.evidence:
            return (
                f"Recommendation: {self.recommendation}. "
                f"Reasoning: {self.reasoning}. "
                f"Evidence: (none yet — I'm not going to invent one). "
                f"Confidence: {self.confidence}."
            )
        return (
            f"Recommendation: {self.recommendation}. "
            f"Reasoning: {self.reasoning}. "
            f"Evidence: {'; '.join(self.evidence)}. "
            f"Confidence: {self.confidence}."
        )

# Architectures/domains that represent genuine engineering stretch (learning).
_HARD_DOMAINS = ("operating system", "kernel", "os in", "ai", "machine learning",
                 "compiler", "real-time", "realtime", "distributed", "async")


def _names(conn):
    return q.architecture_name_map(conn)


def _evidence_lines(conn, today):
    """Pull the disjoint evidence slices each axis needs, once."""
    facts = _repo_facts(conn, today)
    return facts


# ---------------------------------------------------------------------------
# Impact — user value
# ---------------------------------------------------------------------------


def strategy_impact(conn, today: Optional[dt.date] = None) -> list[str]:
    """'Which project has the highest impact?' — judge by USER VALUE, never by
    commits or raw activity."""
    today = today or dt.date.today()
    facts = _evidence_lines(conn, today)
    cands: list[tuple[str, list[str]]] = []
    for f in facts.values():
        reasons: list[str] = []
        if f["biz"]:
            reasons.append("states explicit user/business value")
        if f["maturity"] in ("stable", "beta", "alpha"):
            reasons.append(f"shipped at {f['maturity']} maturity")
        if f["related"]:
            reasons.append(f"tied to {len(f['related'])} other project(s)")
        if reasons:
            cands.append((f["name"], reasons))

    if not cands:
        return [Judgment(
            axis="impact",
            recommendation="I can't give an impact opinion yet",
            reasoning=("No project in the stored evidence states user/business "
                       "value, ships to users, or connects to others — so any "
                       "ranking would be a guess. Run `friday analyze <path>` so "
                       "purpose and business value get captured."),
            evidence=[],
            confidence="Insufficient",
        ).render()]
    cands.sort(key=lambda c: -len(c[1]))
    top_name, top_reasons = cands[0]
    conf = _STRONG if len(top_reasons) >= 2 else _MEDIUM
    others = ""
    if len(cands) > 1:
        others = " For comparison, " + ", ".join(
            f"{n} ({'; '.join(r)})" for n, r in cands[1:])
    return [Judgment(
        axis="impact",
        recommendation=f"Prioritize {top_name} — it has the highest impact",
        reasoning=(f"Judged by user value rather than activity: "
                   + "; ".join(top_reasons) + "." + others),
        evidence=top_reasons,
        confidence=conf,
    ).render()]


# ---------------------------------------------------------------------------
# Platform — reusable capability
# ---------------------------------------------------------------------------


def strategy_platform(conn, today: Optional[dt.date] = None) -> list[str]:
    """'Which should become a platform?' — judge by REUSABLE CAPABILITY."""
    today = today or dt.date.today()
    from .query import shared_components, reuse_opportunities

    shared = shared_components(conn, "Medium")
    abs_rels = [r for r in get_all_relationships(conn)
                if r.kind in ("shared-abstraction", "shared-implementation",
                              "shared-framework", "shared-architecture")
                and r.strength != _WEAK]
    names = _names(conn)
    capcount: Counter = Counter()
    for repos in shared.values():
        for name in repos:
            capcount[name] += 1
    for r in abs_rels:
        if names.get(r.repo_a):
            capcount[names[r.repo_a]] += 1
        if names.get(r.repo_b):
            capcount[names[r.repo_b]] += 1

    if not capcount:
        return [Judgment(
            axis="platform",
            recommendation="I can't name a platform candidate yet",
            reasoning=("No reusable capability is visible — no shared components, "
                       "abstractions or reuse opportunities are stored. A platform "
                       "needs code or abstractions more than one project already "
                       "consumes, and that isn't in the evidence."),
            evidence=[],
            confidence="Insufficient",
        ).render()]

    lead, n = capcount.most_common(1)[0]
    caps = [c for c, repos in shared.items() if lead in repos]
    bits = []
    if caps:
        bits.append("it already provides reusable capability others consume ("
                    + ", ".join(caps) + ")")
    if abs_rels and names.get(abs_rels[0].repo_a) == lead:
        partners = [names.get(r.repo_b) for r in abs_rels
                    if names.get(r.repo_a) == lead and names.get(r.repo_b)]
        if partners:
            bits.append("shares engineering with " + ", ".join(filter(None, partners)))
    other = ""
    if len(capcount) > 1:
        other = " Other repos contributing reusable capability: " + ", ".join(
            f"{name} ({c})" for name, c in capcount.most_common()[1:])
    reuse = reuse_opportunities(conn)
    if reuse:
        other += " Concrete reuse already detected: " + "; ".join(reuse[:2]) + "."
    return [Judgment(
        axis="platform",
        recommendation=f"Grow {lead} into a platform",
        reasoning=(f"Strongest platform candidate because "
                   + ("; ".join(bits) if bits else "it carries the most reusable "
                     "capability across the workspace") + "." + other),
        evidence=bits,
        confidence=_MEDIUM,
    ).render()]


# ---------------------------------------------------------------------------
# Learning — engineering complexity
# ---------------------------------------------------------------------------


def strategy_learning(conn, today: Optional[dt.date] = None) -> list[str]:
    """'Which teaches me the most?' — judge by ENGINEERING COMPLEXITY."""
    today = today or dt.date.today()
    facts = _evidence_lines(conn, today)
    cands: list[tuple[str, list[str]]] = []
    for rid, f in facts.items():
        reasons: list[str] = []
        if f["complexity"]:
            reasons.append(f"took on complexity ({f['complexity']})")
        nlang = len({l.language for l in _langs(conn, rid)})
        if nlang >= 2:
            reasons.append(f"spans {nlang} languages on one project")
        if any(d in f["purpose"] for d in _HARD_DOMAINS):
            reasons.append(f"entered a hard domain ({_domain_label(f['purpose'])})")
        if reasons:
            cands.append((f["name"], reasons))

    if not cands:
        return [Judgment(
            axis="learning",
            recommendation="I can't say which taught you most yet",
            reasoning=("No project records engineering complexity in the evidence "
                       "— no complexity, language breadth or hard-domain signal is "
                       "stored. Run `friday analyze <path>` to capture what each "
                       "project stretched you on."),
            evidence=[],
            confidence="Insufficient",
        ).render()]
    cands.sort(key=lambda c: -len(c[1]))
    top_name, top_reasons = cands[0]
    conf = _STRONG if len(top_reasons) >= 2 else _MEDIUM
    others = ""
    if len(cands) > 1:
        others = " Also stretching you: " + ", ".join(
            f"{n} ({'; '.join(r)})" for n, r in cands[1:])
    return [Judgment(
        axis="learning",
        recommendation=f"{top_name} taught you the most engineering-wise",
        reasoning=("; ".join(top_reasons) + "." + others),
        evidence=top_reasons,
        confidence=conf,
    ).render()]


# ---------------------------------------------------------------------------
# Opportunity — leverage
# ---------------------------------------------------------------------------


def strategy_opportunity(conn, today: Optional[dt.date] = None) -> list[str]:
    """'What opportunities am I missing?' — judge by LEVERAGE."""
    today = today or dt.date.today()
    from .query import reuse_opportunities
    from .portfolio import integration_opportunities

    reuse = reuse_opportunities(conn)
    integ = integration_opportunities(conn, today)
    names = _names(conn)
    pot = [r for r in get_all_relationships(conn)
           if r.kind == "potential-reuse" and r.strength != _WEAK]

    bits: list[str] = []
    if reuse:
        bits.append("shared code you have not yet unified (" + "; ".join(reuse[:2]) + ")")
    if integ:
        bits.append("integration leverage from " + ", ".join(c.repo for c in integ[:2]))
    if pot:
        flagged = ", ".join(f"{names.get(r.repo_a)} and {names.get(r.repo_b)}"
                            for r in pot[:2] if names.get(r.repo_a))
        if flagged:
            bits.append("flagged potential-reuse not acted on (" + flagged + ")")

    if not bits:
        return [Judgment(
            axis="opportunity",
            recommendation="I don't see a missed opportunity to call out yet",
            reasoning=("No leverage signal is evident in the stored evidence — no "
                       "reuse, integration or potential-reuse. Leverage shows up "
                       "where one effort unlocks several others, and that isn't "
                       "recorded yet. Keep ingesting."),
            evidence=[],
            confidence="Insufficient",
        ).render()]
    return [Judgment(
        axis="opportunity",
        recommendation="Exploit leverage you're leaving on the table",
        reasoning=("The missed opportunities are about leverage, not value: "
                   + "; ".join(bits) + "."),
        evidence=bits,
        confidence=_MEDIUM,
    ).render()]


# ---------------------------------------------------------------------------
# Priority — current blockers / momentum
# ---------------------------------------------------------------------------


def strategy_priority(conn, today: Optional[dt.date] = None) -> list[str]:
    """'What should become the center of my engineering universe?' — judge by
    CURRENT BLOCKERS and momentum."""
    today = today or dt.date.today()
    facts = _evidence_lines(conn, today)
    prios = q.workspace_priorities(conn, today, n=3)
    blocked: list[tuple[str, list[str]]] = []
    for rid, f in facts.items():
        if f["identity"] and f["identity"].blockers:
            blocked.append((f["name"], f["identity"].blockers))

    if not prios and not blocked:
        return [Judgment(
            axis="priority",
            recommendation="I can't pick a center of attention yet",
            reasoning=("Nothing in the stored evidence is urgent or actively "
                       "moving — no current blockers or momentum signal. A "
                       "priority needs something pressing now."),
            evidence=[],
            confidence="Insufficient",
        ).render()]

    thesis_bits: list[str] = []
    if prios:
        top_repo, top_reasons = prios[0]
        thesis_bits.append(
            f"make {top_repo.name} the center now — " + "; ".join(top_reasons))
    if blocked:
        thesis_bits.append("resolve blockers first in "
                           + ", ".join(f"{n} ({'; '.join(r)})" for n, r in blocked))
    stale = q.inactive_repos(conn, today)
    if stale:
        thesis_bits.append("do not center on the stalled repos (" +
                           ", ".join(r.name for r in stale[:3]) + ") yet")
    return [Judgment(
        axis="priority",
        recommendation="Follow current momentum, not lifetime size",
        reasoning=("The center of your engineering universe should follow current "
                   "momentum and blockers: " + "; ".join(thesis_bits) + "."),
        evidence=thesis_bits,
        confidence=_MEDIUM,
    ).render()]


# ---------------------------------------------------------------------------
# Converge — synthesis of where the work is heading (6.5C thesis)
# ---------------------------------------------------------------------------


def strategy_converge(conn, today: Optional[dt.date] = None) -> list[str]:
    """'What am I ultimately trying to build?' — SYNTHESIZE a thesis about where
    the body of work converges, citing evidence. No inventory, no bullet dump."""
    today = today or dt.date.today()
    themes = detect_themes(conn, today)
    strong = [t for t in themes if t.confidence in (_STRONG, _MEDIUM)]
    if not strong:
        return [Judgment(
            axis="converge",
            recommendation="I can't synthesize where your work is heading yet",
            reasoning=("Too few projects state a purpose or theme in the stored "
                       "evidence to form a thesis. Run `friday analyze <path>` on "
                       "more repos so convergence becomes visible."),
            evidence=[],
            confidence="Insufficient",
        ).render()]

    # Build the converging picture from evidence: the strong themes + how many
    # repos each spans. Stay strictly evidence-bound.
    theme_bits = ", ".join(f"{t.theme} ({len(t.repos)} projects)" for t in strong)
    named = sorted({r for t in strong for r in t.repos})
    named_str = ", ".join(named)
    conf = _STRONG if any(t.confidence == _STRONG for t in strong) else _MEDIUM

    return [Judgment(
        axis="converge",
        recommendation=f"You're converging on {_phrase(strong)}",
        reasoning=(f"The evidence across your projects clusters around {theme_bits}, "
                   f"most directly in {named_str}."),
        evidence=[theme_bits, f"most directly in {named_str}"],
        confidence=conf,
    ).render()]


# ---------------------------------------------------------------------------
# Merge — which project should stay independent (inverse of integration)
# ---------------------------------------------------------------------------


def strategy_merge(conn, today: Optional[dt.date] = None) -> list[str]:
    """'What project should never merge?' — the inverse of integration: name the
    project whose independence is worth protecting, and why."""
    today = today or dt.date.today()
    from .portfolio import integration_opportunities

    integ = integration_opportunities(conn, today)
    if not integ:
        # No integration candidate -> nothing is pulling projects together, so
        # the honest opinion is that no merge is on the table to oppose.
        return [Judgment(
            axis="merge",
            recommendation="Nothing should be forced to merge right now",
            reasoning=("No project shows a credible integration case in the stored "
                       "evidence, so there's no merge to oppose — keep each project "
                       "independent until a real reason to combine appears."),
            evidence=[],
            confidence="Insufficient",
        ).render()]
    # The strongest integration candidate is precisely the one worth questioning:
    # argue for keeping it independent unless the reuse is genuine.
    lead = integ[0]
    return [Judgment(
        axis="merge",
        recommendation=f"Don't merge {lead.repo} by default — earn it",
        reasoning=(f"{lead.repo} is the only plausible integration candidate "
                   f"({lead.reason}). That is a reason to scrutinize a merge, not "
                   f"to do it: keep it independent unless the shared purpose or "
                   f"technology actually multiplies effort. Merge only when the "
                   f"reuse is real, not incidental."),
        evidence=[lead.reason],
        confidence=lead.confidence,
    ).render()]


def _phrase(strong_themes) -> str:
    """Compose a human 'X where Y' phrase from the strongest themes."""
    labels = [t.theme.lower() for t in strong_themes]
    if "AI infrastructure" in labels and "Operating systems" in labels:
        return "an AI operating system — a persistent intelligence layer that spans your engineering projects"
    if "AI infrastructure" in labels:
        return "an AI-assisted engineering practice"
    if "Operating systems" in labels:
        return "systems software / operating-system work"
    if "Developer tooling" in labels:
        return "developer tooling"
    return ", ".join(labels)


def _domain_label(purpose: str) -> str:
    for d in ("operating system", "kernel", "ai", "machine learning", "compiler",
              "real-time", "distributed", "async"):
        if d in purpose:
            return d
    return "hard domain"
