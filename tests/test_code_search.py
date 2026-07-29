"""Tests for the Semantic Code Search engine."""

from __future__ import annotations

import pytest

from friday.code_search import (
    SearchResult, SearchMatch,
    semantic_search, _expand_query,
    _parse_rg_output,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def conn():
    """In-memory SQLite connection seeded with a minimal repo."""
    from friday.db import connect, SCHEMA, now_iso
    c = connect(":memory:")
    c.executescript(SCHEMA)
    yield c
    c.close()


# ---------------------------------------------------------------------------
# Data model tests
# ---------------------------------------------------------------------------


class TestSearchMatch:
    def test_format_basic(self):
        m = SearchMatch(
            repo="test", file_path="src/main.py", line_number=10,
            line_content="def hello():", term_matched="hello",
        )
        text = m.format()
        assert "src/main.py:10" in text
        assert "def hello()" in text

    def test_format_with_context(self):
        m = SearchMatch(
            repo="test", file_path="src/main.py", line_number=10,
            line_content="def hello():",
            context_before=["# previous line"],
            context_after=["# next line"],
            relevance_score=0.9, relevance_reason="Direct match",
        )
        text = m.format()
        assert "Direct match" in text
        assert "previous line" in text
        assert "next line" in text

    def test_to_dict(self):
        m = SearchMatch(
            repo="test", file_path="src/main.py", line_number=5,
            line_content="print('hello')",
        )
        d = m.to_dict()
        assert d["repo"] == "test"
        assert d["line_number"] == 5


class TestSearchResult:
    def test_empty_result(self):
        r = SearchResult(query="auth")
        text = r.format()
        assert "auth" in text
        assert "No results found" in text

    def test_result_with_matches(self):
        r = SearchResult(
            query="auth",
            expanded_terms=["auth", "login", "token"],
            total_matches=2,
            matches_by_repo={
                "friday": [
                    SearchMatch(repo="friday", file_path="src/auth.py",
                                line_number=10, line_content="def login():",
                                term_matched="auth"),
                ],
            },
        )
        text = r.format()
        assert "auth" in text
        assert "login, token" in text
        assert "src/auth.py:10" in text

    def test_to_dict(self):
        r = SearchResult(query="test", total_matches=5)
        d = r.to_dict()
        assert d["query"] == "test"
        assert d["total_matches"] == 5

    def test_with_errors(self):
        r = SearchResult(query="test", errors=["No repos found"])
        text = r.format()
        assert "No repos found" in text


# ---------------------------------------------------------------------------
# Query expansion
# ---------------------------------------------------------------------------


class TestQueryExpansion:
    def test_fallback_to_split(self):
        """Query expansion produces search terms (LLM available or splits)."""
        terms = _expand_query("find auth code")
        # Either LLM-expanded terms OR raw split — both are valid.
        assert len(terms) >= 3  # at least the original words or substitutes
        assert all(isinstance(t, str) and len(t) > 0 for t in terms)


# ---------------------------------------------------------------------------
# ripgrep output parsing
# ---------------------------------------------------------------------------


class TestRgOutputParsing:
    def test_parse_single_match(self):
        """Parse a single ripgrep match line (--no-heading format)."""
        stdout = "src/main.py:10:def hello():\nsrc/main.py:11-    pass\n"
        matches: list = []
        seen: set = set()

        _parse_rg_output(stdout, "test-repo", "hello", matches, seen, 200)

        assert len(matches) == 1
        assert matches[0]["file_path"] == "src/main.py"
        assert matches[0]["line_number"] == 10
        assert matches[0]["line_content"] == "def hello():"
        assert matches[0]["term_matched"] == "hello"

    def test_parse_multiple_matches(self):
        """Parse multiple match lines with context."""
        stdout = (
            "src/main.py:10:def hello():\n"
            "src/main.py:11-    pass\n"
            "--\n"
            "src/main.py:20:def world():\n"
            "src/main.py:21-    return None\n"
        )
        matches: list = []
        seen: set = set()

        _parse_rg_output(stdout, "test-repo", "hello", matches, seen, 200)

        assert len(matches) == 2
        assert matches[0]["line_number"] == 10
        assert matches[1]["line_number"] == 20

    def test_parse_context_before_and_after(self):
        """Parse context before and after match lines."""
        stdout = (
            "src/main.py:8-  # setup\n"
            "src/main.py:9-  x = 1\n"
            "src/main.py:10:print(x)\n"
            "src/main.py:11-  y = 2\n"
            "src/main.py:12-  # done\n"
        )
        matches: list = []
        seen: set = set()

        _parse_rg_output(stdout, "test-repo", "print", matches, seen, 200)

        assert len(matches) == 1
        assert matches[0]["line_number"] == 10
        assert len(matches[0]["context_before"]) == 2
        assert matches[0]["context_before"] == ["  # setup", "  x = 1"]
        assert len(matches[0]["context_after"]) == 2
        assert matches[0]["context_after"] == ["  y = 2", "  # done"]

    def test_parse_deduplicates(self):
        """Same line matched twice is deduplicated."""
        stdout = "src/main.py:10:print('hello')\n"
        matches: list = []
        seen: set = set()

        _parse_rg_output(stdout, "test-repo", "hello", matches, seen, 200)
        _parse_rg_output(stdout, "test-repo", "hello", matches, seen, 200)

        assert len(matches) == 1

    def test_parse_respects_max(self):
        """Doesn't add matches beyond max_per_term."""
        stdout = "\n".join(f"src/main.py:{i}:line {i}" for i in range(10))
        matches: list = []
        seen: set = set()

        _parse_rg_output(stdout, "test-repo", "term", matches, seen, 3)

        assert len(matches) <= 3


# ---------------------------------------------------------------------------
# Full search (without ripgrep)
# ---------------------------------------------------------------------------


class TestSemanticSearch:
    def test_empty_workspace(self, conn):
        """Search on empty workspace returns early with error."""
        result = semantic_search(conn, "find auth code")
        assert result.query == "find auth code"
        assert len(result.errors) > 0 or result.total_matches == 0

    def test_no_ripgrep_returns_graceful_error(self, conn, monkeypatch):
        """When ripgrep is not installed, returns a helpful error."""
        def mock_run(*args, **kwargs):
            raise FileNotFoundError("rg not found")

        import subprocess
        monkeypatch.setattr(subprocess, "run", mock_run)

        # Insert a repo so we get past the empty check.
        from friday.db import now_iso
        conn.execute(
            "INSERT INTO repositories (name, path, ingestion_time) VALUES (?, ?, ?)",
            ("test", "/tmp", now_iso()),
        )
        conn.commit()

        result = semantic_search(conn, "find auth code")
        assert any("ripgrep" in e.lower() for e in result.errors)

    def test_repos_not_accessible(self, conn):
        """Repos whose paths don't exist on disk are skipped gracefully."""
        from friday.db import now_iso
        conn.execute(
            "INSERT INTO repositories (name, path, ingestion_time) VALUES (?, ?, ?)",
            ("nonexistent-repo", "/nonexistent/path", now_iso()),
        )
        conn.commit()

        result = semantic_search(conn, "find auth")
        assert result.total_matches == 0

    def test_query_expansion_off(self, conn, monkeypatch):
        """Without expand_query, uses query.split()."""
        import subprocess
        # Mock rg to return no matches (so the search completes without errors).
        original_run = subprocess.run

        def mock_rg(args, **kwargs):
            if args and "rg" in str(args[0]):
                return type("Proc", (), {"returncode": 1, "stdout": ""})()
            return original_run(args, **kwargs)

        monkeypatch.setattr(subprocess, "run", mock_rg)

        from friday.db import now_iso
        conn.execute(
            "INSERT INTO repositories (name, path, ingestion_time) VALUES (?, ?, ?)",
            ("test", "/tmp", now_iso()),
        )
        conn.commit()

        result = semantic_search(conn, "test query", expand_query=False)
        assert "test" in result.expanded_terms
        assert "query" in result.expanded_terms


# ---------------------------------------------------------------------------
# End-to-end test (requires ripgrep installed)
# ---------------------------------------------------------------------------


class TestEndToEnd:
    """End-to-end tests that create real files and run ripgrep against them.

    These tests require ``rg`` to be installed on the system.
    """

    def test_e2e_finds_matches_in_real_files(self, conn, tmp_path):
        """Create real source files, insert a repo, and verify semantic_search finds them."""
        from friday.db import now_iso

        # Create a small repo-like directory structure.
        src_dir = tmp_path / "my-project" / "src"
        src_dir.mkdir(parents=True)

        (src_dir / "auth.py").write_text(
            "def login(username, password):\n"
            "    # Authenticate the user\n"
            "    token = generate_jwt(username)\n"
            "    return token\n"
            "\n"
            "def logout(session):\n"
            "    session.clear()\n"
        )

        (src_dir / "db.py").write_text(
            "import sqlite3\n"
            "\n"
            "def connect(database):\n"
            "    conn = sqlite3.connect(database)\n"
            "    return conn\n"
            "\n"
            "def query(conn, sql):\n"
            "    return conn.execute(sql).fetchall()\n"
        )

        (src_dir / "utils.py").write_text(
            "import hashlib\n"
            "\n"
            "def hash_password(password):\n"
            "    return hashlib.sha256(password.encode()).hexdigest()\n"
        )

        repo_path = str(tmp_path / "my-project")
        conn.execute(
            "INSERT INTO repositories (name, path, ingestion_time) VALUES (?, ?, ?)",
            ("my-project", repo_path, now_iso()),
        )
        conn.commit()

        # ── Search for authentication-related code ─────────────────────
        result = semantic_search(conn, "auth", expand_query=False, max_results=20)

        assert result.total_matches >= 1, (
            f"Expected at least 1 match for 'auth', got {result.total_matches}. "
            f"Errors: {result.errors}")
        assert "my-project" in result.matches_by_repo

        # Check that auth.py matches contain the right content.
        matches = result.matches_by_repo["my-project"]
        auth_matches = [m for m in matches if "auth.py" in m.file_path]
        assert len(auth_matches) >= 1
        # One of the matches should be the "Authenticate" line.
        all_content = " ".join(m.line_content for m in auth_matches)
        assert "token" in all_content or "Authenticate" in all_content or "login" in all_content

        # Check metadata.
        for m in matches:
            assert m.repo == "my-project"
            assert m.file_path
            assert m.line_number >= 1
            assert m.line_content
            assert m.term_matched

    def test_e2e_search_across_multiple_files(self, conn, tmp_path):
        """Semantic search across multiple files finds all matches."""
        from friday.db import now_iso

        project = tmp_path / "big-project"
        (project / "src").mkdir(parents=True)

        (project / "src" / "routes.py").write_text(
            "from flask import Blueprint\n"
            "bp = Blueprint('api', __name__)\n"
            "\n"
            "@bp.route('/login')\n"
            "def handle_login():\n"
            "    return {'status': 'ok'}\n"
            "\n"
            "@bp.route('/logout')\n"
            "def handle_logout():\n"
            "    return {'status': 'ok'}\n"
        )

        (project / "src" / "config.py").write_text(
            "import os\n"
            "SECRET_KEY = os.environ.get('SECRET')\n"
            "DATABASE_URL = os.environ.get('DB_URL')\n"
        )

        (project / "src" / "test_routes.py").write_text(
            "def test_login_returns_ok():\n"
            "    assert True\n"
        )

        repo_path = str(project)
        conn.execute(
            "INSERT INTO repositories (name, path, ingestion_time) VALUES (?, ?, ?)",
            ("big-project", repo_path, now_iso()),
        )
        conn.commit()

        result = semantic_search(conn, "login route", expand_query=False, max_results=20)

        assert result.total_matches >= 1
        assert "big-project" in result.matches_by_repo

        matches = result.matches_by_repo["big-project"]
        # Should find matches in at least two files (routes.py has 'login', test_routes.py has 'test_login').
        files_found = {m.file_path for m in matches}
        assert len(files_found) >= 2, f"Expected matches in >=2 files, got: {files_found}"

    def test_e2e_no_matches_returns_empty(self, conn, tmp_path):
        """Searching for something that doesn't exist returns empty gracefully."""
        from friday.db import now_iso

        project = tmp_path / "empty-project"
        (project / "src").mkdir(parents=True)
        (project / "src" / "readme.txt").write_text("This is a readme.\n")

        repo_path = str(project)
        conn.execute(
            "INSERT INTO repositories (name, path, ingestion_time) VALUES (?, ?, ?)",
            ("empty-project", repo_path, now_iso()),
        )
        conn.commit()

        result = semantic_search(conn, "nonexistent_symbol_xyz", expand_query=False)
        assert result.total_matches == 0
        assert len(result.errors) == 0 or len(result.matches_by_repo) == 0

    def test_e2e_context_captured(self, conn, tmp_path):
        """Verify surrounding context lines are captured in the match."""
        from friday.db import now_iso

        project = tmp_path / "ctx-project"
        project.mkdir(parents=True)

        (project / "server.py").write_text(
            "import socket\n"
            "\n"
            "def start_server(host, port):\n"
            "    # Create a TCP socket\n"
            "    sock = socket.socket()\n"
            "    sock.bind((host, port))\n"
            "    sock.listen(5)\n"
            "    return sock\n"
        )

        repo_path = str(project)
        conn.execute(
            "INSERT INTO repositories (name, path, ingestion_time) VALUES (?, ?, ?)",
            ("ctx-project", repo_path, now_iso()),
        )
        conn.commit()

        result = semantic_search(conn, "socket", expand_query=False, max_results=10)

        assert result.total_matches >= 1
        matches = result.matches_by_repo.get("ctx-project", [])
        # At least one match should have context (either before or after).
        has_context = any(
            len(m.context_before) > 0 or len(m.context_after) > 0
            for m in matches
        )
        assert has_context, "Expected at least one match with context lines captured"
