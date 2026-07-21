"""Milestone 9.8 — Observe & Refresh regression tests.

`friday observe` composes the EXISTING pipeline (ingest -> knowledge ->
understanding -> initiative -> insight) on top of existing change detection.
No new subsystem. These tests guard the spec's hard requirements:

  - refreshing an unchanged workspace produces 0 new knowledge, 0 new
    observations, 0 new relationships (no duplication);
  - a real repo change is detected and the dependent layers refresh;
  - README / architecture / dependency edits are detected;
  - single-repo, whole-workspace, and --changed scopes behave;
  - the --summary report renders every required field;
  - refresh is idempotent (a no-change second run does no work).

All builds are idempotent; refresh SKIPs the expensive rebuilds entirely when
nothing's observable state changed.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from friday.db import connect
from friday.discovery import Repo
from friday.observe import refresh, RefreshReport


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "observe_refresh.db")
    yield c
    c.close()


def _init_repo(d: Path, readme: str = "# Seed\n\nA project.\n") -> None:
    subprocess.run(["git", "-C", str(d), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(d), "config", "user.email", "x@y.z"], check=True)
    subprocess.run(["git", "-C", str(d), "config", "user.name", "t"], check=True)
    (d / "README.md").write_text(readme)
    (d / "main.py").write_text("def main(): pass\n")
    subprocess.run(["git", "-C", str(d), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(d), "commit", "-q", "-m", "init"], check=True)


def _ingest(conn, root: Path) -> None:
    from friday.ingest import ingest_paths
    ingest_paths([root], conn)


def _counts(conn) -> dict:
    out = {}
    for t in ("knowledge", "observations", "relationships"):
        out[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    return out


# --- Unchanged workspace: no duplication -----------------------------------


def test_observe_unchanged_workspace_no_duplication(conn, tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    _init_repo(repo)
    _ingest(conn, repo)

    # First refresh records the baseline snapshot and refreshes the stack.
    refresh(conn)
    # Second refresh with NO repository change must produce zero new work and
    # zero duplication.
    before = _counts(conn)
    rep = refresh(conn)
    after = _counts(conn)
    assert isinstance(rep, RefreshReport)
    assert after == before            # no duplicate knowledge/observations/relationships
    assert rep.repos_changed == 0
    assert rep.knowledge_updated == 0
    assert rep.understanding_updated == 0
    assert rep.insights_changed == 0

    # And a third identical run is still a no-op.
    rep2 = refresh(conn)
    assert _counts(conn) == after
    assert rep2.repos_changed == 0


# --- Idempotency ----------------------------------------------------------


def test_observe_idempotency(conn, tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    _init_repo(repo)
    _ingest(conn, repo)

    # Make one real change so the first refresh actually does work.
    (repo / "feature.py").write_text("def feature(): return 1\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "feat"], check=True)

    r1 = refresh(conn)
    assert r1.repos_changed >= 1  # change detected

    # Now a no-change re-run must do NO work and add nothing.
    before = _counts(conn)
    r2 = refresh(conn)
    after = _counts(conn)
    assert after == before
    assert r2.repos_changed == 0
    assert r2.knowledge_updated == 0
    assert r2.understanding_updated == 0
    assert r2.identity_updated == 0
    assert r2.insights_changed == 0
    assert r2.portfolio_updated is False


# --- Changed repository ----------------------------------------------------


def test_observe_changed_repository_detected(conn, tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    _init_repo(repo)
    _ingest(conn, repo)
    refresh(conn)  # baseline

    # New commit with a new file -> observable state changed.
    (repo / "added.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "add"], check=True)

    rep = refresh(conn)
    assert rep.repos_changed >= 1
    assert rep.knowledge_updated >= 1


# --- README edit -----------------------------------------------------------


def test_observe_readme_edit_refreshes(conn, tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    _init_repo(repo)
    _ingest(conn, repo)
    refresh(conn)

    (repo / "README.md").write_text("# Seed\n\nA project.\n\nNew section added.\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "doc"], check=True)

    rep = refresh(conn)
    assert rep.repos_changed >= 1
    assert rep.knowledge_updated >= 1


# --- Architecture / dependency change --------------------------------------


def test_observe_architecture_change_detected(conn, tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    _init_repo(repo)
    _ingest(conn, repo)
    refresh(conn)

    # A new Python module changes the detected architecture/dependencies.
    (repo / "service.py").write_text(
        "import os\n\ndef run():\n    return os.getcwd()\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "svc"], check=True)

    rep = refresh(conn)
    assert rep.repos_changed >= 1
    assert rep.knowledge_updated >= 1


# --- Multiple repositories -------------------------------------------------


def test_observe_multiple_repositories(conn, tmp_path):
    for name in ("a", "b"):
        repo = tmp_path / name
        repo.mkdir()
        _init_repo(repo, readme=f"# {name}\n\nProj {name}.\n")
        _ingest(conn, repo)
    refresh(conn)

    # Change both.
    for name in ("a", "b"):
        repo = tmp_path / name
        (repo / "new.py").write_text("y = 2\n")
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "add"], check=True)

    rep = refresh(conn)
    assert rep.repos_scanned == 2
    assert rep.repos_changed == 2


# --- Single repository scope -----------------------------------------------


def test_observe_single_repo_scope(conn, tmp_path):
    repo_a = tmp_path / "a"
    repo_a.mkdir()
    _init_repo(repo_a, readme="# A\n\nA.\n")
    repo_b = tmp_path / "b"
    repo_b.mkdir()
    _init_repo(repo_b, readme="# B\n\nB.\n")
    _ingest(conn, repo_a)
    _ingest(conn, repo_b)
    refresh(conn)

    # Change only repo A; refresh just A.
    (repo_a / "z.py").write_text("z = 9\n")
    subprocess.run(["git", "-C", str(repo_a), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo_a), "commit", "-q", "-m", "add"], check=True)

    rep = refresh(conn, repos=["a"])
    assert rep.repos_scanned == 1
    assert rep.repos_changed == 1


# --- --changed scope --------------------------------------------------------


def test_observe_changed_only_skips_untouched(conn, tmp_path):
    repo_a = tmp_path / "a"
    repo_a.mkdir()
    _init_repo(repo_a, readme="# A\n\nA.\n")
    repo_b = tmp_path / "b"
    repo_b.mkdir()
    _init_repo(repo_b, readme="# B\n\nB.\n")
    _ingest(conn, repo_a)
    _ingest(conn, repo_b)
    refresh(conn)  # baseline for both

    # Change only A; --changed should still pick it up (baseline differs).
    (repo_a / "z.py").write_text("z = 9\n")
    subprocess.run(["git", "-C", str(repo_a), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo_a), "commit", "-q", "-m", "add"], check=True)

    rep = refresh(conn, only_changed=True)
    assert rep.repos_changed >= 1
    # No further change -> --changed refreshes nothing.
    rep2 = refresh(conn, only_changed=True)
    assert rep2.repos_changed == 0


# --- Summary report (CLI) --------------------------------------------------


def test_observe_workspace_summary(conn, tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    _init_repo(repo)
    _ingest(conn, repo)

    # cmd_observe opens its own connection; validate the summary shape via
    # RefreshReport.to_text (the same renderer cmd_observe uses).
    rep = refresh(conn)
    text = rep.to_text()
    assert "Workspace refreshed" in text
    assert "Repositories scanned:" in text
    assert "Repositories changed:" in text
    assert "Knowledge updated:" in text
    assert "Understanding updated:" in text
    assert "Identity updated:" in text
    assert "Portfolio updated:" in text
    assert "Insights updated:" in text
    assert "Elapsed:" in text


# --- Dependency rules: insights gated on understanding ----------------------


def test_observe_insights_gated_on_understanding(conn, tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    _init_repo(repo)
    _ingest(conn, repo)
    refresh(conn)  # baseline, no change

    # No change -> insights rebuild is skipped (dependency rule).
    rep = refresh(conn)
    assert rep.repos_changed == 0
    assert rep.insights_changed == 0
    assert rep.portfolio_updated is False
