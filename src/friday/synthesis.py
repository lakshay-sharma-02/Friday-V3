"""Cross-project synthesis — identify genuine structural overlap between two repos.

Extension layer (not frozen). Grounded in real evidence from the ingestion layer:
architecture, technologies, components, entry points, relationships. No LLM
guesswork without evidence basis.

Usage:
    friday synthesize <repo-name> <other-repo-name>

Output is always confidence-labeled and never auto-acts on findings.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

from .services.llm import _call as llm_call


# ---------------------------------------------------------------------------
# Evidence bundle — what we know about one repo
# ---------------------------------------------------------------------------


@dataclass
class _RepoEvidence:
    name: str
    purpose: str
    architecture: str
    technologies: list[str]
    components: list[str]
    entry_points: list[str]
    relationships: list[str]
    dependencies: list[str]  # dependency manifest names, not deep resolution


def _gather(conn, repo_name: str) -> Optional[_RepoEvidence]:
    """Pull all structural evidence for *repo_name* from the DB.

    Returns None if the repo isn't found. Uses ONLY what ingestion already
    stores — no new extraction, no README fallback faking.
    """
    from .db import get_repositories
    from .db import connect as _c

    rows = [r for r in get_repositories(conn) if r.name == repo_name]
    if not rows:
        return None
    r = rows[0]

    # Architecture
    arch_row = conn.execute(
        "SELECT architecture, evidence, data_flow, known_patterns, complexity "
        "FROM architecture WHERE repo_id = ?", (r.id,)
    ).fetchone()
    arch_parts = []
    if arch_row:
        for val in (arch_row["architecture"], arch_row["evidence"],
                    arch_row["data_flow"], arch_row["known_patterns"]):
            if val and val != "None":
                arch_parts.append(val)
    architecture = "; ".join(arch_parts) if arch_parts else "(no architecture data)"

    # Technologies
    tech_rows = conn.execute(
        "SELECT tech FROM technologies WHERE repo_id = ?", (r.id,)
    ).fetchall()
    technologies = sorted(set(t["tech"] for t in tech_rows)) if tech_rows else []

    # Components
    comp_rows = conn.execute(
        "SELECT name FROM components WHERE repo_id = ?", (r.id,)
    ).fetchall()
    components = sorted(set(
        f"{c['name']}" for c in comp_rows
    )) if comp_rows else []

    # Entry points
    ep_rows = conn.execute(
        "SELECT kind, detail FROM entry_points WHERE repo_id = ?", (r.id,)
    ).fetchall()
    entry_points = sorted(set(
        f"{e['kind']}: {e['detail']}" for e in ep_rows
    )) if ep_rows else []

    # Relationships
    rel_data = []
    try:
        rel_rows = conn.execute(
            "SELECT r.kind, ra.name AS name_a, rb.name AS name_b "
            "FROM relationships r "
            "JOIN repositories ra ON ra.id = r.repo_a "
            "JOIN repositories rb ON rb.id = r.repo_b "
            "WHERE ra.name = ? OR rb.name = ?",
            (repo_name, repo_name)
        ).fetchall()
    except Exception:
        rel_rows = []
    if rel_rows:
        for rr in rel_rows:
            other = rr["name_b"] if rr["name_a"] == repo_name else rr["name_a"]
            if other != repo_name:
                rel_data.append(f"{rr['kind']} with {other}")
    rels = sorted(set(rel_data))

    # Dependencies (from purpose / README, not deep resolution)
    # What we actually have: technology evidence strings sometimes mention
    # package names. We surface what's stored.
    deps = technologies[:]  # a reasonable proxy

    return _RepoEvidence(
        name=repo_name,
        purpose=r.readme_summary or "(no purpose stated)",
        architecture=architecture,
        technologies=technologies,
        components=components,
        entry_points=entry_points,
        relationships=rels,
        dependencies=deps,
    )


# ---------------------------------------------------------------------------
# Synthesis prompt
# ---------------------------------------------------------------------------

_SYNTHESIS_SYSTEM = (
    "You are Friday's cross-project synthesis layer. Your job is to identify "
    "genuine technical overlap or complementary capability between TWO separate "
    "codebases. Rules:\n"
    "1. Base your analysis ONLY on the evidence provided below for each repo.\n"
    "2. Look for structural overlap: shared technologies, similar architecture "
    "patterns, complementary entry points, shared component concepts.\n"
    "3. Do NOT force a finding. If the repos genuinely do different things with "
    "no meaningful intersection, return null — that is a valid, honest result.\n"
    "4. Do NOT invent facts about either repo's internals. The evidence IS the "
    "limit of what you know.\n"
    "5. Judge the KIND of overlap if any: 'shared dependency' (both use the "
    "same framework), 'complementary' (one produces input the other consumes), "
    "'parallel' (same problem, different approach), or 'none'.\n"
    "6. Confidence reflects how much evidence supports the finding.\n"
    "Return valid JSON only: "
    '{"overlap_found": bool, "overlap_kind": str|null, '
    '"description": str|null, "confidence": str, "basis": [str], '
    '"note": str|null}'
    '  confidence: "Strong" | "Medium" | "Weak"'
    '  When overlap_found is false, set description=null and basis=[].'
)

_USER_TEMPLATE = """Repo A: {name_a}
{evidence_a}

Repo B: {name_b}
{evidence_b}

Analyse structural overlap between these two codebases. Return null/empty if
there is none — do not force a finding."""


@dataclass
class SynthesisResult:
    overlap_found: bool
    overlap_kind: Optional[str] = None
    description: Optional[str] = None
    confidence: str = "Weak"
    basis: list[str] = field(default_factory=list)
    note: Optional[str] = None

    def to_text(self) -> str:
        if not self.overlap_found:
            return (
                f"No meaningful structural overlap found between these repositories.\n"
                f"Confidence: {self.confidence}"
                + (f"\nNote: {self.note}" if self.note else "")
            )
        lines = [
            f"Overlap detected: {self.overlap_kind or 'unspecified'}",
            f"Confidence: {self.confidence}",
            f"\n{self.description}",
        ]
        if self.basis:
            lines.append("\nBasis:")
            for b in self.basis:
                lines.append(f"  - {b}")
        if self.note:
            lines.append(f"\nNote: {self.note}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public command
# ---------------------------------------------------------------------------


def synthesize(conn, repo_a: str, repo_b: str) -> SynthesisResult:
    """Compare two repositories' structural evidence for genuine overlap.

    Reads only what ingestion already stores. Returns a SynthesisResult
    with confidence labels — never auto-acts on findings.
    """
    ev_a = _gather(conn, repo_a)
    ev_b = _gather(conn, repo_b)

    if ev_a is None:
        return SynthesisResult(
            overlap_found=False, confidence="Weak",
            note=f"Repository '{repo_a}' not found in workspace.")
    if ev_b is None:
        return SynthesisResult(
            overlap_found=False, confidence="Weak",
            note=f"Repository '{repo_b}' not found in workspace.")

    # Quick deterministic no-overlap: if technology sets are disjoint and
    # no relationships exist between them, flag it early.
    tech_a = set(ev_a.technologies)
    tech_b = set(ev_b.technologies)
    has_shared_tech = bool(tech_a & tech_b)
    # Lowercase relationship matching
    a_name_lower = repo_a.lower()
    b_name_lower = repo_b.lower()
    has_rels = any(
        b_name_lower in rel.lower() or a_name_lower in rel.lower()
        for rel in ev_a.relationships + ev_b.relationships
    )
    if not has_shared_tech and not has_rels:
        # No deterministic evidence of overlap, but LLM can still find
        # complementary patterns in architecture/component descriptions.
        pass

    from .services.llm import _enabled as llm_available

    if not llm_available():
        if has_shared_tech:
            shared = tech_a & tech_b
            return SynthesisResult(
                overlap_found=True, overlap_kind="shared dependency",
                description=(
                    f"{repo_a} and {repo_b} share technologies: "
                    f"{', '.join(sorted(shared))}."),
                confidence="Weak",
                basis=[f"Shared technology: {t}" for t in sorted(shared)],
                note="No LLM available — evidence limited to technology overlap.")
        return SynthesisResult(
            overlap_found=False, confidence="Weak",
            note="No LLM available for deep synthesis.")

    # Build evidence blocks
    def _fmt(ev: _RepoEvidence) -> str:
        lines = [
            f"Purpose: {ev.purpose}",
            f"Architecture: {ev.architecture}",
            f"Technologies: {', '.join(ev.technologies) if ev.technologies else '(none captured)'}",
        ]
        if ev.components:
            lines.append(f"Components: {', '.join(ev.components[:10])}")
        if ev.entry_points:
            lines.append(f"Entry points: {', '.join(ev.entry_points[:5])}")
        if ev.relationships:
            lines.append(f"Relationships: {'; '.join(ev.relationships[:6])}")
        return "\n".join(lines)

    user = _USER_TEMPLATE.format(
        name_a=repo_a, evidence_a=_fmt(ev_a),
        name_b=repo_b, evidence_b=_fmt(ev_b),
    )

    content = llm_call(_SYNTHESIS_SYSTEM, user)
    if not content:
        # LLM failure fallback — same as deterministic-no-LLM path
        return SynthesisResult(
            overlap_found=has_shared_tech, overlap_kind="shared dependency" if has_shared_tech else None,
            description=(
                f"Technology overlap detected: {', '.join(sorted(tech_a & tech_b))}."
                if has_shared_tech else None),
            confidence="Weak" if has_shared_tech else "Weak",
            basis=(
                [f"Shared technology: {t}" for t in sorted(tech_a & tech_b)]
                if has_shared_tech else []),
            note="LLM synthesis returned no result — limited to technology-level analysis.")

    # Parse JSON from the LLM response
    content = content.strip()
    if content.startswith("```"):
        content = content.split("```", 2)[1]
        if content.startswith("json"):
            content = content[4:]
    content = content.strip().strip("`").strip()
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, ValueError):
        return SynthesisResult(
            overlap_found=False, confidence="Weak",
            note="Failed to parse synthesis result.")

    return SynthesisResult(
        overlap_found=bool(data.get("overlap_found")),
        overlap_kind=data.get("overlap_kind"),
        description=data.get("description"),
        confidence=data.get("confidence", "Weak"),
        basis=data.get("basis") or [],
        note=data.get("note"),
    )
