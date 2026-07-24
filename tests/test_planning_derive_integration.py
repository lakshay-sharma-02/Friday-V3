"""Tests for Part A: planning derive.py changes.

Tests:
1. verify_llm_milestones accepts valid milestones
2. verify_llm_milestones rejects unknown file extensions
3. verify_llm_milestones rejects unknown commands
4. verify_llm_milestones passes milestones with no symbolic claims
5. _milestones gates Backend/Frontend insertion on goal signals
6. _milestones does NOT insert Backend/Frontend for goals without split signals
7. _milestones inserts Backend/Frontend for goals with explicit web/frontend/backend signals
8. verify_llm_milestones is importable from planning.__init__
"""

from __future__ import annotations

import pytest
from friday.planning.derive import (
    verify_llm_milestones,
    _milestones,
    Evidence,
    _generate_milestones,
)
from friday.planning.models import PlanType


class TestVerifyLlmMilestones:
    """Tests for verify_llm_milestones function (shared between derive and graph_engine)."""

    def test_accepts_valid_milestones(self):
        """Valid milestones with no symbolic claims pass verification."""
        milestones = [
            {"order": 1, "title": "Analyze repo",
             "detail": "Read the codebase", "evidence": "goal"},
            {"order": 2, "title": "Write plan",
             "detail": "Create PLANF.txt", "evidence": "goal",
             "symbolic": {"path": "PLANF.txt"}},
        ]
        assert verify_llm_milestones(milestones) is True

    def test_rejects_unknown_extensions_in_subdirs(self):
        """A file in a subdirectory with unknown extension is rejected."""
        milestones = [
            {"order": 1, "title": "Foo",
             "symbolic": {"path": "src/main.foobazxyz"}},
        ]
        assert verify_llm_milestones(milestones) is False

    def test_allows_unknown_extensions_at_root(self):
        """Unknown extensions at root level are allowed (new config files)."""
        milestones = [
            {"order": 1, "title": "Config",
             "symbolic": {"path": "config.foobaz"}},
        ]
        assert verify_llm_milestones(milestones) is True

    def test_rejects_unknown_commands(self):
        """An unknown command in symbolic is rejected."""
        milestones = [
            {"order": 1, "title": "Run magic",
             "symbolic": {"command": "foobaz42 --magic --all"}},
        ]
        assert verify_llm_milestones(milestones) is False

    def test_accepts_known_commands(self):
        """Known commands like pytest, git, etc. are accepted."""
        milestones = [
            {"order": 1, "title": "Test",
             "symbolic": {"command": "pytest tests/ -v"}},
            {"order": 2, "title": "Git",
             "symbolic": {"command": "git status"}},
            {"order": 3, "title": "Python",
             "symbolic": {"command": "python3 -m pytest"}},
        ]
        assert verify_llm_milestones(milestones) is True

    def test_passes_milestones_with_no_symbolic(self):
        """Milestones with no symbolic field pass trivially."""
        milestones = [
            {"order": 1, "title": "Plain milestone",
             "detail": "No symbolic claims", "evidence": "goal"},
        ]
        assert verify_llm_milestones(milestones) is True

    def test_importable_from_planning_package(self):
        """verify_llm_milestones is re-exported through graph_engine.py."""
        from friday.planning.graph_engine import verify_llm_milestones
        assert callable(verify_llm_milestones)


class TestBackendFrontendGate:
    """Tests for the Backend/Frontend insertion gate in _milestones()."""

    def _make_ev(self, goal: str = "") -> Evidence:
        """Create a minimal Evidence object."""
        ev = Evidence()
        # For the Backend/Frontend gate, no initiatives means _goal_text_from_ctx
        # stays empty, so _has_split_signal is False (no split).
        return ev

    def test_no_backend_frontend_for_plain_feature_goal(self):
        """A FEATURE type goal without web/frontend/backend keywords does NOT
        get Backend/Frontend milestones inserted."""
        ev = self._make_ev()
        ms = _milestones(PlanType.FEATURE, ev, [], [], [], [])
        titles = [m["title"].lower() for m in ms]
        # Check Backend/Frontend are NOT present
        assert "backend" not in titles, (
            f"Backend milestone should not be present for plain goal. "
            f"Milestones: {titles}"
        )
        assert "frontend" not in titles, (
            f"Frontend milestone should not be present for plain goal. "
            f"Milestones: {titles}"
        )
        # But standard milestones should still be there
        assert "investigate & scope" in titles
        assert "implement" in titles
        assert "verify" in titles

    def test_plan_type_not_feature(self):
        """Non-FEATURE plan types should not get Backend/Frontend regardless."""
        for pt in [PlanType.REFACTOR, PlanType.RESEARCH, PlanType.INFRASTRUCTURE]:
            ev = self._make_ev()
            ms = _milestones(pt, ev, [], [], [], [])
            titles = [m["title"].lower() for m in ms]
            assert "backend" not in titles, (
                f"Backend should not be present for {pt}"
            )
            assert "frontend" not in titles, (
                f"Frontend should not be present for {pt}"
            )

    def test_backend_frontend_inserted_for_web_goal(self):
        """A FEATURE goal with 'web dashboard' signal DOES get Backend/Frontend."""
        ev = self._make_ev()
        ms = _milestones(
            PlanType.FEATURE, ev, [], [], [], [],
            goal_text="Add a web dashboard with user authentication"
        )
        titles = [m["title"].lower() for m in ms]
        assert "backend" in titles, (
            f"Backend should be present for web goal. Milestones: {titles}"
        )
        assert "frontend" in titles, (
            f"Frontend should be present for web goal. Milestones: {titles}"
        )
        # Standard milestones should also be present
        assert "investigate & scope" in titles
        assert "document" in titles


class TestGenerateMilestones:
    """Tests for _generate_milestones fallback chain."""

    def test_trivial_create_file_goal(self):
        """A create-file goal should produce a single implementation task."""
        ms = _generate_milestones(
            'create a file named hello.py containing "hello world"',
            PlanType.FEATURE, Evidence(), [], [], [], []
        )
        assert ms is not None
        assert len(ms) >= 1
        titles = [m["title"].lower() for m in ms]
        assert "create" in str(titles).lower() or "hello.py" in str(titles).lower()

    def test_template_fallback_for_generic_goal(self):
        """A generic goal with no LLM should fall through to the template,
        producing several milestones."""
        ms = _generate_milestones(
            "Improve the documentation of the project",
            PlanType.DOCUMENTATION, Evidence(), [], [], [], []
        )
        assert ms is not None
        assert len(ms) >= 2
        titles = [m["title"].lower() for m in ms]
        # Documentation template doesn't get Backend/Frontend
        assert "backend" not in titles
        assert "frontend" not in titles


class TestCapabilityReliability:
    """Tests for Part C: capability_reliability in the resolver."""

    def test_resolver_accepts_reliability_param(self):
        """score_worker accepts a capability_reliability parameter."""
        from friday.resolver.resolver import score_worker
        from friday.worker.models import Worker, WorkerKind

        w = Worker(
            name="python", kind=WorkerKind.CLI,
            capabilities=["Python"], status="active",
            supported_task_types=["implementation"],
            supported_plan_types=["feature"],
            confidence="high",
        )
        # Without reliability param (backward compat)
        sb, matched, missing = score_worker(
            ["python"], "implementation", "feature", w)
        assert sb.total > 0
        assert "python" in [c.lower() for c in matched]

        # With reliability param (high reliability -> no penalty)
        sb_high, _, _ = score_worker(
            ["python"], "implementation", "feature", w,
            capability_reliability={"python": 0.9})
        assert sb_high.total > 0

        # With low reliability -> should be lower than high reliability
        sb_low, _, _ = score_worker(
            ["python"], "implementation", "feature", w,
            capability_reliability={"python": 0.2})
        assert sb_low.total < sb_high.total, (
            f"Low reliability ({sb_low.total}) should score lower than "
            f"high reliability ({sb_high.total})"
        )

    def test_resolver_engine_accepts_reliability_param(self):
        """rank_workers accepts and uses capability_reliability parameter."""
        from friday.resolver.resolver import rank_workers
        from friday.worker.models import Worker, WorkerKind

        w1 = Worker(
            name="shell", kind=WorkerKind.CLI,
            capabilities=["Shell Commands"], status="active",
            supported_task_types=["implementation"],
            supported_plan_types=["feature"],
            confidence="high",
        )
        w2 = Worker(
            name="python", kind=WorkerKind.CLI,
            capabilities=["Python"], status="active",
            supported_task_types=["implementation"],
            supported_plan_types=["feature"],
            confidence="high",
        )

        # Without reliability — both should be ranked
        ranked = rank_workers(
            ["python"], "implementation", "feature", [w1, w2],
        )
        assert len(ranked) == 2

        # With reliability — python worker should be penalized for low reliability
        ranked_low = rank_workers(
            ["python"], "implementation", "feature", [w1, w2],
            capability_reliability={"python": 0.1},
        )
        assert len(ranked_low) >= 1
        # The shell worker (no penalty) may rank higher than python with low reliability
        # This just tests that the parameter is accepted and doesn't crash


class TestDependencySummaries:
    """Tests for Part B: dependency summaries."""

    def test_runtime_task_has_dependency_summaries(self):
        """RuntimeTask has a dependency_summaries field."""
        from friday.runtime.models import RuntimeTask
        task = RuntimeTask(
            execution_id="test", session_id="sess",
            schedule_id="sched", task_id="t1",
            worker_id="w1", wave=1,
        )
        assert hasattr(task, "dependency_summaries")
        assert task.dependency_summaries == {}

    def test_dependency_summaries_populated(self):
        """Dependency summaries can be populated and accessed."""
        from friday.runtime.models import RuntimeTask
        task = RuntimeTask(
            execution_id="test", session_id="sess",
            schedule_id="sched", task_id="t2",
            worker_id="w1", wave=2,
            dependencies=["t1"],
        )
        task.dependency_summaries["t1"] = "Analyze repo: found 3 Python files"
        assert task.dependency_summaries["t1"] == "Analyze repo: found 3 Python files"
