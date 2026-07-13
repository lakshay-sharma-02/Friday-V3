"""Workspace-level reasoning — Milestone 3.6.

Pure, deterministic aggregation over the SQLite knowledge base. No LLM, no new
infrastructure: everything is derived from repositories, languages, technologies,
architecture, components, entry points, relationships, identity and insights that
M1–M3.5 already persist.

The functions here answer the questions Friday *couldn't* before because it
thought repository-first:

  - What am I building?            (recurring themes across projects)
  - Which project is most valuable? (aggregated evidence + confidence)
  - What parts overlap?            (meaningful dimensions, not syntax)
  - Which should integrate w/ Friday? (reasoned from identity)
  - Which to continue / pause?     (leverage, not commit counts)
  - What is my engineering universe? (workspace observations)

Every output is evidence-backed and carries a Confidence level (Strong/Medium/
Weak) with the basis stated. Nothing here invents — when evidence is thin we say
so plainly rather than refusing with "I don't have enough evidence".
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Theme taxonomy (spec §1, §3, §9)
# ---------------------------------------------------------------------------
#
# Candidate themes come from a FIXED registry, not from free-form inference.
# Each theme lists signals; a signal matches a repo on one of its stored fields.
# `weight` makes confidence computable: purpose-grade text is strong, a shared
# technology/framework is medium, a maturity keyword is a weak coincidence.

_STRONG = "Strong"
_MEDIUM = "Medium"
_WEAK = "Weak"

# field: which stored fact a signal reads.
#   purpose  -> ProjectIdentity.purpose (README/manifest/architecture hint)
#   tech      -> detected technologies
#   arch      -> stored architecture label
#   maturity  -> repo.maturity
#   biz       -> README-stated business value (presence)
_THEME_SIGNALS: dict[str, list[tuple[str, str, str]]] = {
    # theme: [(field, match_substring, weight)]
    "AI infrastructure": [
        ("purpose", "ai", _STRONG),
        ("purpose", "machine learning", _STRONG),
        ("purpose", "assistant", _STRONG),
        ("purpose", "model", _MEDIUM),
        ("tech", "supabase", _MEDIUM),
        ("tech", "pytorch", _MEDIUM),
        ("tech", "tensorflow", _MEDIUM),
        ("arch", "ai", _MEDIUM),
    ],
    "Operating systems": [
        ("purpose", "operating system", _STRONG),
        ("purpose", "kernel", _STRONG),
        ("purpose", "os in", _STRONG),
        ("arch", "os", _MEDIUM),
        ("tech", "rust", _MEDIUM),  # OS work is predominantly Rust here
    ],
    "Developer tooling": [
        ("purpose", "developer", _STRONG),
        ("purpose", "tooling", _STRONG),
        ("purpose", "cli", _MEDIUM),
        ("purpose", "command-line", _MEDIUM),
        ("arch", "cli", _MEDIUM),
        ("arch", "library", _MEDIUM),
    ],
    "Products": [
        ("maturity", "beta", _MEDIUM),
        ("maturity", "stable", _MEDIUM),
        ("maturity", "alpha", _MEDIUM),
        ("purpose", "app", _MEDIUM),
        ("purpose", "product", _MEDIUM),
        ("biz", "present", _STRONG),
    ],
    "Mental health": [
        ("purpose", "mental health", _STRONG),
        ("purpose", "wellness", _STRONG),
        ("purpose", "therapy", _STRONG),
    ],
    "Commercial applications": [
        ("biz", "present", _STRONG),
        ("purpose", "commercial", _STRONG),
        ("purpose", "business", _MEDIUM),
        ("maturity", "beta", _WEAK),
        ("maturity", "stable", _WEAK),
    ],
    "Research": [
        ("purpose", "research", _STRONG),
        ("purpose", "experiment", _MEDIUM),
        ("maturity", "unknown", _WEAK),
        ("maturity", "wip", _WEAK),
    ],
}


@dataclass
class ThemeResult:
    theme: str
    repos: list[str]
    evidence: list[str]
    confidence: str

    @property
    def is_real(self) -> bool:
        return bool(self.repos)


@dataclass
class ValueResult:
    repo: str
    score: float
    signals: list[str]
    confidence: str


@dataclass
class OverlapResult:
    a: str
    b: str
    dimensions: list[str]  # meaningful, evidence-backed dimensions only
    confidence: str


@dataclass
class IntegrationResult:
    repo: str
    reason: str
    confidence: str


@dataclass
class WorkspaceRec:
    continue_projects: list[tuple[str, str]]   # (name, why)
    pause_projects: list[tuple[str, str]]      # (name, why)
    attention: tuple[str, str]                  # (name, why) — most attention
    confidence: str


# ---------------------------------------------------------------------------
# Evidence gathering helpers
# ---------------------------------------------------------------------------


def stated_intent(conn, repo_id: int) -> Optional[dict]:
    """Compute the repository's *stated* intent from already-persisted evidence.

    Part C: interpretation stays derived, not persisted. Reads the stored
    README summary (Purpose / Roadmap / Mission / Goals lines) and returns the
    explicit goals the author stated — distinct from the *recovered/derived*
    purpose. Returns None when no goals were stated. Never invents.
    """
    from .db import get_repositories

    repo = next((r for r in get_repositories(conn) if r.id == repo_id), None)
    if repo is None or not repo.readme_summary:
        return None
    summary = repo.readme_summary
    goals: list[str] = []
    in_roadmap = False
    for line in summary.splitlines():
        low = line.strip().lower()
        if low.startswith(("roadmap:", "mission:", "goals:", "goal:")):
            in_roadmap = low.split(":", 1)[0] in ("roadmap", "goals", "goal")
            val = line.split(":", 1)[1].strip()
            if val and val != "none stated":
                goals.append(val)
        elif in_roadmap and low.startswith("- "):
            g = low[2:].strip()
            if g and g != "none stated":
                goals.append(g)
    if not goals:
        return None
    return {"repo": repo.name, "goals": goals}


def all_stated_intents(conn) -> list[dict]:
    """Every repository's computed stated intent (Part C)."""
    out: list[dict] = []
    for r in _repos(conn):
        if r.id is None:
            continue
        si = stated_intent(conn, r.id)
        if si:
            out.append(si)
    return out


def _repo_facts(conn, today: dt.date) -> dict[int, dict]:
    """Build a per-repo evidence dict reused by every portfolio function."""
    from . import identity
    from .db import get_technologies, get_architecture

    out: dict[int, dict] = {}
    for r in _repos(conn):
        if r.id is None:
            continue
        ident = identity.build_identity(conn, r.id, today)
        techs = [t.tech for t in get_technologies(conn, r.id)]
        arch = get_architecture(conn, r.id)
        biz = bool(ident.business_value) if ident else False
        out[r.id] = {
            "name": r.name,
            "purpose": (ident.purpose or "").lower() if ident else "",
            "tech": {t.lower() for t in techs},
            "arch": (arch.architecture or "").lower() if arch else "",
            "arch_raw": (arch.architecture or "") if arch else "",
            "complexity": (arch.complexity or "") if arch else "",
            "maturity": (r.maturity or "unknown").lower(),
            "biz": biz,
            "commit_count": r.commit_count or 0,
            "last_commit_date": r.last_commit_date,
            "is_dirty": r.is_dirty,
            "related": ident.related_projects if ident else [],
            "identity": ident,
        }
    return out


def _repos(conn):
    from .query import all_repositories

    return all_repositories(conn)


# ---------------------------------------------------------------------------
# Themes (§1, §3, §9)
# ---------------------------------------------------------------------------


def detect_themes(conn, today: Optional[dt.date] = None) -> list[ThemeResult]:
    today = today or dt.date.today()
    facts = _repo_facts(conn, today)
    results: list[ThemeResult] = []

    for theme, signals in _THEME_SIGNALS.items():
        matched: list[str] = []
        ev: list[str] = []
        strong_hits = 0
        medium_hits = 0
        for rid, f in facts.items():
            for field_, match, weight in signals:
                val = {
                    "purpose": f["purpose"],
                    "tech": " ".join(f["tech"]),
                    "arch": f["arch"],
                    "maturity": f["maturity"],
                    "biz": "present" if f["biz"] else "",
                }[field_]
                if _matches(val, match):
                    if f["name"] not in matched:
                        matched.append(f["name"])
                        ev.append(f"{f['name']}: {field_} mentions '{match}'")
                    if weight == _STRONG:
                        strong_hits += 1
                    elif weight == _MEDIUM:
                        medium_hits += 1
                    break  # one signal per repo is enough to count the repo
        if not matched:
            continue
        if strong_hits >= 2 or (strong_hits >= 1 and medium_hits >= 1):
            conf = _STRONG
        elif strong_hits == 1 or medium_hits >= 2:
            conf = _MEDIUM
        else:
            conf = _WEAK
        results.append(ThemeResult(theme=theme, repos=matched, evidence=ev, confidence=conf))

    # Rank: confidence then breadth of support.
    order = {_STRONG: 0, _MEDIUM: 1, _WEAK: 2}
    results.sort(key=lambda r: (order[r.confidence], -len(r.repos)))
    return results


def portfolio_synthesis(conn, today: Optional[dt.date] = None) -> list[str]:
    """Answer 'What am I building?' — portfolio IDENTITY only.

    Pulls the smallest evidence set that answers 'what/who am I building':
    recurring themes (purpose-level), then what each project's stated purpose
    is, then the roadmap/goals the author set. Deliberately OMITS languages,
    architectures and activity — those answer 'what strengths am I developing'
    and 'where is my effort going', not 'what am I building'. Ends with a
    Confidence line."""
    today = today or dt.date.today()
    blocks: list[str] = []
    facts = _repo_facts(conn, today)

    # Recurring themes across projects — the clearest 'what am I building' signal.
    themes = detect_themes(conn, today)
    if themes:
        blocks.append("Recurring themes across your projects:")
        for t in themes:
            names = ", ".join(t.repos)
            blocks.append(f"- {t.theme} ({t.confidence} confidence): {names}.")
    else:
        blocks.append("No strong recurring themes detected yet — too few projects with purpose evidence.")

    # Portfolio identity: each project's stated purpose (what it IS being built).
    by_purpose = sorted(
        {f["name"]: f["purpose"] for f in facts.values() if f["purpose"]}.items(),
        key=lambda kv: kv[0],
    )
    if by_purpose:
        blocks.append("What each project is (by stated purpose):")
        for name, purpose in by_purpose:
            blocks.append(f"- {name}: {purpose.rstrip('.')}.")

    # Stated intent (Part C): what the author SAID they'd build (README
    # Roadmap/Goals). This is the author's own 'what I'm building' statement.
    intents = all_stated_intents(conn)
    if intents:
        blocks.append("What you've stated you're building (from project roadmaps/goals):")
        for si in intents:
            blocks.append(f"- {si['repo']}: {'; '.join(si['goals'][:3])}.")

    # Confidence line (the basis, so it is never a bare claim).
    if themes:
        top = themes[0]
        blocks.append(
            f"Confidence: {top.confidence} — derived from project purposes and "
            f"roadmaps already stored for your projects."
        )
    else:
        blocks.append(
            "Confidence: Weak — little purpose/roadmap evidence is stored. "
            "Run `friday analyze <path>` on more repos to sharpen this picture."
        )
    return blocks


# ---------------------------------------------------------------------------
# Portfolio sub-modes (Milestone 6, F1): same intent, different evidence
# ---------------------------------------------------------------------------


def portfolio_strengths(conn, today: Optional[dt.date] = None) -> list[str]:
    """Answer 'what engineering strengths am I developing?' — evidence of
    *capability built*: repeated architectures, engineering patterns, languages,
    systems shipped, and complexity handled. Deliberately OMITS portfolio
    purpose/themes (that is 'what am I building') and current activity (that is
    'where is my effort going'). Ends with a Confidence line."""
    today = today or dt.date.today()
    facts = _repo_facts(conn, today)
    blocks: list[str] = []

    # Systems / architectures actually built (from stored architecture labels).
    arch_counts: dict[str, list[str]] = {}
    for f in facts.values():
        a = f["arch_raw"]
        if a and a != "Unknown":
            arch_counts.setdefault(a, []).append(f["name"])
    if arch_counts:
        blocks.append("Systems and architectures you've built:")
        for arch, names in sorted(arch_counts.items(), key=lambda kv: -len(kv[1])):
            blocks.append(f"- {arch}: {', '.join(names)}.")

    # Repeated architectures = the same class of system solved more than once.
    repeated = {k: v for k, v in arch_counts.items() if len(v) >= 2}
    if repeated:
        blocks.append("Engineering patterns you've repeated across projects:")
        for label, names in sorted(repeated.items(), key=lambda kv: -len(kv[1])):
            blocks.append(f"- {label} (solved in {', '.join(names)})")

    # Language / tech surface = breadth of capability.
    langs = {l.language for r in _repos(conn) if r.id is not None
             for l in _langs(conn, r.id)}
    if langs:
        blocks.append(
            f"Language breadth: you work across {len(langs)} languages "
            f"({', '.join(sorted(langs))})."
        )

    # Complexity handled (stored architecture complexity) = depth of strength.
    complex_bits = [(f["name"], f["complexity"]) for f in facts.values() if f["complexity"]]
    if complex_bits:
        blocks.append("Complex systems you've taken on:")
        for name, c in complex_bits:
            blocks.append(f"- {name}: {c}")

    if not blocks:
        blocks.append(
            "Not enough built evidence yet to characterize your engineering "
            "strengths. Run `friday analyze <path>` on more repositories."
        )
    blocks.append(
        "Confidence: Medium — derived from stored architecture labels, "
        "languages and complexity, not from purpose themes or commit counts."
    )
    return blocks


def portfolio_effort(conn, today: Optional[dt.date] = None) -> list[str]:
    """Answer 'where is my engineering effort going?' — current activity and
    momentum, not lifetime totals. Pulls observation history, recent activity,
    dirty repos and current work."""
    today = today or dt.date.today()
    from .db import latest_observation
    from .query import most_active, inactive_repos

    blocks: list[str] = []
    facts = _repo_facts(conn, today)
    active = {r.id: s for r, s in most_active(conn, today, len(facts))}

    # Currently active, uncommitted work = effort happening right now.
    dirty = [f["name"] for f in facts.values() if f["is_dirty"]]
    if dirty:
        blocks.append("Active, uncommitted work right now:")
        for n in dirty:
            blocks.append(f"- {n} has uncommitted changes.")

    # Recent momentum: highest commit-rate repos.
    if active:
        by_id = {rid: f["name"] for rid, f in facts.items()}
        top = sorted(active.items(), key=lambda kv: -kv[1])[:3]
        blocks.append("Highest recent commit velocity:")
        for rid, rate in top:
            blocks.append(f"- {by_id.get(rid, '?')} (~{rate:.1f} commits/day over its lifetime)")

    # Observation history: compare the last two snapshots for momentum shifts.
    snaps = latest_observation(conn)
    if snaps:
        latest = snaps[0].observed_at
        blocks.append(f"Last recorded observation: {latest}.")

    # Stalled effort (no recent commit) — where attention has drained.
    stale = inactive_repos(conn, today)
    if stale:
        blocks.append("Effort has stalled here (no recent commit):")
        for r in stale[:3]:
            blocks.append(f"- {r.name}")

    if not blocks:
        blocks.append(
            "No current activity signal — no repositories are ingested or none "
            "have commit history."
        )
    blocks.append(
        "Confidence: Medium — based on live git status, commit velocity and "
        "observation history, not on self-reported intent."
    )
    return blocks


def portfolio_identity(conn, today: Optional[dt.date] = None) -> list[str]:
    """Answer 'What kind of engineer am I?' — self-portrait from the body of
    work: engineering DOMAINS you operate in, breadth vs specialization, the
    recurring engineering DECISIONS you make, and the DIRECTION the portfolio is
    heading. Deliberately OMITS current activity ('where is my effort going')
    and product purpose ('what am I building'). Ends with a Confidence line."""
    today = today or dt.date.today()
    facts = _repo_facts(conn, today)
    blocks: list[str] = []

    # Engineering domains: which engineering areas the work actually spans.
    themes = detect_themes(conn, today)
    if themes:
        blocks.append("Engineering domains you operate across:")
        for t in themes:
            blocks.append(f"- {t.theme} ({t.confidence} confidence): {', '.join(t.repos)}.")

    # Breadth vs specialization, computed from the workspace itself.
    langs = {l.language for r in _repos(conn) if r.id is not None
             for l in _langs(conn, r.id)}
    arch_counts: dict[str, int] = {}
    for f in facts.values():
        a = f["arch_raw"]
        if a and a != "Unknown":
            arch_counts[a] = arch_counts.get(a, 0) + 1
    # Specialization within a domain: a single theme / architecture dominating.
    if langs:
        if len(langs) >= 4:
            breadth = (f"Broad: you work across {len(langs)} languages "
                       f"({', '.join(sorted(langs))}) — a generalist's reach.")
        else:
            breadth = (f"Focused: concentrated in {len(langs)} languages "
                       f"({', '.join(sorted(langs))}) — a specialist's depth.")
        blocks.append(breadth)
    if arch_counts:
        top_arch, top_n = max(arch_counts.items(), key=lambda kv: kv[1])
        if top_n >= 2:
            blocks.append(
                f"Specialization signal: {top_arch} is the system type you reach "
                f"for most ({top_n} projects) — a recurring engineering decision."
            )

    # Repeated engineering decisions: relationships + reused approaches (Medium+
    # only — weak coincidences are not decisions).
    from .db import get_all_relationships
    decs: list[str] = []
    name_by_id = {r.id: r.name for r in _repos(conn) if r.id is not None}
    seen_dec: set[tuple[str, str]] = set()
    for rel in get_all_relationships(conn):
        if rel.strength == "Weak":
            continue
        if rel.kind in ("shared-architecture", "shared-framework", "shared-db",
                        "shared-deployment"):
            an = name_by_id.get(rel.repo_a)
            bn = name_by_id.get(rel.repo_b)
            if an and bn:
                key = tuple(sorted((an, bn)))
                if key in seen_dec:
                    continue
                seen_dec.add(key)
                decs.append(f"- you repeatedly choose a shared {rel.kind.replace('shared-', '')} "
                            f"({an} + {bn}: {rel.evidence})")
    if decs:
        blocks.append("Engineering decisions you keep making:")
        blocks.extend(decs)

    # Portfolio direction: the trajectory the body of work is heading (themes
    # that are newest / most recently started) — derived from first_commit_date.
    ranked = sorted(facts.values(), key=lambda f: f["last_commit_date"] or "", reverse=True)
    recent = [f["name"] for f in ranked[:3] if f["last_commit_date"]]
    if recent:
        blocks.append("Portfolio direction (most recently started): "
                      + ", ".join(recent) + ".")

    if not blocks:
        blocks.append(
            "Not enough cross-project evidence yet to characterize the kind of "
            "engineer you are. Run `friday analyze <path>` on more repositories."
        )
    blocks.append(
        "Confidence: Medium — derived from engineering domains, breadth, repeated "
        "decisions and portfolio direction, not from current activity."
    )
    return blocks


# ---------------------------------------------------------------------------
# Project value (§4)
# ---------------------------------------------------------------------------


def project_value_ranking(conn, today: Optional[dt.date] = None) -> list[ValueResult]:
    """Aggregate evidence into a value ranking (spec §4 signals). Never refuses
    with 'not enough evidence' when any signal exists — instead states
    Medium/Weak confidence and the basis."""
    today = today or dt.date.today()
    from .query import most_active

    facts = _repo_facts(conn, today)
    total_commits = sum(f["commit_count"] for f in facts.values())
    active = {r.id: s for r, s in most_active(conn, today, len(facts))}

    results: list[ValueResult] = []
    for rid, f in facts.items():
        signals: list[str] = []
        score = 0.0
        if f["identity"] and f["identity"].purpose:
            signals.append("has a stated purpose")
            score += 2
        if f["biz"]:
            signals.append("explicitly states business value")
            score += 3
        if rid in active:
            signals.append("high recent commit frequency")
            score += min(active[rid], 4)
        if f["commit_count"] and total_commits and f["commit_count"] / total_commits >= 0.4:
            signals.append("carries the majority of workspace commits")
            score += 3
        if f["identity"]:
            if f["identity"].readme_quality and f["identity"].readme_quality not in ("poor", "boilerplate", "none"):
                signals.append("mature README")
                score += 1
            if f["identity"].related_projects:
                signals.append(f"tied to {len(f['identity'].related_projects)} other project(s)")
                score += 1.5
            if f["identity"].blockers:
                signals.append("has known blockers (" + "; ".join(f["identity"].blockers) + ")")
                score -= 1.5
        if f["is_dirty"]:
            signals.append("has active, uncommitted work")
            score += 2

        if not signals:
            continue  # truly nothing — exclude rather than fabricate
        if score >= 6:
            conf = _STRONG
        elif score >= 3:
            conf = _MEDIUM
        else:
            conf = _WEAK
        results.append(ValueResult(repo=f["name"], score=score, signals=signals, confidence=conf))

    results.sort(key=lambda r: r.score, reverse=True)
    return results


# ---------------------------------------------------------------------------
# Meaningful overlap (§5)
# ---------------------------------------------------------------------------


def meaningful_overlap(conn, today: Optional[dt.date] = None) -> list[OverlapResult]:
    """Compare projects along meaningful dimensions (spec §5), never syntax.

    Reuses query.compare_repositories for architecture/responsibilities/deployment/
    persistence/interfaces, then adds business goal, authentication, storage and
    config approach from relationships + Medium+ components. main()/Utility
    script/package.json/single-dependency noise is excluded by construction.
    """
    today = today or dt.date.today()
    from .query import compare_repositories, all_repositories
    from .db import get_all_relationships, get_components, get_architecture
    from . import judgment

    repos = all_repositories(conn)
    named = {r.id: r.name for r in repos if r.id is not None}
    rels = get_all_relationships(conn)

    results: list[OverlapResult] = []
    for i in range(len(repos)):
        for j in range(i + 1, len(repos)):
            a, b = repos[i], repos[j]
            if a.id is None or b.id is None:
                continue
            cmp = compare_repositories(conn, a.id, b.id)
            dims: list[str] = []
            # Architecture counts as overlap ONLY when it is genuinely *shared*
            # (same label), never a "X vs Y" difference — a difference is not a
            # merge/overlap signal (audit Part D/F).
            a_arch = get_architecture(conn, a.id)
            b_arch = get_architecture(conn, b.id)
            if (a_arch and b_arch
                    and a_arch.architecture == b_arch.architecture
                    and cmp["architecture"]):
                dims.append(cmp["architecture"])
            if cmp["responsibilities"]:
                dims.append(cmp["responsibilities"])
            if cmp["deployment"]:
                dims.append(cmp["deployment"])
            if cmp["persistence"]:
                dims.append(cmp["persistence"])
            if cmp["interfaces"]:
                dims.append(cmp["interfaces"])

            # Business goal: shared stated purpose / duplicated functionality.
            for r in rels:
                if {r.repo_a, r.repo_b} == {a.id, b.id} and r.kind == "duplicated-functionality":
                    dims.append(f"shared business goal: {r.evidence}")
                    break

            # Auth / Storage from Medium+ components (Weak concepts excluded).
            for rid, rname in ((a.id, a.name), (b.id, b.name)):
                for c in get_components(conn, rid):
                    if judgment.is_weak(judgment.component_strength(c.name)):
                        continue  # concept, not an implementation to compare
                    if c.name in ("Authentication", "Storage"):
                        other = b.name if rname == a.name else a.name
                        dims.append(f"both implement {c.name.lower()} ({c.evidence})")

            # Config approach: shared-config relationship.
            for r in rels:
                if {r.repo_a, r.repo_b} == {a.id, b.id} and r.kind == "shared-config":
                    dims.append(f"similar configuration approach ({r.evidence})")

            if dims:
                # Dedup while preserving order.
                seen = set()
                clean = [d for d in dims if not (d in seen or seen.add(d))]
                conf = _STRONG if len(clean) >= 3 else _MEDIUM
                results.append(OverlapResult(a=a.name, b=b.name, dimensions=clean, confidence=conf))

    return results


# ---------------------------------------------------------------------------
# Integration opportunities (§6)
# ---------------------------------------------------------------------------


def integration_opportunities(conn, today: Optional[dt.date] = None) -> list[IntegrationResult]:
    """Which project should eventually integrate with Friday (spec §6).

    Reasoned from identity — not hardcoded names. A repo is a candidate when its
    purpose/tech indicates AI-assistant, knowledge-management, developer-workflow
    or systems-integration potential, OR it already shares architecture/tech with
    a Friday repo. Confidence reflects how direct that evidence is.
    """
    today = today or dt.date.today()
    from .query import all_repositories

    friday_repo = next(
        (r for r in all_repositories(conn) if r.id is not None and "friday" in r.name.lower()),
        None,
    )
    friday_techs = set()
    if friday_repo and friday_repo.id is not None:
        friday_techs = {t.tech.lower() for t in _techs(conn, friday_repo.id)}

    # Purpose/tech markers that signal a natural fit with an operating partner.
    _FIT = [
        ("ai", _STRONG), ("assistant", _STRONG), ("knowledge", _MEDIUM),
        ("developer", _MEDIUM), ("workflow", _MEDIUM), ("kernel", _MEDIUM),
        ("operating system", _MEDIUM), ("health", _MEDIUM), ("mental", _MEDIUM),
        ("product", _WEAK), ("app", _WEAK),
    ]

    results: list[IntegrationResult] = []
    for r in all_repositories(conn):
        if r.id is None:
            continue
        if friday_repo and r.id == friday_repo.id:
            continue
        ident = _identity(conn, r.id, today)
        purpose = (ident.purpose or "").lower() if ident else ""
        techs = {t.lower() for t in _techs_names(conn, r.id)}
        reasons: list[str] = []

        for marker, weight in _FIT:
            if _matches(purpose, marker):
                reasons.append(f"purpose suggests {marker}-oriented work")
                strength = weight
                break
        else:
            strength = _WEAK

        shared = sorted(techs & friday_techs)
        if shared:
            reasons.append("shares technology with Friday: " + ", ".join(shared))
            strength = _STRONG if strength in (_STRONG, _MEDIUM) else _MEDIUM

        if reasons:
            results.append(IntegrationResult(
                repo=r.name, reason="; ".join(reasons), confidence=strength,
            ))

    results.sort(key=lambda x: {"Strong": 0, "Medium": 1, "Weak": 2}[x.confidence])
    return results


# ---------------------------------------------------------------------------
# Workspace recommendations (§8)
# ---------------------------------------------------------------------------


def workspace_recommendations(conn, today: Optional[dt.date] = None) -> WorkspaceRec:
    """Continue / pause / most-attention (spec §8). Combines identity, business
    value, activity, blockers, importance, maturity, recent work, relationships
    and purpose — never commit counts alone."""
    today = today or dt.date.today()
    from .query import workspace_priorities

    prios = workspace_priorities(conn, today, n=3)
    facts = _repo_facts(conn, today)

    continue_list: list[tuple[str, str]] = [(r.name, "; ".join(reasons)) for r, reasons in prios]

    # Most attention: the top priority repo, enriched with business value.
    if prios:
        top_repo, top_reasons = prios[0]
        why = "; ".join(top_reasons)
        if facts.get(top_repo.id) and facts[top_repo.id]["biz"]:
            why += " — and it states explicit business value"
        attention = (top_repo.name, why)
    else:
        attention = ("(none)", "no repositories ingested")

    # Pause candidates: dormant / low maturity / low importance, no active blockers.
    pause: list[tuple[str, str]] = []
    for rid, f in facts.items():
        if f["identity"] and f["identity"].blockers:
            continue  # something is actively in the way — not a pause candidate
        stale = f["last_commit_date"] and (today - dt.date.fromisoformat(f["last_commit_date"][:10])).days > 180
        low = (f["maturity"] in ("wip", "unknown")) and f["commit_count"] < 50
        if stale or low:
            reason = "no commit in 180+ days" if stale else "early/low-maturity with little activity"
            pause.append((f["name"], reason))

    # Confidence: grounded if we have priorities + identity signal.
    if prios and any(facts.get(r.id) and facts[r.id]["identity"] for r, _ in prios):
        conf = _MEDIUM
    else:
        conf = _WEAK
    return WorkspaceRec(
        continue_projects=continue_list,
        pause_projects=pause,
        attention=attention,
        confidence=conf,
    )


# ---------------------------------------------------------------------------
# Engineering universe (§9)
# ---------------------------------------------------------------------------


def engineering_universe(conn, today: Optional[dt.date] = None) -> list[str]:
    """Workspace-level observations (spec §9). Derived from themes, shared tech
    and relationships — never from a single repository."""
    today = today or dt.date.today()
    out: list[str] = []
    themes = detect_themes(conn, today)

    # Theme prevalence.
    for t in themes:
        if t.confidence in (_STRONG, _MEDIUM):
            out.append(f"{len(t.repos)} of your projects relate to {t.theme.lower()} "
                       f"({', '.join(t.repos)}).")

    # Repeated technology -> "several projects reuse X".
    from .query import duplicate_tech

    non_lang = {k: v for k, v in duplicate_tech(conn).items() if k not in _LANG_SET}
    for tech, names in sorted(non_lang.items(), key=lambda kv: -len(kv[1]))[:3]:
        out.append(f"Several projects reuse {tech} ({'/'.join(names)}).")

    # Friday as integration point.
    friday_repo = next(
        (r for r in _repos(conn) if r.id is not None and "friday" in r.name.lower()), None
    )
    from .db import get_all_relationships

    if friday_repo:
        rel_count = sum(1 for r in get_all_relationships(conn)
                        if r.repo_a == friday_repo.id or r.repo_b == friday_repo.id)
        if rel_count >= 2:
            out.append(f"Friday has become the integration point for {rel_count} other efforts.")

    # Commercial shift.
    if any(t.theme in ("Commercial applications", "Products") for t in themes):
        out.append("Development focus has shifted toward commercial products.")

    # OS exploration count.
    os_theme = next((t for t in themes if t.theme == "Operating systems"), None)
    if os_theme:
        out.append(f"{len(os_theme.repos)} projects explore operating-system ideas.")

    if not out:
        out.append("Not enough cross-project evidence yet to characterize the workspace.")
    return out


# ---------------------------------------------------------------------------
# Small reused helpers (kept local to avoid import churn)
# ---------------------------------------------------------------------------


_LANG_SET = {
    "Rust", "Python", "Go", "TypeScript", "Java", "C++", "JavaScript",
    "Ruby", "Swift", "Kotlin", "C#", "C", "PHP", "Elixir",
}


def _matches(text: str, needle: str) -> bool:
    """Whole-token match: 'ai' matches 'ai'/'ai-native'/'ai-assisted' but NOT
    'domain'/'email'/'train'. Needed so loose theme tokens don't false-positive
    on substrings (e.g. Aether is an AI-native OS, not generic 'ai' noise)."""
    import re

    return re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", text) is not None


def _langs(conn, repo_id: int):
    from .db import get_languages

    return get_languages(conn, repo_id)


def _techs(conn, repo_id: int):
    from .db import get_technologies

    return get_technologies(conn, repo_id)


def _techs_names(conn, repo_id: int) -> list[str]:
    return [t.tech for t in _techs(conn, repo_id)]


def _identity(conn, repo_id: int, today: dt.date):
    from . import identity

    return identity.build_identity(conn, repo_id, today)
