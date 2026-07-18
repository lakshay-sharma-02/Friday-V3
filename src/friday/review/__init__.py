"""Review subsystem (Milestone 9.6).

Answer: \"Is this engineering work actually good?\" — deterministically.

Review is SYNTHESIS, not generation. It composes evidence already produced by
the frozen lower layers (identity, portfolio, knowledge, understanding, insight,
strategy, planning, graph, runtime) and turns it into a structured verdict.

Design constraints (frozen architecture):
- No new managers / engines / planners / contexts / registries / databases.
- No duplicated logic — every datum here is read from an existing module.
- No invented scores. Findings cite the evidence they come from.
- Deterministic except for the final LLM language step (not built here;
  this module returns structured reports the CLI renders verbatim).

Each reviewer returns a `ReviewReport`: a verdict band (good / fair / weak /
unknown), a confidence band, strengths, weaknesses (each tied to evidence),
risks, and recommendations (each tied to evidence).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import List, Optional

from .. import query as q
from ..db import (
    ArchitectureRow,
    TaskGraphRow,
    get_all_relationships,
    get_architecture,
    get_components,
    get_entry_points,
    get_languages,
    get_runtime_sessions,
    get_runtime_tasks,
    get_technologies,
)
from ..identity import build_identity
from ..planning import PlanEngine, TaskGraphEngine
from ..portfolio import (
    detect_themes,
    engineering_universe,
    integration_opportunities,
    meaningful_overlap,
    project_value_ranking,
    workspace_recommendations,
)
from ..resolver import CapabilityResolver
from ..runtime.models import RunState

# Verdict + confidence bands (reused across every reviewer).
VERDICTS = ("good", "fair", "weak", "unknown")
CONFIDENCES = ("strong", "medium", "weak")


@dataclass
class Finding:
    """One evidence-backed observation. Never invented — `evidence` cites it."""

    label: str
    detail: str
    evidence: str  # where this came from (module/field), never empty


@dataclass
class ReviewReport:
    """Structured review verdict. Rendered verbatim by the CLI."""

    scope: str                 # human label, e.g. "Workspace" / "project: Friday"
    verdict: str = "unknown"   # good | fair | weak | unknown
    confidence: str = "weak"   # strong | medium | weak
    summary: str = ""
    strengths: List[Finding] = field(default_factory=list)
    weaknesses: List[Finding] = field(default_factory=list)
    risks: List[Finding] = field(default_factory=list)
    recommendations: List[Finding] = field(default_factory=list)

    def to_text(self) -> str:
        lines = [f"Review — {self.scope}", ""]
        lines.append(f"Overall:     {self.verdict.title()}")
        lines.append(f"Confidence:  {self.confidence.title()}")
        if self.summary:
            lines.append("")
            lines.append(self.summary)
        lines += _section("Strengths", self.strengths)
        lines += _section("Weaknesses", self.weaknesses)
        lines += _section("Risks", self.risks)
        lines += _section("Recommendations", self.recommendations)
        return "\n".join(lines).rstrip() + "\n"

    def to_dict(self) -> dict:
        def _f(fs: List[Finding]) -> list:
            return [{"label": f.label, "detail": f.detail, "evidence": f.evidence}
                    for f in fs]
        return {
            "scope": self.scope, "verdict": self.verdict,
            "confidence": self.confidence, "summary": self.summary,
            "strengths": _f(self.strengths), "weaknesses": _f(self.weaknesses),
            "risks": _f(self.risks), "recommendations": _f(self.recommendations),
        }


def _section(title: str, items: List[Finding]) -> List[str]:
    out = ["", title + ":"]
    if not items:
        out.append("  (none identified)")
    for f in items:
        out.append(f"  - {f.label}")
        out.append(f"      {f.detail}")
        out.append(f"      evidence: {f.evidence}")
    return out


def _verdict(rank: int) -> str:
    """Map a 0..3 strength count to a verdict band."""
    return {0: "weak", 1: "fair", 2: "fair", 3: "good"}.get(rank, "unknown")


def _confidence(strong: int, total: int) -> str:
    if total == 0:
        return "weak"
    ratio = strong / total
    if ratio >= 0.6:
        return "strong"
    if ratio >= 0.3:
        return "medium"
    return "weak"


def _norm_conf(c: str) -> str:
    """Normalize any confidence token to the lowercase band vocabulary."""
    return (c or "weak").strip().lower()


# ===========================================================================
# Workspace review — composes portfolio + knowledge/understanding/insight health
# ===========================================================================

class ReviewEngine:
    """Composes evidence from existing modules into structured reviews.

    Stateless across calls except for the connection it holds. The four public
    methods map 1:1 to the `friday review ...` subcommands.
    """

    def __init__(self, conn) -> None:
        self.conn = conn

    # --- workspace ----------------------------------------------------------

    def workspace(self) -> ReviewReport:
        repos = q.all_repositories(self.conn)
        ranking = project_value_ranking(self.conn)
        rec = workspace_recommendations(self.conn)
        universe = engineering_universe(self.conn)
        overlaps = meaningful_overlap(self.conn)
        themes = detect_themes(self.conn)
        integ = integration_opportunities(self.conn)
        inactive = q.inactive_repos(self.conn, dt.date.today())
        today = dt.date.today()
        undocumented = [r for r in repos
                        if not r.readme_summary
                        or r.readme_quality in ("none", "boilerplate", "poor")]

        strengths: List[Finding] = []
        weaknesses: List[Finding] = []
        risks: List[Finding] = []
        recs: List[Finding] = []

        if ranking:
            top = ranking[0]
            strengths.append(Finding(
                f"Strongest project by evidence: {top.repo}",
                f"value score {top.score:.1f}; "
                f"{'; '.join(top.signals) or 'no signals'}",
                "portfolio.project_value_ranking"))
        if len(ranking) >= 3:
            strengths.append(Finding(
                "Multiple projects with ranked value",
                f"{len(ranking)} projects carry enough evidence to rank.",
                "portfolio.project_value_ranking"))
        if themes:
            strengths.append(Finding(
                f"Recurring theme: {themes[0].theme}",
                f"spans {len(themes[0].repos)} projects "
                f"({themes[0].confidence} confidence).",
                "portfolio.detect_themes"))
        if universe:
            strengths.append(Finding(
                "Engineering direction is observable",
                f"{len(universe)} workspace-wide observation(s) available.",
                "portfolio.engineering_universe"))

        if not repos:
            weaknesses.append(Finding(
                "No projects ingested", "Workspace knowledge base is empty.",
                "query.all_repositories (empty)"))
        if undocumented:
            weaknesses.append(Finding(
                f"{len(undocumented)} project(s) poorly/undocumented",
                ", ".join(r.name for r in undocumented[:8])
                + ("…" if len(undocumented) > 8 else ""),
                "repository.readme_quality"))
        if inactive:
            weaknesses.append(Finding(
                f"{len(inactive)} inactive project(s) (>90d since last commit)",
                ", ".join(r.name for r in inactive[:8])
                + ("…" if len(inactive) > 8 else ""),
                "query.inactive_repos"))
        if not ranking:
            weaknesses.append(Finding(
                "No project carries enough evidence to rank",
                "Purpose/architecture not yet recovered for any project.",
                "portfolio.project_value_ranking (empty)"))

        # Convergence / portfolio-quality risks.
        if len(overlaps) >= 3:
            risks.append(Finding(
                "Significant duplicated effort across projects",
                f"{len(overlaps)} meaningful overlaps detected; "
                f"consider consolidation.",
                "portfolio.meaningful_overlap"))
        if integ:
            risks.append(Finding(
                "Integration opportunities not yet acted on",
                f"{len(integ)} project(s) could integrate with Friday "
                f"(top: {integ[0].repo}).",
                "portfolio.integration_opportunities"))

        for name, why in rec.pause_projects[:5]:
            recs.append(Finding(
                f"Revisit/pause: {name}", why,
                "portfolio.workspace_recommendations"))
        if rec.continue_projects:
            recs.append(Finding(
                f"Prioritize: {rec.continue_projects[0][0]}",
                rec.continue_projects[0][1],
                "portfolio.workspace_recommendations"))
        if undocumented:
            recs.append(Finding(
                "Improve documentation for low-quality READMEs",
                "Run `friday analyze <path>` after adding purpose/features "
                "sections to recover identity.",
                "repository.readme_quality"))

        strength = 0
        if ranking:
            strength += 1
        if themes or len(ranking) >= 3:
            strength += 1
        if not weaknesses:
            strength += 1
        elif not undocumented and not inactive:
            strength += 1

        summary = (f"{len(repos)} project(s); {len(ranking)} ranked; "
                   f"{len(inactive)} inactive; {len(undocumented)} undocumented.")
        return ReviewReport(
            scope="Workspace",
            verdict=_verdict(min(strength, 3)),
            confidence=_norm_conf(rec.confidence),
            summary=summary,
            strengths=strengths, weaknesses=weaknesses,
            risks=risks, recommendations=recs,
        )

    # --- project ------------------------------------------------------------

    def project(self, name: str) -> Optional[ReviewReport]:
        repo = q.repo_by_name(self.conn, name)
        if repo is None:
            return None
        ident = build_identity(self.conn, repo.id) if repo.id is not None else None
        arch = get_architecture(self.conn, repo.id) if repo.id is not None else None
        comps = get_components(self.conn, repo.id) if repo.id is not None else []
        eps = get_entry_points(self.conn, repo.id) if repo.id is not None else []
        techs = get_technologies(self.conn, repo.id) if repo.id is not None else []
        langs = get_languages(self.conn, repo.id) if repo.id is not None else []
        rels = get_all_relationships(self.conn)
        d = q._parse_date(repo.last_commit_date)
        days_since = (dt.date.today() - d).days if d else None
        card = q.identity_card(self.conn, repo.id, dt.date.today()) \
            if repo.id is not None else None

        strengths: List[Finding] = []
        weaknesses: List[Finding] = []
        risks: List[Finding] = []
        recs: List[Finding] = []

        # Documentation
        rq = repo.readme_quality
        if rq in ("good",) or (ident and ident.purpose_confidence == "High"):
            strengths.append(Finding(
                "Purpose clearly documented",
                (ident.purpose if ident and ident.purpose else "README present")
                + f" (purpose confidence: {ident.purpose_confidence if ident else 'n/a'})",
                "identity.build_identity / repository.readme_quality"))
        elif rq in ("none", "boilerplate", "poor"):
            weaknesses.append(Finding(
                "Documentation is thin or missing",
                f"README quality classified as '{rq}'.",
                "repository.readme_quality"))

        # Architecture
        if arch and arch.architecture and arch.architecture != "Unknown":
            strengths.append(Finding(
                "Architecture recovered",
                f"{arch.architecture} (confidence: {arch.confidence or 'n/a'}); "
                f"{len(comps)} components; "
                f"{len([e for e in eps if e.kind in ('FastAPI app','Flask app','main()','CLI','Next.js app')])} app entry point(s).",
                "db.get_architecture / get_components / get_entry_points"))
        else:
            weaknesses.append(Finding(
                "Architecture not recovered",
                "No architecture label, components, or entry points stored.",
                "db.get_architecture (empty)"))

        # Maintainability / complexity (from architecture.architecture label only)
        if arch and arch.complexity:
            risks.append(Finding(
                "Architecture complexity flagged",
                f"architecture complexity: {arch.complexity}",
                "db.get_architecture.complexity"))

        # Testing — entry points of type testing capability inference not stored;
        # use component/tech signal: presence of a test framework in techs.
        test_tech = [t.tech for t in techs
                     if t.tech.lower() in ("pytest", "jest", "cargo-test", "unittest", "testing")]
        if test_tech:
            strengths.append(Finding(
                "Testing tooling present",
                ", ".join(test_tech) + ".",
                "db.get_technologies"))
        else:
            weaknesses.append(Finding(
                "No testing framework detected",
                "No test framework found among declared technologies.",
                "db.get_technologies"))

        # Activity
        activity = card.activity if card else "Unknown"
        if activity in ("Very active", "Active"):
            strengths.append(Finding(
                "Active development",
                f"last commit {days_since} days ago; activity: {activity}.",
                "query.identity_card.activity"))
        elif activity in ("Dormant", "Stale", "Inactive"):
            weaknesses.append(Finding(
                "Project appears inactive",
                f"last commit {days_since} days ago; activity: {activity}.",
                "query.identity_card.activity"))
            risks.append(Finding(
                "Decay risk",
                "Stale project may accumulate technical debt / bit-rot.",
                "query.identity_card.activity"))

        # Confidence
        if ident and ident.purpose_confidence in ("High", "Medium"):
            strengths.append(Finding(
                "Identity confidence is reasonable",
                f"purpose confidence: {ident.purpose_confidence}.",
                "identity.build_identity.purpose_confidence"))
        else:
            weaknesses.append(Finding(
                "Low identity confidence",
                "Purpose could not be confidently recovered from evidence.",
                "identity.build_identity.purpose_confidence"))

        conf_basis = sum(1 for f in strengths if f.evidence)
        verdict = _verdict(min(len(strengths), 3))
        if weaknesses and not strengths:
            verdict = "weak"
        confidence = _confidence(
            sum(1 for f in strengths if "High" in f.detail or "good" in f.detail),
            max(conf_basis, 1))

        recs.append(Finding(
            "Re-run evidence if stale",
            "Run `friday observe` then `friday analyze <path>` to refresh "
            "identity/architecture.",
            "friday.observe / friday.analyze"))

        summary = (f"maturity: {repo.maturity or 'unknown'}; "
                   f"activity: {activity}; "
                   f"readme: {rq or 'unknown'}; "
                   f"purpose confidence: "
                   f"{ident.purpose_confidence if ident else 'n/a'}.")
        return ReviewReport(
            scope=f"project: {repo.name}",
            verdict=verdict, confidence=confidence, summary=summary,
            strengths=strengths, weaknesses=weaknesses,
            risks=risks, recommendations=recs,
        )

    # --- plan ---------------------------------------------------------------

    def plan(self, goal: str) -> Optional[ReviewReport]:
        eng = PlanEngine(self.conn)
        pid = f"plan:{goal.strip().strip('\"').strip().lower()}"
        p = eng.plan_by_id(pid)
        if p is None:
            return None

        strengths: List[Finding] = []
        weaknesses: List[Finding] = []
        risks: List[Finding] = []
        recs: List[Finding] = []

        ev = p.evidence_count()
        # Missing milestones?
        if p.milestone_count == 0:
            weaknesses.append(Finding(
                "No milestones defined",
                "Plan has zero milestones — nothing to execute or verify.",
                "planning.Plan.milestones (empty)"))
        else:
            strengths.append(Finding(
                f"{p.milestone_count} milestone(s) defined",
                f"type={p.plan_type.value}, status={p.status.value}.",
                "planning.Plan.milestones"))

        # Duplicated work?
        titles = [m.get("title", "").strip().lower() for m in p.milestones]
        dupes = {t for t in titles if titles.count(t) > 1}
        if dupes:
            weaknesses.append(Finding(
                "Duplicated milestones",
                ", ".join(sorted(dupes)),
                "planning.Plan.milestones (duplicate titles)"))

        # Missing verification?
        if p.verification_count == 0:
            weaknesses.append(Finding(
                "No verification defined",
                "Plan does not state how completion is verified.",
                "planning.Plan.verification (empty)"))
        else:
            strengths.append(Finding(
                f"{p.verification_count} verification step(s)",
                "Completion criteria are stated.",
                "planning.Plan.verification"))

        # Acceptance criteria (per-milestone acceptance in plan text not stored
        # separately; rely on verification + risks presence).
        # Impossible dependencies?
        kinds = {d.get("kind") for d in p.dependencies}
        if p.dependency_count == 0 and p.milestone_count > 1:
            weaknesses.append(Finding(
                "No dependencies between milestones",
                "Multi-milestone plan with no ordering/dependencies declared.",
                "planning.Plan.dependencies (empty)"))
        elif p.dependency_count > 0:
            strengths.append(Finding(
                f"{p.dependency_count} dependency/ies declared",
                "Milestones are ordered.",
                "planning.Plan.dependencies"))

        # Acceptance criteria covered? (verification + rollback presence proxy)
        if p.rollback_count == 0 and p.plan_type.value in (
                "infrastructure", "migration", "architecture", "release"):
            risks.append(Finding(
                "No rollback strategy for a risky plan type",
                f"plan type '{p.plan_type.value}' but rollback=0.",
                "planning.Plan.rollback (empty)"))

        # Evidence coverage
        if ev == 0:
            weaknesses.append(Finding(
                "No supporting evidence cited",
                "Plan cites 0 initiatives/insights/understanding/knowledge.",
                "planning.Plan.evidence_count (0)"))
            risks.append(Finding(
                "Low-confidence plan",
                "Plan is not grounded in any lower-layer evidence.",
                "planning.Plan.confidence " + p.confidence.value))
        else:
            strengths.append(Finding(
                f"Grounded in {ev} evidence item(s)",
                f"{p.initiative_count} initiative, {p.insight_count} insight, "
                f"{p.understanding_count} understanding, {p.knowledge_count} knowledge.",
                "planning.Plan.evidence_count"))

        if p.confidence.value == "weak":
            risks.append(Finding(
                "Weak plan confidence",
                "Evidence reinforcement is thin; reconsider before execution.",
                "planning.Plan.confidence"))

        # Acceptance-criteria coverage recommendation
        recs.append(Finding(
            "Ensure every milestone has acceptance criteria",
            "Map each milestone to a verification method before scheduling.",
            "planning.Plan.verification"))
        if ev == 0:
            recs.append(Finding(
                "Build evidence before relying on this plan",
                "Run understanding/insight/knowledge build steps first.",
                "friday.understanding build / insights build"))

        verdict = _verdict(min(len(strengths), 3))
        if p.milestone_count == 0:
            verdict = "weak"
        confidence = p.confidence.value
        summary = (f"goal: {p.goal}; type: {p.plan_type.value}; "
                   f"status: {p.status.value}; evidence: {ev}.")
        return ReviewReport(
            scope=f"plan: {p.goal}",
            verdict=verdict, confidence=confidence, summary=summary,
            strengths=strengths, weaknesses=weaknesses,
            risks=risks, recommendations=recs,
        )

    # --- graph --------------------------------------------------------------

    def graph(self, gid: str) -> Optional[ReviewReport]:
        from ..planning.graph_schema import _detect_cycle
        from ..db import (
            get_edges_for_graph, get_tasks_for_graph, get_task_graph_by_id,
        )
        row = get_task_graph_by_id(self.conn, gid)
        if row is None:
            return None
        tasks = get_tasks_for_graph(self.conn, gid)
        edges = [{"from": e.from_task, "to": e.to_task, "kind": e.kind}
                 for e in get_edges_for_graph(self.conn, gid)]
        ids = {t.id for t in tasks}

        strengths: List[Finding] = []
        weaknesses: List[Finding] = []
        risks: List[Finding] = []
        recs: List[Finding] = []

        # Cycles (the impossible-graph case). _detect_cycle is cycle-safe.
        cyclic = _detect_cycle(edges, list(ids)) if edges or ids else False
        if cyclic:
            risks.append(Finding(
                "Graph contains a cycle",
                "Dependency cycle makes the graph unexecutable.",
                "planning.graph_schema._detect_cycle"))
            weaknesses.append(Finding(
                "Acyclic contract violated",
                "Stored graph failed cycle detection on review.",
                "planning.graph_schema._detect_cycle"))
        else:
            strengths.append(Finding(
                "Graph is acyclic",
                f"{len(edges)} dependency edge(s), no cycle.",
                "planning.graph_schema._detect_cycle"))

        # Disconnected nodes
        connected = {e["from"] for e in edges} | {e["to"] for e in edges}
        disconnected = [t.id for t in tasks if t.id not in connected]
        if disconnected:
            weaknesses.append(Finding(
                f"{len(disconnected)} disconnected node(s)",
                "Tasks with no dependencies and no dependents.",
                "planning.TaskGraph.edges"))

        # Dangling edges
        for e in edges:
            if e["from"] not in ids or e["to"] not in ids:
                weaknesses.append(Finding(
                    "Dangling edge",
                    f"{e['from']} -> {e['to']} references an unknown task.",
                    "planning.TaskGraph.edges"))

        # Parallelism + critical path (only meaningful when acyclic).
        if not cyclic:
            # Recompute levels/critical path defensively (cycle-safe helpers).
            from ..planning.compiler import (
                _compute_levels, _critical_path, _parallel_groups)
            levels = _compute_levels(edges, list(ids))
            cpath = _critical_path(edges, tasks) if tasks else []
            groups, ptasks = _parallel_groups(levels)
            if groups > 0:
                strengths.append(Finding(
                    f"{groups} parallel group(s) ({len(ptasks)} tasks)",
                    "Tasks can be executed concurrently, improving throughput.",
                    "planning.compiler._parallel_groups"))
            if cpath:
                strengths.append(Finding(
                    f"Critical path of {len(cpath)} task(s)",
                    "Longest dependency chain identified.",
                    "planning.compiler._critical_path"))

        # Unnecessary / missing-acceptance tasks. TaskRow stores JSON *strings*,
        # so parse before testing emptiness.
        def _nonempty(js: str) -> bool:
            try:
                return bool(__import__("json").loads(js or "[]"))
            except (ValueError, TypeError):
                return bool(js)

        for t in tasks:
            accepted = _nonempty(t.acceptance_criteria)
            verified = _nonempty(t.verification)
            if (t.id not in connected) and not accepted:
                weaknesses.append(Finding(
                    f"Unnecessary task: {t.title}",
                    "No dependencies, no dependents, no acceptance criteria.",
                    "planning.Task.acceptance_criteria (empty)"))
            elif not accepted:
                weaknesses.append(Finding(
                    f"Task without acceptance criteria: {t.title}",
                    "No acceptance criteria defined.",
                    "planning.Task.acceptance_criteria (empty)"))
            if not verified:
                weaknesses.append(Finding(
                    f"Task without verification: {t.title}",
                    "No verification method defined.",
                    "planning.Task.verification (empty)"))

        verdict = _verdict(min(len(strengths), 3))
        if cyclic:
            verdict = "weak"
        elif disconnected or any(not t.acceptance_criteria for t in tasks):
            verdict = "fair" if strengths else "weak"
        confidence = "strong" if ids else "weak"
        summary = (f"{len(tasks)} tasks, {len(edges)} edges, "
                   f"critical path {len(cpath) if not cyclic else 0}, "
                   f"{groups if not cyclic else 0} parallel groups.")
        return ReviewReport(
            scope=f"graph: {row.id}",
            verdict=verdict, confidence=confidence, summary=summary,
            strengths=strengths, weaknesses=weaknesses,
            risks=risks, recommendations=recs,
        )

    # --- runtime ------------------------------------------------------------

    def runtime(self, session_id: str) -> Optional[ReviewReport]:
        sessions = get_runtime_sessions(self.conn)
        sess = next((s for s in sessions if s["session_id"] == session_id), None)
        if sess is None:
            return None
        tasks = get_runtime_tasks(self.conn, session_id)

        strengths: List[Finding] = []
        weaknesses: List[Finding] = []
        risks: List[Finding] = []
        recs: List[Finding] = []

        total = len(tasks)
        succeeded = [t for t in tasks if t["status"] == RunState.SUCCESS.value]
        failed = [t for t in tasks if t["status"] == RunState.FAILED.value]
        cancelled = [t for t in tasks if t["status"] == RunState.CANCELLED.value]
        workers = sorted({t["worker_id"] for t in tasks if t["worker_id"]})

        if total == 0:
            weaknesses.append(Finding(
                "No tasks recorded for session",
                "Session exists but executed nothing.",
                "db.get_runtime_tasks (empty)"))
        if succeeded:
            strengths.append(Finding(
                f"{len(succeeded)}/{total} task(s) succeeded",
                "Execution completed as scheduled.",
                "db.get_runtime_tasks.status=success"))
        if failed:
            failures = "; ".join(t["task_id"] for t in failed[:6])
            weaknesses.append(Finding(
                f"{len(failed)} task(s) failed",
                failures,
                "db.get_runtime_tasks.status=failed"))
            risks.append(Finding(
                "Failure cascaded to dependents",
                f"{len(cancelled)} task(s) cancelled (blocked descendants).",
                "db.get_runtime_tasks.status=cancelled"))
        if cancelled and not failed:
            weaknesses.append(Finding(
                f"{len(cancelled)} task(s) cancelled",
                "Cancelled without a recorded failure — check scheduling/assignment.",
                "db.get_runtime_tasks.status=cancelled"))

        # Worker utilization
        if workers:
            strengths.append(Finding(
                f"{len(workers)} worker(s) utilized",
                ", ".join(workers),
                "db.get_runtime_tasks.worker_id"))
        else:
            weaknesses.append(Finding(
                "No workers utilized",
                "No task was assigned a worker.",
                "db.get_runtime_tasks.worker_id (all empty)"))

        # Bottlenecks: longest-duration succeeded/failed task
        timed = [t for t in tasks
                 if t.get("duration_ms") and isinstance(t["duration_ms"], int)]
        if timed:
            slowest = max(timed, key=lambda t: t["duration_ms"])
            risks.append(Finding(
                "Slowest task is a bottleneck",
                f"{slowest['task_id']} took {slowest['duration_ms']}ms.",
                "db.get_runtime_tasks.duration_ms"))
            recs.append(Finding(
                "Profile the bottleneck task",
                f"Investigate {slowest['task_id']} for optimization.",
                "db.get_runtime_tasks.duration_ms"))

        if failed:
            recs.append(Finding(
                "Fix failing tasks, then re-run",
                "Address failures before declaring the goal complete.",
                "db.get_runtime_tasks.status=failed"))

        verdict = _verdict(min(len(strengths), 3))
        if failed:
            verdict = "weak" if len(failed) >= len(succeeded) else "fair"
        confidence = "strong" if total else "weak"
        summary = (f"{total} tasks; {len(succeeded)} succeeded; "
                   f"{len(failed)} failed; {len(cancelled)} cancelled; "
                   f"{len(workers)} workers used.")
        return ReviewReport(
            scope=f"runtime: {session_id}",
            verdict=verdict, confidence=confidence, summary=summary,
            strengths=strengths, weaknesses=weaknesses,
            risks=risks, recommendations=recs,
        )

    # --- portfolio ----------------------------------------------------------

    def portfolio(self) -> ReviewReport:
        repos = q.all_repositories(self.conn)
        ranking = project_value_ranking(self.conn)
        overlaps = meaningful_overlap(self.conn)
        themes = detect_themes(self.conn)
        rec = workspace_recommendations(self.conn)
        inactive = q.inactive_repos(self.conn, dt.date.today())
        undocumented = [r for r in repos
                        if not r.readme_summary
                        or r.readme_quality in ("none", "boilerplate", "poor")]

        strengths: List[Finding] = []
        weaknesses: List[Finding] = []
        risks: List[Finding] = []
        recs: List[Finding] = []

        if ranking:
            strengths.append(Finding(
                "Portfolio has ranked value",
                f"{len(ranking)} project(s) ranked; top: {ranking[0].repo} "
                f"({ranking[0].score:.1f}).",
                "portfolio.project_value_ranking"))
        if themes:
            strengths.append(Finding(
                f"Portfolio converges on a theme",
                f"'{themes[0].theme}' spans {len(themes[0].repos)} projects.",
                "portfolio.detect_themes"))

        # Too many inactive projects
        if repos and len(inactive) >= max(1, len(repos) // 2):
            weaknesses.append(Finding(
                f"Too many inactive projects ({len(inactive)}/{len(repos)})",
                "Over half the portfolio is stale.",
                "query.inactive_repos"))
        # Duplicated effort
        if len(overlaps) >= 3:
            weaknesses.append(Finding(
                "Duplicated effort across projects",
                f"{len(overlaps)} meaningful overlaps.",
                "portfolio.meaningful_overlap"))
            risks.append(Finding(
                "Consolidation opportunity",
                "Overlaps suggest redundant work that could be merged.",
                "portfolio.meaningful_overlap"))
        # Missing documentation
        if undocumented:
            weaknesses.append(Finding(
                f"{len(undocumented)} project(s) missing documentation",
                ", ".join(r.name for r in undocumented[:8]),
                "repository.readme_quality"))
        # Convergence
        if not themes and len(repos) >= 3:
            weaknesses.append(Finding(
                "No convergent theme",
                "Portfolio spans unrelated work with no shared direction.",
                "portfolio.detect_themes (empty)"))

        # Technical debt proxy: inactive + undocumented
        debt = [r for r in inactive if r in undocumented]
        if debt:
            risks.append(Finding(
                "Technical debt accumulating in stale/undocumented projects",
                ", ".join(r.name for r in debt[:8]),
                "query.inactive_repos + repository.readme_quality"))

        for name, why in rec.pause_projects[:5]:
            recs.append(Finding(f"Revisit: {name}", why,
                                "portfolio.workspace_recommendations"))
        if overlaps:
            recs.append(Finding(
                "Consolidate overlapping projects",
                "Merge or share code for detected overlaps.",
                "portfolio.meaningful_overlap"))

        verdict = _verdict(min(len(strengths), 3))
        if weaknesses and not strengths:
            verdict = "weak"
        confidence = _norm_conf(rec.confidence)
        summary = (f"{len(repos)} projects; {len(ranking)} ranked; "
                   f"{len(overlaps)} overlaps; {len(inactive)} inactive.")
        return ReviewReport(
            scope="Portfolio",
            verdict=verdict, confidence=confidence, summary=summary,
            strengths=strengths, weaknesses=weaknesses,
            risks=risks, recommendations=recs,
        )


# --- shared id resolution (mirrors cli_graph._resolve_graph_id) -------------

def _resolve_graph_id(gid: str, eng: TaskGraphEngine):
    if gid.isdigit():
        n = int(gid)
        ordered = sorted(eng.all_graphs(), key=lambda r: r.created_at,
                         reverse=True)
        if 1 <= n <= len(ordered):
            return ordered[n - 1].id, None
        return None, 2
    return gid, None


# Reused for runtime session resolution (integer = Nth newest).
def _resolve_session_id(sid: str, conn) -> "tuple[str | None, int | None]":
    if sid.lstrip("-").isdigit():
        n = int(sid)
        ordered = sorted(get_runtime_sessions(conn),
                         key=lambda r: r["created_at"], reverse=True)
        if 1 <= n <= len(ordered):
            return ordered[n - 1]["session_id"], None
        return None, 2
    return sid, None
