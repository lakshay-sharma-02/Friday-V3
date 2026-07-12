"""Repository discovery — ignore rules and nested-repo handling."""

from __future__ import annotations

from pathlib import Path

from friday.discovery import discover, is_repo


def _mkrepo(root: Path, name: str) -> Path:
    p = root / name
    p.mkdir(parents=True)
    (p / ".git").mkdir()
    return p


def test_finds_top_level_repo(tmp_path):
    _mkrepo(tmp_path, "proj")
    repos = discover(tmp_path)
    assert [r.path.name for r in repos] == ["proj"]


def test_skips_node_modules_and_venv(tmp_path):
    _mkrepo(tmp_path, "proj")
    (tmp_path / "node_modules" / "lib" / ".git").mkdir(parents=True)
    (tmp_path / ".venv" / "pkg" / ".git").mkdir(parents=True)
    repos = discover(tmp_path)
    # Only proj; nested fake repos under ignored dirs must be skipped.
    assert [r.path.name for r in repos] == ["proj"]


def test_nested_repo_found_but_not_descended(tmp_path):
    outer = _mkrepo(tmp_path, "outer")
    inner = _mkrepo(outer, "inner")
    repos = discover(tmp_path)
    names = {r.path.name for r in repos}
    assert names == {"outer", "inner"}


def test_hidden_dirs_ignored(tmp_path):
    _mkrepo(tmp_path, "proj")
    (tmp_path / ".cache" / "x" / ".git").mkdir(parents=True)
    repos = discover(tmp_path)
    assert [r.path.name for r in repos] == ["proj"]


def test_is_repo_detects_git_dir(tmp_path):
    (tmp_path / ".git").mkdir()
    assert is_repo(tmp_path) is True
    assert is_repo(Path("/nonexistent/path/xyz")) is False
