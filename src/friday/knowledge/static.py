"""Static knowledge detection (Milestone 8.1.5).

Static knowledge is available IMMEDIATELY after ingest — it does not require
any observation history. It captures what a repository *is*: its identity,
architecture, technology stack, the portfolio's technology surface, and
integration candidates. This is the Class-1 knowledge the spec calls for.

Contrast with trends.py / patterns.py / relationships.py (temporal knowledge),
which need repeated observations / sessions over time.

Every item here is backed by the persisted ingest evidence (repository rows,
languages, technologies, architecture, relationships). No LLM, no inference.
"""

from __future__ import annotations

from typing import List, Optional

from ..db import (
    get_all_relationships,
    get_architecture,
    get_languages,
    get_technologies,
    get_repositories,
)
from .models import (
    Knowledge,
    KnowledgeConfidence,
    KnowledgeStatus,
    KnowledgeType,
)


def detect_static_knowledge(conn) -> List[Knowledge]:
    """Build the static knowledge available right after ingest.

    Returns one Knowledge entry per project-identity / architecture / stack, plus
    portfolio-level technology and integration facts. Empty when nothing has been
    ingested — never raises.
    """
    knowledge: List[Knowledge] = []

    repos = get_repositories(conn)
    if not repos:
        return knowledge

    name_by_id = {r.id: r.name for r in repos if r.id is not None}

    # Per-project identity / architecture / stack.
    for r in repos:
        if r.id is None:
            continue
        knowledge.extend(_project_identity(conn, r.id, r.name))
        knowledge.extend(_project_architecture(conn, r.id, r.name))
        knowledge.extend(_project_stack(conn, r.id, r.name))

    # Portfolio-level technology surface.
    knowledge.extend(_portfolio_technology(conn))

    # Integration candidates (reasoned from identity / shared tech).
    knowledge.extend(_portfolio_integration(conn, name_by_id))

    return knowledge


def _recover_purpose_from_summary(summary: Optional[str]) -> Optional[str]:
    """Extract a project purpose line from a stored README summary.

    Law 19: the Knowledge layer must not depend on the Brain `identity` module.
    Purpose is deterministic evidence already persisted at ingest time in
    `repositories.readme_summary`. The canonical format is the deterministic
    summary's "Purpose:\\n<value>\\n\\n..." block, but some callers store a raw
    "purpose: <value>" line; both are accepted (case-insensitive marker).
    """
    if not summary:
        return None
    value: Optional[str] = None
    # Canonical summary: a line beginning with "Purpose:" (value on the next line).
    for line in summary.splitlines():
        stripped = line.strip()
        low = stripped.lower()
        if low.startswith("purpose:"):
            rest = stripped[len("purpose:"):].strip()
            if rest:
                value = rest
            else:
                # Value on the following line; scan ahead.
                idx = summary.lower().find("purpose:")
                after = summary[idx + len("purpose:"):]
                first_blank = after.find("\n")
                if first_blank != -1:
                    nxt = after[first_blank + 1:].split("\n", 1)[0].strip()
                    if nxt and nxt.lower() not in ("none stated", "unknown"):
                        value = nxt
            break
    if value is None:
        return None
    # A blank line ends the Purpose paragraph.
    value = value.split("\n\n", 1)[0].strip().lstrip(":")
    if not value or len(value.split()) < 3:
        return None
    if value.lower() in ("none stated", "unknown"):
        return None
    return value


def _project_identity(conn, repo_id: int, name: str) -> List[Knowledge]:
    repos = get_repositories(conn)
    repo = next((r for r in repos if r.id == repo_id), None)
    summary = repo.readme_summary if repo else None
    out: List[Knowledge] = []
    purpose = _recover_purpose_from_summary(summary)
    if purpose:
        out.append(
            Knowledge(
                type=KnowledgeType.PROJECT_IDENTITY,
                subject=name,
                statement=f"{name} is a project for: {purpose}.",
                confidence=KnowledgeConfidence.MEDIUM,
                evidence_ids=[f"repo:{repo_id}"],
                status=KnowledgeStatus.OBSERVED,
                is_static=True,
            )
        )
    return out


def _project_architecture(conn, repo_id: int, name: str) -> List[Knowledge]:
    arch = get_architecture(conn, repo_id)
    out: List[Knowledge] = []
    if arch and arch.architecture and arch.architecture != "Unknown":
        out.append(
            Knowledge(
                type=KnowledgeType.PROJECT_ARCHITECTURE,
                subject=name,
                statement=f"{name} is built as a {arch.architecture}.",
                confidence=KnowledgeConfidence.MEDIUM,
                evidence_ids=[f"repo:{repo_id}"],
                status=KnowledgeStatus.OBSERVED,
                is_static=True,
            )
        )
    return out


def _project_stack(conn, repo_id: int, name: str) -> List[Knowledge]:
    out: List[Knowledge] = []
    langs = [l.language for l in get_languages(conn, repo_id)]
    techs = [t.tech for t in get_technologies(conn, repo_id)]
    parts: List[str] = []
    if langs:
        parts.append("languages: " + ", ".join(sorted(langs)))
    if techs:
        parts.append("technologies: " + ", ".join(sorted(techs)))
    if parts:
        out.append(
            Knowledge(
                type=KnowledgeType.PROJECT_STACK,
                subject=name,
                statement=f"{name} uses {'; '.join(parts)}.",
                confidence=KnowledgeConfidence.MEDIUM,
                evidence_ids=[f"repo:{repo_id}"],
                status=KnowledgeStatus.OBSERVED,
                is_static=True,
            )
        )
    return out


def _portfolio_technology(conn) -> List[Knowledge]:
    """Portfolio-wide technology surface, grouped by tech."""
    from collections import defaultdict

    by_tech: dict[str, List[str]] = defaultdict(list)
    for r in get_repositories(conn):
        if r.id is None:
            continue
        for t in get_technologies(conn, r.id):
            by_tech[t.tech].append(r.name)
    out: List[Knowledge] = []
    for tech, names in sorted(by_tech.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        if len(names) >= 2:
            out.append(
                Knowledge(
                    type=KnowledgeType.PORTFOLIO_TECHNOLOGY,
                    subject=tech,
                    statement=(
                        f"{tech} is used across {len(names)} projects "
                        f"({', '.join(sorted(set(names)))})."
                    ),
                    confidence=(
                        KnowledgeConfidence.STRONG
                        if len(set(names)) >= 3
                        else KnowledgeConfidence.MEDIUM
                    ),
                    evidence_ids=[f"repo:{n}" for n in sorted(set(names))],
                    status=KnowledgeStatus.OBSERVED,
                    is_static=True,
                )
            )
    return out


def _portfolio_integration(conn, name_by_id: dict) -> List[Knowledge]:
    """Integration candidates, reasoned from identity (no hardcoded names)."""
    friday_repo = next(
        (r for r in get_repositories(conn) if r.id is not None and "friday" in r.name.lower()),
        None,
    )
    if not friday_repo:
        return []
    friday_techs = {t.tech.lower() for t in get_technologies(conn, friday_repo.id)}
    rels = get_all_relationships(conn)
    friday_related = {
        name_by_id.get(r.repo_a) for r in rels if r.repo_b == friday_repo.id
    } | {name_by_id.get(r.repo_b) for r in rels if r.repo_a == friday_repo.id}
    friday_related.discard(None)

    _FIT = (
        "ai", "assistant", "knowledge", "developer", "workflow",
        "operating system", "health", "mental", "product", "app",
    )
    out: List[Knowledge] = []
    for r in get_repositories(conn):
        if r.id is None or r.id == friday_repo.id:
            continue
        repo_summary = r.readme_summary
        purpose = (_recover_purpose_from_summary(repo_summary) or "").lower()
        techs = {t.tech.lower() for t in get_technologies(conn, r.id)}
        reasons: List[str] = []
        if any(m in purpose for m in _FIT):
            reasons.append("purpose indicates a natural fit with an operating partner")
        if techs & friday_techs:
            reasons.append("shares technology with Friday")
        if r.name in friday_related:
            reasons.append("stored relationship with Friday")
        if reasons:
            out.append(
                Knowledge(
                    type=KnowledgeType.PORTFOLIO_INTEGRATION,
                    subject=r.name,
                    statement=(
                        f"{r.name} is a candidate to integrate with Friday "
                        f"({'; '.join(reasons)})."
                    ),
                    confidence=KnowledgeConfidence.MEDIUM,
                    evidence_ids=[f"repo:{r.id}"],
                    status=KnowledgeStatus.OBSERVED,
                    is_static=True,
                )
            )
    return out
