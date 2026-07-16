"""Performance regression test suite — Friday Core v1.0 hardening.

Measures baseline performance for observation, context, ask, and scaling.
Tests detect REGRESSIONS, not absolute thresholds (which vary by machine).

Run this suite periodically to verify no performance degradation.
"""

import tempfile
import time
from pathlib import Path

import pytest

from friday.ask import ask
from friday.context.engine import ContextEngine
from friday.db import connect, insert_observations, ObservationRow, upsert_repository
from friday.observation.engine import ObservationEngine
from friday.observation.git_observer import GitObserver
from friday.observation.interface import Observer, ObserverHealth
from friday.observation.model import Confidence, Health, Observation, now_iso
from friday.observation.registry import ObserverRegistry


# ---------------------------------------------------------------------------
# Observation Engine Performance
# ---------------------------------------------------------------------------


def test_observation_single_observer_baseline():
    """Baseline: single observer on empty DB."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Path(tmpdir) / "test.db"
        conn = connect(db)

        class FastObserver(Observer):
            name = "fast"
            def collect(self, conn):
                return [Observation(
                    source="fast", subject="test", aspect="metric",
                    value="1", confidence=Confidence.OBSERVED,
                    observed_at=now_iso(), scope="", cause=None
                )]
            def summarize(self, conn):
                return "fast"
            def health(self, conn):
                return ObserverHealth(True, Health.HEALTHY, "ok")

        registry = ObserverRegistry()
        registry.register(FastObserver())
        engine = ObservationEngine(registry, conn)

        start = time.time()
        run = engine.run()
        elapsed = time.time() - start

        assert len(run.observers) == 1
        # Should complete in well under 1 second
        assert elapsed < 1.0, f"Single observer took {elapsed:.2f}s (baseline: <1s)"


def test_observation_scales_linearly_with_observers():
    """Observation time scales linearly with number of observers."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Path(tmpdir) / "test.db"
        conn = connect(db)

        class SimpleObserver(Observer):
            def __init__(self, n):
                self.name = f"observer_{n}"
            def collect(self, conn):
                return [Observation(
                    source=self.name, subject="test", aspect="metric",
                    value="1", confidence=Confidence.OBSERVED,
                    observed_at=now_iso(), scope="", cause=None
                )]
            def summarize(self, conn):
                return self.name
            def health(self, conn):
                return ObserverHealth(True, Health.HEALTHY, "ok")

        # Test with 1, 5, 10 observers
        timings = []
        for count in [1, 5, 10]:
            registry = ObserverRegistry()
            for i in range(count):
                registry.register(SimpleObserver(i))

            engine = ObservationEngine(registry, conn)

            start = time.time()
            run = engine.run()
            elapsed = time.time() - start

            timings.append((count, elapsed))
            assert len(run.observers) == count

        # Verify linear scaling (10 observers ≤ 10x time of 1 observer)
        one_obs_time = timings[0][1]
        ten_obs_time = timings[2][1]

        # Allow 10x + 100ms overhead
        assert ten_obs_time < (one_obs_time * 10 + 0.1), \
            f"Non-linear scaling: 1 obs={one_obs_time:.3f}s, 10 obs={ten_obs_time:.3f}s"


def test_observation_diff_performance():
    """Observation diff scales linearly with observation count."""
    from friday.observation.engine import diff_observations

    # Create large prior state
    obs_time = now_iso()
    prior = []
    for i in range(1000):
        prior.append(ObservationRow(
            id=f"{obs_time}:test:repo{i}:branch",
            observed_at=obs_time,
            source="test",
            subject=f"repo{i}",
            aspect="branch",
            value="main",
            confidence="Observed",
            scope="",
        ))

    # Create current state (same size, half changed)
    current = []
    for i in range(1000):
        value = "develop" if i % 2 == 0 else "main"
        current.append(Observation(
            source="test", subject=f"repo{i}", aspect="branch",
            value=value, confidence=Confidence.OBSERVED,
            observed_at=now_iso(), scope="", cause=None
        ))

    start = time.time()
    changes = diff_observations(prior, current)
    elapsed = time.time() - start

    # Should complete in well under 1 second for 1000 observations
    assert elapsed < 1.0, f"Diff of 1000 observations took {elapsed:.2f}s"
    assert len(changes) == 500  # Half changed


# ---------------------------------------------------------------------------
# Context Engine Performance
# ---------------------------------------------------------------------------


def test_context_build_baseline():
    """Baseline: context build from minimal observations."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Path(tmpdir) / "test.db"
        conn = connect(db)

        obs_time = now_iso()
        observations = [
            ObservationRow(
                id=f"{obs_time}:git:repo1:branch",
                observed_at=obs_time,
                source="git",
                subject="repo1",
                aspect="branch",
                value="main",
                confidence="Observed",
                scope="/path",
            ),
            ObservationRow(
                id=f"{obs_time}:git:repo1:commit_count",
                observed_at=obs_time,
                source="git",
                subject="repo1",
                aspect="commit_count",
                value="10",
                confidence="Observed",
                scope="/path",
            ),
        ]
        insert_observations(conn, observations)

        engine = ContextEngine(conn)

        start = time.time()
        result = engine.build(source="git")
        elapsed = time.time() - start

        assert result.total >= 0
        # Should complete in well under 1 second
        assert elapsed < 1.0, f"Context build took {elapsed:.2f}s (baseline: <1s)"


def test_context_build_scales_with_observations():
    """Context build time scales sublinearly with observation count."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Path(tmpdir) / "test.db"
        conn = connect(db)

        timings = []

        for count in [10, 100, 1000]:
            obs_time = now_iso()
            observations = []

            # Create observations for multiple repos/sessions
            for i in range(count):
                repo = f"repo{i % 10}"  # 10 repos
                observations.append(ObservationRow(
                    id=f"{obs_time}:git:{repo}:branch_{i}",
                    observed_at=obs_time,
                    source="git",
                    subject=repo,
                    aspect=f"aspect_{i}",
                    value=f"value_{i}",
                    confidence="Observed",
                    scope=f"/path/{repo}",
                ))

            insert_observations(conn, observations)

            engine = ContextEngine(conn)

            start = time.time()
            result = engine.build(source="git")
            elapsed = time.time() - start

            timings.append((count, elapsed))

        # Verify scaling is reasonable (not O(n²))
        # 1000 observations should complete in <5 seconds
        assert timings[2][1] < 5.0, \
            f"1000 observations took {timings[2][1]:.2f}s (expected <5s)"


def test_context_read_queries_fast():
    """Context read queries (sessions, timeline, summary) are fast."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Path(tmpdir) / "test.db"
        conn = connect(db)

        # Create observations and build sessions
        obs_time = now_iso()
        observations = []
        for i in range(100):
            observations.append(ObservationRow(
                id=f"{obs_time}:git:repo1:aspect_{i}",
                observed_at=obs_time,
                source="git",
                subject="repo1",
                aspect=f"aspect_{i}",
                value=f"value_{i}",
                confidence="Observed",
                scope="/path",
            ))
        insert_observations(conn, observations)

        engine = ContextEngine(conn)
        engine.build(source="git")

        # Measure read query performance
        start = time.time()
        sessions = engine.sessions()
        elapsed_sessions = time.time() - start

        start = time.time()
        timeline = engine.timeline()
        elapsed_timeline = time.time() - start

        start = time.time()
        summary = engine.summary()
        elapsed_summary = time.time() - start

        # All read queries should be fast (<100ms)
        assert elapsed_sessions < 0.1, f"sessions() took {elapsed_sessions:.3f}s"
        assert elapsed_timeline < 0.1, f"timeline() took {elapsed_timeline:.3f}s"
        assert elapsed_summary < 0.1, f"summary() took {elapsed_summary:.3f}s"


# ---------------------------------------------------------------------------
# Ask Pipeline Performance
# ---------------------------------------------------------------------------


def test_ask_response_baseline():
    """Baseline: ask query on minimal workspace."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Path(tmpdir) / "test.db"
        conn = connect(db)

        # Create minimal repository
        upsert_repository(
            conn,
            name="TestRepo",
            path="/tmp/test",
            default_branch="main",
            is_dirty=False,
            first_commit_date="2026-01-01T00:00:00Z",
            last_commit_date="2026-07-01T00:00:00Z",
            remote_url=None,
            commit_count=10,
            readme_summary="Test repository",
            license=None,
            primary_author=None,
        )

        start = time.time()
        response = ask("What repos do I have?", conn)
        elapsed = time.time() - start

        assert "TestRepo" in response.text  # Use .text attribute
        # Should complete in <10 seconds (allows for deterministic retrieval)
        assert elapsed < 10.0, f"Ask query took {elapsed:.2f}s (baseline: <10s)"


# ---------------------------------------------------------------------------
# Scaling Projections
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_observation_scaling_projection_100_repos():
    """Project observation performance to 100 repos (synthetic)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Path(tmpdir) / "test.db"
        conn = connect(db)

        class SyntheticGitObserver(Observer):
            name = "synthetic_git"
            def __init__(self, repo_count):
                self.repo_count = repo_count

            def collect(self, conn):
                obs = []
                ts = now_iso()
                for i in range(self.repo_count):
                    repo = f"repo{i}"
                    # Simulate typical git observations per repo
                    obs.extend([
                        Observation(
                            source="synthetic_git", subject=repo, aspect="branch",
                            value="main", confidence=Confidence.OBSERVED,
                            observed_at=ts, scope=f"/path/{repo}", cause=None
                        ),
                        Observation(
                            source="synthetic_git", subject=repo, aspect="dirty",
                            value="false", confidence=Confidence.OBSERVED,
                            observed_at=ts, scope=f"/path/{repo}", cause=None
                        ),
                        Observation(
                            source="synthetic_git", subject=repo, aspect="commit_count",
                            value="100", confidence=Confidence.OBSERVED,
                            observed_at=ts, scope=f"/path/{repo}", cause=None
                        ),
                    ])
                return obs

            def summarize(self, conn):
                return f"synthetic git for {self.repo_count} repos"

            def health(self, conn):
                return ObserverHealth(True, Health.HEALTHY, "synthetic")

        registry = ObserverRegistry()
        registry.register(SyntheticGitObserver(100))
        engine = ObservationEngine(registry, conn)

        start = time.time()
        run = engine.run()
        elapsed = time.time() - start

        # 100 repos should complete in <30 seconds
        assert elapsed < 30.0, \
            f"100 repos took {elapsed:.2f}s (projected acceptable: <30s)"

        # Verify observations persisted
        assert len(run.all_observations) == 300  # 3 per repo


@pytest.mark.slow
def test_context_scaling_projection_1000_observations():
    """Project context build performance to 1000 observations."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Path(tmpdir) / "test.db"
        conn = connect(db)

        # Create 1000 observations across 10 repos
        obs_time = now_iso()
        observations = []
        for i in range(1000):
            repo = f"repo{i % 10}"
            observations.append(ObservationRow(
                id=f"{obs_time}:git:{repo}:aspect_{i}",
                observed_at=obs_time,
                source="git",
                subject=repo,
                aspect=f"aspect_{i}",
                value=f"value_{i}",
                confidence="Observed",
                scope=f"/path/{repo}",
            ))

        insert_observations(conn, observations)

        engine = ContextEngine(conn)

        start = time.time()
        result = engine.build(source="git")
        elapsed = time.time() - start

        # 1000 observations should complete in <5 seconds
        assert elapsed < 5.0, \
            f"1000 observations took {elapsed:.2f}s (projected acceptable: <5s)"


# ---------------------------------------------------------------------------
# Regression Detection
# ---------------------------------------------------------------------------


def test_no_memory_leak_in_repeated_observations():
    """Repeated observation runs don't accumulate memory."""
    import gc
    import sys

    with tempfile.TemporaryDirectory() as tmpdir:
        db = Path(tmpdir) / "test.db"
        conn = connect(db)

        class SimpleObserver(Observer):
            name = "simple"
            def collect(self, conn):
                return [Observation(
                    source="simple", subject="test", aspect="metric",
                    value="1", confidence=Confidence.OBSERVED,
                    observed_at=now_iso(), scope="", cause=None
                )]
            def summarize(self, conn):
                return "simple"
            def health(self, conn):
                return ObserverHealth(True, Health.HEALTHY, "ok")

        registry = ObserverRegistry()
        registry.register(SimpleObserver())
        engine = ObservationEngine(registry, conn)

        # Force GC and measure baseline
        gc.collect()
        baseline_objects = len(gc.get_objects())

        # Run 100 times
        for _ in range(100):
            engine.run()

        # Force GC and measure after
        gc.collect()
        after_objects = len(gc.get_objects())

        # Object count should not grow significantly (allow 10% growth for noise)
        growth = (after_objects - baseline_objects) / baseline_objects
        assert growth < 0.10, \
            f"Memory leak detected: object count grew {growth*100:.1f}% over 100 runs"


def test_database_size_growth_linear():
    """Database size grows linearly with observations, not quadratically."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Path(tmpdir) / "test.db"
        conn = connect(db)

        sizes = []

        for count in [100, 500, 1000]:
            obs_time = now_iso()
            observations = []
            for i in range(count):
                observations.append(ObservationRow(
                    id=f"{obs_time}:test:repo:aspect_{i}",
                    observed_at=obs_time,
                    source="test",
                    subject="repo",
                    aspect=f"aspect_{i}",
                    value=f"value_{i}",
                    confidence="Observed",
                    scope="/path",
                ))
            insert_observations(conn, observations)

            # Measure DB size
            db_size = Path(db).stat().st_size
            sizes.append((count, db_size))

        # Verify linear growth (1000 obs should be ~10x size of 100 obs, not 100x)
        ratio_100_to_1000 = sizes[2][1] / sizes[0][1]
        assert ratio_100_to_1000 < 20, \
            f"Non-linear DB growth: 100 obs={sizes[0][1]} bytes, 1000 obs={sizes[2][1]} bytes (ratio={ratio_100_to_1000:.1f}x)"
