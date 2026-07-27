"""Integration Engine — wire synthesis output into evidence-backed task graphs.

Previously, IntegrationEngine called TaskGraphEngine.generate(goal) which went
through the template planner (derive.py::plan()) that stamps out hardcoded
milestones (Investigate -> Design -> Verify -> Document -> Roll Out) regardless
of the actual integration goal. This produced generic READMEs and nothing useful.

The fix: build a Plan DIRECTLY from the synthesis evidence, with specific
integration milestones that each create a meaningful file (comparative analysis,
shared patterns doc, feasibility plan, adapter design, prototype stub). Each
milestone carries `task_type` and `symbolic` (create_file) so the compiler
propagates them verbatim — bypassing the template planner entirely.

Now extended to support 2+ repositories (variadic ``integrate(*repo_names)``).
Runs pairwise synthesis for every pair, aggregates the strongest signal, and
builds milestones referencing all repos.

Design:
- Runs synthesis.synthesize() (structural overlap analysis) for all pairs
- Builds evidence-backed integration milestones with real file paths
- Creates a Plan object with these milestones
- Compiles via compile_plan() (same deterministic compiler)
- Tags source='integration:<repo_a>/<repo_b>/...' for traceability
- Lands in review as 'proposal' (never auto-approves)
- Extension layer, no frozen modules modified
"""

from __future__ import annotations

import json as _json
from dataclasses import dataclass, field
from itertools import combinations
from typing import Optional

from ..db import (
    TaskEdgeRow,
    TaskGraphRow,
    TaskRow,
    get_repositories,
    insert_plan,
    insert_task_graph,
    update_task_graph_source,
    update_task_graph_status,
    now_iso as _now_iso,
)

from ..planning.compiler import compile_plan
from ..planning.models import Plan, PlanType, PlanConfidence, PlanStatus
from ..synthesis import synthesize as run_synthesis


# ──────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────

_MAX_REPOS = 8  # sanity cap — avoid O(n²) LLM calls explosion


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────


def _dumps(xs: list) -> str:
    """JSON-serialize a list (same as graph_engine's helper)."""
    try:
        return _json.dumps(xs, separators=(",", ":"))
    except (TypeError, ValueError):
        return "[]"


def _dumpd(d: dict) -> str:
    """JSON-serialize a dict (for symbolic column)."""
    try:
        return _json.dumps(d, separators=(",", ":"))
    except (TypeError, ValueError):
        return "{}"


def _safe_repo_name(name: str) -> str:
    """Sanitize a repo name for use in file paths and IDs."""
    return name.replace(" ", "_").replace("/", "_").lower()


def _safe_name_join(names: list[str]) -> str:
    """Join sanitized repo names with '--' to avoid ambiguity with
    underscores that may appear inside individual repo names."""
    return "--".join(_safe_repo_name(n) for n in names)


# ──────────────────────────────────────────────────────────────────────
# IntegrateResult
# ──────────────────────────────────────────────────────────────────────


@dataclass
class IntegrateResult:
    """Outcome of a multi-repo integration analysis + graph generation."""

    goal: str
    repo_names: list[str]
    graph_id: Optional[str]
    plan_id: Optional[str]
    correlation_score: float  # max overlap score across all pairs
    overlap_found: bool
    overlap_kind: Optional[str]
    overlap_description: Optional[str]
    confidence: str
    basis: list[str]
    warnings: list[str] = field(default_factory=list)
    docs_generated: list[str] = field(default_factory=list)
    note: Optional[str] = None

    def to_text(self) -> str:
        """Render the integration result to terminal text."""
        lines = [
            "┌─ Cross-Project Integration ─────────────────────────────┐",
            f"│  Goal: {self.goal}",
            f"│  Repos: {', '.join(self.repo_names)}",
            f"│  Correlation score: {self.correlation_score:.3f}",
            f"│  Confidence: {self.confidence}",
            "├────────────────────────────────────────────────────────┤",
        ]

        if self.overlap_found:
            lines.append(f"│  Overlap: {self.overlap_kind or 'detected'}              │")
            if self.overlap_description:
                lines.append(f"│  {self.overlap_description[:70]}")
        else:
            lines.append("│  No meaningful structural overlap detected.          │")
            if self.note:
                lines.append(f"│  Note: {self.note[:65]}")

        if self.basis:
            lines.append("├─ Evidence basis ───────────────────────────────────┤")
            for b in self.basis[:5]:
                text = b[:70]
                lines.append(f"│  • {text}")
            if len(self.basis) > 5:
                lines.append(f"│  … and {len(self.basis) - 5} more")

        if self.warnings:
            lines.append("├─ Warnings ─────────────────────────────────────────┤")
            for w in self.warnings[:3]:
                lines.append(f"│  ⚠ {w[:65]}")
            if len(self.warnings) > 3:
                lines.append(f"│  … and {len(self.warnings) - 3} more")

        lines.append("├────────────────────────────────────────────────────────┤")
        if self.docs_generated:
            lines.append("│  Generated artifacts:                              │")
            for doc in self.docs_generated[:4]:
                lines.append(f"│    • {doc}")
        if self.plan_id:
            lines.append(f"│  Plan: {self.plan_id}")
        if self.graph_id:
            lines.append(f"│  Task Graph: {self.graph_id}")
            lines.append("│  Review: friday graph review                       │")

        lines.append("└──────────────────────────────────────────────────────┘")
        return "\n".join(lines) + "\n"


# ──────────────────────────────────────────────────────────────────────
# IntegrationEngine
# ──────────────────────────────────────────────────────────────────────


class IntegrationEngine:
    """Bridge between synthesis analysis and the execution pipeline.

    Builds evidence-backed integration tasks directly from synthesis findings,
    bypassing the generic template planner. Every task creates a meaningful file
    (analysis doc, patterns doc, feasibility plan, etc.).

    Supports 2+ repositories. For N repos, runs pairwise synthesis for every
    pair, aggregates the strongest signal, and builds milestones that cover
    all repos.

    The caller owns the connection lifecycle.
    """

    def __init__(self, conn) -> None:
        self.conn = conn

    # ── Public entrypoint ────────────────────────────────────────────

    def integrate(self, *repo_names: str) -> IntegrateResult:
        """Run full integration analysis for 2+ repositories.

        1. Validates input (min 2, max 8, no empty, all exist in DB)
        2. Runs pairwise synthesis for every pair
        3. Aggregates results (strongest overlap signal wins)
        4. Builds integration-specific milestones covering all repos
        5. Creates a Plan + compiles into a TaskGraph
        6. Persists plan and graph with provenance tags
        7. Returns IntegrateResult with graph id and analysis

        The graph lands with status='proposal' (reviewable, non-executing) and
        source='integration:...', ready for review via ``friday graph review``.
        """
        # ── Validate input ───────────────────────────────────────────
        repo_names = [r.strip() for r in repo_names if r and r.strip()]
        if len(repo_names) < 2:
            raise ValueError(
                f"Need at least 2 repositories, got {len(repo_names)}. "
                "Usage: friday integrate <repo-a> <repo-b> [<repo-c> ...]"
            )
        if len(repo_names) > _MAX_REPOS:
            raise ValueError(
                f"Max {_MAX_REPOS} repositories per integration, "
                f"got {len(repo_names)}."
            )

        # ── Verify all repos exist in DB ─────────────────────────────
        repos = get_repositories(self.conn)
        repo_map = {r.name: r for r in repos}
        missing = [n for n in repo_names if n not in repo_map]
        if missing:
            plural = "s" if len(missing) > 1 else ""
            raise ValueError(
                f"Repository{plural} not found: {', '.join(missing)}. "
                "Use ``friday ingest`` first or check the names."
            )

        resolved_repos = [repo_map[n] for n in repo_names]
        repo_paths = [r.path for r in resolved_repos]

        goal = f"Integrate {' ⇿ '.join(repo_names)}"

        # ── Step 1: Run pairwise synthesis ───────────────────────────
        pair_results = []
        for a, b in combinations(repo_names, 2):
            synth = run_synthesis(self.conn, a, b)
            pair_results.append((a, b, synth))

        # ── Step 2: Aggregate ────────────────────────────────────────
        max_overlap_found = any(s.overlap_found for _, _, s in pair_results)
        max_overlap_kind = None
        max_overlap_desc = None
        max_confidence = "Weak"
        all_basis: list[str] = []
        max_score = 0.0
        warnings: list[str] = []
        conf_rank = {"Strong": 3, "Medium": 2, "Weak": 1}

        for a, b, synth in pair_results:
            if synth.overlap_found:
                max_overlap_kind = synth.overlap_kind or max_overlap_kind
                max_overlap_desc = synth.description or max_overlap_desc
                if conf_rank.get(synth.confidence, 0) > conf_rank.get(max_confidence, 0):
                    max_confidence = synth.confidence
                if synth.basis:
                    all_basis.extend(synth.basis)
            # Heuristic score: 1.0 if overlap found, 0.1 otherwise
            pair_score = 1.0 if synth.overlap_found else 0.1
            max_score = max(max_score, pair_score)

        if not max_overlap_found:
            warnings.append(
                f"Low structural correlation ({max_score:.2f}) among repos. "
                "Documents will be based on available metadata only."
            )

        # Deduplicate basis
        all_basis = list(dict.fromkeys(all_basis))  # unique, order-preserving
        correlation_score = max_score

        # ── Step 3: Build milestones ─────────────────────────────────
        milestones = self._build_milestones(repo_names, repo_paths, pair_results)

        # ── Step 4: Create Plan ──────────────────────────────────────
        generated_at = _now_iso()
        plan_id = (
            f"plan:integrate:{_safe_name_join(repo_names)}"
        )

        plan = Plan(
            id=plan_id,
            goal=goal,
            plan_type=PlanType.INTEGRATION,
            confidence=PlanConfidence.from_str(max_confidence.lower()),
            status=PlanStatus.PLANNED,
            milestones=milestones,
            dependencies=[],
            risks=[
                {"kind": "Complexity", "severity": "medium",
                 "detail": f"Integration across {len(repo_names)} repos may reveal "
                           "unexpected coupling or incompatibilities."},
                {"kind": "Compatibility", "severity": "medium",
                 "detail": "Dependency version conflicts between repos may "
                           "require additional refactoring."},
                {"kind": "Scope creep", "severity": "low",
                 "detail": "Keep the integration plan bounded to explicit milestones."},
            ],
            verification=[{
                "method": "acceptance",
                "detail": "Each integration artifact exists and contains the required analysis.",
            }],
            rollback=[{
                "strategy": "review",
                "detail": "Integration graph is proposal-only; can be rejected without executing.",
            }],
            estimated_complexity="high" if len(repo_names) > 3 else "medium",
            estimated_effort="large" if len(repo_names) > 3 else "medium",
            created_at=generated_at,
            updated_at=generated_at,
        )

        # ── Step 5: Compile into Task Graph ──────────────────────────
        graph = compile_plan(plan, generated_at=generated_at)
        graph.status = "proposal"
        graph.id = f"integration_graph:{_safe_name_join(repo_names)}"
        for t in graph.tasks:
            t.graph_id = graph.id

        # ── Step 6: Persist plan + graph ─────────────────────────────
        insert_plan(self.conn, [plan.to_row()])
        self._persist_graph(graph, generated_at)

        # ── Step 7: Tag provenance ───────────────────────────────────
        source_tag = f"integration:{'/'.join(repo_names)}"
        update_task_graph_source(self.conn, graph.id, source_tag)
        update_task_graph_status(self.conn, graph.id, "proposal")

        # ── Build docs_generated list ────────────────────────────────
        safe = _safe_name_join(repo_names)
        docs_generated = [
            f"integration-analysis-{safe}.md",
            f"shared-patterns-{safe}.md",
            f"integration-plan-{safe}.md",
        ]
        if max_overlap_found:
            docs_generated.append(f"adapter-design-{safe}.md")

        return IntegrateResult(
            goal=goal,
            repo_names=repo_names,
            graph_id=graph.id,
            plan_id=plan_id,
            correlation_score=correlation_score,
            overlap_found=max_overlap_found,
            overlap_kind=max_overlap_kind,
            overlap_description=max_overlap_desc,
            confidence=max_confidence,
            basis=all_basis,
            warnings=warnings,
            docs_generated=docs_generated,
            note=None,
        )

    # ── Internal: build integration-specific milestones ──────────────



    def _build_milestones(self, repo_names: list[str],
                          repo_paths: list[Optional[str]],
                          pair_results: list) -> list[dict]:
        """Build evidence-backed integration milestones for N repos.

        Every milestone carries:
        - task_type: explicitly set so the compiler passes it through verbatim
        - symbolic: create_file op with a real file path and content direction
        - acceptance_criteria: what success looks like

        This ensures every task produces a real file with meaningful content,
        instead of the generic README the template planner would produce.
        """
        safe = _safe_name_join(repo_names)
        name_list = ", ".join(repo_names)

        # Aggregate synthesis evidence across all pairs.
        found_any = any(s.overlap_found for _, _, s in pair_results)
        all_basis: list[str] = []
        all_descs: list[str] = []
        all_kinds: list[str] = []
        for a, b, s in pair_results:
            if s.overlap_found:
                if s.basis:
                    all_basis.extend(s.basis)
                if s.description:
                    all_descs.append(f"{a}↔{b}: {s.description}")
                if s.overlap_kind:
                    all_kinds.append(f"{a}↔{b}: {s.overlap_kind}")

        basis_text = "\n".join(f"- {b}" for b in all_basis[:10]) if all_basis else "No specific basis recorded."
        desc_text = "\n".join(all_descs[:5]) if all_descs else "No specific overlap description available."
        kind_text = "; ".join(dict.fromkeys(all_kinds)) if all_kinds else "none detected"
        confidences = [s.confidence for _, _, s in pair_results if s.confidence]
        max_conf = max(confidences, key=lambda c: {"Strong": 3, "Medium": 2, "Weak": 1}.get(c, 0)) if confidences else "Weak"

        milestones = []
        _synthesis_caps = ["synthesis"]

        # Milestone 1: Comparative architecture analysis
        arch_title = f"Analyse architectures of {name_list}"
        milestones.append({
            "order": 1,
            "required_capabilities": _synthesis_caps,
            "title": arch_title,
            "detail": (
                f"Produce a detailed comparative analysis of all {len(repo_names)} "
                f"repositories: {name_list}.\n\n"
                f"Synthesis findings:\n"
                f"- Overlap kinds: {kind_text}\n"
                f"- Overlap found: {found_any}\n"
                f"- Max confidence: {max_conf}\n\n"
                f"Detailed descriptions:\n{desc_text}\n\n"
                f"Basis:\n{basis_text}\n\n"
                f"Analyse each repo's: architecture pattern, technology stack, "
                f"entry points, components, dependencies, data flow, testing "
                f"strategy, and deployment model. Identify specific integration "
                f"points between all pairs."
            ),
            "evidence": "goal",
            "task_type": "implementation",
            "symbolic": {
                "op": "create_file",
                "path": f"integration-analysis-{safe}.md",
                "content": (
                    f"# Multi-Repo Integration Analysis: {name_list}\n\n"
                    f"## Overview\n"
                    f"This document analyses {len(repo_names)} repositories for "
                    f"integration opportunities.\n\n"
                    f"## Synthesis Summary\n"
                    f"- Overlap kinds: {kind_text}\n"
                    f"- Overlap found: {found_any}\n"
                    f"- Max confidence: {max_conf}\n\n"
                    f"### Pairwise Findings\n"
                    f"{desc_text}\n\n"
                    f"## Architecture Comparison\n\n"
                    f"Analyse each repository's architecture pattern, technology "
                    f"stack, entry points, components, dependencies, data flow, "
                    f"testing strategy, and deployment model.\n\n"
                    f"## Technology Overlap\n\n"
                    f"Identify shared technologies, frameworks, and languages "
                    f"across all repositories.\n\n"
                    f"## Integration Candidates\n\n"
                    f"Based on the analysis above, propose specific integration "
                    f"points between the codebases.\n"
                ),
                "goal": arch_title,
                "repo_paths": repo_paths,
            },
            "acceptance_criteria": [
                f"integration-analysis-{safe}.md exists",
                "File documents all repos' architectures",
                "Integration points are identified and assessed",
            ],
            "parallel_next": False,
        })

        # Milestone 2: Shared patterns and divergences
        patterns_title = f"Document shared patterns across {name_list}"
        milestones.append({
            "order": 2,
            "required_capabilities": _synthesis_caps,
            "title": patterns_title,
            "detail": (
                f"Document the architectural and code-level patterns shared "
                f"across all {len(repo_names)} repositories: {name_list}, "
                f"as well as key divergences.\n\n"
                f"Focus on: shared technologies, similar component structures, "
                f"common patterns (CLI argument parsing, configuration files, "
                f"LLM interfaces, authentication, testing approach), "
                f"and areas where the projects differ significantly."
            ),
            "evidence": "goal",
            "task_type": "documentation",
            "symbolic": {
                "op": "create_file",
                "path": f"shared-patterns-{safe}.md",
                "content": (
                    f"# Shared Patterns Across {name_list}\n\n"
                    f"## Overview\n"
                    f"{desc_text}\n\n"
                    f"## Synthesis Basis\n"
                    f"{basis_text}\n\n"
                    f"## Shared Technologies\n"
                    f"Inventory all technologies used across all projects.\n\n"
                    f"## Similar Architectural Patterns\n"
                    f"Analyse architectural patterns present in all codebases.\n\n"
                    f"## Key Divergences\n"
                    f"Identify areas where the projects differ significantly.\n\n"
                    f"## Reuse Opportunities\n"
                    f"Recommend specific code or patterns that could be extracted "
                    f"into a shared library or module.\n"
                ),
                "goal": patterns_title,
                "repo_paths": repo_paths,
            },
            "acceptance_criteria": [
                f"shared-patterns-{safe}.md exists",
                "File documents shared technologies and patterns",
                "Integration opportunities are identified",
            ],
            "parallel_next": False,
        })

        # Milestone 3: Integration feasibility plan
        feasibility_title = f"Assess integration feasibility for {name_list}"
        milestones.append({
            "order": 3,
            "required_capabilities": _synthesis_caps,
            "title": feasibility_title,
            "detail": (
                f"Produce an integration feasibility assessment for "
                f"{name_list}. Evaluate the effort, risk, and value of "
                f"integrating the projects.\n\n"
                f"Synthesis findings:\n"
                f"- Overlap kinds: {kind_text}\n"
                f"- Overlap found: {found_any}\n"
                f"- Max confidence: {max_conf}\n\n"
                f"Basis:\n{basis_text}\n\n"
                f"Include: integration strategy options, estimated effort, "
                f"risk assessment, dependency analysis, and a phased migration "
                f"plan if integration is recommended."
            ),
            "evidence": "goal",
            "task_type": "documentation",
            "symbolic": {
                "op": "create_file",
                "path": f"integration-plan-{safe}.md",
                "content": (
                    f"# Integration Plan: {name_list}\n\n"
                    f"## Feasibility Assessment\n"
                    f"- Overlap detected: {found_any}\n"
                    f"- Kinds: {kind_text}\n"
                    f"- Max confidence: {max_conf}\n\n"
                    f"## Basis for Assessment\n"
                    f"{basis_text}\n\n"
                    f"## Strategy Options\n"
                    f"Evaluate integration strategies: shared library, adapter "
                    f"layer, communication protocol, or merge.\n\n"
                    f"## Effort Estimate\n"
                    f"Break down the effort by phase.\n\n"
                    f"## Risk Assessment\n"
                    f"Identify and rate risks.\n\n"
                    f"## Phased Migration Plan\n"
                    f"Describe a phased approach with validation gates.\n\n"
                    f"## Success Criteria\n"
                    f"Define measurable criteria for integration success.\n"
                ),
                "goal": feasibility_title,
                "repo_paths": repo_paths,
            },
            "acceptance_criteria": [
                f"integration-plan-{safe}.md exists",
                "Plan includes feasibility assessment and effort estimate",
                "Risk assessment is documented",
            ],
            "parallel_next": False,
        })

        # When overlap is found, add adapter design milestone
        if found_any:
            adapter_title = f"Design integration interface for {name_list}"
            milestones.append({
                "order": 4,
                "required_capabilities": _synthesis_caps,
                "title": adapter_title,
                "detail": (
                    f"Design a shared integration interface between all "
                    f"{len(repo_names)} repositories: {name_list}. "
                    f"Based on the synthesis analysis:\n\n"
                    f"- Overlap kinds: {kind_text}\n"
                    f"- Descriptions: {desc_text}\n\n"
                    f"Design an adapter layer, shared library, or communication "
                    f"protocol that allows the projects to interoperate "
                    f"without deep coupling."
                ),
                "evidence": "goal",
                "task_type": "implementation",
                "symbolic": {
                    "op": "create_file",
                    "path": f"adapter-design-{safe}.md",
                    "content": (
                        f"# Adapter Design: {name_list}\n\n"
                        f"## Integration Approach\n"
                        f"- Overlap kinds: {kind_text}\n"
                        f"- Description: {desc_text}\n\n"
                        f"## Interface Specification\n"
                        f"Define the API surface of the integration.\n\n"
                        f"## Data Flow\n"
                        f"Describe how data moves between the systems.\n\n"
                        f"## Error Handling\n"
                        f"Define error categories and propagation strategy.\n\n"
                        f"## Testing Strategy\n"
                        f"Describe how the integration will be tested.\n\n"
                        f"## Rollout Plan\n"
                        f"How to safely introduce the integration.\n"
                    ),
                    "goal": adapter_title,
                    "repo_paths": repo_paths,
                },
                "acceptance_criteria": [
                    f"adapter-design-{safe}.md exists",
                    "Interface specification is documented",
                    "Data flow between systems is defined",
                ],
                "parallel_next": False,
            })

        return milestones

    # ── Internal: persist the compiled task graph ────────────────────

    def _persist_graph(self, graph, generated_at: str) -> None:
        """Persist the compiled TaskGraph to the DB.

        Mirrors TaskGraphEngine._persist() to avoid coupling to the engine.
        """
        graph_row = TaskGraphRow(
            id=graph.id, goal=graph.goal, plan_id=graph.plan_id,
            plan_type=graph.plan_type,
            task_count=len(graph.tasks), edge_count=len(graph.edges),
            critical_path_length=len(graph.critical_path),
            parallel_groups=graph.parallel_groups,
            status=graph.status,
            created_at=generated_at, updated_at=generated_at,
        )
        task_rows = []
        for t in graph.tasks:
            task_rows.append(TaskRow(
                id=t.id, graph_id=t.graph_id, plan_id=t.plan_id,
                milestone_order=t.milestone_order, title=t.title,
                description=t.description, task_type=t.task_type,
                required_capabilities=",".join(t.required_capabilities),
                complexity=t.complexity, priority=t.priority,
                estimated_effort=t.estimated_effort,
                dependencies=_dumps(t.dependencies),
                inputs=_dumps(t.inputs), outputs=_dumps(t.outputs),
                acceptance_criteria=_dumps(t.acceptance_criteria),
                verification=_dumps(t.verification),
                rollback=_dumps(t.rollback), evidence=_dumps(t.evidence),
                symbolic=_dumpd(t.symbolic), status=t.status,
                confidence=t.confidence, sequence=t.sequence,
            ))
        edge_rows = []
        for i, e in enumerate(graph.edges):
            edge_rows.append(TaskEdgeRow(
                id=f"{graph.id}#e{i}", graph_id=graph.id,
                from_task=e["from"], to_task=e["to"],
                kind=e.get("kind", "depends_on"),
            ))
        insert_task_graph(self.conn, [graph_row], task_rows, edge_rows)
