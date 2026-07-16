"""Milestone 7.2 — READ/WRITE separation regression tests.

Architectural guarantee: read commands (`friday context`, `sessions`, `timeline`,
`session <id>`) must NEVER mutate persistent state. Only `friday context build`
writes. These tests prove that invariant deterministically.

No LLM. No schema change. Append-only + idempotency preserved.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from friday.context import ContextEngine, EngineeringSession
from friday.context.engine import ContextBuildResult
from friday.db import connect, insert_observations, latest_observation_time, upsert_repository
from friday.observation.model import Confidence, Observation

UTC = timezone.utc


def _obs(subject, aspect, value, at):
    return Observation(source="git", subject=subject, aspect=aspect, value=value,
                       observed_at=at, scope=subject,
                       confidence=Confidence.OBSERVED)


def _t(start, mins):
    return (start + timedelta(minutes=mins)).isoformat()


@pytest.fixture
def start():
    return datetime(2026, 7, 14, 9, 0, tzinfo=UTC)


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "rw.db")
    yield c
    c.close()


def _seed(conn, start, tmp_path):
    upsert_repository(conn, name="FridayV3", path=str(tmp_path / "a"),
                      default_branch="main", is_dirty=False,
                      first_commit_date="2026-01-01", last_commit_date="2026-07-01",
                      remote_url=None, commit_count=1, readme_summary=None,
                      license=None, primary_author=None)
    obs = [_obs("FridayV3", "commit_count", "1", _t(start, 0)),
           _obs("FridayV3", "branch", "main", _t(start, 5))]
    insert_observations(conn, [o.to_row() for o in obs])


def _count_sessions(conn) -> int:
    return conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"]


def _count_observations(conn) -> int:
    return conn.execute("SELECT COUNT(*) AS n FROM observations").fetchone()["n"]


# --- build WRITES -----------------------------------------------------------


def test_build_writes_sessions(conn, start, tmp_path):
    _seed(conn, start, tmp_path)
    eng = ContextEngine(conn)
    result = eng.build()
    assert isinstance(result, ContextBuildResult)
    assert _count_sessions(conn) == 1
    assert result.created == 1
    assert result.total >= 1
    assert result.latest_observation is not None


def test_build_print_summary_shape(conn, start, tmp_path, capsys):
    _seed(conn, start, tmp_path)
    eng = ContextEngine(conn)
    print(eng.build().to_text())
    out = capsys.readouterr().out
    assert "Built" in out and "Created" in out and "Latest observation" in out
    assert "Done." in out


# --- reads NEVER write ------------------------------------------------------


def test_context_read_does_not_write(conn, start, tmp_path):
    _seed(conn, start, tmp_path)
    eng = ContextEngine(conn)
    eng.build()  # establish a baseline
    before = _count_sessions(conn)
    # READ-ONLY query methods — none should change the row count.
    _ = eng.sessions()
    _ = eng.summary()
    _ = eng.timeline()
    _ = eng.is_stale()
    assert _count_sessions(conn) == before


def test_repeated_reads_never_modify_db(conn, start, tmp_path):
    _seed(conn, start, tmp_path)
    eng = ContextEngine(conn)
    eng.build()
    before_sessions = _count_sessions(conn)
    before_obs = _count_observations(conn)
    # Simulate many read invocations (as the CLI would issue).
    for _ in range(5):
        _ = eng.sessions()
        _ = eng.timeline()
        _ = eng.summary()
    assert _count_sessions(conn) == before_sessions
    assert _count_observations(conn) == before_obs


def test_session_by_id_read_does_not_write(conn, start, tmp_path):
    _seed(conn, start, tmp_path)
    eng = ContextEngine(conn)
    s = eng.build()
    sid = eng.sessions()[0].id
    before = _count_sessions(conn)
    got = eng.session(sid)
    assert got is not None
    assert _count_sessions(conn) == before
    # Unknown id is also a pure read.
    assert eng.session("does-not-exist") is None
    assert _count_sessions(conn) == before


# --- stale detection (read-only) --------------------------------------------


def test_stale_warning_appears_when_observations_newer(conn, start, tmp_path):
    _seed(conn, start, tmp_path)
    eng = ContextEngine(conn)
    eng.build()
    assert eng.is_stale() is False
    # Add a NEW observation timestamped AFTER the build.
    newer = _t(start, 120)  # 2h later, still same data shape
    insert_observations(conn, [_obs("FridayV3", "commit_count", "2", newer).to_row()])
    # is_stale is a pure read and must now report True.
    assert eng.is_stale() is True
    # And crucially it did NOT build anything.
    assert _count_sessions(conn) == 1  # unchanged by the read


def test_is_stale_pure_read(conn, start, tmp_path):
    _seed(conn, start, tmp_path)
    eng = ContextEngine(conn)
    eng.build()
    insert_observations(conn, [_obs("FridayV3", "commit_count", "2",
                                    _t(start, 120)).to_row()])
    before = _count_sessions(conn)
    _ = eng.is_stale()
    _ = eng.is_stale()
    assert _count_sessions(conn) == before  # read-only even when stale


# --- repeated builds remain idempotent --------------------------------------


def test_repeated_builds_idempotent(conn, start, tmp_path):
    _seed(conn, start, tmp_path)
    eng = ContextEngine(conn)
    r1 = eng.build()
    n1 = _count_sessions(conn)
    r2 = eng.build()
    n2 = _count_sessions(conn)
    assert n1 == n2  # same data => same number of persisted sessions
    assert r2.created == 0  # nothing new on the second identical build
    assert r2.updated == 0


def test_build_is_append_only_across_distinct_windows(conn, start, tmp_path):
    _seed(conn, start, tmp_path)
    eng = ContextEngine(conn)
    eng.build(as_of="W1")
    n1 = _count_sessions(conn)
    eng.build(as_of="W2")  # distinct window => appended
    n2 = _count_sessions(conn)
    assert n2 == n1 + 1  # append, not overwrite
    # Re-running W2 replaces itself (no duplicate).
    eng.build(as_of="W2")
    assert _count_sessions(conn) == n2


def test_build_does_not_duplicate_observations(conn, start, tmp_path):
    _seed(conn, start, tmp_path)
    obs_before = _count_observations(conn)
    eng = ContextEngine(conn)
    eng.build()
    eng.build()
    # Observations are read-only inputs; building never copies or mutates them.
    assert _count_observations(conn) == obs_before
