"""Milestone 5/6 — observation benchmarks.

Permanent regression guards for `friday observe`: append-only snapshots,
deterministic diffing, concise reporting, and (M6) engineering-language output
with evidence-backed causes. Exercises diff_snapshots / Change directly (the 6
brief examples + rename) and the full observe() path against a real git repo.
No LLM; assertions target deterministic Change records and rendered text.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from friday import observe as ob
from friday.db import (
    SnapshotRow,
    connect,
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


def _kinds(changes):
    return {c.kind for c in changes}


def _render(changes):
    return "\n".join(ob._render_change(c) for c in changes)


# --- Benchmark Ex1: dirty transition -----------------------------------------


def test_b1_became_dirty(conn):
    p = _snap("/a", "Vivaha", dirty=False)
    cur = _snap("/a", "Vivaha", dirty=True)
    out = ob.diff_snapshots([p], [cur])
    assert "became dirty" in _kinds(out)
    assert "Vivaha now has uncommitted changes because" in _render(out)
    # Forbidden: full repository dump.
    assert "architecture:" not in _render(out).lower()


# --- Benchmark Ex2: repository added ----------------------------------------


def test_b2_repo_added(conn):
    p = [_snap("/a", "A")]
    cur = [_snap("/a", "A"), _snap("/b", "B")]
    out = ob.diff_snapshots(p, cur)
    assert "New repository detected: B." in _render(out)


# --- Benchmark Ex3: README changed (not architecture) -----------------------


def test_b3_readme_changed_not_arch(conn):
    p = _snap("/a", "A", readme="r1", arch="x", identity="i1")
    cur = _snap("/a", "A", readme="r2", arch="x", identity="i1")
    out = ob.diff_snapshots([p], [cur])
    assert "A README changed because" in _render(out)
    assert "A architecture changed." not in _render(out)


# --- Benchmark Ex4: no differences ------------------------------------------


def test_b4_no_changes(conn):
    p = _snap("/a", "A", readme="r1", arch="x", identity="i1")
    cur = _snap("/a", "A", readme="r1", arch="x", identity="i1")
    out = ob.diff_snapshots([p], [cur])
    assert len(out) == 1 and out[0].kind == "no changes"
    assert "No significant workspace changes detected." in _render(out)


# --- Benchmark Ex5: repository removed ---------------------------------------


def test_b5_repo_removed(conn):
    p = [_snap("/a", "A"), _snap("/b", "B")]
    cur = [_snap("/a", "A")]
    out = ob.diff_snapshots(p, cur)
    assert "Repository removed: B." in _render(out)


# --- Benchmark Ex6: architecture changed (no speculation) --------------------


def test_b6_architecture_changed_no_speculation(conn):
    p = _snap("/a", "A", readme="r1", arch="x", identity="i1")
    cur = _snap("/a", "A", readme="r1", arch="y", identity="i1")
    out = ob.diff_snapshots([p], [cur])
    assert "A architecture changed because" in _render(out)
    # Cause is evidence-backed, not invented narrative.
    ch = next(c for c in out if c.kind == "architecture changed")
    assert ch.cause and "framework" in ch.cause.lower()


# --- M6 F3/F4: internal "identity" vocab never leaks; causes are stated ------


def test_m6_identity_change_uses_engineering_language(conn):
    # README changed AND identity hash changed -> purpose changed (engineering
    # language), never the internal word "identity".
    p = _snap("/a", "A", readme="r1", arch="x", identity="i1")
    cur = _snap("/a", "A", readme="r2", arch="x", identity="i2")
    out = ob.diff_snapshots([p], [cur])
    kinds = _kinds(out)
    assert "purpose changed" in kinds
    assert "identity changed" not in kinds
    rendered = _render(out)
    assert "A purpose changed because" in rendered
    # Forbidden: the raw internal vocabulary.
    assert "identity changed" not in rendered.lower()


def test_m6_change_includes_evidence_cause(conn):
    p = _snap("/a", "A", readme="r1", arch="x", identity="i1")
    cur = _snap("/a", "A", readme="r2", arch="x", identity="i2")
    out = ob.diff_snapshots([p], [cur])
    purpose = next(c for c in out if c.kind == "purpose changed")
    assert purpose.cause and "README summary changed" in purpose.cause
    assert "A purpose changed because the README summary changed." in _render(out)


def test_m6_architecture_cause_present(conn):
    p = _snap("/a", "A", readme="r1", arch="x", identity="i1")
    cur = _snap("/a", "A", readme="r1", arch="y", identity="i2")
    out = ob.diff_snapshots([p], [cur])
    arch = next(c for c in out if c.kind == "architecture changed")
    assert arch.cause and "framework" in arch.cause.lower()


# --- Rename: same path, different name ---------------------------------------


def test_rename_detected(conn):
    p = _snap("/a", "OldName")
    cur = _snap("/a", "NewName")
    out = ob.diff_snapshots([p], [cur])
    assert "OldName was renamed to NewName." in _render(out)


# --- Became clean -----------------------------------------------------------


def test_became_clean(conn):
    p = _snap("/a", "A", dirty=True)
    cur = _snap("/a", "A", dirty=False)
    out = ob.diff_snapshots([p], [cur])
    assert "A is now clean (no uncommitted changes) because" in _render(out)


# --- Commit gain / branch move ----------------------------------------------


def test_commit_gain_and_branch(conn):
    p = _snap("/a", "A", commits=100, branch="main")
    cur = _snap("/a", "A", commits=103, branch="develop")
    out = ob.diff_snapshots([p], [cur])
    assert "A gained 3 commits." in _render(out)
    assert "A moved from main to develop." in _render(out)


# --- Unchanged repos are NOT reported ---------------------------------------


def test_unchanged_repo_silent(conn):
    p = [_snap("/a", "A", readme="r1", arch="x", identity="i1"),
         _snap("/b", "B", readme="r2", arch="y", identity="i2")]
    cur = [_snap("/a", "A", readme="r1", arch="x", identity="i1"),
           _snap("/b", "B", readme="r2", arch="y", identity="i2")]
    out = ob.diff_snapshots(p, cur)
    assert len(out) == 1 and out[0].kind == "no changes"


# --- Appendix: baseline + real-git end-to-end --------------------------------


def test_observe_baseline_on_empty(conn):
    prev, changes = ob.observe(conn)
    assert prev is None  # no prior observation
    # No repos ingested -> nothing to snapshot; report stays honest.
    assert len(changes) == 1 and changes[0].kind == "no changes"


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
    _repo(conn, "proj", str(repo), summary="Purpose:\nA project.\nMaturity:\nUnknown")
    # Build architecture + identity so the hashes are meaningful.
    from friday.db import upsert_architecture
    from friday.architecture import analyze_and_store
    from friday.discovery import Repo
    analyze_and_store(conn, Repo(path=repo))

    # First observation: baseline.
    ob.observe(conn)
    # Edit the README on disk + commit.
    (repo / "README.md").write_text("# Seed\n\nA project.\n\nNew section.\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "doc"], check=True)

    # Second observation must report the README change, not architecture.
    prev, changes = ob.observe(conn)
    assert prev is not None
    rendered = _render(changes)
    assert "proj README changed because" in rendered
    assert "proj architecture changed." not in rendered
    # Two observations stored, each with one row.
    assert len(latest_observation(conn)) == 1
