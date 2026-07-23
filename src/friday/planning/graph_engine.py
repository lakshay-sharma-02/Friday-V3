"""Task Graph Engine (Milestone 9.1).

Orchestration + persistence for the Task Graph Compiler. WRITE entrypoint:
`generate()` derives the Plan (via the FROZEN PlanEngine) and compiles it into a
TaskGraph, then persists both the graph header and its tasks/edges to the new
dedicated tables. READ entrypoints: list / explain / export / history / evolution.

NEVER executes, edits files, calls workers, or uses an LLM. NEVER reads
observations/context/git/repositories directly. The Planning Engine is FROZEN;
this layer only invokes it and consumes the structured Plan it returns. Idle on
recompilation: recompiling a goal REPLACES the same graph row (idempotent on
goal->graph id) and records the prior version in task_history (append-only).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import List, Optional, Tuple

from ..db import (
    TaskEdgeRow,
    TaskEvolutionRow,
    TaskGraphRow,
    TaskHistoryRow,
    TaskRow,
    get_all_task_graphs,
    get_edges_for_graph,
    get_initiative_by_id,
    get_plan_by_id,
    get_task_graph_by_id,
    get_tasks_for_graph,
    get_repositories,
    insert_plan,
    insert_task_evolution,
    insert_task_graph,
    insert_task_history,
    task_evolution_for,
    task_history_for,
)
from ..initiative import InitiativeEngine
from .compiler import Task, TaskGraph, TaskType, _graph_id, compile_plan
from .graph_schema import validate_task_graph
from .engine import PlanEngine
from .models import Plan, PlanType, now_iso
from .derive import Evidence
from ..knowledge.store import get_all_knowledge
from ..understanding import UnderstandingEngine
import os
import re


# ---------------------------------------------------------------------------
# InitiativeType → PlanType mapping
#
# The Plan Engine's derive_plan() re-derives the plan type by keyword-matching
# on the goal/title, which silently defaults everything to FEATURE for titles
# like "Typescript Engineering Initiative". When generating a graph from an
# initiative, we MUST use the initiative's actual type instead.
# ---------------------------------------------------------------------------


def _initiative_type_to_plan_type(initiative) -> PlanType:
    """Map an initiative's actual type to the corresponding PlanType.

    Most InitiativeType values map 1:1 to PlanType. A few that have no direct
    PlanType counterpart are mapped to the closest semantic equivalent:
      PLATFORM → INFRASTRUCTURE
      AUTOMATION → INFRASTRUCTURE
      DEPLOYMENT → RELEASE
    """
    from ..initiative.models import InitiativeType

    table = {
        InitiativeType.FEATURE: PlanType.FEATURE,
        InitiativeType.INFRASTRUCTURE: PlanType.INFRASTRUCTURE,
        InitiativeType.ARCHITECTURE: PlanType.ARCHITECTURE,
        InitiativeType.RESEARCH: PlanType.RESEARCH,
        InitiativeType.MIGRATION: PlanType.MIGRATION,
        InitiativeType.REFACTOR: PlanType.REFACTOR,
        InitiativeType.COMMERCIAL: PlanType.COMMERCIAL,
        InitiativeType.LEARNING: PlanType.LEARNING,
        InitiativeType.OPTIMIZATION: PlanType.OPTIMIZATION,
        InitiativeType.PLATFORM: PlanType.INFRASTRUCTURE,
        InitiativeType.INTEGRATION: PlanType.INTEGRATION,
        InitiativeType.AUTOMATION: PlanType.INFRASTRUCTURE,
        InitiativeType.DOCUMENTATION: PlanType.DOCUMENTATION,
        InitiativeType.TESTING: PlanType.TESTING,
        InitiativeType.DEPLOYMENT: PlanType.RELEASE,
        InitiativeType.RELEASE: PlanType.RELEASE,
        InitiativeType.MAINTENANCE: PlanType.MAINTENANCE,
    }
    return table.get(initiative.type, PlanType.FEATURE)


@dataclass
class GraphBuildResult:
    total: int
    created: int
    updated: int
    events: int = 0

    def to_text(self) -> str:
        lines = [
            "Task Graph Compiler",
            "",
            f"Total graphs: {self.total}",
            f"Created: {self.created}",
            f"Updated: {self.updated}",
            f"Evolution events: {self.events}",
            "",
            "Done.",
        ]
        return "\n".join(lines) + "\n"


class TaskGraphEngine:
    """Derives and stores task graphs. WRITE entrypoint: generate()."""

    def __init__(self, conn) -> None:
        self.conn = conn
        self._plan_eng = PlanEngine(conn)

    # --- READ (never mutate) --------------------------------------------------

    def all_graphs(self) -> List[TaskGraphRow]:
        return get_all_task_graphs(self.conn)

    def graph_by_id(self, gid: str) -> Optional[TaskGraph]:
        row = get_task_graph_by_id(self.conn, gid)
        if row is None:
            return None
        return self._rebuild(row)

    def _rebuild(self, row: TaskGraphRow) -> TaskGraph:
        tasks = [
            self._task_from_row(r) for r in get_tasks_for_graph(self.conn, row.id)]
        edges = [
            {"from": e.from_task, "to": e.to_task, "kind": e.kind}
            for e in get_edges_for_graph(self.conn, row.id)]
        g = TaskGraph(
            id=row.id, goal=row.goal, plan_id=row.plan_id,
            plan_type=row.plan_type, tasks=tasks, edges=edges,
            status=row.status, created_at=row.created_at,
            updated_at=row.updated_at,
        )
        # Recompute derived metrics deterministically from the persisted graph.
        self._recompute(g)
        # Loading from storage must also satisfy the frozen contract.
        validate_task_graph(g.to_json())
        return g

    @staticmethod
    def _task_from_row(r: TaskRow):
        return Task(
            id=r.id, graph_id=r.graph_id, plan_id=r.plan_id,
            milestone_order=r.milestone_order, title=r.title,
            description=r.description,
            task_type=TaskType.from_str(r.task_type),
            required_capabilities=[
                c for c in r.required_capabilities.split(",") if c],
            complexity=r.complexity, priority=r.priority,
            estimated_effort=r.estimated_effort,
            dependencies=_split_deps(r.dependencies),
            inputs=_loads(r.inputs), outputs=_loads(r.outputs),
            acceptance_criteria=_loads(r.acceptance_criteria),
            verification=_loads(r.verification),
            rollback=_loads(r.rollback), evidence=_loads(r.evidence),
            symbolic=_loads_dict(r.symbolic), status=r.status, confidence=r.confidence,
            sequence=r.sequence,
        )

    @staticmethod
    def _recompute(g: TaskGraph) -> None:
        from .compiler import (
            _compute_levels, _critical_path, _parallel_groups)
        ids = [t.id for t in g.tasks]
        g.levels = _compute_levels(g.edges, ids)
        g.critical_path = _critical_path(g.edges, g.tasks)
        groups, ptasks = _parallel_groups(g.levels)
        g.parallel_groups = groups
        g.parallel_tasks = ptasks

    def history(self, gid: str) -> List[TaskHistoryRow]:
        return task_history_for(self.conn, gid)

    def evolution(self, gid: Optional[str] = None) -> List[TaskEvolutionRow]:
        if gid is None:
            from ..db import task_evolution_all
            return task_evolution_all(self.conn)
        return task_evolution_for(self.conn, gid)

    # --- WRITE ----------------------------------------------------------------

    @staticmethod
    def _evidence_type_to_milestone(uid_type: str, uid_subject: str, uid_id: str, uid_stmt: str) -> Optional[dict]:
        """Map an understanding record type to an evidence-differentiated milestone.

        Returns a milestone dict with title, detail, evidence (specific ID), and
        task_type, so the compiler propagates them. Returns None for types that
        don't produce actionable tasks.

        Titles are built from the understanding type + evidence statement content
        (not just the subject name) so identical types across projects produce
        distinct task titles when the evidence statements carry different key
        phrases (e.g. "recurring" from "A recurring weakness around X is appearing"
        vs "accumulating" from "An engineering risk is accumulating around X").
        """
        t = (uid_type or "").lower()
        s = (uid_subject or "").strip()
        stmt = (uid_stmt or "").strip()

        # Extract a content-rich key phrase from the evidence statement beyond
        # the bare subject. We look for adjectives/adverbs/verbs that carry
        # meaning (e.g. "recurring", "accumulating", "now visible", "clear")
        # and inject them into the task title. If nothing useful is found,
        # fall back to just the type template.
        # Extract a content-rich key phrase from the evidence statement.
        from ..vocabulary import SIGNAL_WORDS, UNDERSTANDING_TEMPLATES
        _key_phrase = ""
        if stmt:
            s_lower = stmt.lower()
            found = [w for w in SIGNAL_WORDS if w in s_lower]
            if found:
                _key_phrase = found[0]

        result = UNDERSTANDING_TEMPLATES.get(t)
        if result is None:
            return None
        task_type, title_fn = result
        return {
            "order": 0,  # filled by caller
            "title": title_fn(s, _key_phrase),
            "detail": uid_stmt,
            "evidence": uid_id,
            "task_type": task_type,
        }

    @staticmethod
    def _knowledge_to_milestone(knowledge_entry) -> Optional[dict]:
        """Map a knowledge record to an evidence-differentiated milestone.

        Unlike the understanding path (which maps type -> task_type), knowledge
        records carry diverse types that SHOULD produce different task shapes:

          project_architecture  -> architecture review
          project_stack         -> technology-stack audit
          portfolio_integration -> integration evaluation
          project_identity      -> identity documentation
          portfolio_technology  -> tech-standardisation task

        All produce richer titles than the historic "Audit {subject} usage"
        monolith. Unknown knowledge types fall through to the generic audit
        (safe fallback).
        """
        subject = (getattr(knowledge_entry, "subject", None) or "").strip()
        stmt = (getattr(knowledge_entry, "statement", None) or "").strip()
        kid = getattr(knowledge_entry, "id", None) or ""
        ktype = getattr(knowledge_entry, "type", None)
        ktype_str = ktype.value if hasattr(ktype, "value") else str(ktype or "")

        # Knowledge-type dispatch table (from consolidated vocabulary.py).
        from ..vocabulary import KNOWLEDGE_TEMPLATES
        task_type, title_fmt = KNOWLEDGE_TEMPLATES.get(
            ktype_str, ("analysis", f"Audit {{subject}} usage across projects"))
        title = title_fmt.format(subject=subject)

        return {
            "order": 0,
            "title": title,
            "detail": stmt,
            "evidence": kid,
            "task_type": task_type,
        }

    # -------------------------------------------------------------------
    # Phase 7: LLM-backed milestone generation from initiative evidence
    # -------------------------------------------------------------------

    def _llm_initiative_milestones(
        self, understanding: list, knowledge: list,
    ) -> Optional[List[dict]]:
        """Ask the LLM to generate evidence-grounded milestones from the
        initiative's supporting understanding/knowledge records.

        Each returned milestone cites its originating evidence ID so the
        downstream task traces to a real evidence record. Returns None if
        the LLM is unavailable, the response cannot be parsed, or the
        output fails verification.

        This mirrors derive.py's _llm_milestones() but works from specific
        initiative evidence (understanding + knowledge records) rather than
        a generic Evidence object.
        """
        try:
            from ..services.llm import plan_goal
        except Exception:
            return None

        # Build a compact evidence summary from the initiative's own records.
        evidence_parts = []
        for u in understanding:
            uid = getattr(u, 'id', None) or ''
            subj = getattr(u, 'subject', None) or ''
            stmt = getattr(u, 'statement', None) or ''
            evidence_parts.append(f"Understanding[{uid}]: {subj} — {stmt}")
        for k in knowledge:
            kid = getattr(k, 'id', None) or ''
            subj = getattr(k, 'subject', None) or ''
            stmt = getattr(k, 'statement', None) or ''
            evidence_parts.append(f"Knowledge[{kid}]: {subj} — {stmt}")

        # Add language/technology context from knowledge store.
        _langs = set()
        _all_knowledge = get_all_knowledge(self.conn)
        for k in _all_knowledge:
            ktype = getattr(k, 'type', None)
            ktype_str = ktype.value if hasattr(ktype, 'value') else str(ktype or '')
            if 'stack' in ktype_str.lower():
                _langs.add(k.statement[:200])
        if _langs:
            evidence_parts.append(f"Workspace stack context: {'; '.join(sorted(_langs))}")

        # Add repo names for subject grounding.
        repos = get_repositories(self.conn)
        repo_names = [r.name for r in repos if r.name][:20]
        if repo_names:
            evidence_parts.append(f"Known projects: {', '.join(sorted(repo_names))}")

        evidence_summary = "\n".join(evidence_parts[:20])

        # Build a goal string that captures the initiative's purpose.
        goal_parts = []
        for u in understanding[:3]:
            s = getattr(u, 'statement', None) or ''
            if s and s not in goal_parts:
                goal_parts.append(s)
        goal = '; '.join(goal_parts) or 'engineering initiative'

        raw = plan_goal(goal, evidence_summary)
        if not raw:
            return None

        # Strip markdown code fences.
        text = raw.strip()
        if text.startswith('```'):
            idx = text.find('\n')
            if idx != -1:
                text = text[idx:].strip()
        if text.endswith('```'):
            text = text[:-3].strip()

        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return None

        tasks = data if isinstance(data, list) else data.get('tasks', [])
        if not tasks:
            return None

        milestones = []
        # Match evidence records to tasks by content overlap, not by position.
        # For each task, find the evidence record whose statement has the most
        # non-stopword token overlap with the task title/description.
        # ponytail: token overlap is cheap but crude; upgrade to embedding-based
        # matching when the query engine is production. Add when: evidence
        # mismatches are reported in user feedback.
        all_evidence: list[tuple[str, str]] = []
        for u in understanding:
            uid = getattr(u, 'id', None) or ''
            stmt = getattr(u, 'statement', None) or ''
            if uid:
                all_evidence.append((uid, stmt))
        if not all_evidence:
            for k in knowledge:
                kid = getattr(k, 'id', None) or ''
                stmt = getattr(k, 'statement', None) or ''
                if kid:
                    all_evidence.append((kid, stmt))

        from ..vocabulary import STOPWORDS as _stopwords

        def _tokenize(text: str) -> set:
            """Lowercase, split on non-alpha, remove stopwords and short tokens."""
            tokens = set()
            for word in re.sub(r'[^a-z0-9]', ' ', text.lower()).split():
                w = word.strip()
                if len(w) > 2 and w not in _stopwords:
                    tokens.add(w)
            return tokens

        def _evidence_score(task_tokens: set, evidence_text: str) -> int:
            """Count overlapping non-stopword tokens between task and evidence."""
            ev_tokens = _tokenize(evidence_text)
            return len(task_tokens & ev_tokens)

        for i, t in enumerate(tasks, start=1):
            sym = t.get('symbolic', {}) or {}
            ac = t.get('acceptance_criteria') or []
            tt = t.get('task_type', 'implementation')
            # Score-based evidence matching: find the best-matching evidence
            # record for this task's content.
            task_text = t.get('title', '')
            task_text += ' ' + (sym.get('goal', '') or '')
            task_tokens = _tokenize(task_text)

            best_score = -1
            best_eid = ''
            for eid, stmt in all_evidence:
                score = _evidence_score(task_tokens, stmt)
                if score > best_score:
                    best_score = score
                    best_eid = eid
            # If no evidence has any overlap, assign empty rather than a
            # misleading arbitrary citation.
            eid = best_eid if best_score > 0 else ''

            milestones.append({
                'order': i,
                'title': t.get('title', f'Task {i}'),
                'detail': sym.get('goal', t.get('title', '')),
                'evidence': eid,
                'task_type': tt,
                'symbolic': sym,
                'acceptance_criteria': ac if isinstance(ac, list) else [str(ac)],
                'parallel_next': bool(t.get('parallel_next', False)),
            })

        # Build repo path list for file-existence check in verification.
        repo_paths = [r.path for r in repos if getattr(r, 'path', None)]

        # Run the verification gate on LLM output.
        verified = self._verify_llm_milestones(milestones, repo_roots=repo_paths)
        if not verified:
            # Verification failed — fall back to template path.
            return None
        return milestones

    def _verify_llm_milestones(
        self, milestones: List[dict],
        repo_roots: Optional[list[str]] = None,
    ) -> bool:
        """Verify each LLM-proposed milestone against real workspace evidence.

        Checks:
        1. File paths (symbolic.path) — the extension must be a known
           software development file extension.
        2. File existence — if repo roots are known, check that referenced
           paths actually exist under at least one repo root (loud refusal
           rather than silent hallucination).
        3. Commands — must reference a known tool/language.

        The known-extensions list is deliberately broad (all common lang/tool
        formats) so the gate catches obviously fabricated file types while
        allowing legitimate new files in any standard language. Unknown
        extensions at the project root are allowed (could be new config files);
        unknown extensions in nested paths are rejected.

        File-existence checks are lenient by design: a path is accepted if it
        exists under ANY known repo root. This avoids false-rejecting paths
        for files that happen to be created during the initiative. However, a
        task that claims to modify a file that doesn't actually exist anywhere
        in the workspace is flagged. We use os.path.isfile (not .exists)
        so directories aren't counted as files — the task should name the
        actual source file, not its parent directory.

        Returns True if the milestone list passes verification (or has no
        specific claims to verify). Returns False if a milestone makes an
        unverifiable claim, causing fallback to the template path.
        """
        # Known software development file extensions. Broad enough to allow
        # legitimate new files but narrow enough to catch fabricated types.
        known_extensions: set = {
            # Languages
            '.py', '.pyi', '.pyx', '.pxd',
            '.ts', '.tsx', '.js', '.jsx', '.mjs', '.cjs', '.mts', '.cts',
            '.rs', '.rlib',
            '.go',
            '.java', '.class', '.jar', '.kt', '.kts',
            '.rb', '.erb', '.rake', '.gemspec',
            '.c', '.h', '.cpp', '.hpp', '.cxx', '.hxx', '.cc', '.hh',
            '.cs', '.fs', '.vb',
            '.swift',
            '.scala', '.sc',
            '.ex', '.exs',
            '.php', '.phtml',
            '.r', '.rda',
            '.lua',
            '.pl', '.pm', '.t',
            '.dart',
            '.zig',
            # Web / config
            '.html', '.css', '.scss', '.sass', '.less', '.styl',
            '.json', '.yaml', '.yml', '.toml', '.cfg', '.ini', '.conf',
            '.xml', '.xsl', '.xslt', '.xsd', '.dtd',
            '.svg', '.png', '.jpg', '.jpeg', '.gif', '.ico', '.webp',
            # Documentation
            '.md', '.mdx', '.rst', '.txt', '.adoc', '.wiki',
            # Scripts / shell
            '.sh', '.bash', '.zsh', '.fish', '.ps1', '.bat', '.cmd',
            # Build / CI
            '.gradle', '.groovy', '.mvn', '.cmake', '.make', '.mk',
            '.dockerfile', '.containerfile',
            # Database
            '.sql', '.sqlite', '.db',
            # Python packaging
            '.toml', '.lock', '.whl', '.egg',
            # Environment / secrets
            '.env', '.envrc',
        }

        known_commands = {
            'python', 'python3', 'pip', 'pip3', 'poetry', 'conda',
            'npm', 'npx', 'yarn', 'pnpm', 'bun',
            'cargo', 'rustc', 'rustup',
            'go', 'gofmt',
            'docker', 'docker-compose', 'dockerfile',
            'git',
            'make', 'cmake', 'bazel', 'mvn', 'gradle', 'sbt',
            'node', 'deno', 'tsc', 'esbuild', 'webpack', 'vite',
            'pytest', 'nosetests', 'unittest',
            'jest', 'mocha', 'vitest', 'cypress', 'playwright',
            'cargo test',
            'go test',
            'rails', 'rake',
            'curl', 'wget',
            'kubectl', 'helm', 'terraform', 'ansible', 'pulumi',
            'ssh', 'scp', 'rsync',
            'cat', 'echo', 'ls', 'mkdir', 'mv', 'cp', 'rm', 'chmod', 'chown',
            'grep', 'sed', 'awk', 'sort', 'uniq', 'wc', 'tee',
            'sleep', 'timeout', 'date',
        }

        for m in milestones:
            sym = m.get('symbolic', {}) or {}
            path = sym.get('path', '') or ''
            command = sym.get('command', '') or ''

            # Check 1: File path verification.
            # If the LLM proposes a file with an unknown extension at a
            # nested path, reject it (unknown extension at root level is
            # allowed — could be a new config file).
            if path:
                _, ext = os.path.splitext(path)
                if ext and ext not in known_extensions:
                    if '/' in path and ext:
                        return False

                # Check 1b: File existence (when repo roots are known).
                # A path with a directory separator should exist somewhere
                # in the workspace. Root-level bare filenames are allowed
                # (new config files, generation targets).
                if repo_roots and '/' in path:
                    exists = any(
                        os.path.isfile(os.path.join(rr, path))
                        for rr in repo_roots
                    )
                    if not exists:
                        return False

            # Check 2: Command verification.
            # Unknown commands with no tool match are rejected.
            if command:
                cmd_name = command.strip().split()[0].lower()
                if cmd_name not in known_commands:
                    return False

        return True

    # -------------------------------------------------------------------

    def generate_from_initiative(
        self, initiative_id: str,
        generated_at: Optional[str] = None,
    ) -> TaskGraph:
        """Derive a Task Graph proposal from an APPROVED initiative.

        The graph is built ONLY from the initiative's actual supporting evidence
        (understanding/knowledge records it cites), with each task tracing to its
        specific originating evidence record. Status is set to "proposal"
        — a reviewable, non-executing state that appears in `friday graph review`.

        Raises ValueError if:
        - The initiative is not approved (reviewed=1 in pending_initiatives)
        - The initiative has no supporting evidence (<2 understanding ids)
        - The evidence is too thin to produce a meaningful graph
        """
        if generated_at is None:
            generated_at = now_iso()

        # 1. Verify the initiative is approved.
        pending_row = self.conn.execute(
            "SELECT reviewed, dismissed_at FROM pending_initiatives WHERE id=?",
            (initiative_id,),
        ).fetchone()
        if not pending_row or not pending_row["reviewed"]:
            raise ValueError(
                f"Initiative '{initiative_id}' is not approved. "
                f"Run `friday review pending approve {initiative_id}` first.\n"
                f"Tip: use `friday graph \"<goal>\"` to compile a goal as a "
                f"task graph (no approval needed for goals).")
        if pending_row["dismissed_at"] is not None:
            raise ValueError(
                f"Initiative '{initiative_id}' was already dismissed.")

        # 2. Load the initiative and its evidence.
        initiative_row = get_initiative_by_id(self.conn, initiative_id)
        if initiative_row is None:
            raise ValueError(f"Initiative not found: {initiative_id}")

        from ..initiative.models import Initiative
        initiative = Initiative.from_row(initiative_row)

        # 3. Check evidence is sufficient.
        total_evidence = len(initiative.understanding_ids) + len(initiative.knowledge_ids)
        if total_evidence < 2:
            raise ValueError(
                f"Initiative '{initiative.title}' has only "
                f"{len(initiative.understanding_ids)} supporting understanding record(s) "
                f"and {len(initiative.knowledge_ids)} knowledge record(s). "
                f"At least 2 total evidence records are needed to produce a "
                f"meaningful task graph. Evidence is too thin.")

        # 4. Build a scoped Evidence object with ONLY the initiative's own
        #    supporting records.
        u_ids = initiative.understanding_ids
        k_ids = initiative.knowledge_ids

        understanding = [
            u for u in UnderstandingEngine(self.conn).all_understanding()
            if u.id in u_ids
        ]
        knowledge = [
            k for k in get_all_knowledge(self.conn)
            if k.id in k_ids
        ] if k_ids else []

        # 5. Build evidence-differentiated milestones from the initiative's
        #    actual understanding/knowledge records.
        #
        # Phase 7 fallback chain:
        #   1. LLM path — generates parameterized, evidence-grounded milestones
        #      from the initiative's evidence records. Each task traces to a
        #      specific evidence ID.
        #   2. Template path — deterministic per-evidence-type templates
        #      (Investigate {s} weakness, Audit {s} usage, etc.).
        #
        # The LLM path can produce richer, more specific task descriptions but
        # requires an LLM backend. The template path always works.
        #
        llm_ms = self._llm_initiative_milestones(understanding, knowledge)

        if llm_ms is not None:
            # Phase 7: LLM path succeeded and passed verification.
            milestones = llm_ms
        else:
            # Phase 4-6: Template fallback path.
            evidence_milestones: List[dict] = []

            # Ordering strategy for parallelism:
            #   - Understanding milestones: grouped by subject, each subject-group
            #     gets a sequential phase order. Milestones within the same
            #     subject-group share that order (same phase) and run sequentially
            #     via intra-phase edges.
            #   - Knowledge milestones: ALL share a single phase order AFTER
            #     understanding, with parallel_next=True so independent audits
            #     (different subjects) run simultaneously.
            #
            # This means understanding phases are sequential (e.g. all aether
            # tasks before all finance-tracker tasks), but knowledge audits run
            # in parallel once understanding completes.
            _current_phase = 0
            _seen_subjects: set = set()

            for u in understanding:
                if not u.id:
                    continue
                m = self._evidence_type_to_milestone(u.type, u.subject, u.id, u.statement)
                if m is None:
                    continue
                # Each new subject gets a new phase order. Same subject = same
                # phase (sequential within the subject-group).
                u_subj = (u.subject or "").strip().lower()
                if u_subj not in _seen_subjects:
                    _seen_subjects.add(u_subj)
                    _current_phase += 1
                m["order"] = _current_phase
                evidence_milestones.append(m)

            # Deduplicate knowledge milestones by subject.
            seen_k_subjects: set = set()
            k_milestones: List[dict] = []
            for k in knowledge:
                if not k.id:
                    continue
                subject = (getattr(k, "subject", None) or "").strip().lower()
                if subject in seen_k_subjects:
                    continue
                seen_k_subjects.add(subject)
                m = self._knowledge_to_milestone(k)
                if m is not None:
                    k_milestones.append(m)

            # Knowledge milestones: all in the NEXT phase after understanding.
            if k_milestones:
                _k_phase = _current_phase + 1
                for i, m in enumerate(k_milestones):
                    m["order"] = _k_phase
                    # All knowledge audits are independent — run in parallel.
                    if i < len(k_milestones) - 1:
                        m["parallel_next"] = True
                    evidence_milestones.append(m)

            milestones = evidence_milestones

        # 6. Call derive_plan() for its structure (dependencies, risks, etc.) but
        #    OVERRIDE its generic template milestones with the evidence-specific ones.
        ev = Evidence(
            initiatives=[initiative],
            insights=[],
            understanding=understanding,
            knowledge=knowledge,
        )
        ev.initiatives_by_id = {initiative.id} if initiative.id else set()
        ev.insights_by_id = set()
        ev.understanding_by_id = {u.id for u in understanding if u.id}
        ev.knowledge_by_id = {k.id for k in knowledge if k.id}

        from .derive import plan as derive_plan
        structured = derive_plan(initiative.title, ev)

        # PHASE 6 FIX: Override the plan type with the initiative's actual type
        # (derive_plan() re-derives via keyword matching on the title, which
        # silently defaults everything to FEATURE for initiative titles).
        structured.plan_type = _initiative_type_to_plan_type(initiative)

        # Override the generic milestones with evidence-specific ones.
        structured.milestones = milestones

        # Override the plan id so it references the initiative distinctly.
        # Replace both colons and spaces in the initiative ID to keep the
        # derived ID token-free (important for CLI parsing).
        _safe_iid = initiative_id.replace(':', '_').replace(' ', '_')
        pid = f"proposal_plan:{_safe_iid}"
        structured.id = pid
        structured.created_at = generated_at
        structured.updated_at = generated_at

        # Ensure the plan cites the initiative and ALL its evidence.
        if initiative.id and initiative.id not in structured.affected_initiative_ids:
            structured.affected_initiative_ids = [initiative.id] + structured.affected_initiative_ids
        for uid in initiative.understanding_ids:
            if uid not in structured.affected_understanding_ids:
                structured.affected_understanding_ids.append(uid)
        for kid in initiative.knowledge_ids:
            if kid not in structured.affected_knowledge_ids:
                structured.affected_knowledge_ids.append(kid)

        # Persist the plan so the task_graphs FK constraint is satisfied.
        insert_plan(self.conn, [structured.to_row()])

        # 7. Compile the plan into a task graph.
        graph = compile_plan(structured, generated_at=generated_at)

        # 8. Derive a distinct graph id prefixed to show it came from an
        #    initiative. This avoids colliding with goal-based generate().
        gid = f"initiative_graph:{_safe_iid}"
        graph.id = gid

        # Mark as proposal (reviewable, non-executing).
        graph.status = "proposal"

        # 9. Post-process: assign per-task evidence tracing and graph ids.
        milestone_evidence: dict = {}
        for m in milestones:
            eid = m.get("evidence", "")
            if eid and eid not in ("goal", "verification", "rollback"):
                milestone_evidence[m["title"]] = eid

        for t in graph.tasks:
            t.graph_id = gid
            t.plan_id = pid
            # Every task traces to its specific originating evidence record.
            specific_eid = milestone_evidence.get(t.title)
            if specific_eid:
                t.evidence = [specific_eid]

        # 10. Enforce the frozen contract.
        validate_task_graph(graph.to_json())

        self._persist(graph, prev_created=generated_at)
        self._record_history(generated_at, graph, prev=None)
        self._record_evolution(generated_at, graph, prev=None)
        return graph

    def generate(self, goal: str, generated_at: Optional[str] = None) -> TaskGraph:
        """Derive the Plan (frozen PlanEngine) and compile + persist the graph.

        Idempotent on goal: recompiling REPLACES the same graph row and appends
        a snapshot to task_history.
        """
        if generated_at is None:
            generated_at = now_iso()

        plan = self._plan_eng.generate(goal, generated_at=generated_at)
        graph = compile_plan(plan, generated_at=generated_at)

        # Enforce the frozen Task Graph contract before persisting. A malformed
        # graph must fail loudly, not silently enter the execution pipeline.
        validate_task_graph(graph.to_json())

        gid = graph.id
        prev_row = get_task_graph_by_id(self.conn, gid)
        prev = self._rebuild(prev_row) if prev_row else None

        created = 0
        updated = 0
        if prev is None:
            created = 1
        else:
            updated = 1

        self._persist(graph, prev_created=prev_row.created_at if prev_row else generated_at)
        self._record_history(generated_at, graph, prev)
        self._record_evolution(generated_at, graph, prev)
        return graph

    def _persist(self, g: TaskGraph, prev_created: str) -> None:
        graph_row = TaskGraphRow(
            id=g.id, goal=g.goal, plan_id=g.plan_id, plan_type=g.plan_type,
            task_count=len(g.tasks), edge_count=len(g.edges),
            critical_path_length=len(g.critical_path),
            parallel_groups=g.parallel_groups, status=g.status,
            created_at=prev_created, updated_at=g.updated_at,
        )
        task_rows: List[TaskRow] = []
        for t in g.tasks:
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
                symbolic=_dumps(t.symbolic), status=t.status,
                confidence=t.confidence, sequence=t.sequence,
            ))
        edge_rows: List[TaskEdgeRow] = []
        for i, e in enumerate(g.edges):
            edge_rows.append(TaskEdgeRow(
                id=f"{g.id}#e{i}", graph_id=g.id, from_task=e["from"],
                to_task=e["to"], kind=e.get("kind", "depends_on")))
        insert_task_graph(self.conn, [graph_row], task_rows, edge_rows)

    def _record_history(self, generated_at: str, g: TaskGraph,
                        prev: Optional[TaskGraph]) -> int:
        insert_task_history(self.conn, [TaskHistoryRow(
            generated_at=generated_at, graph_id=g.id, goal=g.goal,
            task_count=len(g.tasks), edge_count=len(g.edges),
            critical_path_length=len(g.critical_path),
            parallel_groups=g.parallel_groups,
            tasks_json=_dumps([t.to_dict() for t in g.tasks]),
            edges_json=_dumps(g.edges),
        )])
        return 1

    def _record_evolution(self, generated_at: str, g: TaskGraph,
                          prev: Optional[TaskGraph]) -> int:
        events: List[TaskEvolutionRow] = []
        gid = g.id
        if prev is None:
            events.append(self._event(
                generated_at, "Compiled", gid, None, g.status, None,
                len(g.tasks), None, len(g.edges),
                f"Task graph compiled for plan {g.plan_id}."))
            insert_task_evolution(self.conn, events)
            return len(events)
        if len(g.tasks) != len(prev.tasks) or len(g.edges) != len(prev.edges):
            events.append(self._event(
                generated_at, "Recompiled", gid, prev.status, g.status,
                len(prev.tasks), len(g.tasks), len(prev.edges), len(g.edges),
                "Graph shape changed on recompilation (plan changed)."))
            insert_task_evolution(self.conn, events)
            return len(events)
        return 0

    @staticmethod
    def _event(gen_at, etype, gid, prev_status, new_status, prev_tasks,
               new_tasks, prev_edges, new_edges, reason):
        return TaskEvolutionRow(
            id=f"{gen_at}:{etype}:{gid}", generated_at=gen_at,
            event_type=etype, graph_id=gid, previous_status=prev_status,
            new_status=new_status, reason=reason, task_count=new_tasks or 0,
            edge_count=new_edges or 0, timestamp=gen_at)


def _dumps(xs: list) -> str:
    try:
        return json.dumps(xs, separators=(",", ":"))
    except (TypeError, ValueError):
        return "[]"


def _loads(s: str) -> list:
    if not s:
        return []
    try:
        out = json.loads(s)
        return out if isinstance(out, list) else []
    except (TypeError, ValueError):
        return []


def _split_deps(s: str) -> list:
    """Parse a dependencies field: JSON array (new) or comma-separated (legacy)."""
    if not s:
        return []
    try:
        out = json.loads(s)
        if isinstance(out, list):
            return out
    except (ValueError, TypeError):
        pass
    return [d for d in s.split(",") if d]


def _loads_dict(s: str) -> dict:
    """Parse a JSON object column (e.g. a task's symbolic intent)."""
    if not s:
        return {}
    try:
        out = json.loads(s)
        return out if isinstance(out, dict) else {}
    except (TypeError, ValueError):
        return {}
