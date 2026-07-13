"""Workspace summary: aggregate stored knowledge into per-project and
cross-project understanding. Relationships and observations are computed
deterministically from stored rows + live, evidence-backed file signals.
"""

from __future__ import annotations

import datetime as dt
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .db import (
    LangRow,
    RelationshipRow,
    Repository,
    TechRow,
    connect,
    get_all_relationships,
    get_languages,
    get_repositories,
    get_technologies,
)

STALE_DAYS = 90

# Languages that are documentation/markup/config, not primary programming
# languages. Used to avoid noisy "shared language: Markdown" relationships.
NON_CODE_LANGS = {
    "Markdown",
    "reStructuredText",
    "HTML",
    "CSS",
    "SCSS",
    "JSON",
    "YAML",
    "XML",
    "TOML",
    "Shell",
}


@dataclass
class RepoView:
    repo: Repository
    languages: list[LangRow]
    technologies: list[TechRow]
    config_files: list[str] = field(default_factory=list)

    @property
    def tech_names(self) -> set[str]:
        return {t.tech for t in self.technologies}

    @property
    def lang_names(self) -> set[str]:
        return {l.language for l in self.languages}

    @property
    def code_lang_names(self) -> set[str]:
        return {l.language for l in self.languages if l.language not in NON_CODE_LANGS}


@dataclass
class Relationship:
    """In-memory relationship (names resolved) used for rendering."""

    kind: str  # shared-<kind>
    a: str
    b: str
    evidence: str
    priority: int = 0
    strength: str = "Medium"


@dataclass
class Observation:
    text: str


def _parse_date(iso: Optional[str]) -> Optional[dt.date]:
    if not iso:
        return None
    try:
        return dt.date.fromisoformat(iso[:10])
    except ValueError:
        return None


def _tracked_files(repo_path: Path) -> list[str]:
    try:
        res = subprocess.run(
            ["git", "-C", str(repo_path), "ls-files"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    if res.returncode != 0:
        return []
    return [l for l in res.stdout.splitlines() if l]


_CONFIG_PATTERNS = [
    re.compile(r"^(config|settings|conf)(\.[a-z]+)?$"),
    re.compile(r"\.env\.example$"),
    re.compile(r"^config/"),
    re.compile(r"appsettings\.[a-z]+$"),
    re.compile(r"[a-z_]config\.[a-z]+$"),
]


def _detect_config(repo_path: Path) -> list[str]:
    hits = []
    for f in _tracked_files(repo_path):
        low = f.lower()
        for pat in _CONFIG_PATTERNS:
            if pat.search(low):
                hits.append(f)
                break
    return hits


def _github_org(remote_url: Optional[str]) -> Optional[str]:
    if not remote_url:
        return None
    # https://github.com/org/repo  or  git@github.com:org/repo.git
    m = re.search(r"github\.com[:/]([^/]+)/", remote_url)
    if m:
        return m.group(1)
    return None


def build_views(conn) -> list[RepoView]:
    views = []
    for repo in get_repositories(conn):
        langs = get_languages(conn, repo.id) if repo.id is not None else []
        techs = get_technologies(conn, repo.id) if repo.id is not None else []
        views.append(
            RepoView(
                repo=repo,
                languages=langs,
                technologies=techs,
                config_files=_detect_config(Path(repo.path)),
            )
        )
    return views


# Relationship kinds, ranked by analytical value (higher = more insightful).
_PRIORITY = {
    "shared-implementation": 100,
    "shared-abstraction": 95,
    "shared-architecture": 90,
    "shared-framework": 85,
    "shared-deployment": 80,
    "shared-db": 75,
    "shared-config": 70,
    "potential-reuse": 65,
    "duplicated-functionality": 60,
    "shared-tech": 50,
    "shared-lang-ecosystem": 40,
    "shared-language": 35,
    "shared-org": 15,
    "shared-author": 10,
}

# Mapping from a detected tech to the "framework" family it implies.
_FRAMEWORK_TECH = {"Next.js", "React", "FastAPI", "Django", "Flask", "Supabase", "Vue", "Angular"}
_DB_TECH = {"SQLite", "Postgres", "Redis"}
_DEPLOY_TECH = {"Docker"}


def _add(rels: list[Relationship], a: str, b: str, kind: str, evidence: str) -> None:
    rels.append(
        Relationship(
            kind=kind, a=a, b=b, evidence=evidence,
            priority=_PRIORITY.get(kind, 0),
            strength=_relationship_strength(kind),
        )
    )


def _relationship_strength(kind: str) -> str:
    from . import judgment

    return judgment.relationship_strength(kind)


def infer_relationships(views: list[RepoView]) -> list[Relationship]:
    """Compute evidence-backed relationships between repositories.

    Every relationship carries a strength (Weak/Medium/Strong). Weak
    relationships (shared author/organization/language) are still recorded for
    completeness but must never be presented as architectural insight or drive a
    reuse recommendation. Engineering recommendations are generated only from
    Medium/Strong evidence (see `query.reuse_opportunities`).
    """
    rels: list[Relationship] = []
    for i in range(len(views)):
        for j in range(i + 1, len(views)):
            a, b = views[i], views[j]
            an, bn = a.repo.name, b.repo.name
            shared_tech = sorted(a.tech_names & b.tech_names)
            shared_lang = sorted(a.code_lang_names & b.code_lang_names)

            # High-value: shared framework / db / deployment.
            shared_fw = sorted(set(shared_tech) & _FRAMEWORK_TECH)
            for fw in shared_fw:
                _add(rels, an, bn, "shared-framework", f"Both use the {fw} framework")
            shared_db = sorted(set(shared_tech) & _DB_TECH)
            for db in shared_db:
                _add(rels, an, bn, "shared-db", f"Both use {db}")
            if "Docker" in shared_tech:
                _add(rels, an, bn, "shared-deployment", "Both containerize with Docker")

            # Shared architecture: same primary framework family.
            if shared_fw:
                _add(
                    rels, an, bn, "shared-architecture",
                    f"Both are built on {'/'.join(shared_fw)}",
                )

            # Shared configuration loading (Medium, not a reuse recommendation).
            shared_cfg = sorted(set(a.config_files) & set(b.config_files))
            if shared_cfg:
                _add(
                    rels, an, bn, "shared-config",
                    "Both implement configuration loading ("
                    + ", ".join(shared_cfg[:3]) + ")",
                )

            # Shared language ecosystem (programming language overlap) — Weak.
            if shared_lang:
                _add(
                    rels, an, bn, "shared-lang-ecosystem",
                    "Both in the " + "/".join(shared_lang) + " ecosystem",
                )

            # Potential code reuse: substantial shared tech stack (Medium).
            # NOTE: this is evidence of *stack overlap*, not of identical code.
            if len(shared_tech) >= 3:
                _add(
                    rels, an, bn, "potential-reuse",
                    f"Overlapping stack: {', '.join(shared_tech)}",
                )

            # Duplicated functionality: same stated purpose / features.
            if _same_purpose(a, b):
                _add(
                    rels, an, bn, "duplicated-functionality",
                    "Similar stated purpose in their READMEs",
                )

            # Generic shared tech (lower value, when not already a framework/db).
            for tech in shared_tech:
                if tech not in _FRAMEWORK_TECH and tech not in _DB_TECH and tech != "Docker":
                    _add(rels, an, bn, "shared-tech", f"Both use {tech}")

            # Generic shared language (Weak catch-all).
            if shared_lang and not shared_fw:
                _add(rels, an, bn, "shared-language", "Both use " + ", ".join(shared_lang))

            # Weak: shared org / author. Recorded but flagged Weak; never surfaced
            # as architectural insight.
            org_a, org_b = _github_org(a.repo.remote_url), _github_org(b.repo.remote_url)
            if org_a and org_a == org_b:
                _add(rels, an, bn, "shared-org", f"Both under GitHub org '{org_a}'")
            if a.repo.primary_author and a.repo.primary_author == b.repo.primary_author:
                _add(
                    rels, an, bn, "shared-author",
                    f"Both primarily authored by {a.repo.primary_author}",
                )

    rels.sort(key=lambda r: r.priority, reverse=True)
    return rels


def _same_purpose(a: RepoView, b: RepoView) -> bool:
    pa = (a.repo.readme_summary or "")
    pb = (b.repo.readme_summary or "")
    # Compare the Purpose line from each stored summary.
    def purpose(s: str) -> str:
        for line in s.splitlines():
            if line.strip().lower().startswith("purpose:"):
                return line.split(":", 1)[1].strip().lower()
        return ""
    pa, pb = purpose(pa), purpose(pb)
    if not pa or not pb:
        return False
    # Same if identical, or one contains the other.
    return pa == pb or pa in pb or pb in pa


def infer_relationship_rows(views: list[RepoView]) -> list[RelationshipRow]:
    """Same as infer_relationships but as persisted RelationshipRow objects."""
    rels = infer_relationships(views)
    id_by_name = {v.repo.name: v.repo.id for v in views}
    rows: list[RelationshipRow] = []
    for r in rels:
        ra, rb = id_by_name.get(r.a), id_by_name.get(r.b)
        if ra is None or rb is None:
            continue
        rows.append(
            RelationshipRow(
                repo_a=ra, repo_b=rb, kind=r.kind, evidence=r.evidence,
                priority=r.priority, strength=r.strength,
            )
        )
    return rows


def cross_project_observations(
    views: list[RepoView], rels: list[Relationship], today: dt.date
) -> list[Observation]:
    obs: list[Observation] = []

    # Duplicate technologies across repos (languages excluded — a shared
    # language is not a "configuration to duplicate"; that wording was a bug).
    tech_to_repos: dict[str, list[str]] = {}
    for v in views:
        for t in v.tech_names:
            if t in NON_CODE_LANGS:
                continue
            tech_to_repos.setdefault(t, []).append(v.repo.name)
    for tech, repos in sorted(tech_to_repos.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        if len(repos) >= 2:
            obs.append(
                Observation(
                    text=f"{len(repos)} repositories use {tech}: "
                    + ", ".join(repos)
                )
            )

    # Shared relationship bullets — only Medium/Strong kinds are worth surfacing
    # as cross-project observations. Weak kinds (shared-language, shared-org) are
    # coincidences and are intentionally omitted (audit: a staff eng would not
    # trust "they share a GitHub org" as insight).
    seen_pairs: set[tuple[str, str, str]] = set()
    _STRONG_PHRASE = {
        "shared-framework": "a framework",
        "shared-db": "a database engine",
        "shared-deployment": "a deployment stack",
        "shared-architecture": "an architecture",
        "shared-config": "configuration loading",
        "shared-tech": "a technology",
    }
    for r in rels:
        if r.strength == "Weak":
            continue
        if r.kind in _STRONG_PHRASE:
            key = (r.a, r.b, r.kind)
            if key in seen_pairs:
                continue
            seen_pairs.add(key)
            obs.append(
                Observation(text=f"{r.a} and {r.b} share {_STRONG_PHRASE[r.kind]} ({r.evidence}).")
            )

    # Stale repos (no commit in STALE_DAYS).
    for v in views:
        d = _parse_date(v.repo.last_commit_date)
        if d and (today - d).days > STALE_DAYS:
            obs.append(
                Observation(
                    text=f"{v.repo.name} has not been modified recently "
                    f"(last commit {v.repo.last_commit_date[:10]})."
                )
            )

    # Largest by total file count.
    sized = [(sum(l.file_count for l in v.languages), v.repo.name) for v in views]
    if sized:
        top = max(sized)
        if top[0] > 0:
            obs.append(
                Observation(
                    text=f"{top[1]} is the largest project ({top[0]} tracked source files)."
                )
            )

    # Most active by commits-per-day, measured over the repo's real lifetime
    # (first commit -> last commit). Repos with no commits or no span are skipped.
    active = []
    for v in views:
        first = _parse_date(v.repo.first_commit_date)
        last = _parse_date(v.repo.last_commit_date)
        if first and last and v.repo.commit_count:
            span_days = max((last - first).days, 1)
            active.append(
                (v.repo.commit_count / span_days, v.repo.name, v.repo.commit_count)
            )
    if active:
        top = max(active)
        obs.append(
            Observation(
                text=f"{top[1]} has the highest commit frequency "
                f"(~{top[0]:.1f} commits/day, {top[2]} total)."
            )
        )

    return obs


def render(views: list[RepoView], rels: list[Relationship], obs: list[Observation]) -> str:
    lines: list[str] = []
    lines.append(f"Projects discovered: {len(views)}")
    lines.append("")

    rel_lookup: dict[str, list[Relationship]] = {}
    for r in rels:
        rel_lookup.setdefault(r.a, []).append(r)
        rel_lookup.setdefault(r.b, []).append(r)

    for v in views:
        lines.append(v.repo.name)
        lines.append("-" * len(v.repo.name))
        if v.lang_names:
            lines.append("Language:")
            for lang in sorted(v.lang_names):
                if lang in NON_CODE_LANGS and len(v.lang_names) > 1:
                    continue  # hide pure markup when real languages exist
                lines.append(f"- {lang}")
            lines.append("")
        lines.append("Purpose:")
        lines.append(_indent(_purpose_line(v.repo.readme_summary), 0))
        lines.append("")
        if v.tech_names:
            lines.append("Important technologies:")
            for t in sorted(v.tech_names):
                lines.append(f"- {t}")
            lines.append("")
        # Current state.
        state = _current_state(v)
        lines.append("Current state:")
        lines.append(state)
        lines.append("")
        # Relationships involving this repo.
        my_rels = rel_lookup.get(v.repo.name, [])
        if my_rels:
            lines.append("Relationships:")
            for r in my_rels:
                other = r.b if r.a == v.repo.name else r.a
                label = r.kind.replace("shared-", "shared ")
                lines.append(f"- {label} with {other}.")
            lines.append("")
        # Open observations (per-repo): dirty / license / branch.
        open_obs = _open_observations(v)
        if open_obs:
            lines.append("Open observations:")
            for o in open_obs:
                lines.append(f"- {o}")
            lines.append("")
        lines.append("---------------------")
        lines.append("")

    if obs:
        lines.append("Cross-project observations")
        lines.append("")
        for o in obs:
            lines.append(f"• {o.text}")
        lines.append("")

    return "\n".join(lines)


def _indent(text: str, n: int) -> str:
    pad = " " * n
    return "\n".join(pad + line if line else line for line in text.splitlines())


def _purpose_line(summary: Optional[str]) -> str:
    """Extract just the Purpose prose from a stored README summary.

    Handles both the deterministic block (where Purpose: is followed by a
    prose line) and the LLM summary (where Purpose: is on the same line).
    """
    if not summary:
        return "No README summary available."
    lines = [l.strip() for l in summary.splitlines()]
    for i, line in enumerate(lines):
        if line.lower().startswith("purpose:"):
            rest = line.split(":", 1)[1].strip()
            if rest:
                return rest
            # look at the next non-empty line
            for nxt in lines[i + 1 :]:
                if nxt and not nxt.startswith(("Maturity:", "Important", "Roadmap:")):
                    return nxt
    # Fallback: first prose line.
    for line in lines:
        s = line.strip()
        if s and not s.startswith(("Title:", "Purpose:", "Maturity:", "Important",
                                    "Roadmap:", "-")):
            return s
    return "No README summary available."


def _current_state(v: RepoView) -> str:
    if v.repo.is_dirty:
        return "Active (uncommitted changes)"
    d = _parse_date(v.repo.last_commit_date)
    if d is None:
        return "Unknown"
    age = (dt.date.today() - d).days
    if age <= 7:
        return "Very active"
    if age <= STALE_DAYS:
        return "Active"
    return "Dormant"


def _open_observations(v: RepoView) -> list[str]:
    out: list[str] = []
    if v.repo.is_dirty:
        out.append("Has uncommitted changes.")
    if v.repo.license:
        out.append(f"Licensed under {v.repo.license}.")
    if v.repo.default_branch and v.repo.default_branch not in ("main", "master"):
        out.append(f"Default branch is '{v.repo.default_branch}'.")
    if v.repo.commit_count is not None:
        out.append(f"{v.repo.commit_count} commit{'s' if v.repo.commit_count != 1 else ''}.")
    return out


def _stored_relationships(conn, views: list[RepoView]) -> Optional[list[Relationship]]:
    """Read relationships from the persisted table, resolved to in-memory form.

    Returns None when the table has no rows for these repos (e.g. a DB ingested
    under M1), so callers fall back to live inference.
    """
    rows = get_all_relationships(conn)
    if not rows:
        return None
    name_by_id = {v.repo.id: v.repo.name for v in views}
    rels: list[Relationship] = []
    for r in rows:
        an = name_by_id.get(r.repo_a)
        bn = name_by_id.get(r.repo_b)
        if an is None or bn is None:
            continue
        rels.append(
            Relationship(kind=r.kind, a=an, b=bn, evidence=r.evidence, priority=r.priority)
        )
    return rels or None


def generate_summary(conn) -> str:
    today = dt.date.today()
    views = build_views(conn)
    rels = _stored_relationships(conn, views) or infer_relationships(views)
    obs = cross_project_observations(views, rels, today)
    text = render(views, rels, obs)
    # Append deterministic insights (M2) below the existing observations.
    try:
        from .insights import generate_insights

        extra = generate_insights(conn, today)
        if extra:
            block = "\n".join(f"• {i.text}" for i in extra)
            text = text.rstrip() + "\n\nWorkspace insights\n\n" + block + "\n"
    except Exception:
        pass
    return text
