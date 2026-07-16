"""Milestone 7.2 — Engineering Context benchmarks.

Permanent regression guards for the context layer's deterministic properties and
correctness across the eight required scenarios: single observation, multiple
observations, cross-repository work, branch switch, idle gap, long session,
short session, append-only history, timeline ordering, summary correctness, and
no duplicated sessions.

No LLM. Assertions target session grouping, activity labels, timeline order, and
append-only persistence — the same guarantees the Observation Engine benchmarks
make at the fact level, raised to the work level.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from friday.context import (
    ContextEngine,
    EngineeringSession,
    SessionActivity,
    build_sessions,
    build_timeline,
    correlate,
    summarize_day,
)
from friday.db import connect, insert_observations, upsert_repository
from friday.observation.model import Confidence, Observation

UTC = timezone.utc


def _obs(subject, aspect, value, at, scope="", conf="Observed", cause=None):
    return Observation(
        source="git", subject=subject, aspect=aspect, value=value,
        observed_at=at, scope=scope or subject,
        confidence=Confidence.from_str(conf), cause=cause,
    )


def _t(start, mins):
    return (start + timedelta(minutes=mins)).isoformat()


@pytest.fixture
def start():
    return datetime(2026, 7, 14, 9, 0, tzinfo=UTC)


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "bench.db")
    yield c
    c.close()


# --- Bench 1: single observation -> one session -----------------------------


def test_bench_single_observation(start):
    o = _obs("FridayV3", "dirty", "true", _t(start, 0))
    s = build_sessions([o])
    assert len(s) == 1
    assert s[0].repositories == ["FridayV3"]


# --- Bench 2: multiple observations -> grouped session -----------------------


def test_bench_multiple_observations_fuse(start):
    obs = [
        _obs("FridayV3", "dirty", "true", _t(start, 0)),
        _obs("FridayV3", "commit_count", "3", _t(start, 8)),
        _obs("FridayV3", "branch", "main", _t(start, 15)),
    ]
    s = build_sessions(obs)
    assert len(s) == 1
    assert s[0].duration_min == 15.0


# --- Bench 3: cross-repository work splits -----------------------------------


def test_bench_cross_repository_splits(start):
    obs = [
        _obs("FridayV3", "commit_count", "1", _t(start, 0)),
        _obs("Vivaha", "commit_count", "1", _t(start, 4)),
    ]
    s = build_sessions(obs)
    assert len(s) == 2
    assert {x.primary_repo for x in s} == {"FridayV3", "Vivaha"}


# --- Bench 4: branch switch splits ------------------------------------------


def test_bench_branch_switch_splits(start):
    obs = [
        _obs("FridayV3", "branch", "main", _t(start, 0)),
        _obs("FridayV3", "commit_count", "2", _t(start, 5)),
        _obs("FridayV3", "branch", "feat", _t(start, 10)),
    ]
    s = build_sessions(obs)
    assert len(s) == 2
    assert s[0].branch == "main" and s[1].branch == "feat"


# --- Bench 5: idle gap splits -----------------------------------------------


def test_bench_idle_gap_splits(start):
    obs = [
        _obs("FridayV3", "commit_count", "1", _t(start, 0)),
        _obs("FridayV3", "commit_count", "2", _t(start, 200)),  # >90 min
    ]
    s = build_sessions(obs)
    assert len(s) == 2


# --- Bench 6: long session --------------------------------------------------


def test_bench_long_session(start):
    obs = [
        _obs("FridayV3", "commit_count", "1", _t(start, 0)),
        _obs("FridayV3", "commit_count", "5", _t(start, 54)),
    ]
    s = build_sessions(obs)
    assert s[0].duration_min == 54.0
    assert s[0].duration_min >= 30  # qualifies as a "long" session


# --- Bench 7: short session -------------------------------------------------


def test_bench_short_session(start):
    obs = [
        _obs("FridayV3", "dirty", "true", _t(start, 0)),
        _obs("FridayV3", "commit_count", "1", _t(start, 3)),
    ]
    s = build_sessions(obs)
    assert s[0].duration_min == 3.0
    assert s[0].duration_min < 30  # short


# --- Bench 8: append-only history (no overwrite) ----------------------------


def test_bench_append_only_no_overwrite(conn, start, tmp_path):
    upsert_repository(conn, name="FridayV3", path=str(tmp_path / "a"),
                      default_branch="main", is_dirty=False,
                      first_commit_date="2026-01-01", last_commit_date="2026-07-01",
                      remote_url=None, commit_count=1, readme_summary=None,
                      license=None, primary_author=None)
    obs = [_obs("FridayV3", "commit_count", "1", _t(start, 0)),
           _obs("FridayV3", "branch", "main", _t(start, 5))]
    insert_observations(conn, [o.to_row() for o in obs])
    eng = ContextEngine(conn)
    eng.build(as_of="2026-07-14T09:00:00+00:00")
    first = len(eng.sessions())
    assert first == 1
    # A second, genuinely distinct window appends rather than overwrites.
    eng.build(as_of="2026-07-14T17:00:00+00:00")
    assert len(eng.sessions()) == 2  # two persisted sessions, not one
    # Re-running the SAME explicit window replaces (idempotent), not appends.
    eng.build(as_of="2026-07-14T09:00:00+00:00")
    assert len(eng.sessions()) == 2


def test_bench_same_window_is_idempotent(conn, start, tmp_path):
    upsert_repository(conn, name="FridayV3", path=str(tmp_path / "a"),
                      default_branch="main", is_dirty=False,
                      first_commit_date="2026-01-01", last_commit_date="2026-07-01",
                      remote_url=None, commit_count=1, readme_summary=None,
                      license=None, primary_author=None)
    obs = [_obs("FridayV3", "commit_count", "1", _t(start, 0))]
    insert_observations(conn, [o.to_row() for o in obs])
    eng = ContextEngine(conn)
    eng.build(as_of="W1")
    eng.build(as_of="W1")  # identical window key
    # Same (built_at, repo, start) => one row, not two.
    assert len(eng.sessions()) == 1


def test_bench_deterministic_default_is_idempotent(conn, start, tmp_path):
    upsert_repository(conn, name="FridayV3", path=str(tmp_path / "a"),
                      default_branch="main", is_dirty=False,
                      first_commit_date="2026-01-01", last_commit_date="2026-07-01",
                      remote_url=None, commit_count=1, readme_summary=None,
                      license=None, primary_author=None)
    obs = [_obs("FridayV3", "commit_count", "1", _t(start, 0)),
           _obs("FridayV3", "branch", "main", _t(start, 5))]
    insert_observations(conn, [o.to_row() for o in obs])
    eng = ContextEngine(conn)
    # Default as_of keys on the latest observation timestamp, so repeated
    # builds over unchanged data replace the same window (the CLI behavior).
    eng.build()
    eng.build()
    eng.build()
    assert len(eng.sessions()) == 1


# --- Bench 9: timeline ordering ---------------------------------------------


def test_bench_timeline_ordered_with_idle(start):
    obs = [
        _obs("FridayV3", "commit_count", "1", _t(start, 0)),
        _obs("FridayV3", "commit_count", "2", _t(start, 120)),  # idle gap
        _obs("FridayV3", "commit_count", "3", _t(start, 125)),
    ]
    s = [correlate(x) for x in build_sessions(obs)]
    tl = build_timeline(s)
    assert [e.kind for e in tl] == ["session", "idle", "session"]
    # Strict chronological order.
    times = [e.start_time for e in tl]
    assert times == sorted(times)


# --- Bench 10: summary correctness ------------------------------------------


def test_bench_summary_correctness(start):
    obs = [
        _obs("FridayV3", "commit_count", "1", _t(start, 0)),
        _obs("FridayV3", "commit_count", "2", _t(start, 30)),
        _obs("Vivaha", "commit_count", "1", _t(start, 40)),
    ]
    s = [correlate(x) for x in build_sessions(obs)]
    summ = summarize_day(s, day="2026-07-14")
    assert summ.session_count == 2
    assert summ.repositories == ["FridayV3", "Vivaha"]
    assert summ.estimated_active_min == 30.0
    assert summ.context_switches == 1
    assert summ.longest_session_min == 30.0
    assert summ.most_active_repo == "FridayV3"
    # Current focus reflects the LAST session.
    assert "Vivaha" in (summ.current_focus or "")


# --- Bench 11: no duplicated sessions ---------------------------------------


def test_bench_no_duplicated_sessions(conn, start, tmp_path):
    upsert_repository(conn, name="FridayV3", path=str(tmp_path / "a"),
                      default_branch="main", is_dirty=False,
                      first_commit_date="2026-01-01", last_commit_date="2026-07-01",
                      remote_url=None, commit_count=1, readme_summary=None,
                      license=None, primary_author=None)
    obs = [_obs("FridayV3", "commit_count", "1", _t(start, 0)),
           _obs("FridayV3", "commit_count", "2", _t(start, 10))]
    insert_observations(conn, [o.to_row() for o in obs])
    eng = ContextEngine(conn)
    eng.build(as_of="A")
    eng.build(as_of="B")
    eng.build(as_of="C")
    ids = [s.id for s in eng.sessions()]
    assert len(ids) == len(set(ids))  # never duplicates a session row


# --- Bench 12: correlation is conservative (unknown beats wrong) ------------


def test_bench_correlation_conservative(start):
    # Only a non-committal activity fact => stays UNKNOWN, never guesses.
    o = _obs("FridayV3", "activity", "active", _t(start, 0))
    s = build_sessions([o])[0]
    correlate(s)
    assert s.activity is SessionActivity.UNKNOWN


# --- Bench 13: engine reads from frozen observation store -------------------


def test_bench_engine_reads_observations(conn, start, tmp_path):
    upsert_repository(conn, name="FridayV3", path=str(tmp_path / "a"),
                      default_branch="main", is_dirty=False,
                      first_commit_date="2026-01-01", last_commit_date="2026-07-01",
                      remote_url=None, commit_count=1, readme_summary=None,
                      license=None, primary_author=None)
    obs = [_obs("FridayV3", "commit_count", "3", _t(start, 0)),
           _obs("FridayV3", "branch", "main", _t(start, 5))]
    insert_observations(conn, [o.to_row() for o in obs])
    eng = ContextEngine(conn)
    eng.build()
    sessions = eng.sessions()
    assert len(sessions) == 1
    # Committing label, observed confidence (direct fact).
    assert sessions[0].activity is SessionActivity.COMMITTING
    # Session references observations, does not duplicate them.
    assert set(sessions[0].observations) == {o.id for o in obs}
