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

Design:
- Runs synthesis.synthesize() (structural overlap analysis)
- Builds evidence-backed integration milestones with real file paths
- Creates a Plan object with these milestones
- Compiles via compile_plan() (same deterministic compiler)
- Tags source='integration:<repo_a>/<repo_b>' for traceability
- Lands in review as 'proposal' (never auto-approves)
- Extension layer, no frozen modules modified
"""

from __future__ import annotations

import json as _json
from dataclasses import dataclass
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
)

from ..planning.compiler import compile_plan
from ..planning.models import Plan, PlanType, PlanConfidence, PlanStatus, now_iso
from ..synthesis import synthesize as run_synthesis


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


@dataclass
class IntegrateResult:
    """Outcome of an integration analysis + graph generation."""

    graph_id: Optional[str]
    repo_a: str
    repo_b: str
    overlap_found: bool
    overlap_kind: Optional[str]
    description: Optional[str]
    confidence: str
    basis: list[str]
    note: Optional[str]
    _goal_hint: str = ""

    def to_text(self) -> str:
        """Render the integration result to terminal text."""
        lines = [
            f"Integration: {self.repo_a} ↔ {self.repo_b}",
            "",
        ]
        if not self.overlap_found:
            lines.append("No meaningful structural overlap detected.")
            lines.append(f"Confidence: {self.confidence}")
            if self.note:
                lines.append(f"Note: {self.note}")
            if self.graph_id:
                lines.append("")
                lines.append("(A minimal graph was still generated for review.)")
        else:
            lines.append(f"Overlap detected: {self.overlap_kind or 'unspecified'}")
            lines.append(f"Confidence: {self.confidence}")
            if self.description:
                lines.append("")
                lines.append(self.description)
            if self.basis:
                lines.append("")
                lines.append("Basis:")
                for b in self.basis:
                    lines.append(f"  - {b}")
            if self.note:
                lines.append("")
                lines.append(f"Note: {self.note}")

        if self.graph_id:
            lines.append("")
            lines.append("── Next steps ──")
            lines.append(f"  Review graph:  friday graph review")
            lines.append(f"  Approve graph:  friday graph review approve {self.graph_id}")
            lines.append(f"  Execute goal:  friday execute \"{self._goal_hint or 'integration goal'}\"")
            lines.append("  (tip: use the full graph ID for approve/reject)")

        return "\n".join(lines) + "\n"


def _safe_repo_name(name: str) -> str:
    """Sanitize a repo name for use in file paths and IDs."""
    return name.replace(" ", "_").replace("/", "_").lower()


class IntegrationEngine:
    """Bridge between synthesis analysis and the execution pipeline.

    Builds evidence-backed integration tasks directly from synthesis findings,
    bypassing the generic template planner. Every task creates a meaningful file
    (analysis doc, patterns doc, feasibility plan, etc.).

    The caller owns the connection lifecycle.
    """

    def __init__(self, conn) -> None:
        self.conn = conn

    def integrate(self, repo_a: str, repo_b: str) -> IntegrateResult:
        """Run full integration analysis: synthesize -> evidence-backed graph.

        1. Runs structural overlap analysis (synthesis.synthesize)
        2. Builds integration-specific milestones from the evidence
        3. Creates a Plan + compiles into a TaskGraph
        4. Persists plan and graph with provenance tags
        5. Returns IntegrateResult with graph id and analysis

        The graph lands with status='proposal' (reviewable, non-executing) and
        source='integration:...', ready for review via `friday graph review`.
        """
        # Step 1: Run structural overlap analysis.
        synth = run_synthesis(self.conn, repo_a, repo_b)

        # Step 2: Build evidence-backed integration milestones.
        milestones = self._build_milestones(repo_a, repo_b, synth)

        # Step 3: Build a Plan with these milestones.
        goal = self._goal_for(repo_a, repo_b, synth)
        plan_id = f"plan:integrate:{_safe_repo_name(repo_a)}:{_safe_repo_name(repo_b)}"
        generated_at = now_iso()

        plan = Plan(
            id=plan_id,
            goal=goal,
            plan_type=PlanType.INTEGRATION,
            confidence=PlanConfidence.from_str(synth.confidence.lower()),
            status=PlanStatus.PLANNED,
            milestones=milestones,
            dependencies=[],
            risks=[],
            verification=[{"method": "acceptance",
                           "detail": "Each integration artifact exists and contains the required analysis."}],
            rollback=[{"strategy": "review",
                       "detail": "Integration graph is proposal-only; can be rejected without executing"}],
            estimated_complexity="medium" if len(milestones) <= 3 else "large",
            estimated_effort="medium",
            created_at=generated_at,
            updated_at=generated_at,
        )

        # Step 4: Compile into a Task Graph (reuses the same deterministic
        # compiler used by the normal planning path).
        graph = compile_plan(plan, generated_at=generated_at)
        graph.status = "proposal"

        # Override the auto-generated graph id with a traceable integration id.
        graph.id = f"integration_graph:{_safe_repo_name(repo_a)}:{_safe_repo_name(repo_b)}"
        for t in graph.tasks:
            t.graph_id = graph.id

        # Step 5: Persist plan + graph.
        insert_plan(self.conn, [plan.to_row()])
        self._persist_graph(graph, generated_at)

        # Step 6: Tag provenance.
        source_tag = f"integration:{repo_a}/{repo_b}"
        update_task_graph_source(self.conn, graph.id, source_tag)
        update_task_graph_status(self.conn, graph.id, "proposal")

        return IntegrateResult(
            graph_id=graph.id,
            repo_a=repo_a,
            repo_b=repo_b,
            overlap_found=synth.overlap_found,
            overlap_kind=synth.overlap_kind,
            description=synth.description,
            confidence=synth.confidence,
            basis=synth.basis,
            note=synth.note,
            _goal_hint=goal[:120],
        )

    # ------------------------------------------------------------------
    # Internal: build integration-specific milestones from synthesis
    # ------------------------------------------------------------------

    @staticmethod
    def _goal_for(repo_a: str, repo_b: str, synth) -> str:
        """Build a goal string from the synthesis result."""
        if synth.overlap_found:
            return (
                f"Integrate {repo_a} and {repo_b}: "
                f"{synth.overlap_kind or 'integration'} — "
                f"{synth.description or 'Merge shared functionality.'}"
            )
        return (
            f"Explore integration between {repo_a} and {repo_b}: "
            f"Investigate potential shared patterns despite no "
            f"detected structural overlap."
        )

    def _repo_paths(self, repo_a: str, repo_b: str) -> tuple:
        """Look up the on-disk paths for both repos from the DB.

        Returns (path_a, path_b) or (None, None) if not found.
        The SynthesisExecutor reads actual source files from these paths
        to produce deeper analysis than what synthesis evidence provides.
        """
        repos = get_repositories(self.conn)
        path_a = None
        path_b = None
        for r in repos:
            if r.name == repo_a:
                path_a = r.path
            if r.name == repo_b:
                path_b = r.path
        return path_a, path_b

    def _build_milestones(self, repo_a: str, repo_b: str, synth) -> list[dict]:
        """Build evidence-backed integration milestones.

        Every milestone carries:
        - task_type: explicitly set so the compiler passes it through verbatim
        - symbolic: create_file op with a real file path and content direction
        - acceptance_criteria: what success looks like

        This ensures every task produces a real file with meaningful content,
        instead of the generic README the template planner would produce.
        """
        safe_a = _safe_repo_name(repo_a)
        safe_b = _safe_repo_name(repo_b)

        # Look up repo on-disk paths so the SynthesisExecutor can read
        # actual source files for deeper analysis content.
        repo_a_path, repo_b_path = self._repo_paths(repo_a, repo_b)

        basis_text = "\n".join(f"- {b}" for b in synth.basis) if synth.basis else "No specific basis recorded."
        desc_text = synth.description or "No specific description available."

        milestones = []

        # Common required_capabilities for all integration milestones: route
        # to the SynthesisExecutor (worker:synthesis) which calls the LLM to
        # generate proper analysis content instead of writing template stubs.
        _synthesis_caps = ["synthesis"]

        # Milestone 1: Comparative architecture analysis.
        # Analyzes both repos' structures, technologies, and patterns.
        milestones.append({
            "order": 1,
            "required_capabilities": _synthesis_caps,
            "title": f"Analyse {repo_a} and {repo_b} architectures",
            "detail": (
                f"Produce a detailed comparative analysis of {repo_a} and {repo_b}.\n\n"
                f"Synthesis finding:\n- Overlap: {synth.overlap_kind or 'none detected'}\n"
                f"- Description: {desc_text}\n"
                f"- Confidence: {synth.confidence}\n\n"
                f"Basis:\n{basis_text}\n\n"
                f"Analyse each repo's: architecture pattern, technology stack, "
                f"entry points, components, dependencies, data flow, testing strategy, "
                f"and deployment model. Identify specific integration points."
            ),
            "evidence": "goal",
            "task_type": "implementation",
            "symbolic": {
                "op": "create_file",
                "path": f"integration-analysis-{safe_a}-{safe_b}.md",
                "content": (
                    f"# Integration Analysis: {repo_a} ↔ {repo_b}\n\n"
                    f"## Synthesis Result\n"
                    f"- Overlap: {synth.overlap_kind or 'none detected'}\n"
                    f"- Confidence: {synth.confidence}\n"
                    f"- Description: {desc_text}\n\n"
                    f"## Basis\n{basis_text}\n\n"
                    f"## Architecture Comparison\n\n"
                    f"Analyse each repo's architecture pattern, technology stack, "
                    f"entry points, components, dependencies, data flow, "
                    f"testing strategy, and deployment model.\n\n"
                    f"### {repo_a}\n\n"
                    f"Document: architecture pattern, tech stack, components, "
                    f"entry points, dependencies, data flow, testing strategy.\n\n"
                    f"### {repo_b}\n\n"
                    f"Document: architecture pattern, tech stack, components, "
                    f"entry points, dependencies, data flow, testing strategy.\n\n"
                    f"## Technology Overlap\n\n"
                    f"Identify shared technologies, frameworks, languages.\n\n"
                    f"## Integration Candidates\n\n"
                    f"Based on the analysis above, propose specific integration "
                    f"points between the two codebases."
                ),
                "goal": f"Analyse {repo_a} and {repo_b} architectures for integration",
                "repo_paths": [repo_a_path, repo_b_path],
            },
            "acceptance_criteria": [
                f"integration-analysis-{safe_a}-{safe_b}.md exists",
                "File documents both repos' architectures",
                "Integration points are identified and assessed",
            ],
            "parallel_next": False,
        })

        # Milestone 2: Document shared patterns and divergences.
        milestones.append({
            "order": 2,
            "required_capabilities": _synthesis_caps,
            "title": f"Document shared patterns between {repo_a} and {repo_b}",
            "detail": (
                f"Document the architectural and code-level patterns shared between "
                f"{repo_a} and {repo_b}, as well as key divergences.\n\n"
                f"Focus on: shared technologies, similar component structures, "
                f"common patterns (CLI argument parsing, configuration files, "
                f"LLM interfaces, authentication, testing approach), "
                f"and areas where the two projects differ significantly."
            ),
            "evidence": "goal",
            "task_type": "documentation",
            "symbolic": {
                "op": "create_file",
                "path": f"shared-patterns-{safe_a}-{safe_b}.md",
                "content": (
                    f"# Shared Patterns: {repo_a} ↔ {repo_b}\n\n"
                    f"## Overview\n"
                    f"{desc_text}\n\n"
                    f"## Synthesis Basis\n"
                    f"{basis_text}\n\n"
                    f"## Shared Technologies\n"
                    f"Inventory all technologies used by both projects: "
                    f"languages, frameworks, libraries, databases, "
                    f"infrastructure tools, and CI/CD systems.\n\n"
                    f"## Similar Architectural Patterns\n"
                    f"Analyse architectural patterns present in both codebases. "
                    f"Consider: module/package structure, CLI design patterns, "
                    f"configuration loading, plugin systems, test organization, "
                    f"error handling patterns, logging strategies, "
                    f"and data persistence approaches.\n\n"
                    f"## Key Divergences\n"
                    f"Identify areas where the two projects differ "
                    f"significantly in approach, technology choice, "
                    f"or architecture. Note whether these divergences "
                    f"complicate or simplify integration.\n\n"
                    f"## Reuse Opportunities\n"
                    f"Based on the shared patterns identified, recommend "
                    f"specific code or patterns that could be extracted "
                    f"into a shared library or module.\n"
                ),
                "goal": f"Document shared patterns between {repo_a} and {repo_b}",
                "repo_paths": [repo_a_path, repo_b_path],
            },
            "acceptance_criteria": [
                f"shared-patterns-{safe_a}-{safe_b}.md exists",
                "File documents shared technologies and patterns",
                "Integration opportunities are identified",
            ],
            "parallel_next": False,
        })

        # Milestone 3: Integration feasibility plan.
        milestones.append({
            "order": 3,
            "required_capabilities": _synthesis_caps,
            "title": f"Assess integration feasibility for {repo_a} and {repo_b}",
            "detail": (
                f"Produce an integration feasibility assessment for {repo_a} "
                f"and {repo_b}. Evaluate the effort, risk, and value of "
                f"integrating the two projects.\n\n"
                f"Synthesis finding: {desc_text}\n"
                f"Basis:\n{basis_text}\n\n"
                f"Include: integration strategy options, estimated effort, "
                f"risk assessment, dependency analysis, and a phased migration "
                f"plan if integration is recommended."
            ),
            "evidence": "goal",
            "task_type": "documentation",
            "symbolic": {
                "op": "create_file",
                "path": f"integration-plan-{safe_a}-{safe_b}.md",
                "content": (
                    f"# Integration Plan: {repo_a} ↔ {repo_b}\n\n"
                    f"## Feasibility Assessment\n"
                    f"- Overlap detected: {synth.overlap_found}\n"
                    f"- Kind: {synth.overlap_kind or 'none'}\n"
                    f"- Confidence: {synth.confidence}\n"
                    f"- Description: {desc_text}\n\n"
                    f"## Basis for Assessment\n"
                    f"{basis_text}\n\n"
                    f"## Strategy Options\n"
                    f"Evaluate these integration strategies and recommend one:\n"
                    f"1. **Shared library** — Extract common code into a "
                    f"separate package both projects depend on.\n"
                    f"2. **Adapter layer** — Build a thin integration layer "
                    f"that translates between the two codebases.\n"
                    f"3. **Communication protocol** — Define an API or message "
                    f"format for runtime interop.\n"
                    f"4. **Merge** — Combine both projects into a single "
                    f"codebase with unified architecture.\n\n"
                    f"## Effort Estimate\n"
                    f"Break down the effort by phase: discovery, design, "
                    f"implementation, testing, rollout. Include person-weeks "
                    f"or story-point ranges for each phase.\n\n"
                    f"## Risk Assessment\n"
                    f"Identify risks: compatibility issues, breaking changes, "
                    f"dependency conflicts, performance impact, "
                    f"and maintenance burden after integration.\n"
                    f"Rate each risk (Low/Medium/High) and propose "
                    f"mitigations.\n\n"
                    f"## Phased Migration Plan\n"
                    f"Describe a phased approach: what gets built first, "
                    f"what can be incremental, and what the final state "
                    f"looks like. Include validation gates at each phase.\n\n"
                    f"## Success Criteria\n"
                    f"Define measurable criteria that indicate the "
                    f"integration is successful: e.g., all tests pass, "
                    f"no regressions, performance meets thresholds.\n"
                ),
                "goal": f"Assess integration feasibility for {repo_a} and {repo_b}",
                "repo_paths": [repo_a_path, repo_b_path],
            },
            "acceptance_criteria": [
                f"integration-plan-{safe_a}-{safe_b}.md exists",
                "Plan includes feasibility assessment and effort estimate",
                "Risk assessment is documented",
            ],
            "parallel_next": False,
        })

        # When overlap is found, add concrete integration design + prototype tasks.
        if synth.overlap_found:
            milestones.append({
                "order": 4,
                "required_capabilities": _synthesis_caps,
                "title": f"Design integration interface for {repo_a} and {repo_b}",
                "detail": (
                    f"Design a shared integration interface between {repo_a} "
                    f"and {repo_b}. Based on the synthesis analysis:\n\n"
                    f"- Overlap kind: {synth.overlap_kind}\n"
                    f"- Description: {desc_text}\n\n"
                    f"Design an adapter layer, shared library, or communication "
                    f"protocol that allows the two projects to interoperate "
                    f"without deep coupling."
                ),
                "evidence": "goal",
                "task_type": "implementation",
            "symbolic": {
                "op": "create_file",
                "path": f"adapter-design-{safe_a}-{safe_b}.md",
                "content": (
                    f"# Adapter Design: {repo_a} ↔ {repo_b}\n\n"
                    f"## Integration Approach\n"
                    f"- Overlap kind: {synth.overlap_kind}\n"
                    f"- Description: {desc_text}\n\n"
                    f"Choose and justify one integration strategy:\n"
                    f"1. **Shared library** - Extract common code into a "
                    f"separate package.\n"
                    f"2. **Adapter layer** - Build a thin integration layer.\n"
                    f"3. **Communication protocol** - Define an API or message "
                    f"format.\n"
                    f"4. **Plugin system** - Make one a plugin of the other.\n\n"
                    f"## Interface Specification\n"
                    f"Define the API surface of the integration: function "
                    f"signatures, class interfaces, configuration schemas, "
                    f"data formats, and error types. Include example usage.\n\n"
                    f"## Data Flow\n"
                    f"Describe how data moves between the two systems. "
                    f"Include: data formats, serialization, validation, "
                    f"and lifecycle management. Diagram the flow with text.\n\n"
                    f"## Error Handling\n"
                    f"Define error categories, propagation strategy, "
                    f"retry logic, and circuit-breaking behaviour for "
                    f"the integration layer.\n\n"
                    f"## Testing Strategy\n"
                    f"Describe how the integration will be tested: unit tests, "
                    f"integration tests, contract tests, end-to-end tests. "
                    f"Define the test environment and CI integration.\n\n"
                    f"## Rollout Plan\n"
                    f"How to safely introduce the integration: feature flags, "
                    f"canary deployments, backward compatibility guarantees, "
                    f"rollback procedures, and monitoring.\n"
                ),
                "goal": f"Design integration interface for {repo_a} and {repo_b}",
                "repo_paths": [repo_a_path, repo_b_path],
            },
                "acceptance_criteria": [
                    f"adapter-design-{safe_a}-{safe_b}.md exists",
                    "Interface specification is documented",
                    "Data flow between systems is defined",
                ],
                "parallel_next": False,
            })

        return milestones

    # ------------------------------------------------------------------
    # Internal: persist the compiled task graph
    # ------------------------------------------------------------------

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
