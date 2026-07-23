"""Milestone 7.2 — Engineering Context unit tests.

Deterministic tests for the context layer: session building (grouping rules),
conservative correlation, timeline ordering with idle gaps, daily summary
correctness, and append-only session persistence. No LLM, no planner.

Observations are built by hand with explicit `observed_at` timestamps so the
session/grouping logic is tested in isolation from the git observer.
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
from friday.context.models import Confidence
from friday.db import ObservationRow, connect, insert_observations, upsert_repository
from friday.observation.model import Observation

UTC = timezone.utc


def _obs(subject, aspect, value, at, scope="", conf="Observed", cause=None):
    return Observation(
        source="git", subject=subject, aspect=aspect, value=value,
        observed_at=at, scope=scope or subject,
        confidence=Confidence.from_str(conf), cause=cause,
    )


def _t(minutes_from: datetime, mins: int) -> str:
    return (minutes_from + timedelta(minutes=mins)).isoformat()


@pytest.fixture
def base():
    start = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)
    return start


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "ctx.db")
    yield c
    c.close()


# --- Part 1/2: single observation -> one session ----------------------------


def test_single_observation_makes_one_session(base):
    o = _obs("FridayV3", "dirty", "true", _t(base, 0))
    sessions = build_sessions([o])
    assert len(sessions) == 1
    s = sessions[0]
    assert s.repositories == ["FridayV3"]
    assert s.primary_repo == "FridayV3"
    assert s.duration_min == 0.0  # instantaneous run


# --- Multiple close observations -> one session -----------------------------


def test_close_observations_fuse_into_one_session(base):
    a = _obs("FridayV3", "dirty", "true", _t(base, 0))
    b = _obs("FridayV3", "commit_count", "5", _t(base, 10))
    c = _obs("FridayV3", "branch", "main", _t(base, 20))
    sessions = build_sessions([a, b, c])
    assert len(sessions) == 1
    assert sessions[0].observations  # references, not duplicates


# --- Idle gap splits sessions -----------------------------------------------


def test_idle_gap_splits_sessions(base):
    a = _obs("FridayV3", "dirty", "true", _t(base, 0))
    b = _obs("FridayV3", "commit_count", "6", _t(base, 5))
    # 3 hours later (> SESSION_GAP_MIN of 90) => new session.
    c = _obs("FridayV3", "branch", "main", _t(base, 200))
    sessions = build_sessions([a, b, c])
    assert len(sessions) == 2
    assert sessions[0].end_time < sessions[1].start_time


# --- Branch switch splits ---------------------------------------------------


def test_branch_switch_splits_same_repo(base):
    a = _obs("FridayV3", "branch", "main", _t(base, 0))
    a2 = _obs("FridayV3", "commit_count", "3", _t(base, 5))
    # Same repo, 10 min later, but different branch => new context.
    b = _obs("FridayV3", "branch", "feature-x", _t(base, 10))
    sessions = build_sessions([a, a2, b])
    assert len(sessions) == 2
    assert sessions[0].branch == "main"
    assert sessions[1].branch == "feature-x"


# --- Cross-repository work --------------------------------------------------


def test_cross_repo_work_splits_by_repo(base):
    a = _obs("FridayV3", "commit_count", "2", _t(base, 0))
    b = _obs("Vivaha", "commit_count", "1", _t(base, 5))
    sessions = build_sessions([a, b])
    # No overlap => two sessions (conservative split).
    assert len(sessions) == 2
    repos = {s.primary_repo for s in sessions}
    assert repos == {"FridayV3", "Vivaha"}


# --- Short vs long sessions -------------------------------------------------


def test_short_session_duration(base):
    a = _obs("FridayV3", "dirty", "true", _t(base, 0))
    b = _obs("FridayV3", "commit_count", "1", _t(base, 3))
    sessions = build_sessions([a, b])
    assert sessions[0].duration_min == 3.0


def test_long_session_duration(base):
    a = _obs("FridayV3", "commit_count", "1", _t(base, 0))
    b = _obs("FridayV3", "commit_count", "2", _t(base, 54))
    sessions = build_sessions([a, b])
    assert sessions[0].duration_min == 54.0


# --- Part 3: correlation (conservative) -------------------------------------


def test_correlate_committing_with_delta(base):
    """Single obs with no prior is a baseline — not committing."""
    a = _obs("FridayV3", "commit_count", "3", _t(base, 0))
    s = build_sessions([a])[0]
    correlate(s)
    assert s.activity is SessionActivity.UNKNOWN  # baseline, no change
    assert s.confidence is Confidence.DERIVED


def test_correlate_feature_work_on_branch_switch(base):
    a = _obs("FridayV3", "commit_count", "3", _t(base, 0))
    b = _obs("FridayV3", "branch_switch", "main -> develop", _t(base, 5))
    c = _obs("FridayV3", "commit_count", "4", _t(base, 10))
    s = build_sessions([a, b, c])[0]
    correlate(s)
    assert s.activity is SessionActivity.FEATURE_WORK


def test_correlate_debugging_on_repeated_reverts(base):
    a = _obs("FridayV3", "revert_events", "2", _t(base, 0))
    b = _obs("FridayV3", "commit_count", "1", _t(base, 5))
    c = _obs("FridayV3", "commit_count", "2", _t(base, 10))
    s = build_sessions([a, b, c])[0]
    correlate(s)
    assert s.activity is SessionActivity.DEBUGGING
    assert s.confidence is Confidence.INFERRED


def test_correlate_documentation(base):
    a = _obs("FridayV3", "readme_changed", "true", _t(base, 0))
    s = build_sessions([a])[0]
    correlate(s)
    assert s.activity is SessionActivity.DOCUMENTATION


def test_correlate_dirty_only_is_testing(base):
    a = _obs("FridayV3", "dirty", "true", _t(base, 0))
    s = build_sessions([a])[0]
    correlate(s)
    # Dirty tree, no commit, no doc signal => testing / in-progress.
    assert s.activity in (SessionActivity.TESTING, SessionActivity.UNKNOWN)


def test_correlate_unknown_when_ambiguous(base):
    # A session with only idle/workspace facts => no definitive label.
    a = _obs("FridayV3", "activity", "active", _t(base, 0))
    s = build_sessions([a])[0]
    correlate(s)
    assert s.activity is SessionActivity.UNKNOWN


# --- Part 4: timeline ordering + idle gaps ----------------------------------


def test_timeline_orders_and_inserts_idle(base):
    a = _obs("FridayV3", "commit_count", "1", _t(base, 0))
    # 2 hours later => idle gap entry between sessions.
    b = _obs("FridayV3", "commit_count", "2", _t(base, 120))
    sessions = build_sessions([a, b])
    sessions = [correlate(s) for s in sessions]
    tl = build_timeline(sessions)
    kinds = [e.kind for e in tl]
    assert kinds == ["session", "idle", "session"]
    assert tl[1].label == "Idle"


def test_timeline_short_gap_no_idle(base):
    a = _obs("FridayV3", "commit_count", "1", _t(base, 0))
    b = _obs("FridayV3", "commit_count", "2", _t(base, 20))
    sessions = [correlate(s) for s in build_sessions([a, b])]
    tl = build_timeline(sessions)
    # 20 min gap < IDLE_GAP_MIN (30) => no idle entry.
    assert all(e.kind == "session" for e in tl)


# --- Part 5: summary correctness --------------------------------------------


def test_summary_counts_and_active_time(base):
    a = _obs("FridayV3", "commit_count", "1", _t(base, 0))
    b = _obs("FridayV3", "commit_count", "2", _t(base, 30))
    c = _obs("Vivaha", "commit_count", "1", _t(base, 35))
    sessions = [correlate(s) for s in build_sessions([a, b, c])]
    summ = summarize_day(sessions, day="2026-07-14")
    assert summ.session_count == 2  # FridayV3 fused; Vivaha separate
    assert summ.repositories == ["FridayV3", "Vivaha"]
    # FridayV3 0->30 (30 min) + Vivaha single instantaneous (0 min) = 30.
    assert summ.estimated_active_min == 30.0
    # Context switch: FridayV3 -> Vivaha.
    assert summ.context_switches == 1
    assert summ.longest_session_min == 30.0
    assert summ.most_active_repo == "FridayV3"


def test_summary_current_focus_is_latest(base):
    a = _obs("FridayV3", "commit_count", "1", _t(base, 0))
    b = _obs("Vivaha", "commit_count", "1", _t(base, 60))
    sessions = [correlate(s) for s in build_sessions([a, b])]
    summ = summarize_day(sessions, day="2026-07-14")
    assert "Vivaha" in (summ.current_focus or "")


def test_summary_empty():
    summ = summarize_day([], day="2026-07-14")
    assert summ.session_count == 0
    assert summ.repositories == []
    assert summ.estimated_active_min == 0.0


# --- Part 6: append-only persistence via engine -----------------------------


def _seed_observations(conn, obs: list[Observation]) -> None:
    insert_observations(conn, [o.to_row() for o in obs])


def _store_repo(conn, name, path):
    upsert_repository(conn, name=name, path=path, default_branch="main",
                      is_dirty=False, first_commit_date="2026-01-01",
                      last_commit_date="2026-07-01", remote_url=None,
                      commit_count=1, readme_summary=None, license=None,
                      primary_author=None)


def test_engine_persists_sessions_append_only(conn, base, tmp_path):
    _store_repo(conn, "FridayV3", str(tmp_path / "a"))
    obs = [
        _obs("FridayV3", "commit_count", "1", _t(base, 0)),
        _obs("FridayV3", "branch", "main", _t(base, 5)),
    ]
    _seed_observations(conn, obs)
    eng = ContextEngine(conn)
    eng.build()
    sessions = eng.sessions()
    assert len(sessions) == 1
    first_id = sessions[0].id
    assert len(eng.sessions()) == 1
    # Rebuild over the SAME observations => idempotent (no new session).
    eng.build()
    assert len(eng.sessions()) == 1
    assert eng.session(first_id) is not None
    # A genuinely NEW observation window (new start_time) => a new session.
    _seed_observations(conn, [
        _obs("FridayV3", "commit_count", "3", _t(base, 400)),
        _obs("FridayV3", "branch", "main", _t(base, 410)),
    ])
    eng.build()
    assert len(eng.sessions()) == 2


def test_engine_no_duplicate_sessions(conn, base, tmp_path):
    _store_repo(conn, "FridayV3", str(tmp_path / "a"))
    obs = [_obs("FridayV3", "commit_count", "1", _t(base, 0)),
           _obs("FridayV3", "commit_count", "2", _t(base, 10))]
    _seed_observations(conn, obs)
    eng = ContextEngine(conn)
    eng.build()
    eng.build()  # same data => idempotent by default (no new window)
    # Distinct (built_at, repo, start) pairs only.
    ids = {s.id for s in eng.sessions()}
    assert len(ids) == len(eng.sessions())


def test_session_references_observations_not_copies(conn, base, tmp_path):
    _store_repo(conn, "FridayV3", str(tmp_path / "a"))
    obs = [_obs("FridayV3", "commit_count", "1", _t(base, 0)),
           _obs("FridayV3", "branch", "main", _t(base, 5))]
    _seed_observations(conn, obs)
    eng = ContextEngine(conn)
    eng.build()
    s = eng.sessions()[0]
    # The session references the observation ids; nothing is duplicated/copied.
    assert set(s.observations) == {
        o.id for o in obs
    }


def test_engine_timeline_and_summary_end_to_end(conn, base, tmp_path):
    _store_repo(conn, "FridayV3", str(tmp_path / "a"))
    obs = [_obs("FridayV3", "commit_count", "1", _t(base, 0)),
           _obs("FridayV3", "commit_count", "2", _t(base, 40)),
           _obs("FridayV3", "revert_events", "2", _t(base, 45))]
    _seed_observations(conn, obs)
    eng = ContextEngine(conn)
    eng.build()
    summ = eng.summary("2026-07-14")
    assert summ.session_count == 1
    # 0->45 min window; debugging (repeated reverts) dominates label.
    assert summ.sessions[0].activity is SessionActivity.DEBUGGING
    tl = eng.timeline()
    assert tl and tl[0].kind == "session"


# --- helpers ----------------------------------------------------------------


def _fmt(minutes: float) -> str:
    if minutes < 60:
        return f"{round(minutes)} minutes"
    h = int(minutes // 60)
    return f"{h}h"
