"""Milestone 7 — Observation Engine tests.

Deterministic unit tests for the generic engine, the Observation model, the
registry, and confidence/health reporting. Observers are exercised against
real git repos where git facts matter; the engine diff itself is tested with
hand-built Observation/row lists so it is observer-independent.

No LLM, no planner, no daemon. Assertions target Observation/Change records,
confidence levels, and health status.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from friday.db import ObservationRow, connect, latest_observations, upsert_repository
from friday.observation import (
    Change,
    Confidence,
    GitObserver,
    Health,
    Observation,
    ObservationEngine,
    ObserverHealth,
    ObserverRegistry,
    default_registry,
    diff_observations,
    format_run,
)
from friday.observation.engine import ObservationRun, ObserverResult


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "obs.db")
    yield c
    c.close()


# --- Model -----------------------------------------------------------------


def test_observation_id_is_deterministic():
    a = Observation("git", "Vivaha", "dirty", "true", observed_at="T")
    b = Observation("git", "Vivaha", "dirty", "true", observed_at="T")
    assert a.id == b.id
    assert a.key() == ("Vivaha", "dirty")


def test_confidence_from_str_roundtrip():
    assert Confidence.from_str("Derived") is Confidence.DERIVED
    assert Confidence.from_str("INFERRED") is Confidence.INFERRED
    assert Confidence.from_str("nonsense") is Confidence.OBSERVED


def test_observation_to_row_roundtrip(conn):
    o = Observation("git", "A", "dirty", "true", Confidence.OBSERVED,
                    observed_at="T", cause="uncommitted changes")
    row = o.to_row()
    assert isinstance(row, ObservationRow)
    back = Observation.from_row(row)
    assert back.subject == "A" and back.value == "true"
    assert back.confidence is Confidence.OBSERVED


# --- Registry --------------------------------------------------------------


def test_registry_registers_and_lists_in_order():
    reg = ObserverRegistry()
    reg.register(GitObserver())
    assert reg.names() == ["git"]
    assert "git" in reg
    with pytest.raises(ValueError):
        reg.register(GitObserver())  # duplicate name


def test_default_registry_seeded():
    assert default_registry().names() == ["git", "terminal", "artifact", "github", "research", "calendar"]


# --- Engine diff (observer-independent) -------------------------------------


def _row(subject, aspect, value, conf="Observed", at="T"):
    return ObservationRow(
        id=f"{at}:git:{subject}:{aspect}", observed_at=at, source="git",
        subject=subject, aspect=aspect, value=value, confidence=conf,
    )


def test_diff_new_fact():
    cur = [Observation("git", "A", "dirty", "true", Confidence.OBSERVED)]
    out = diff_observations([], cur)
    assert len(out) == 1
    assert out[0].kind == "dirty observed"
    assert out[0].new == "true"
    assert out[0].confidence is Confidence.OBSERVED


def test_diff_value_change():
    prior = [_row("A", "branch", "main")]
    cur = [Observation("git", "A", "branch", "develop", Confidence.OBSERVED)]
    out = diff_observations(prior, cur)
    assert out[0].kind == "branch changed"
    assert out[0].old == "main" and out[0].new == "develop"


def test_diff_removed_fact():
    prior = [_row("A", "remote_url", "git@x")]
    out = diff_observations(prior, [])
    assert out[0].kind == "remote_url removed"
    assert out[0].old == "git@x"


def test_diff_unchanged_silent():
    prior = [_row("A", "dirty", "false")]
    cur = [Observation("git", "A", "dirty", "false", Confidence.OBSERVED)]
    assert diff_observations(prior, cur) == []


def test_diff_preserves_cause_for_inferred():
    prior = [_row("A", "dormant", "false")]
    cur = [Observation("git", "A", "dormant", "true", Confidence.INFERRED,
                       cause="idle 40 days")]
    out = diff_observations(prior, cur)
    assert out[0].confidence is Confidence.INFERRED
    assert out[0].cause == "idle 40 days"


# --- Engine run against real git -------------------------------------------


def _init_repo(d: Path, message="init") -> None:
    subprocess.run(["git", "-C", str(d), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(d), "config", "user.email", "x@y.z"], check=True)
    subprocess.run(["git", "-C", str(d), "config", "user.name", "t"], check=True)
    (d / "README.md").write_text("# Seed\n\nA project.\n")
    (d / "main.py").write_text("def main(): pass\n")
    subprocess.run(["git", "-C", str(d), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(d), "commit", "-q", "-m", message], check=True)


def _commit_at(d: Path, message: str, date: str) -> None:
    """Commit at a fixed author/committer date (ISO, UTC)."""
    env = {"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
    subprocess.run(["git", "-C", str(d), "commit", "-q", "--allow-empty",
                    "-m", message], env=env, check=True)


def test_git_observer_health(conn):
    h = GitObserver().health(conn)
    assert isinstance(h, ObserverHealth)
    assert h.healthy is True
    assert h.status is Health.HEALTHY


def test_git_observer_collect_emits_observed_facts(conn, tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    _init_repo(repo)
    upsert_repository(conn, name="proj", path=str(repo), default_branch="master",
                      is_dirty=False, first_commit_date="2026-01-01",
                      last_commit_date="2026-07-01", remote_url=None,
                      commit_count=1, readme_summary=None, license=None,
                      primary_author=None)
    obs = observations_as_dict(GitObserver().collect(conn))
    assert obs[("proj", "dirty")].value == "false"
    assert obs[("proj", "commit_count")].value == "1"
    assert obs[("proj", "branch")].value == "master"
    # Derived facts always present for a repo with a commit date.
    assert ("proj", "idle_days") in obs
    assert ("proj", "activity") in obs
    assert obs[("proj", "activity")].confidence is Confidence.DERIVED


def test_git_observer_detects_dirty_and_dormant(conn, tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    _init_repo(repo)
    # Add a commit dated far in the past so idle_days >= DORMANT_DAYS.
    old = "2000-01-01T00:00:00+00:00"
    _commit_at(repo, "old work", old)
    (repo / "dirty.txt").write_text("x")  # make working tree dirty
    # master is git's default initial branch in this test environment.
    upsert_repository(conn, name="proj", path=str(repo), default_branch="master",
                      is_dirty=True, first_commit_date="2000-01-01",
                      last_commit_date=old, remote_url=None, commit_count=2,
                      readme_summary=None, license=None, primary_author=None)
    obs = observations_as_dict(GitObserver().collect(conn))
    assert obs[("proj", "dirty")].value == "true"
    assert obs[("proj", "dormant")].value == "true"
    assert obs[("proj", "dormant")].confidence is Confidence.INFERRED
    assert "idle" in (obs[("proj", "dormant")].cause or "")


def test_git_observer_detects_merge_and_reverts(conn, tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    _init_repo(repo, message="init")
    # A branch whose work we merge (a merge commit).
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "feat"], check=True)
    (repo / "f.py").write_text("x=1\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "feat work"], check=True)
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "master"], check=True)
    subprocess.run(["git", "-C", str(repo), "merge", "-q", "--no-ff", "feat",
                    "-m", "Merge branch 'feat'"], check=True)
    # Two commits whose messages mention revert (the observer counts these).
    (repo / "r1.py").write_text("a\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "revert bad config"], check=True)
    (repo / "r2.py").write_text("b\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "revert flaky test"], check=True)
    upsert_repository(conn, name="proj", path=str(repo), default_branch="master",
                      is_dirty=False, first_commit_date="2026-01-01",
                      last_commit_date="2026-07-01", remote_url=None,
                      commit_count=5, readme_summary=None, license=None,
                      primary_author=None)
    obs = observations_as_dict(GitObserver().collect(conn))
    assert int(obs[("proj", "merge_events")].value) >= 1
    assert int(obs[("proj", "revert_events")].value) >= 2
    # Two revert messages -> inferred "repeated reverts".
    assert obs[("proj", "repeated_reverts")].value == "true"
    assert obs[("proj", "repeated_reverts")].confidence is Confidence.INFERRED


def test_engine_run_persists_and_diff_across_runs(conn, tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    _init_repo(repo)
    upsert_repository(conn, name="proj", path=str(repo), default_branch="main",
                      is_dirty=False, first_commit_date="2026-01-01",
                      last_commit_date="2026-07-01", remote_url=None,
                      commit_count=1, readme_summary=None, license=None,
                      primary_author=None)
    reg = default_registry()
    # Run 1: baseline.
    run1 = ObservationEngine(reg, conn).run()
    assert run1.observers[0].health.healthy
    stored = latest_observations(conn)
    assert len(stored) > 0

    # Mutate: add a commit + dirty the tree.
    (repo / "more.py").write_text("y=2\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "second"], check=True)
    (repo / "dirty.txt").write_text("z")
    upsert_repository(conn, name="proj", path=str(repo), default_branch="main",
                      is_dirty=True, first_commit_date="2026-01-01",
                      last_commit_date="2026-07-01", remote_url=None,
                      commit_count=2, readme_summary=None, license=None,
                      primary_author=None)

    # Run 2: must report commit_count change + dirty change.
    run2 = ObservationEngine(reg, conn).run()
    kinds = {c.kind for ores in run2.observers for c in ores.changes}
    assert "commit_count changed" in kinds
    assert "dirty changed" in kinds
    rendered = format_run(run2)
    assert "git" in rendered
    assert "commit_count changed" in rendered


def test_engine_run_is_idempotent_on_repeat(conn, tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    _init_repo(repo)
    upsert_repository(conn, name="proj", path=str(repo), default_branch="main",
                      is_dirty=False, first_commit_date="2026-01-01",
                      last_commit_date="2026-07-01", remote_url=None,
                      commit_count=1, readme_summary=None, license=None,
                      primary_author=None)
    # Isolate the artifact observer so it scans the fixture, not the live home.
    os.environ["FRIDAY_ARTIFACT_ROOTS"] = str(tmp_path)
    try:
        reg = default_registry()
        ObservationEngine(reg, conn).run()
        run2 = ObservationEngine(reg, conn).run()  # identical state
    finally:
        os.environ.pop("FRIDAY_ARTIFACT_ROOTS", None)
    # No meaningful change on a no-op second run.
    assert all(not ores.changes for ores in run2.observers)


# --- helpers ---------------------------------------------------------------


def observations_as_dict(rows):
    return {(o.subject, o.aspect): o for o in rows}
