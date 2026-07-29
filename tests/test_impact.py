"""Tests for the Change Impact Analysis engine — resolving file-to-repo,
collecting git history, and building the impact report."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from friday.impact import (
    ImpactReport, RelatedRepo, KnowledgeEntry,
    analyze_impact, format_impact_summary,
    _resolve_repos_by_path,
    _file_commit_count, _recent_commits, _blame_authors,
    _last_file_info,
    _parse_python_imports, build_import_graph, trace_symbol, format_symbol_impact,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def conn():
    """In-memory SQLite connection seeded with a minimal repo."""
    from friday.db import connect, SCHEMA
    c = connect(":memory:")
    c.executescript(SCHEMA)
    from friday.db import now_iso
    c.execute(
        "INSERT INTO repositories (name, path, ingestion_time) VALUES (?, ?, ?)",
        ("test-repo", "/tmp/test-repo", now_iso()),
    )
    yield c
    c.close()


@pytest.fixture
def git_repo(tmp_path):
    """Create a real git repo with commits touching a test file."""
    repo_dir = tmp_path / "git-test-repo"
    repo_dir.mkdir()
    file_path = repo_dir / "src" / "main.py"
    file_path.parent.mkdir()

    subprocess.run(["git", "init"], cwd=str(repo_dir), capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(repo_dir), capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test Author"], cwd=str(repo_dir), capture_output=True)

    # Commit 1: create file
    file_path.write_text("print('hello')\n")
    subprocess.run(["git", "add", "."], cwd=str(repo_dir), capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=str(repo_dir), capture_output=True)

    # Commit 2: modify file
    file_path.write_text("print('hello')\nprint('world')\n")
    subprocess.run(["git", "add", "."], cwd=str(repo_dir), capture_output=True)
    subprocess.run(["git", "commit", "-m", "Add world print"], cwd=str(repo_dir), capture_output=True)

    # Commit 3: modify again
    file_path.write_text("print('hello')\nprint('world')\ndef foo():\n    pass\n")
    subprocess.run(["git", "add", "."], cwd=str(repo_dir), capture_output=True)
    subprocess.run(["git", "commit", "-m", "Add foo function"], cwd=str(repo_dir), capture_output=True)

    return str(repo_dir), str(file_path)


# ---------------------------------------------------------------------------
# Repo resolution
# ---------------------------------------------------------------------------


class TestRepoResolution:
    def test_resolve_repos_by_path_matches(self):
        repos = [
            type("Repo", (), {"id": 1, "name": "proj", "path": "/home/user/proj"})(),
        ]
        results = _resolve_repos_by_path("/home/user/proj/src/main.py", repos)
        assert len(results) == 1
        assert results[0].name == "proj"

    def test_resolve_repos_by_path_empty(self):
        repos = [
            type("Repo", (), {"id": 1, "name": "proj", "path": "/home/user/proj"})(),
        ]
        results = _resolve_repos_by_path("/other/path/file.py", repos)
        assert len(results) == 0


# ---------------------------------------------------------------------------
# ImpactReport model
# ---------------------------------------------------------------------------


class TestImpactReport:
    def test_empty_report_to_text(self):
        report = ImpactReport(file_path="/fake/path.py")
        text = report.to_text()
        assert "Impact Analysis" in text
        assert "/fake/path.py" in text

    def test_report_with_related_repos(self):
        report = ImpactReport(
            file_path="/test/main.py",
            resolved_repo="test-repo",
            related_repos=[
                RelatedRepo(name="other-repo", reason="Shared tech", strength="Strong"),
            ],
        )
        text = report.to_text()
        assert "other-repo" in text
        assert "Shared tech" in text

    def test_report_with_knowledge(self):
        report = ImpactReport(
            file_path="/test/main.py",
            knowledge=[
                KnowledgeEntry(type="trend", statement="Active repo", confidence="high", status="stable"),
            ],
        )
        text = report.to_text()
        assert "Active repo" in text
        assert "trend" in text


# ---------------------------------------------------------------------------
# Git history helpers
# ---------------------------------------------------------------------------


class TestGitHelpers:
    def test_commit_count(self, git_repo):
        repo_dir, file_path = git_repo
        rel_path = os.path.relpath(file_path, repo_dir)
        count = _file_commit_count(repo_dir, rel_path)
        assert count == 3

    def test_recent_commits(self, git_repo):
        repo_dir, file_path = git_repo
        rel_path = os.path.relpath(file_path, repo_dir)
        commits = _recent_commits(repo_dir, rel_path, n=5)
        assert len(commits) == 3
        assert "Initial commit" in commits[-1]["summary"]
        assert "Add foo function" in commits[0]["summary"]

    def test_blame_authors(self, git_repo):
        repo_dir, file_path = git_repo
        rel_path = os.path.relpath(file_path, repo_dir)
        authors = _blame_authors(repo_dir, rel_path)
        assert len(authors) > 0
        total_pct = sum(a["pct"] for a in authors)
        assert abs(total_pct - 100) < 0.1

    def test_last_file_info(self, git_repo):
        repo_dir, file_path = git_repo
        rel_path = os.path.relpath(file_path, repo_dir)
        date, author = _last_file_info(repo_dir, rel_path)
        assert author == "Test Author"
        assert date is not None

    def test_commit_count_no_such_file(self, tmp_path):
        """A file that doesn't exist in git history returns 0."""
        repo_dir = tmp_path / "empty-repo"
        repo_dir.mkdir()
        subprocess.run(["git", "init"], cwd=str(repo_dir), capture_output=True)
        count = _file_commit_count(str(repo_dir), "nonexistent.py")
        assert count == 0

    def test_recent_commits_no_such_file(self, tmp_path):
        repo_dir = tmp_path / "empty-repo"
        repo_dir.mkdir()
        subprocess.run(["git", "init"], cwd=str(repo_dir), capture_output=True)
        commits = _recent_commits(str(repo_dir), "nonexistent.py", n=5)
        assert commits == []

    def test_blame_authors_no_such_file(self, tmp_path):
        repo_dir = tmp_path / "empty-repo"
        repo_dir.mkdir()
        subprocess.run(["git", "init"], cwd=str(repo_dir), capture_output=True)
        authors = _blame_authors(str(repo_dir), "nonexistent.py")
        assert authors == []


# ---------------------------------------------------------------------------
# Full analysis
# ---------------------------------------------------------------------------


class TestAnalyzeImpact:
    def test_analyze_no_matching_repo(self, conn):
        """When no repo contains the file, errors are populated."""
        report = analyze_impact(conn, "/nonexistent/file.py")
        assert len(report.errors) > 0
        assert "Could not resolve" in report.errors[0]
        assert report.resolved_repo is None

    def test_analyze_happy_path(self, conn, git_repo):
        """With a real git repo in the DB, the report is populated."""
        repo_dir, file_path = git_repo
        # Update the DB to point to the real repo.
        conn.execute("UPDATE repositories SET name=?, path=? WHERE id=1",
                     ("git-test-repo", repo_dir))
        conn.commit()

        report = analyze_impact(conn, file_path)
        assert report.resolved_repo == "git-test-repo"
        assert report.commit_count == 3
        assert report.last_author == "Test Author"
        assert len(report.recent_commits) == 3
        assert len(report.blame_authors) == 1
        assert report.total_authors == 1
        assert report.repo_commit_count is not None

    def test_analyze_non_git_path(self, conn, tmp_path):
        """When the repo path doesn't exist, git helpers fail gracefully."""
        file_path = tmp_path / "src" / "test.py"
        file_path.parent.mkdir(parents=True)
        file_path.write_text("x = 1\n")

        conn.execute("UPDATE repositories SET name=?, path=? WHERE id=1",
                     ("fake-repo", str(tmp_path)))
        conn.commit()

        report = analyze_impact(conn, str(file_path))
        assert report.resolved_repo == "fake-repo"
        # Git commands will fail, but the report should still be returned.
        assert report.errors or report.commit_count == 0

    def test_analyze_related_repos(self, conn):
        """Related repos from the relationships table are included."""
        # Add a second repo.
        conn.execute(
            "INSERT INTO repositories (name, path, ingestion_time) VALUES (?, ?, ?)",
            ("other-repo", "/tmp/other-repo", "2026-01-01T00:00:00"),
        )
        # Add a relationship.
        conn.execute(
            "INSERT INTO relationships (repo_a, repo_b, kind, evidence, strength) "
            "VALUES (?, ?, ?, ?, ?)",
            (1, 2, "shared_tech", "Both use Python", "Strong"),
        )
        conn.commit()
        report = analyze_impact(conn, "/tmp/test-repo/src/main.py")
        assert len(report.related_repos) > 0
        assert report.related_repos[0].name == "other-repo"

    def test_analyze_no_file_exists(self, conn, tmp_path):
        """File path that doesn't exist on disk."""
        report = analyze_impact(conn, "/tmp/nonexistent.py")
        # Should still work — git helpers just return empty.
        assert report.errors or report.commit_count == 0


# ---------------------------------------------------------------------------
# Import graph analysis
# ---------------------------------------------------------------------------


class TestParsePythonImports:
    def test_parse_simple_import(self, tmp_path):
        pyfile = tmp_path / "test.py"
        pyfile.write_text("import os\nimport sys\n")
        imports = _parse_python_imports(str(pyfile))
        assert len(imports) == 2
        assert imports[0]["module"] == "os"
        assert imports[0]["type"] == "import"

    def test_parse_from_import(self, tmp_path):
        pyfile = tmp_path / "test.py"
        pyfile.write_text("from datetime import datetime\nfrom pathlib import Path\n")
        imports = _parse_python_imports(str(pyfile))
        assert len(imports) == 2
        assert imports[0]["module"] == "datetime"
        assert imports[0]["name"] == "datetime"
        assert imports[0]["type"] == "from"

    def test_parse_aliased_import(self, tmp_path):
        pyfile = tmp_path / "test.py"
        pyfile.write_text("import numpy as np\n")
        imports = _parse_python_imports(str(pyfile))
        assert len(imports) == 1
        assert imports[0]["module"] == "numpy"
        assert imports[0]["name"] == "np"

    def test_parse_no_file_returns_empty(self):
        imports = _parse_python_imports("/nonexistent/nope.py")
        assert imports == []

    def test_parse_syntax_error_returns_empty(self, tmp_path):
        pyfile = tmp_path / "bad.py"
        pyfile.write_text("this is not valid python code !!!")
        imports = _parse_python_imports(str(pyfile))
        assert imports == []

    def test_parse_multiple_imports_on_one_line(self, tmp_path):
        pyfile = tmp_path / "test.py"
        pyfile.write_text("import os, sys, re\n")
        imports = _parse_python_imports(str(pyfile))
        assert len(imports) == 3
        assert imports[0]["module"] == "os"
        assert imports[1]["module"] == "sys"
        assert imports[2]["module"] == "re"

    def test_parse_dotted_import(self, tmp_path):
        pyfile = tmp_path / "test.py"
        pyfile.write_text("import os.path\n")
        imports = _parse_python_imports(str(pyfile))
        assert len(imports) == 1
        assert imports[0]["module"] == "os.path"

    def test_parse_empty_file(self, tmp_path):
        pyfile = tmp_path / "empty.py"
        pyfile.write_text("")
        imports = _parse_python_imports(str(pyfile))
        assert imports == []


class TestBuildImportGraph:
    def test_build_unknown_repo_returns_zero(self, conn):
        count = build_import_graph(conn, "nonexistent-repo")
        assert count == 0

    def test_build_with_python_repo(self, conn, tmp_path):
        from friday.db import now_iso
        # Create a small git repo with Python files.
        repo_dir = tmp_path / "py-repo"
        repo_dir.mkdir()
        (repo_dir / "main.py").write_text("import os\nfrom datetime import datetime\n")
        (repo_dir / "utils.py").write_text("import json\ndef helper(): pass\n")
        __import__("subprocess").run(["git", "init"], cwd=str(repo_dir), capture_output=True)
        __import__("subprocess").run(["git", "config", "user.email", "t@t.com"], cwd=str(repo_dir), capture_output=True)
        __import__("subprocess").run(["git", "config", "user.name", "T"], cwd=str(repo_dir), capture_output=True)
        __import__("subprocess").run(["git", "add", "."], cwd=str(repo_dir), capture_output=True)
        __import__("subprocess").run(["git", "commit", "-m", "init"], cwd=str(repo_dir), capture_output=True)

        conn.execute(
            "INSERT INTO repositories (name, path, ingestion_time) VALUES (?, ?, ?)",
            ("py-repo", str(repo_dir), now_iso()),
        )
        conn.commit()

        count = build_import_graph(conn, "py-repo")
        assert count >= 3  # os, datetime, json

        # Check data was stored.
        rows = conn.execute("SELECT * FROM code_imports").fetchall()
        assert len(rows) >= 3
        modules = {r["imported_module"] for r in rows}
        assert "os" in modules
        assert "datetime" in modules
        assert "json" in modules


class TestTraceSymbol:
    def test_trace_nonexistent_symbol(self, conn):
        trace = trace_symbol(conn, "no_such_function")
        assert trace["total"] == 0

    def test_trace_with_data(self, conn):
        from friday.db import now_iso
        conn.execute(
            "INSERT INTO code_dependencies "
            "(repo_id, file_path, symbol, dep_type, line_number, built_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (1, "src/auth.py", "verify_token", "import", 10, now_iso()),
        )
        conn.execute(
            "INSERT INTO code_dependencies "
            "(repo_id, file_path, symbol, dep_type, line_number, built_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (1, "tests/test_auth.py", "verify_token", "import", 5, now_iso()),
        )
        conn.commit()

        trace = trace_symbol(conn, "verify_token")
        assert trace["total"] == 2
        assert len(trace["direct"]) == 1
        assert len(trace["test"]) == 1

    def test_format_symbol_impact(self):
        trace = {
            "direct": [{"file_path": "src/auth.py", "line_number": 10, "dep_type": "import"}],
            "transitive": [],
            "test": [{"file_path": "tests/test_auth.py", "line_number": 5, "dep_type": "import"}],
            "config": [],
            "total": 2,
        }
        text = format_symbol_impact("verify_token", trace)
        assert "verify_token" in text
        assert "DIRECT (1 file(s))" in text
        assert "TEST (1 file(s))" in text
        assert "TRANSITIVE (0 files)" in text


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


class TestFormatting:
    def test_format_summary_empty(self):
        report = ImpactReport(file_path="/test.py")
        summary = format_impact_summary(report)
        assert "No impact data available" in summary

    def test_format_summary_with_data(self):
        report = ImpactReport(
            file_path="/test.py",
            resolved_repo="test",
            commit_count=5,
            total_authors=2,
            related_repos=[RelatedRepo(name="other", reason="test", strength="Weak")],
        )
        summary = format_impact_summary(report)
        assert "Repo: test" in summary
        assert "Commits: 5" in summary
        assert "Authors: 2" in summary
        assert "Relationships: 1" in summary
