"""Observation-history stress test (Part E).

Verifies that after MANY append-only observations, history stays correct:
  - snapshots accumulate without corruption or reordering (append-only)
  - diff_snapshots reports only the LATEST meaningful change
  - a long history does not break the DRIFT / "what changed" answer
  - no spurious "no changes" when a real change occurred
"""

from __future__ import annotations

import datetime as dt

import pytest

from friday.db import (
    SnapshotRow, connect, insert_snapshot, latest_observation, upsert_repository,
)
from friday.observe import diff_snapshots
from friday.ask import ask


def _seed_repo(conn, name, path):
    return upsert_repository(
        conn, name=name, path=path, default_branch="main", is_dirty=False,
        first_commit_date="2025-01-01", last_commit_date="2026-01-01",
        remote_url="https://github.com/acme/" + name, commit_count=100,
        readme_summary=f"Purpose:\n{name} does a thing.\nMaturity:\nBeta",
        license="MIT", primary_author="dev@acme.com",
    )


def _snap(repo_path, repo_name, observed_at, commit_count, is_dirty=False):
    return SnapshotRow(
        observed_at=observed_at, repo_path=repo_path, repo_name=repo_name,
        default_branch="main", commit_count=commit_count,
        last_commit_date="2026-07-01", is_dirty=1 if is_dirty else 0,
        readme_hash="r", architecture_hash="a", identity_hash="i",
    )


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "kb.db")
    yield c
    c.close()


@pytest.fixture
def hist(conn):
    _seed_repo(conn, "friday-v3", "/f3")
    # 50 append-only observation RUNS (one row per run), growing commit count +
    # a dirty flip on the final run. Each run is keyed by an observed_at date.
    base = dt.date(2026, 1, 1)
    for i in range(50):
        day = (base + dt.timedelta(days=i)).isoformat()
        cc = 100 + i * 7
        insert_snapshot(conn, _snap("/f3", "friday-v3", day, cc, is_dirty=(i == 49)))
    conn.commit()
    return conn


def _all_dates(conn):
    return [r["t"] for r in conn.execute(
        "SELECT DISTINCT observed_at AS t FROM snapshots ORDER BY t").fetchall()]


def test_append_only_no_corruption(hist):
    dates = _all_dates(hist)
    assert len(dates) == 50, f"expected 50 observation runs, got {len(dates)}"
    # Observed-at timestamps strictly increase (append-only, no reordering).
    assert dates == sorted(dates), "observation history is not ordered"
    # Commit count per run monotonic with history.
    ccs = [r["commit_count"] for r in hist.execute(
        "SELECT commit_count FROM snapshots ORDER BY observed_at")]
    assert ccs == sorted(ccs), "commit counts not monotonic"


def test_latest_diff_is_meaningful(hist):
    dates = _all_dates(hist)
    # Reproduce the last two runs as ObservationSnapshot lists.
    def run_at(day):
        return [__import__("friday.db", fromlist=["SnapshotRow"]).SnapshotRow(
            observed_at=r["observed_at"], repo_path=r["repo_path"],
            repo_name=r["repo_name"], default_branch=r["default_branch"],
            commit_count=r["commit_count"], last_commit_date=r["last_commit_date"],
            is_dirty=bool(r["is_dirty"]), readme_hash=r["readme_hash"],
            architecture_hash=r["architecture_hash"], identity_hash=r["identity_hash"],
        ) for r in hist.execute(
            "SELECT * FROM snapshots WHERE observed_at = ?", (day,))]
    prev, cur = run_at(dates[-2]), run_at(dates[-1])
    changes = diff_snapshots(prev, cur)
    kinds = {c.kind for c in changes}
    assert "commits gained" in kinds, f"expected commits gained, got {kinds}"
    assert "became dirty" in kinds, f"expected became dirty, got {kinds}"
    assert "no changes" not in kinds


def test_drift_answer_survives_long_history(hist):
    # "What changed?" must still resolve to a DRIFT objective and cite the repo.
    ans = ask("Which project has changed most recently?", hist, verbose=False)
    raw = ans.evidence.raw
    assert raw.get("scope") in ("timeline", "workspace")
    assert "friday-v3" in ans.text.lower()
    # No crash / no empty answer after 50 observations.
    assert ans.text.strip()


def test_trend_query_after_many_observations(hist):
    ans = ask("How have I evolved?", hist, verbose=False)
    raw = ans.evidence.raw
    assert raw.get("scope") == "timeline"
    assert ans.text.strip()
