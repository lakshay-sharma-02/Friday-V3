"""Query engine: deterministic SQL retrieval over the knowledge base.

No embeddings, no semantic search — simple SQL + filtering. Every function
returns plain data (dataclasses / rows) so the `ask` layer can assemble an
evidence package for the LLM to synthesize from.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .db import (
    ArchitectureRow,
    ComponentRow,
    EntryPointRow,
    LangRow,
    RelationshipRow,
    Repository,
    TechRow,
    get_all_relationships,
    get_languages,
    get_repositories,
    get_technologies,
)

STALE_DAYS = 90
ABANDONED_DAYS = 180


def _parse_date(iso: Optional[str]) -> Optional[dt.date]:
    if not iso:
        return None
    try:
        return dt.date.fromisoformat(iso[:10])
    except ValueError:
        return None


def _activity(repo: Repository, today: dt.date) -> str:
    if repo.is_dirty:
        return "Active (uncommitted changes)"
    d = _parse_date(repo.last_commit_date)
    if d is None:
        return "Unknown"
    age = (today - d).days
    if age <= 7:
        return "Very active"
    if age <= STALE_DAYS:
        return "Active"
    return "Dormant"


@dataclass
class IdentityCard:
    repo: Repository
    languages: list[LangRow]
    technologies: list[TechRow]
    activity: str
    relationships: list[RelationshipRow]
    key_observations: list[str] = field(default_factory=list)

    @property
    def tech_names(self) -> set[str]:
        return {t.tech for t in self.technologies}

    @property
    def lang_names(self) -> set[str]:
        return {l.language for l in self.languages}


def all_repositories(conn) -> list[Repository]:
    return get_repositories(conn)


def repo_by_name(conn, name: str) -> Optional[Repository]:
    """Case-insensitive / substring match on repository name."""
    low = name.lower().strip().strip("?.")
    repos = get_repositories(conn)
    # Exact (case-insensitive) first.
    for r in repos:
        if r.name.lower() == low:
            return r
    # Substring / token match.
    for r in repos:
        if low in r.name.lower():
            return r
    # Token overlap (e.g. "friday v3" -> "Friday V3").
    toks = {t for t in low.replace("-", " ").split() if len(t) > 1}
    if toks:
        for r in repos:
            rlow = r.name.lower()
            if any(t in rlow for t in toks):
                return r
    return None


def projects_by_tech(conn, tech: str) -> list[Repository]:
    rows = conn.execute(
        """SELECT r.* FROM repositories r
           JOIN technologies t ON t.repo_id = r.id
           WHERE t.tech = ? ORDER BY r.name""",
        (tech,),
    ).fetchall()
    return [Repository(**{k: row[k] for k in row.keys()}) for row in rows]


def projects_by_language(conn, lang: str) -> list[Repository]:
    rows = conn.execute(
        """SELECT r.* FROM repositories r
           JOIN languages l ON l.repo_id = r.id
           WHERE l.language = ? ORDER BY r.name""",
        (lang,),
    ).fetchall()
    return [Repository(**{k: row[k] for k in row.keys()}) for row in rows]


def inactive_repos(conn, today: dt.date, days: int = STALE_DAYS) -> list[Repository]:
    out = []
    for r in get_repositories(conn):
        d = _parse_date(r.last_commit_date)
        if d and (today - d).days > days:
            out.append(r)
    return out


def abandoned_repos(conn, today: dt.date, days: int = ABANDONED_DAYS) -> list[Repository]:
    return inactive_repos(conn, today, days)


def newest_repos(conn, n: int = 3) -> list[Repository]:
    repos = [r for r in get_repositories(conn) if r.first_commit_date]
    repos.sort(key=lambda r: r.first_commit_date, reverse=True)
    return repos[:n]


def most_active(conn, today: dt.date, n: int = 3) -> list[tuple[Repository, float]]:
    scored = []
    for r in get_repositories(conn):
        first = _parse_date(r.first_commit_date)
        last = _parse_date(r.last_commit_date)
        if first and last and r.commit_count:
            span = max((last - first).days, 1)
            scored.append((r, r.commit_count / span))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:n]


def identity_card(conn, repo_id: int, today: dt.date) -> Optional[IdentityCard]:
    repos = get_repositories(conn)
    repo = next((r for r in repos if r.id == repo_id), None)
    if repo is None:
        return None
    langs = get_languages(conn, repo_id)
    techs = get_technologies(conn, repo_id)
    rels = get_all_relationships(conn)
    my_rels = [
        r for r in rels if r.repo_a == repo_id or r.repo_b == repo_id
    ]
    observations = _key_observations(repo, langs, today)
    return IdentityCard(
        repo=repo,
        languages=langs,
        technologies=techs,
        activity=_activity(repo, today),
        relationships=my_rels,
        key_observations=observations,
    )


def _key_observations(repo: Repository, langs: list[LangRow], today: dt.date) -> list[str]:
    out: list[str] = []
    total = sum(l.file_count for l in langs)
    if total > 0:
        out.append(f"{total} tracked source files")
    if repo.commit_count is not None:
        out.append(f"{repo.commit_count} commits")
    if repo.is_dirty:
        out.append("has uncommitted changes")
    if repo.license:
        out.append(f"licensed under {repo.license}")
    if repo.readme_quality:
        out.append(f"README quality: {repo.readme_quality}")
    if repo.readme_completeness and repo.readme_completeness != "none":
        out.append(f"README completeness: {repo.readme_completeness}")
    if repo.maturity and repo.maturity != "Unknown":
        out.append(f"maturity: {repo.maturity}")
    return out


def relationships_between(conn, repo_a: int, repo_b: int) -> list[RelationshipRow]:
    rows = get_all_relationships(conn)
    return [r for r in rows if {r.repo_a, r.repo_b} == {repo_a, repo_b}]


def duplicate_tech(conn) -> dict[str, list[str]]:
    """tech -> list of repo names using it (only those used by >=2 repos)."""
    out: dict[str, list[str]] = {}
    for r in get_repositories(conn):
        if r.id is None:
            continue
        for t in get_technologies(conn, r.id):
            out.setdefault(t.tech, []).append(r.name)
    return {k: v for k, v in out.items() if len(v) >= 2}


def projects_sharing_config(conn) -> list[tuple[str, str]]:
    """Return (repo_a_name, repo_b_name) pairs that both implement config loading.

    Uses the persisted shared-config relationships.
    """
    pairs = []
    name_by_id = {r.id: r.name for r in get_repositories(conn)}
    for r in get_all_relationships(conn):
        if r.kind == "shared-config":
            an = name_by_id.get(r.repo_a)
            bn = name_by_id.get(r.repo_b)
            if an and bn:
                pairs.append((an, bn))
    return pairs


# ---------------------------------------------------------------------------
# Architecture (Milestone 3)
# ---------------------------------------------------------------------------


def architecture_of(conn, repo_id: int) -> Optional[ArchitectureRow]:
    from .db import get_architecture

    return get_architecture(conn, repo_id)


def components_of(conn, repo_id: int) -> list[ComponentRow]:
    from .db import get_components

    return get_components(conn, repo_id)


def entry_points_of(conn, repo_id: int) -> list[EntryPointRow]:
    from .db import get_entry_points

    return get_entry_points(conn, repo_id)


def architecture_name_map(conn) -> dict[int, str]:
    return {r.id: r.name for r in get_repositories(conn) if r.id is not None}


def shared_components(conn, min_strength: str = "Medium") -> dict[str, list[str]]:
    """component name -> repos that implement it (only those with >=2 repos).

    `min_strength` filters by evidence strength. Component *names* are Weak
    concepts (Database/Config/Auth/...); the default "Medium" therefore excludes
    them from anything that would drive a reuse recommendation. Pass "Weak" to
    include everything (e.g. for a plain inventory).
    """
    from .db import all_components
    from . import judgment

    if min_strength not in ("Weak", "Medium", "Strong"):
        min_strength = "Medium"

    out: dict[str, list[str]] = {}
    name_by_id = architecture_name_map(conn)
    for c in all_components(conn):
        if judgment.is_weak(c.strength) and min_strength == "Medium":
            # Concepts are NOT evidence of reusable implementation.
            continue
        rn = name_by_id.get(c.repo_id)
        if rn:
            out.setdefault(c.name, []).append(rn)
    return {k: v for k, v in out.items() if len(v) >= 2}


def shared_entry_points(conn) -> dict[str, list[str]]:
    """entry-point kind -> repos that expose it (only those with >=2 repos)."""
    from .db import all_entry_points

    out: dict[str, set[str]] = {}
    name_by_id = architecture_name_map(conn)
    for e in all_entry_points(conn):
        rn = name_by_id.get(e.repo_id)
        if rn:
            # Dedup per repo so multiple rows (e.g. several main() files) don't
            # inflate the count.
            out.setdefault(e.kind, set()).add(rn)
    return {k: sorted(v) for k, v in out.items() if len(v) >= 2}


def similar_layouts(conn) -> list[tuple[str, str]]:
    """Repo pairs with the same architecture label.

    NOTE: architecture *label* is a Weak/Medium proxy for layout similarity (two
    "Library" repos may be unrelated). Prefer `reuse_opportunities` / shared
    frameworks for anything actionable. Kept for the `ask` "similar layouts"
    surface, but never as a standalone reuse claim.
    """
    from .db import get_architecture

    arch_by_repo: dict[str, list[str]] = {}
    for r in get_repositories(conn):
        if r.id is None:
            continue
        a = get_architecture(conn, r.id)
        if a:
            arch_by_repo.setdefault(r.name, [a.architecture])
    groups: dict[str, list[str]] = {}
    for name, archs in arch_by_repo.items():
        groups.setdefault(archs[0], []).append(name)
    pairs: list[tuple[str, str]] = []
    for label, repos in groups.items():
        repos = sorted(set(repos))
        for i in range(len(repos)):
            for j in range(i + 1, len(repos)):
                pairs.append((repos[i], repos[j]))
    return pairs


def reuse_opportunities(conn) -> list[str]:
    """Evidence-backed suggestions of realistic shared code across repos.

    Engineering rule (audit §4): reuse is only recommended from Medium/Strong
    implementation-level evidence — same framework, same architecture, the same
    public entry point, or an overlapping stack. Concept components (Database,
    Configuration, Authentication, ...) are Weak and are explicitly excluded:
    "two repos both have a db.py" is NOT a reuse recommendation.
    """
    lines: list[str] = []
    # Actionable: shared entry points (e.g. both expose a FastAPI app / CLI).
    shared_ep = shared_entry_points(conn)
    for ep, repos in sorted(shared_ep.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        if ep in ("main()", "Executable script", "Utility script"):
            continue  # a main()/script in two repos is not an abstraction to share
        lines.append(
            f"{len(repos)} repositories expose a {ep} entry point: " + ", ".join(repos)
        )
    # Actionable: overlapping architecture / framework (from relationships).
    from .db import get_all_relationships

    arch_pairs: list[tuple[str, str, str, str]] = []
    name_by_id = architecture_name_map(conn)
    for r in get_all_relationships(conn):
        if r.strength in ("Medium", "Strong") and r.kind in (
            "shared-framework", "shared-architecture", "shared-deployment",
            "shared-db", "potential-reuse",
        ):
            an = name_by_id.get(r.repo_a)
            bn = name_by_id.get(r.repo_b)
            if an and bn:
                arch_pairs.append((an, bn, r.kind, r.evidence))
    seen: set[tuple[str, str]] = set()
    for an, bn, kind, evidence in arch_pairs:
        key = tuple(sorted((an, bn)))
        if key in seen:
            continue
        seen.add(key)
        label = kind.replace("shared-", "shared ")
        lines.append(
            f"{an} and {bn} share {label} ({evidence}) — candidate for shared code/abstraction."
        )
    return lines


# ---------------------------------------------------------------------------
# Milestone 3.5: human-centric compare + priorities
# ---------------------------------------------------------------------------


def compare_repositories(conn, a_id: int, b_id: int) -> dict:
    """Compare two repos along human-meaningful dimensions (audit §9).

    Answers "could these teach each other something?" — comparing architecture,
    responsibilities, deployment, persistence and interfaces — rather than a bare
    list of shared dependencies. Each dimension is a short phrase or None (no
    evidence / not comparable). Never fabricates a similarity.
    """
    from . import judgment

    a = architecture_of(conn, a_id)
    b = architecture_of(conn, b_id)
    a_comps = {c.name for c in components_of(conn, a_id)}
    b_comps = {c.name for c in components_of(conn, b_id)}
    # Concept components (Database/Config/Auth/...) are Weak ideas, not shared
    # implementations — they must not drive a "shared responsibility" claim.
    a_real = {c for c in a_comps if not judgment.is_weak(judgment.component_strength(c))}
    b_real = {c for c in b_comps if not judgment.is_weak(judgment.component_strength(c))}
    a_techs = {t.tech for t in get_technologies(conn, a_id)}
    b_techs = {t.tech for t in get_technologies(conn, b_id)}
    a_eps = entry_points_of(conn, a_id)
    b_eps = entry_points_of(conn, b_id)

    shared_comps = sorted(a_real & b_real)
    shared_tech = sorted(a_techs & b_techs)
    # Persistence = db-ish techs only (not every shared dependency).
    _DB = {"SQLite", "Postgres", "Redis", "MySQL", "MongoDB", "Prisma"}
    shared_persistence = [t for t in shared_tech if t in _DB]
    # Shared *meaningful* interface surface only — a main()/script in two repos
    # is not an abstraction to share (audit: reused by meaningful_overlap, which
    # must NOT surface main() as overlap, M3.6 §5).
    _WEAK_EP = {"main()", "Executable script", "Utility script"}
    shared_interfaces = sorted(
        ({e.kind for e in a_eps} & {e.kind for e in b_eps}) - _WEAK_EP
    )

    def arch_label(x):
        return x.architecture if x else "Unknown"

    return {
        "architecture": (
            f"both are {arch_label(a)}" if arch_label(a) == arch_label(b)
            else f"{arch_label(a)} vs {arch_label(b)}"
        ) if (a or b) else None,
        "responsibilities": (
            "shared components: " + ", ".join(shared_comps) if shared_comps
            else None
        ),
        "deployment": (
            "both containerize with Docker" if "Docker" in shared_tech else None
        ),
        "persistence": (
            "shared persistence: " + ", ".join(shared_persistence) if shared_persistence
            else None
        ),
        "interfaces": (
            "shared interface surface: " + ", ".join(shared_interfaces)
            if shared_interfaces else None
        ),
        "shared_tech_count": len(shared_tech),
    }


def workspace_priorities(conn, today: dt.date, n: int = 3) -> list[tuple[Repository, list[str]]]:
    """Rank repos by a human notion of "what to continue" (audit §10).

    Combines activity, blockers, importance, business value, recent work,
    uncommitted changes, README maturity and purpose — not commit counts alone.
    Returns (repo, reasons) pairs; reasons are evidence-backed strings.
    """
    repos = all_repositories(conn)
    active = {r.id: score for r, score in most_active(conn, today, len(repos))}
    newest = {r.id for r in newest_repos(conn, 3)}

    scored: list[tuple[Repository, list[str], float]] = []
    for r in repos:
        reasons: list[str] = []
        score = 0.0
        if r.is_dirty:
            reasons.append("has uncommitted changes ready to continue")
            score += 3
        if r.id in active:
            rate = active[r.id]
            # Only cite commit frequency when it is actually meaningful, so we
            # never claim "high activity" for a near-idle repo (audit §10).
            if rate >= 0.5:
                reasons.append(f"high recent commit frequency (~{rate:.1f}/day)")
                score += min(rate, 5)
            elif rate > 0:
                score += min(rate, 1)
        if r.id in newest:
            reasons.append("it is the newest project (recently started)")
            score += 1.5
        d = _parse_date(r.last_commit_date)
        if d and (today - d).days > STALE_DAYS:
            reasons.append(f"stalled ({ (today - d).days } days since last commit)")
            score -= 4
        if r.readme_quality in ("poor", "boilerplate", "none"):
            reasons.append("thin README — documentation is a known gap")
        if r.maturity and r.maturity in ("WIP", "Alpha", "Beta"):
            reasons.append(f"still maturing ({r.maturity})")
            score += 1
        # Importance: majority-of-commits project.
        total = sum(x.commit_count or 0 for x in repos)
        if r.commit_count and total and r.commit_count / total >= 0.4:
            reasons.append("carries the majority of workspace commits")
            score += 2
        if not reasons:
            reasons.append("no strong signal; default by recent activity")
        scored.append((r, reasons, score))

    scored.sort(key=lambda x: x[2], reverse=True)
    return [(r, reasons) for r, reasons, _ in scored[:n]]

