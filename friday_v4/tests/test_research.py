"""Hermetic tests for the Wave 11 research layer (fixture repos)."""

from __future__ import annotations

from pathlib import Path

import pytest

from friday_v4.research import analyze, correlate, CodeSearch, impact
from friday_v4.research.architecture import _CACHE, RepoProfile


@pytest.fixture
def repo_a(tmp_path: Path) -> Path:
    r = tmp_path / "vivaha"
    (r / "src").mkdir(parents=True)
    (r / "tests").mkdir(parents=True)
    (r / "src" / "auth.py").write_text("from fastapi import FastAPI\n")
    (r / "src" / "main.py").write_text("import auth\n")
    (r / "tests" / "test_auth.py").write_text("def test_auth(): pass\n")
    (r / "README.md").write_text("# vivaha\nShared auth for the family.\n")
    (r / "pyproject.toml").write_text("[project]\ndeps = [\"fastapi\", \"sqlalchemy\"]\n")
    return r


@pytest.fixture
def repo_b(tmp_path: Path) -> Path:
    r = tmp_path / "mindwell"
    (r / "src").mkdir(parents=True)
    (r / "src" / "auth.py").write_text("from fastapi import FastAPI\n")
    (r / "src" / "sessions.py").write_text("import auth\n")
    (r / "README.md").write_text("# MindWell\nMindfulness tracking.\n")
    return r


def test_analyze_profile(repo_a: Path):
    p = analyze(repo_a)
    assert p.available
    assert p.languages.get("Python", 0) >= 1
    assert p.test_files >= 1
    assert p.has_readme
    assert "fastapi" in " ".join(p.framework_signals).lower()
    assert p.evidence  # cited claims


def test_analyze_missing_dir():
    p = analyze("/nonexistent/nowhere")
    assert not p.available


def test_analyze_cached(repo_a: Path):
    _CACHE.clear()
    first = analyze(repo_a)
    assert analyze(repo_a) is first  # cached — same object


def test_correlate_shared(repo_a: Path, repo_b: Path):
    est = correlate(repo_a, repo_b)
    assert "auth.py" in est.overlapping_files
    assert est.days_range
    assert est.confidence in ("high", "medium", "low")
    assert est.evidence


def test_correlate_missing():
    est = correlate("/nonexistent/a", "/nonexistent/b")
    assert not est.overlapping_files


def test_code_search(repo_a: Path):
    cs = CodeSearch(repo_a)
    hits = cs.search("auth")
    assert any("auth.py" in h.path for h in hits)
    assert all(h.snippet for h in hits)


def test_impact(repo_a: Path):
    rep = impact(repo_a, "auth.py")
    assert rep.reference_count >= 1  # main.py imports auth


def test_readme_purpose(repo_a: Path):
    from friday_v4.research import readme_purpose
    rp = readme_purpose(repo_a)
    assert "Shared auth" in rp.purpose
    assert rp.source
