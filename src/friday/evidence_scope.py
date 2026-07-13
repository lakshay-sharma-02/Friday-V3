"""EvidenceScope — deterministic evidence-assembly guarantees.

This module hardens the assembly step of the FROZEN pipeline
(Question -> RetrievalRequirements -> Engineering Judgment -> Evidence Assembly
-> Answer). It does NOT route and does NOT reason: it measures the evidence
package AFTER the deterministic providers have filled it, and it reports, in a
structured, machine-readable way:

  - scope    : the EvidenceScope the objective required
  - coverage : how many of the required repositories the evidence actually spans
  - bias     : whether one repository dominates a workspace-wide answer
  - missing  : the specific kinds of evidence that are absent per repo

The existing providers already aggregate correctly along the right dimension
(e.g. portfolio_synthesis spans all repositories). The remaining failure class
was a WORKSPACE question collapsing to a single repository's describe dump
because the understanding step polluted `subjects` — that is fixed in the
judgment/understanding layers. This module adds the verifiable guard rails on
top, so a regression can never pass silently again.

Coverage and bias are computed from the repository NAMES referenced in the
assembled evidence blocks, never from the user's question text. No keyword
patching. No embeddings, graphs, or vector stores.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from . import objective as obj_mod

# A repository is considered "dominant" in a workspace-wide answer when its
# share of referenced evidence exceeds this fraction. Above it we flag bias.
BIAS_THRESHOLD = 0.5

# Below this coverage fraction, a workspace-wide answer is flagged as incomplete
# and the user must be told how many repos it actually rests on.
COVERAGE_WARN_PCT = 0.6


@dataclass
class ScopeReport:
    """Structured evidence-assembly verdict, exposed on Evidence.raw."""

    scope: str
    secondary: list[str] = field(default_factory=list)
    requested: int = 0          # repositories the scope required
    represented: int = 0        # repositories actually present in evidence
    pct: float = 0.0            # represented / requested
    dominant: Optional[str] = None
    dominant_pct: float = 0.0   # share of evidence held by the dominant repo
    bias: bool = False          # one repo dominates a workspace-wide answer
    missing: list[str] = field(default_factory=list)  # specific missing kinds

    def as_dict(self) -> dict:
        return {
            "scope": self.scope,
            "secondary": list(self.secondary),
            "requested": self.requested,
            "represented": self.represented,
            "pct": round(self.pct, 3),
            "dominant": self.dominant,
            "dominant_pct": round(self.dominant_pct, 3) if self.dominant else 0.0,
            "bias": self.bias,
            "missing": list(self.missing),
        }


def _repo_names(conn) -> list[str]:
    from .query import all_repositories

    return [r.name for r in all_repositories(conn) if r.id is not None]


def _subject_repo_names(conn, subjects: list[str]) -> list[str]:
    """Resolve a scope's required repository set: PROJECT/RELATIONSHIP use the
    named subjects; WORKSPACE/PORTFOLIO/TIMELINE/OBSERVATION use every repo."""
    all_names = _repo_names(conn)
    if not subjects:
        return list(all_names)
    # Keep only names that actually exist in the workspace (drop stale mentions).
    low = {n.lower() for n in all_names}
    return [s for s in subjects if s.lower() in low]


def _mentions(text: str, name: str) -> bool:
    """Whole-token mention of a repository name (so 'aether' != 'aethernet')."""
    return re.search(rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])", text) is not None


def _reference_counts(blocks: list[str], repo_names: list[str]) -> dict[str, int]:
    """Count how many evidence blocks mention each repository."""
    counts: dict[str, int] = {}
    for block in blocks:
        for name in repo_names:
            if name not in counts:
                counts[name] = 0
            if _mentions(block, name):
                counts[name] += 1
    return counts


def _missing_kinds(conn, repo_names: list[str], blocks: list[str],
                   scope: str) -> list[str]:
    """Specific evidence kinds that are absent for repos that SHOULD be covered.

    Reports exactly what is missing (per the spec) rather than a bare
    "I don't have enough evidence": purpose summary, observation history,
    relationship evidence, README, architecture, activity history. Computed
    from the DB, never invented."""
    from .db import get_architecture, get_all_relationships, get_technologies
    from .query import all_repositories

    repos = [r for r in all_repositories(conn) if r.id is not None
             and r.name in set(repo_names)]
    missing: list[str] = []
    for r in repos:
        repo_missing: list[str] = []
        if not r.readme_summary or r.readme_summary.strip() in ("", "None stated"):
            repo_missing.append("README/purpose summary")
        if not get_architecture(conn, r.id):
            repo_missing.append("architecture profile")
        if not get_technologies(conn, r.id):
            repo_missing.append("detected technologies")
        if repo_missing:
            missing.append(f"{r.name}: missing " + ", ".join(repo_missing))
    # Workspace-wide historical signal.
    if scope in (obj_mod.EvidenceScope.TIMELINE, obj_mod.EvidenceScope.WORKSPACE,
                 obj_mod.EvidenceScope.PORTFOLIO):
        from .db import latest_observation
        if not latest_observation(conn):
            missing.append("observation history (run `friday observe`)")
    return missing


def build_scope_report(req, decision, conn, blocks: list[str]) -> ScopeReport:
    """Compute the deterministic coverage/bias/missing verdict for an answer.

    `decision` is an ObjectiveDecision. `blocks` is the assembled evidence text.
    The scope is derived from the OBJECTIVE (general), never from keywords.
    """
    obj = decision.objective if decision is not None else obj_mod.Objective.GENERAL
    scope = obj_mod.scope_for(obj)
    secondary = obj_mod.secondary_scopes(obj)

    potential = _subject_repo_names(conn, list(req.subjects or []))
    represented = [n for n in potential if any(_mentions(b, n) for b in blocks)]

    requested = len(potential) or 0
    represented_n = len(represented)
    pct = (represented_n / requested) if requested else 0.0

    # Bias: one repo dominating a workspace-wide answer's evidence share.
    dominant: Optional[str] = None
    dominant_pct = 0.0
    bias = False
    total_blocks = len(blocks) or 1
    if scope in (obj_mod.EvidenceScope.WORKSPACE, obj_mod.EvidenceScope.PORTFOLIO):
        counts = _reference_counts(blocks, potential)
        total_mentions = sum(counts.values())
        if counts and requested > 1 and total_mentions > 0:
            top = max(counts.items(), key=lambda kv: kv[1])
            share = top[1] / total_mentions
            if top[1] > 0 and share > BIAS_THRESHOLD:
                dominant, dominant_pct, bias = top[0], share, True

    missing = _missing_kinds(conn, potential, blocks, scope)

    return ScopeReport(
        scope=scope, secondary=secondary, requested=requested,
        represented=represented_n, pct=pct, dominant=dominant,
        dominant_pct=dominant_pct, bias=bias, missing=missing,
    )


def build_coverage_report(req, decision, conn, blocks: list[str]) -> dict:
    """Full, auditable coverage picture for one answer (Part C).

    Deterministic. Surfaces how complete the evidence was BEFORE reasoning, so a
    human (or a future CI gate) can audit an answer's basis. No LLM, no keywords.
    Exposed via `ask --verbose`; never clutters a normal answer.
    """
    from .db import get_architecture, get_all_relationships, latest_observation
    from .query import all_repositories

    scope_report = build_scope_report(req, decision, conn, blocks)
    all_repos = [r for r in all_repositories(conn) if r.id is not None]
    n_repos = len(all_repos)

    # Purpose / architecture / relationship evidence presence across the workspace.
    repos_with_purpose = sum(
        1 for r in all_repos if r.readme_summary and r.readme_summary.strip() not in ("", "None stated"))
    repos_with_arch = sum(1 for r in all_repos if get_architecture(conn, r.id))
    rels = get_all_relationships(conn)
    strong_rels = sum(1 for r in rels if r.strength != "Weak")
    obs = latest_observation(conn)

    def conf(frac: float) -> str:
        if frac >= 0.8:
            return "Strong"
        if frac >= 0.5:
            return "Medium"
        if frac > 0:
            return "Weak"
        return "None"

    return {
        "scope": scope_report.scope,
        "secondary_scopes": scope_report.secondary,
        "repositories_considered": scope_report.requested,
        "repositories_represented": scope_report.represented,
        "coverage_pct": round(scope_report.pct * 100, 1),
        "bias": {"flagged": scope_report.bias, "dominant": scope_report.dominant,
                 "dominant_pct": round(scope_report.dominant_pct * 100, 1)},
        "missing_evidence": scope_report.missing,
        "workspace_purpose_confidence": conf(repos_with_purpose / n_repos) if n_repos else "None",
        "workspace_architecture_confidence": conf(repos_with_arch / n_repos) if n_repos else "None",
        "relationship_confidence": conf(strong_rels / max(1, n_repos)) if n_repos else "None",
        "observation_history": "available" if obs else "none",
        "timeline_confidence": "Medium" if obs else "None",
    }


def audit_evidence_completeness(conn) -> list[dict]:
    """Per-repository evidence-completeness audit (Part D).

    For every repository, list exactly WHY it contributes weak evidence — never
    silently degrade. Computed from the DB, never invented. Empty gaps = strong.
    """
    from .db import get_architecture, get_all_relationships, latest_observation
    from .query import all_repositories

    repos = [r for r in all_repositories(conn) if r.id is not None]
    rels = get_all_relationships(conn)
    name_by_id = {r.id: r.name for r in repos}
    paired: set[str] = set()
    for r in rels:
        if r.strength != "Weak":
            paired.add(name_by_id[r.repo_a])
            paired.add(name_by_id[r.repo_b])
    obs = latest_observation(conn)

    out: list[dict] = []
    for r in repos:
        gaps: list[str] = []
        if not r.readme_summary or r.readme_summary.strip() in ("", "None stated"):
            gaps.append("missing README / purpose summary")
        elif (r.readme_quality or "good") in ("boilerplate", "poor", "none"):
            gaps.append(f"boilerplate/poor README (quality={r.readme_quality})")
        if not get_architecture(conn, r.id):
            gaps.append("missing architecture profile")
        if r.id not in {rid for rid in (name_by_id.keys())}:
            pass
        if r.name not in paired:
            gaps.append("missing relationship evidence (no strong link to another repo)")
        if not obs:
            gaps.append("no observation history")
        out.append({
            "repo": r.name,
            "gaps": gaps,
            "complete": not gaps,
        })
    return out


def format_completeness_audit(rows: list[dict]) -> str:
    lines = ["Evidence completeness audit:"]
    for row in rows:
        if row["complete"]:
            lines.append(f"  {row['repo']}: complete")
        else:
            lines.append(f"  {row['repo']}: weak evidence")
            for g in row["gaps"]:
                lines.append(f"    - {g}")
    return "\n".join(lines)


def format_coverage_report(report: dict) -> str:
    """Human, --verbose rendering of build_coverage_report()."""
    lines = ["Coverage report:"]
    lines.append(f"  Scope: {report['scope']}"
                 + (f" (+{', '.join(report['secondary_scopes'])})" if report['secondary_scopes'] else ""))
    lines.append(f"  Repositories used: {report['repositories_represented']}"
                 f"/{report['repositories_considered']} "
                 f"({report['coverage_pct']}%)")
    if report["bias"]["flagged"]:
        lines.append(f"  Bias: {report['bias']['dominant']} dominates "
                     f"({report['bias']['dominant_pct']}%)")
    lines.append(f"  Purpose confidence: {report['workspace_purpose_confidence']}")
    lines.append(f"  Architecture confidence: {report['workspace_architecture_confidence']}")
    lines.append(f"  Relationship confidence: {report['relationship_confidence']}")
    lines.append(f"  Observation history: {report['observation_history']}")
    lines.append(f"  Timeline confidence: {report['timeline_confidence']}")
    if report["missing_evidence"]:
        lines.append("  Missing evidence:")
        for m in report["missing_evidence"]:
            lines.append(f"    - {m}")
    return "\n".join(lines)


def coverage_note(report: ScopeReport) -> str:
    """Human line stating how many repos the answer actually rests on.

    Returned (possibly empty) so the deterministic answer / synthesizer can
    append it without ever claiming completeness it does not have. Never
    fabricates: states the count and the missing-kinds when relevant.

    Gated to workspace-wide scopes (WORKSPACE / PORTFOLIO / TIMELINE): a
    PROJECT / RELATIONSHIP / BY_TECH answer is self-contained (about one or two
    named repos), so a coverage note would be noise and could mask a complete
    "no match" answer. Bias is also only meaningful for workspace-wide answers.
    """
    if report.scope not in (obj_mod.EvidenceScope.WORKSPACE,
                            obj_mod.EvidenceScope.PORTFOLIO,
                            obj_mod.EvidenceScope.TIMELINE):
        return ""
    if report.requested >= 2 and report.represented < report.requested:
        line = (f"This answer is based on {report.represented} of "
                f"{report.requested} repositories.")
        if report.missing:
            line += " Missing evidence: " + "; ".join(report.missing) + "."
        return line
    if report.bias and report.dominant:
        return (f"Note: {report.dominant} dominates this answer "
                f"({report.dominant_pct * 100:.0f}% of cited evidence) — "
                f"other projects have little recorded evidence, so the picture "
                f"may be skewed toward it.")
    if report.missing:
        return "Missing evidence: " + "; ".join(report.missing) + "."
    return ""
