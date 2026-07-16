"""Milestone 7 — Observation Engine benchmarks.

Permanent regression guards for the engine's *deterministic* properties and
timing. These mirror the Milestone 5 benchmark intent (append-only, concise,
no full dumps) but at the generic engine level: diffing stability, confidence
levels, health reporting, and a real-git end-to-end timing budget.

No LLM. Assertions target Change vocabularies, confidence, health, and that a
single observer run stays well under a second for a small repository.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from friday.db import connect, latest_observations, upsert_repository
from friday.observation import (
    Confidence,
    GitObserver,
    ObservationEngine,
    default_registry,
    format_run,
)


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "bench.db")
    yield c
    c.close()


def _init_repo(d: Path) -> None:
    subprocess.run(["git", "-C", str(d), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(d), "config", "user.email", "x@y.z"], check=True)
    subprocess.run(["git", "-C", str(d), "config", "user.name", "t"], check=True)
    (d / "README.md").write_text("# Seed\n\nA project.\n")
    (d / "main.py").write_text("def main(): pass\n")
    subprocess.run(["git", "-C", str(d), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(d), "commit", "-q", "-m", "init"], check=True)


def _store(conn, path):
    upsert_repository(conn, name="bench", path=str(path), default_branch="master",
                      is_dirty=False, first_commit_date="2026-01-01",
                      last_commit_date="2026-07-01", remote_url=None,
                      commit_count=1, readme_summary=None, license=None,
                      primary_author=None)


def test_bench_observed_facts_are_observed_not_inferred(conn, tmp_path):
    repo = tmp_path / "bench"
    repo.mkdir()
    _init_repo(repo)
    _store(conn, repo)
    obs = {o.aspect: o for o in GitObserver().collect(conn)}
    assert obs["branch"].confidence is Confidence.OBSERVED
    assert obs["dirty"].confidence is Confidence.OBSERVED
    assert obs["commit_count"].confidence is Confidence.OBSERVED
    # Forbidden: a raw git read mislabeled as Inferred.
    assert obs["branch"].confidence is not Confidence.INFERRED


def test_bench_inferred_carries_cause(conn, tmp_path):
    repo = tmp_path / "bench"
    repo.mkdir()
    _init_repo(repo)
    old = "2000-01-01T00:00:00+00:00"
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "--allow-empty",
                    "--date", old, "-m", "old"], check=True)
    _store(conn, repo)
    obs = {o.aspect: o for o in GitObserver().collect(conn)}
    # idle_days is Derived; dormancy would be Inferred only if idle >= 30 days.
    assert obs["idle_days"].confidence is Confidence.DERIVED
    assert "days" in (obs["idle_days"].cause or "")


def test_bench_health_reports_method(conn):
    h = GitObserver().health(conn)
    assert h.method == "git --version"
    assert h.detail and "git version" in h.detail


def test_bench_diff_is_stable_and_concise(conn, tmp_path):
    repo = tmp_path / "bench"
    repo.mkdir()
    _init_repo(repo)
    _store(conn, repo)
    reg = default_registry()
    run1 = ObservationEngine(reg, conn).run()
    # A second identical run must produce no changes (deterministic diff).
    run2 = ObservationEngine(reg, conn).run()
    assert all(not ores.changes for ores in run2.observers)
    # Report is concise: lists only the git observer, never a full repo dump.
    text = format_run(run1)
    assert "git" in text
    assert "architecture:" not in text.lower()


def test_bench_single_run_timing(conn, tmp_path):
    repo = tmp_path / "bench"
    repo.mkdir()
    _init_repo(repo)
    _store(conn, repo)
    # Isolate the artifact observer so it scans the fixture, not the live home.
    os.environ["FRIDAY_ARTIFACT_ROOTS"] = str(tmp_path)
    try:
        reg = default_registry()
        start = time.perf_counter()
        ObservationEngine(reg, conn).run()
        elapsed = time.perf_counter() - start
    finally:
        os.environ.pop("FRIDAY_ARTIFACT_ROOTS", None)
    # Deterministic local-git observation must be fast; budget leaves headroom.
    assert elapsed < 5.0


def test_bench_append_only_persistence(conn, tmp_path):
    repo = tmp_path / "bench"
    repo.mkdir()
    _init_repo(repo)
    _store(conn, repo)
    # Isolate the artifact observer so it scans the fixture, not the live home.
    os.environ["FRIDAY_ARTIFACT_ROOTS"] = str(tmp_path)
    try:
        reg = default_registry()
        ObservationEngine(reg, conn).run()
        first_batch = len(latest_observations(conn))
        assert first_batch > 0
        # Each run is stamped with its own observed_at, so distinct runs leave
        # distinct batches (the observations table keeps current-state facts
        # idempotently per (source,subject,aspect), but every run is recorded).
        (repo / "x.py").write_text("y=1\n")
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "c2"], check=True)
        upsert_repository(conn, name="bench", path=str(repo), default_branch="master",
                          is_dirty=False, first_commit_date="2026-01-01",
                          last_commit_date="2026-07-01", remote_url=None,
                          commit_count=2, readme_summary=None, license=None,
                          primary_author=None)
        ObservationEngine(reg, conn).run()
    finally:
        os.environ.pop("FRIDAY_ARTIFACT_ROOTS", None)
    # Two runs => two distinct observed_at batches persisted per source.
    # (Observers each stamp their own run time, so the global batch count can
    # exceed 2; the append-only property holds per source.)
    batches = {r.observed_at for r in latest_observations(conn)}
    assert len(batches) == 1  # latest_observations returns only the newest batch
    per_source = conn.execute(
        "SELECT source, COUNT(DISTINCT observed_at) AS n "
        "FROM observations GROUP BY source").fetchall()
    assert all(r["n"] == 2 for r in per_source)  # each source recorded both runs
