"""Milestone 5 — observation benchmarks.

Permanent regression guards for `friday observe`: append-only snapshots,
deterministic diffing, and concise reporting. Exercises diff_snapshots directly
(the 6 brief examples + rename) and the full observe() path against a real git
repo. No LLM; assertions target the exact bullet text.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from friday import observe as ob
from friday.db import (
    SnapshotRow,
    connect,
    insert_snapshot,
    latest_observation,
    upsert_repository,
)


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "kb.db")
    yield c
    c.close()


def _repo(c, name, path, **kw):
    return upsert_repository(
        c, name=name, path=path, default_branch=kw.get("branch", "main"),
        is_dirty=kw.get("dirty", False),
        first_commit_date=kw.get("first", "2025-01-01"),
        last_commit_date=kw.get("last", "2026-07-01"),
        remote_url=None, commit_count=kw.get("commits", 100),
        readme_summary=kw.get("summary"),
        license=kw.get("license"), primary_author=kw.get("author"),
    )


def _snap(path, name, **kw) -> SnapshotRow:
    return SnapshotRow(
        observed_at=kw.get("t", "2026-07-13T09:00:00+00:00"),
        repo_path=path, repo_name=name,
        default_branch=kw.get("branch", "main"),
        commit_count=kw.get("commits", 100),
        last_commit_date=kw.get("last", "2026-07-01"),
        is_dirty=kw.get("dirty", False),
        readme_hash=kw.get("readme"),
        architecture_hash=kw.get("arch"),
        identity_hash=kw.get("identity"),
    )


# --- Benchmark Ex1: dirty transition -----------------------------------------


def test_b1_became_dirty(conn):
    p = _snap("/a", "Vivaha", dirty=False)
    cur = _snap("/a", "Vivaha", dirty=True)
    out = ob.diff_snapshots([p], [cur])
    assert "Vivaha now has uncommitted changes." in out
    # Forbidden: full repository dump.
    assert "architecture:" not in "\n".join(out).lower()


# --- Benchmark Ex2: repository added ----------------------------------------


def test_b2_repo_added(conn):
    p = [_snap("/a", "A")]
    cur = [_snap("/a", "A"), _snap("/b", "B")]
    out = ob.diff_snapshots(p, cur)
    assert "New repository detected: B." in out


# --- Benchmark Ex3: README changed (not architecture) -----------------------


def test_b3_readme_changed_not_arch(conn):
    p = _snap("/a", "A", readme="r1", arch="x", identity="i1")
    cur = _snap("/a", "A", readme="r2", arch="x", identity="i1")
    out = ob.diff_snapshots([p], [cur])
    assert "A README changed." in out
    assert "A architecture changed." not in out


# --- Benchmark Ex4: no differences ------------------------------------------


def test_b4_no_changes(conn):
    p = _snap("/a", "A", readme="r1", arch="x", identity="i1")
    cur = _snap("/a", "A", readme="r1", arch="x", identity="i1")
    out = ob.diff_snapshots([p], [cur])
    assert out == ["No significant workspace changes detected."]


# --- Benchmark Ex5: repository removed ---------------------------------------


def test_b5_repo_removed(conn):
    p = [_snap("/a", "A"), _snap("/b", "B")]
    cur = [_snap("/a", "A")]
    out = ob.diff_snapshots(p, cur)
    assert "Repository removed: B." in out


# --- Benchmark Ex6: architecture changed (no "because") ---------------------


def test_b6_architecture_changed_no_speculation(conn):
    p = _snap("/a", "A", readme="r1", arch="x", identity="i1")
    cur = _snap("/a", "A", readme="r1", arch="y", identity="i1")
    out = ob.diff_snapshots([p], [cur])
    assert "A architecture changed." in out
    assert "because" not in "\n".join(out).lower()


# --- Rename: same path, different name ---------------------------------------


def test_rename_detected(conn):
    p = _snap("/a", "OldName")
    cur = _snap("/a", "NewName")
    out = ob.diff_snapshots([p], [cur])
    assert "OldName was renamed to NewName." in out


# --- Became clean -----------------------------------------------------------


def test_became_clean(conn):
    p = _snap("/a", "A", dirty=True)
    cur = _snap("/a", "A", dirty=False)
    out = ob.diff_snapshots([p], [cur])
    assert "A is now clean (no uncommitted changes)." in out


# --- Commit gain / branch move ----------------------------------------------


def test_commit_gain_and_branch(conn):
    p = _snap("/a", "A", commits=100, branch="main")
    cur = _snap("/a", "A", commits=103, branch="develop")
    out = ob.diff_snapshots([p], [cur])
    assert "A gained 3 commits." in out
    assert "A moved from main to develop." in out


# --- Unchanged repos are NOT reported ---------------------------------------


def test_unchanged_repo_silent(conn):
    p = [_snap("/a", "A", readme="r1", arch="x", identity="i1"),
         _snap("/b", "B", readme="r2", arch="y", identity="i2")]
    cur = [_snap("/a", "A", readme="r1", arch="x", identity="i1"),
           _snap("/b", "B", readme="r2", arch="y", identity="i2")]
    out = ob.diff_snapshots(p, cur)
    # Neither A nor B name appears in a change (only the "no changes" line).
    assert out == ["No significant workspace changes detected."]


# --- Appendix: baseline + real-git end-to-end --------------------------------


def test_observe_baseline_on_empty(conn):
    prev, changes = ob.observe(conn)
    assert prev is None  # no prior observation
    # No repos ingested -> nothing to snapshot; report stays honest.
    assert changes == ["No significant workspace changes detected."]


def _init_repo(d: Path) -> None:
    subprocess.run(["git", "-C", str(d), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(d), "config", "user.email", "x@y.z"], check=True)
    subprocess.run(["git", "-C", str(d), "config", "user.name", "t"], check=True)
    (d / "README.md").write_text("# Seed\n\nA project.\n")
    (d / "main.py").write_text("def main(): pass\n")
    subprocess.run(["git", "-C", str(d), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(d), "commit", "-q", "-m", "init"], check=True)


def test_observe_end_to_end_detects_readme_change(conn, tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    _init_repo(repo)
    rid = _repo(conn, "proj", str(repo), summary="Purpose:\nA project.\nMaturity:\nUnknown")
    # Build architecture + identity so the hashes are meaningful.
    from friday.db import upsert_architecture, replace_components, replace_entry_points
    from friday.architecture import analyze_and_store
    from friday.discovery import Repo
    analyze_and_store(conn, Repo(path=repo))

    # First observation: baseline.
    ob.observe(conn)
    # Edit the README on disk.
    (repo / "README.md").write_text("# Seed\n\nA project.\n\nNew section.\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "doc"], check=True)

    # Second observation must report the README change, not architecture.
    prev, changes = ob.observe(conn)
    assert prev is not None
    text = "\n".join(changes)
    assert "proj README changed." in text
    assert "proj architecture changed." not in text
    # Two observations stored, each with one row.
    assert len(latest_observation(conn)) == 1
