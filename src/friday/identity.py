"""Project identity — the human-facing interpretation of Friday's evidence.

Identity is DERIVED ON READ from facts already persisted by M1–M3 (README
summary, architecture, components, entry points, relationships, git metadata).
There is deliberately no `identities` table: every input survives re-analysis,
and identity is recomputed whenever asked so it never goes stale.

Nothing here invents. Every field is Optional; when evidence is missing the
field is None and the renderer states that plainly ("not enough evidence").

This module is the heart of Milestone 3.5: it turns static analysis into the
kind of explanation a senior engineer gives when onboarding a colleague.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import query as q
from .db import (
    ArchitectureRow,
    ComponentRow,
    EntryPointRow,
    Repository,
    get_all_relationships,
    get_architecture,
    get_components,
    get_entry_points,
    get_languages,
    get_technologies,
)
from .summary import _purpose_line


# Entry-point kinds, by role. Architecture roots (app/, pages/, src/) describe
# where code lives, not how it is launched; they are "Framework root", never a
# primary application entry (audit §7).
_APP_EP = {
    "main()", "CLI", "FastAPI app", "Flask app", "Next.js app", "Cargo binary",
    "Executable script",
}
_FRAMEWORK_ROOT_FROM_DETAIL = {"app/", "pages/", "src/"}


@dataclass
class ProjectIdentity:
    repo: Repository
    purpose: Optional[str] = None
    problem: Optional[str] = None
    maturity: str = "Unknown"
    activity: str = "Unknown"
    phase: Optional[str] = None
    importance: Optional[str] = None
    blockers: list[str] = field(default_factory=list)
    business_value: Optional[str] = None
    technologies: list[str] = field(default_factory=list)
    related_projects: list[str] = field(default_factory=list)
    readme_quality: Optional[str] = None
    evidence_sources: list[str] = field(default_factory=list)

    @property
    def has_identity(self) -> bool:
        return bool(self.purpose or self.problem or self.technologies or self.related_projects)


@dataclass
class EntryPointGroups:
    application: list[EntryPointRow] = field(default_factory=list)
    framework_root: list[str] = field(default_factory=list)  # detail strings
    utility: list[EntryPointRow] = field(default_factory=list)


def _end(s: str) -> str:
    """End a sentence with exactly one period (avoids 'tool..')."""
    return s.rstrip().rstrip(".") + "."


def entry_point_groups(entry_points: list[EntryPointRow]) -> EntryPointGroups:
    """Split entry points into Application / Framework root / Utility (audit §7).

    Utility scripts never become the primary application entry.
    """
    g = EntryPointGroups()
    for e in entry_points:
        if e.kind == "Utility script":
            g.utility.append(e)
        elif e.kind in _APP_EP:
            g.application.append(e)
        # Details that look like framework roots are grouped separately.
        if any(e.detail == r or e.detail.startswith(r) for r in _FRAMEWORK_ROOT_FROM_DETAIL):
            if e.detail not in g.framework_root:
                g.framework_root.append(e.detail)
    # Framework roots can also come from app/pages/src directories alone.
    for e in entry_points:
        for r in _FRAMEWORK_ROOT_FROM_DETAIL:
            if e.detail == r or e.detail.startswith(r):
                if e.detail not in g.framework_root:
                    g.framework_root.append(e.detail)
    return g


def recover_purpose(
    repo: Repository, conn, arch: Optional[ArchitectureRow] = None
) -> tuple[Optional[str], list[str]]:
    """Recover project purpose from deterministic evidence (audit §6).

    Order of authority:
      1. README summary Purpose line (richest, human-written).
      2. Manifest description (package.json / pyproject.toml).
      3. Framework/architecture hint (e.g. "Next.js App Router" => web app).
    Returns (purpose, evidence_sources). Never invents; returns (None, [...])
    when nothing supports a purpose.
    """
    sources: list[str] = []

    # 1. README. The deterministic summary stores "Purpose:\n<value>\n\n...";
    # `_purpose_line` extracts just the value, not the surrounding block.
    summary = repo.readme_summary
    if summary:
        line = _purpose_line(summary)
        if line and line != "No README summary available." and len(line.split()) >= 3:
            sources.append("README")
            return line, sources

    # 2. Manifest description.
    from .readme import manifest_description

    desc = manifest_description(repo.path)
    if desc:
        sources.append("manifest description")
        return desc, sources

    # 3. Framework/architecture hint — only a weak, general statement.
    if arch and arch.architecture and arch.architecture != "Unknown":
        label = arch.architecture
        sources.append("architecture")
        # Phrase as what kind of thing it is, not a fabricated mission.
        if "FastAPI" in label or "Flask" in label or "Django" in label or "web app" in label:
            return "A web application.", sources
        if "Next.js" in label or "React" in label:
            return "A web frontend application.", sources
        if "CLI" in label:
            return "A command-line tool.", sources
        if "Library" in label:
            return "A software library / package.", sources
        if "Cargo" in label:
            return "A Rust application or workspace.", sources

    return None, sources


def build_identity(conn, repo_id: int, today: Optional[dt.date] = None) -> Optional[ProjectIdentity]:
    """Assemble a ProjectIdentity for `repo_id` from persistent evidence.

    Returns None if the repo is unknown; otherwise always returns an identity
    (possibly sparse, with None fields) so the caller can render honestly.
    """
    today = today or dt.date.today()
    repos = q.all_repositories(conn)
    repo = next((r for r in repos if r.id == repo_id), None)
    if repo is None:
        return None

    arch = get_architecture(conn, repo_id)
    comps = get_components(conn, repo_id)
    eps = get_entry_points(conn, repo_id)
    langs = get_languages(conn, repo_id)
    techs = get_technologies(conn, repo_id)

    # Purpose: README -> manifest -> architecture hint.
    purpose, purpose_src = recover_purpose(repo, conn, arch)

    # Maturity from README quality pass (stored); fall back to architecture conf.
    maturity = repo.maturity or "Unknown"

    # Activity + phase.
    activity = q.identity_card(conn, repo_id, today).activity if repo.id is not None else "Unknown"
    phase: Optional[str] = None
    if repo.is_dirty and activity in ("Active", "Very active"):
        phase = "active development"
    elif activity == "Dormant":
        phase = "stalled / dormant"

    # Importance: evidence-backed only.
    importance: Optional[str] = None
    active = q.most_active(conn, today, 3)
    if active and active[0][0].id == repo_id:
        top, _ = active[0]
        total = sum(r.commit_count or 0 for r in repos)
        if top.commit_count and total and top.commit_count / total >= 0.4:
            importance = "the most actively developed project in the workspace"
    if importance is None and q.newest_repos(conn, 3) and q.newest_repos(conn, 3)[0].id == repo_id:
        importance = "the newest project in the workspace"

    # Blockers: only concrete, evidence-backed friction.
    blockers: list[str] = []
    if repo.is_dirty:
        blockers.append("has uncommitted changes")
    d = q._parse_date(repo.last_commit_date)
    if d and (today - d).days > q.STALE_DAYS:
        blockers.append(f"no commit in {(today - d).days} days")
    if repo.readme_quality in ("poor", "boilerplate", "none"):
        blockers.append("thin or missing README (onboarding friction)")

    # Business value: only when the README explicitly states it. We do NOT infer.
    business_value: Optional[str] = None
    if repo.readme_summary:
        for line in repo.readme_summary.splitlines():
            if line.strip().lower().startswith("value:") or line.strip().lower().startswith("business value:"):
                v = line.split(":", 1)[1].strip()
                if v:
                    business_value = v

    # Related projects: Medium/Strong relationships only (audit §8).
    related: list[str] = []
    name_by_id = {r.id: r.name for r in repos}
    for rel in get_all_relationships(conn):
        if rel.repo_a != repo_id and rel.repo_b != repo_id:
            continue
        if rel.strength == "Weak":
            continue
        other = rel.repo_b if rel.repo_a == repo_id else rel.repo_a
        on = name_by_id.get(other)
        if on and on not in related:
            related.append(on)

    tech_names = sorted({t.tech for t in techs})
    return ProjectIdentity(
        repo=repo,
        purpose=purpose,
        problem=None,  # problem is not reliably separable from purpose; stay honest
        maturity=maturity,
        activity=activity,
        phase=phase,
        importance=importance,
        blockers=blockers,
        business_value=business_value,
        technologies=tech_names,
        related_projects=related,
        readme_quality=repo.readme_quality,
        evidence_sources=purpose_src,
    )


def explain_project_from_conn(conn, repo_id: int, detailed: bool = True) -> str:
    """Like explain_project but with DB access for the architecture detail."""
    identity = build_identity(conn, repo_id)
    if identity is None:
        return "I don't have enough evidence: that project is not in the knowledge base."
    r = identity.repo
    arch = get_architecture(conn, repo_id)
    comps = get_components(conn, repo_id)
    eps = get_entry_points(conn, repo_id)
    groups = entry_point_groups(eps)

    if not identity.has_identity and not identity.purpose:
        return (
            f"I don't have enough evidence to explain {r.name}. "
            f"No README, manifest description, or recognizable architecture was found. "
            f"Run `friday analyze <path>` or add a README to recover its identity."
        )

    lines: list[str] = []
    intro = f"{r.name}"
    if identity.purpose:
        intro += f" — {identity.purpose}"
    elif arch and arch.architecture != "Unknown":
        intro += f" — a {arch.architecture} project"
    lines.append(_end(intro))

    if identity.phase:
        lines.append(f"It is currently in {identity.phase}.")

    extra: list[str] = []
    if identity.business_value:
        extra.append(f"Business value: {identity.business_value}")
    status = [f"status: {identity.activity.lower()}"]
    if identity.maturity and identity.maturity != "Unknown":
        status.append(f"maturity {identity.maturity}")
    if identity.importance:
        status.append(identity.importance)
    if identity.blockers:
        status.append("blockers: " + "; ".join(identity.blockers))
    extra.append("; ".join(status) + ".")
    if identity.technologies:
        extra.append("Major technologies: " + ", ".join(identity.technologies) + ".")
    lines.append(" ".join(extra))

    # Architecture detail.
    arch_lines: list[str] = []
    if arch and arch.architecture != "Unknown":
        arch_lines.append(f"Architecturally it is a {arch.architecture} project")
        if arch.confidence and arch.confidence != "Unknown":
            arch_lines[-1] += f" (confidence: {arch.confidence})"
        arch_lines[-1] += "."
    comp_names = [c.name for c in comps]
    if comp_names:
        arch_lines.append("Major components: " + ", ".join(comp_names) + ".")
    # Entry points grouped (audit §7).
    if groups.application:
        apps = ", ".join(f"{e.kind} ({e.detail})" for e in groups.application)
        arch_lines.append(f"Application entry points: {apps}.")
    if groups.framework_root:
        arch_lines.append("Framework root: " + ", ".join(groups.framework_root) + ".")
    if groups.utility:
        utils = ", ".join(e.detail for e in groups.utility)
        arch_lines.append(f"Utility scripts (not application entry points): {utils}.")
    if arch_lines:
        lines.append(" ".join(arch_lines))

    # Observations.
    obs: list[str] = []
    if identity.related_projects:
        obs.append("Related projects (shared architecture/framework/implementation): "
                   + ", ".join(identity.related_projects) + ".")
    if identity.readme_quality and identity.readme_quality in ("poor", "boilerplate", "none"):
        obs.append(f"Its README is {identity.readme_quality}; documentation is a gap.")
    if obs:
        lines.append(" ".join(obs))

    return " ".join(lines).strip()
