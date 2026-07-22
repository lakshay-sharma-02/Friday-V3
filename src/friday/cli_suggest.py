"""CLI command for `friday suggest` (Phase 6, Task 4).

Read-only command that surfaces cross-project integration opportunities by
cross-referencing the existing portfolio evidence — themes, relationships,
technology stacks, architecture, and components.

Every suggestion is backed by a traceable evidence record. Like every other
Friday command, nothing here invents — when evidence is thin we say so plainly.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass

from .db import (
    connect,
    get_all_relationships,
    get_architecture,
    get_components,
    get_repositories,
    get_technologies,
)
from .portfolio import (
    detect_themes,
    meaningful_overlap,
    integration_opportunities,
)
from .judgment import is_weak, component_strength


# ---------------------------------------------------------------------------
# Suggestion types and severity
# ---------------------------------------------------------------------------

_SEVERITY = ("high", "medium", "low")


@dataclass
class Suggestion:
    """One actionable cross-project suggestion, with evidence trace."""

    title: str
    detail: str
    severity: str  # high / medium / low
    evidence: list[str]  # evidence records that support this


@dataclass
class SuggestResult:
    """Collection of suggestions with metadata."""

    suggestions: list[Suggestion]
    total_projects: int

    def to_text(self) -> str:
        lines = [
            "Cross-project integration suggestions",
            f"Based on {self.total_projects} ingested projects",
            "",
        ]
        if not self.suggestions:
            lines.append("No specific integration opportunities detected yet.")
            lines.append("")
            lines.append(
                "Run `friday ingest <paths>` to add more projects, then "
                "`friday observe` to refresh the knowledge stack."
            )
            return "\n".join(lines) + "\n"

        severity_order = {"high": 0, "medium": 1, "low": 2}
        sorted_sugs = sorted(
            self.suggestions,
            key=lambda s: (severity_order.get(s.severity, 2), s.title),
        )

        for i, sug in enumerate(sorted_sugs, start=1):
            mark = {"high": "!!", "medium": "! ", "low": "  "}.get(
                sug.severity, "  "
            )
            conf_label = {"high": "HIGH", "medium": "MEDIUM", "low": "LOW"}.get(
                sug.severity, "LOW"
            )
            lines.append(f"  {i}. [{conf_label}] {sug.title}")
            lines.append(f"     {sug.detail}")
            for ev in sug.evidence:
                lines.append(f"     evidence: {ev}")
            lines.append("")

        lines.append("---")
        lines.append(f"Total: {len(self.suggestions)} suggestion(s)")
        return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Suggestion detectors (each cross-references existing portfolio data)
# ---------------------------------------------------------------------------


def _shared_tech_suggestions(conn) -> list[Suggestion]:
    """Suggestions based on shared technology across projects.

    When two or more projects use the same non-language tech, and at least one
    of them has that tech as a primary concern, suggest sharing/consolidating
    the implementation.
    """
    from .query import all_repositories

    repos = all_repositories(conn)
    name_by_id = {r.id: r.name for r in repos if r.id is not None}
    tech_by_repo: dict[int, set[str]] = {}

    for r in repos:
        if r.id is None:
            continue
        techs = {t.tech for t in get_technologies(conn, r.id)}
        tech_by_repo[r.id] = techs

    suggestions: list[Suggestion] = []
    seen_pairs: set[tuple[str, str]] = set()

    # Check every pair of projects for shared non-language tech
    ids = list(tech_by_repo.keys())
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            a_id, b_id = ids[i], ids[j]
            a_tech = tech_by_repo[a_id]
            b_tech = tech_by_repo[b_id]
            shared = a_tech & b_tech

            if not shared:
                continue

            a_name = name_by_id.get(a_id, "?")
            b_name = name_by_id.get(b_id, "?")
            pair = tuple(sorted((a_name, b_name)))
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)

            # Filter to non-obvious tech (exclude pure languages)
            interesting = {
                t for t in shared
                if t.lower() not in {
                    "python", "typescript", "javascript", "rust", "go",
                    "java", "c++", "c", "ruby", "swift", "kotlin",
                }
            }
            if not interesting:
                continue

            tech_str = ", ".join(sorted(interesting))
            severity = "high" if len(interesting) >= 2 else "medium"

            suggestions.append(Suggestion(
                title=f"{a_name} and {b_name} share {tech_str}",
                detail=(
                    f"Both {a_name} and {b_name} use {tech_str}. "
                    "Consolidate shared configuration, library versions, "
                    "or CI setup into a shared module."
                ),
                severity=severity,
                evidence=[
                    f"{a_name} technologies: {', '.join(sorted(a_tech & interesting)[:5])}",
                    f"{b_name} technologies: {', '.join(sorted(b_tech & interesting)[:5])}",
                ],
            ))

    return suggestions


def _duplicated_effort_suggestions(conn) -> list[Suggestion]:
    """Suggestions from meaningful_overlap and stored relationships.

    Uses the already-computed overlap data and relationship records to
    identify duplicated functionality that could be consolidated.
    """
    suggestions: list[Suggestion] = []

    # Existing overlaps from portfolio
    overlaps = meaningful_overlap(conn)
    for ov in overlaps:
        dims = "; ".join(ov.dimensions[:3])
        severity = "high" if ov.confidence == "Strong" and len(ov.dimensions) >= 3 else "medium"
        suggestions.append(Suggestion(
            title=f"{ov.a} and {ov.b} overlap in {len(ov.dimensions)} area(s)",
            detail=(
                f"These projects share meaningful overlap: {dims}. "
                "Consolidate shared concerns into a common library."
            ),
            severity=severity,
            evidence=[f"overlap: {d}" for d in ov.dimensions[:3]],
        ))

    # Duplicated-functionality relationships
    rels = get_all_relationships(conn)
    from .db import get_repositories
    name_by_id = {r.id: r.name for r in get_repositories(conn) if r.id is not None}

    for rel in rels:
        if rel.kind != "duplicated-functionality":
            continue
        a_name = name_by_id.get(rel.repo_a, "?")
        b_name = name_by_id.get(rel.repo_b, "?")
        suggestions.append(Suggestion(
            title=f"{a_name} and {b_name}: duplicated functionality",
            detail=f"Both projects implement similar functionality: {rel.evidence}.",
            severity="high",
            evidence=[f"relationship: {rel.kind} — {rel.evidence}"],
        ))

    return suggestions


def _architecture_suggestions(conn) -> list[Suggestion]:
    """Suggestions based on shared architecture patterns.

    When multiple projects share the same architecture label, suggest
    cross-project knowledge sharing and tooling reuse.
    """
    from .query import all_repositories

    repos = all_repositories(conn)
    arch_by_repo: dict[str, list[str]] = {}

    for r in repos:
        if r.id is None:
            continue
        arch = get_architecture(conn, r.id)
        if arch and arch.architecture and arch.architecture != "Unknown":
            arch_by_repo.setdefault(arch.architecture, []).append(r.name)

    suggestions: list[Suggestion] = []
    for arch_label, names in arch_by_repo.items():
        if len(names) < 2:
            continue
        suggestions.append(Suggestion(
            title=f"Shared {arch_label} architecture in {len(names)} projects",
            detail=(
                f"Multiple projects share the '{arch_label}' architecture "
                f"({', '.join(sorted(names))}). Standardize patterns, "
                f"linting, and dependency management across them."
            ),
            severity="medium",
            evidence=[
                f"architecture '{arch_label}' in: {', '.join(sorted(names))}"
            ],
        ))

    return suggestions


def _component_suggestions(conn) -> list[Suggestion]:
    """Suggestions based on shared component types.

    When two projects both implement the same type of component (e.g.,
    'Authentication', 'CLI', 'Database'), suggest sharing the implementation.
    """
    from .query import all_repositories

    repos = all_repositories(conn)
    component_by_type: dict[str, list[tuple[str, str]]] = {}

    for r in repos:
        if r.id is None:
            continue
        for c in get_components(conn, r.id):
            if is_weak(component_strength(c.name)):
                continue
            component_by_type.setdefault(c.name, []).append((r.name, c.evidence))

    suggestions: list[Suggestion] = []
    for comp_name, entries in component_by_type.items():
        if len(entries) < 2:
            continue
        names_str = ", ".join(sorted(set(e[0] for e in entries)))
        evidence = [f"{name}: {ev}" for name, ev in entries[:4]]
        suggestions.append(Suggestion(
            title=f"'{comp_name}' implemented in multiple projects",
            detail=(
                f"The {comp_name.lower()} pattern appears in {len(entries)} "
                f"project(s): {names_str}. Extract into a shared library."
            ),
            severity="high",
            evidence=evidence,
        ))

    return suggestions


def _theme_suggestions(conn) -> list[Suggestion]:
    """Suggestions from recurring portfolio themes."""
    themes = detect_themes(conn)
    suggestions: list[Suggestion] = []

    for t in themes:
        if len(t.repos) < 2 or t.confidence == "Weak":
            continue
        suggestions.append(Suggestion(
            title=f"Theme: {t.theme} spans {len(t.repos)} projects",
            detail=(
                f"Projects {', '.join(t.repos[:5])} share the "
                f"'{t.theme}' theme. Coordinate cross-project strategy "
                f"to avoid duplicated research/implementation."
            ),
            severity="medium",
            evidence=t.evidence[:3],
        ))

    return suggestions


def _integration_suggestions(conn) -> list[Suggestion]:
    """Suggestions from existing integration_opportunities analysis."""
    integ = integration_opportunities(conn)
    suggestions: list[Suggestion] = []

    for i in integ:
        severity = "high" if i.confidence == "Strong" else (
            "medium" if i.confidence == "Medium" else "low")
        suggestions.append(Suggestion(
            title=f"Integration candidate: {i.repo}",
            detail=i.reason,
            severity=severity,
            evidence=[f"integration opportunity: {i.reason}"],
        ))

    return suggestions


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def generate_suggestions(conn) -> SuggestResult:
    """Run all suggestion detectors and return deduplicated results.

    Runs ALL detectors and de-duplicates suggestions that share the same
    title. No AI, no LLM — purely cross-referencing existing portfolio data.
    """
    from .query import all_repositories

    repos = all_repositories(conn)
    total = len(repos)

    all_suggestions: list[Suggestion] = []
    all_suggestions.extend(_shared_tech_suggestions(conn))
    all_suggestions.extend(_duplicated_effort_suggestions(conn))
    all_suggestions.extend(_architecture_suggestions(conn))
    all_suggestions.extend(_component_suggestions(conn))
    all_suggestions.extend(_theme_suggestions(conn))
    all_suggestions.extend(_integration_suggestions(conn))

    # De-duplicate by title
    seen_titles: set[str] = set()
    deduped: list[Suggestion] = []
    for s in all_suggestions:
        if s.title not in seen_titles:
            seen_titles.add(s.title)
            deduped.append(s)

    return SuggestResult(suggestions=deduped, total_projects=total)


# ---------------------------------------------------------------------------
# CLI handler
# ---------------------------------------------------------------------------


def cmd_suggest(args: argparse.Namespace) -> int:
    """READ: suggest cross-project integration opportunities."""
    conn = connect()
    result = generate_suggestions(conn)
    conn.close()
    sys.stdout.write(result.to_text())
    return 0
