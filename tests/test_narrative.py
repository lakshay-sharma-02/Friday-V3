"""Tests for the Codebase Narrative engine — git archaeology for project evolution."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from collections import Counter

import pytest

from friday.narrative import (
    NarrativeReport, AuthorStats, TimelinePhase, GitCommit,
    build_narrative, format_narrative_summary,
    _parse_log_line, _classify_commit_pattern,
    _detect_phases, _get_large_commits,
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
    c.execute(
        "INSERT INTO repositories (name, path, ingestion_time) VALUES (?, ?, ?)",
        ("test-repo", "/tmp/test-repo", now_iso()),
    )
    yield c
    c.close()


@pytest.fixture
def git_repo(tmp_path):
    """Create a real git repo with history across multiple phases."""
    repo_dir = tmp_path / "narrative-test-repo"
    repo_dir.mkdir()

    subprocess.run(["git", "init"], cwd=str(repo_dir), capture_output=True)
    subprocess.run(["git", "config", "user.email", "alice@test.com"], cwd=str(repo_dir), capture_output=True)
    subprocess.run(["git", "config", "user.name", "Alice"], cwd=str(repo_dir), capture_output=True)

    # Phase 1: Initial build (3 commits by Alice)
    (repo_dir / "README.md").write_text("# My Project\n")
    subprocess.run(["git", "add", "."], cwd=str(repo_dir), capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit: project setup"], cwd=str(repo_dir), capture_output=True)

    (repo_dir / "src").mkdir()
    (repo_dir / "src" / "main.py").write_text("print('hello')\n")
    subprocess.run(["git", "add", "."], cwd=str(repo_dir), capture_output=True)
    subprocess.run(["git", "commit", "-m", "Add main module"], cwd=str(repo_dir), capture_output=True)

    (repo_dir / "src" / "utils.py").write_text("def helper():\n    pass\n")
    subprocess.run(["git", "add", "."], cwd=str(repo_dir), capture_output=True)
    subprocess.run(["git", "commit", "-m", "Add utility functions"], cwd=str(repo_dir), capture_output=True)

    # Phase 2: Growth (2 commits by Bob)
    subprocess.run(["git", "config", "user.email", "bob@test.com"], cwd=str(repo_dir), capture_output=True)
    subprocess.run(["git", "config", "user.name", "Bob"], cwd=str(repo_dir), capture_output=True)

    (repo_dir / "tests").mkdir()
    (repo_dir / "tests" / "test_main.py").write_text("def test_hello():\n    pass\n")
    subprocess.run(["git", "add", "."], cwd=str(repo_dir), capture_output=True)
    subprocess.run(["git", "commit", "-m", "Add test suite"], cwd=str(repo_dir), capture_output=True)

    (repo_dir / "src" / "main.py").write_text("print('hello world')\nprint('goodbye')\n")
    subprocess.run(["git", "add", "."], cwd=str(repo_dir), capture_output=True)
    subprocess.run(["git", "commit", "-m", "Update main with goodbye message"], cwd=str(repo_dir), capture_output=True)

    # Phase 3: Refinement (3 commits by Alice again)
    subprocess.run(["git", "config", "user.email", "alice@test.com"], cwd=str(repo_dir), capture_output=True)
    subprocess.run(["git", "config", "user.name", "Alice"], cwd=str(repo_dir), capture_output=True)

    (repo_dir / "pyproject.toml").write_text("[tool.pytest]\n")
    subprocess.run(["git", "add", "."], cwd=str(repo_dir), capture_output=True)
    subprocess.run(["git", "commit", "-m", "Add pyproject.toml config"], cwd=str(repo_dir), capture_output=True)

    (repo_dir / "README.md").write_text("# My Project\n\nA great project.\n")
    subprocess.run(["git", "add", "."], cwd=str(repo_dir), capture_output=True)
    subprocess.run(["git", "commit", "-m", "Update README with description"], cwd=str(repo_dir), capture_output=True)

    (repo_dir / "Makefile").write_text("test:\n\tpytest\n")
    subprocess.run(["git", "add", "."], cwd=str(repo_dir), capture_output=True)
    subprocess.run(["git", "commit", "-m", "Add Makefile for build automation"], cwd=str(repo_dir), capture_output=True)

    return str(repo_dir)


# ---------------------------------------------------------------------------
# GitCommit parsing
# ---------------------------------------------------------------------------


class TestGitCommit:
    def test_parse_log_line_valid(self):
        c = _parse_log_line("abc123|2026-01-15T10:00:00|Alice|alice@test.com|Initial commit")
        assert c is not None
        assert c.sha == "abc123"
        assert c.author == "Alice"
        assert c.summary == "Initial commit"

    def test_parse_log_line_invalid(self):
        c = _parse_log_line("invalid")
        assert c is None


# ---------------------------------------------------------------------------
# Commit pattern classification
# ---------------------------------------------------------------------------


class TestCommitPatterns:
    def test_classify_batch_and_single(self):
        """3 commits within 1 hour = batch, 1 isolated = single."""
        commits = [
            GitCommit(sha="1", date="2026-01-15T10:00:00", author="A", email="", summary="c1"),
            GitCommit(sha="2", date="2026-01-15T10:05:00", author="A", email="", summary="c2"),
            GitCommit(sha="3", date="2026-01-15T10:10:00", author="A", email="", summary="c3"),
            GitCommit(sha="4", date="2026-01-16T10:00:00", author="A", email="", summary="c4"),
        ]
        batch, single = _classify_commit_pattern(commits)
        assert batch == 3
        assert single == 1

    def test_classify_all_isolated(self):
        commits = [
            GitCommit(sha="1", date="2026-01-15T10:00:00", author="A", email="", summary="c1"),
            GitCommit(sha="2", date="2026-01-16T10:00:00", author="A", email="", summary="c2"),
        ]
        batch, single = _classify_commit_pattern(commits)
        assert batch == 0
        assert single == 2

    def test_classify_empty(self):
        batch, single = _classify_commit_pattern([])
        assert batch == 0
        assert single == 0


# ---------------------------------------------------------------------------
# Phase detection
# ---------------------------------------------------------------------------


class TestPhaseDetection:
    def test_detect_phases_with_multiple_segments(self):
        commits = []
        for i in range(30):
            day = f"2026-01-{i+1:02d}T10:00:00"
            commits.append(GitCommit(sha=str(i), date=day, author="A", email="", summary=f"c{i}"))
        phases = _detect_phases(commits)
        assert len(phases) >= 1
        assert phases[0].commit_count > 0

    def test_detect_phases_not_enough_commits(self):
        phases = _detect_phases([], min_phase_commits=5)
        assert phases == []


# ---------------------------------------------------------------------------
# Large commits
# ---------------------------------------------------------------------------


class TestLargeCommits:
    def test_get_large_commits_filters(self):
        commits = [
            GitCommit(sha="1", date="2026-01-01", author="A", email="",
                      summary="small", insertions=10, deletions=5, files_changed=1),
            GitCommit(sha="2", date="2026-01-02", author="A", email="",
                      summary="large", insertions=300, deletions=100, files_changed=5),
        ]
        large = _get_large_commits("", commits, threshold=200)
        assert len(large) == 1
        assert large[0].summary == "large"


# ---------------------------------------------------------------------------
# NarrativeReport model
# ---------------------------------------------------------------------------


class TestNarrativeReport:
    def test_empty_report_to_text(self):
        report = NarrativeReport(repo_name="test")
        text = report.to_text()
        assert "Codebase Narrative: test" in text

    def test_report_with_authors(self):
        report = NarrativeReport(
            repo_name="test",
            total_commits=100,
            authors=[AuthorStats(name="Alice", commit_count=60, pct=60.0)],
        )
        text = report.to_text()
        assert "Alice" in text
        assert "60.0%" in text

    def test_report_with_phases(self):
        report = NarrativeReport(
            repo_name="test",
            phases=[TimelinePhase(
                label="Initial Build",
                start_date="2026-01-01",
                end_date="2026-03-01",
                commit_count=10,
                author_count=2,
            )],
        )
        text = report.to_text()
        assert "Initial Build" in text
        assert "Evolution Timeline" in text


# ---------------------------------------------------------------------------
# Full narrative build
# ---------------------------------------------------------------------------


class TestBuildNarrative:
    def test_build_repo_not_found(self, conn):
        """Unknown repo returns report with errors."""
        report = build_narrative(conn, "nonexistent-repo")
        assert len(report.errors) > 0
        assert "not found" in report.errors[0].lower()

    def test_build_happy_path(self, conn, git_repo):
        """With a real git repo, the narrative is fully populated."""
        # Update DB to point to the real repo.
        from friday.db import now_iso
        conn.execute("UPDATE repositories SET name=?, path=? WHERE id=1",
                     ("narrative-test", git_repo))
        conn.commit()

        report = build_narrative(conn, "narrative-test")

        assert report.repo_name == "narrative-test"
        assert report.total_commits == 8
        assert report.total_authors == 2
        assert report.primary_author is not None
        assert report.primary_author == "alice@test.com"  # gitmeta returns email from shortlog
        # Author names come from the parsed commit log.
        author_names = {a.name for a in report.authors}
        assert "Alice" in author_names
        assert "Bob" in author_names
        assert report.age_days > 0
        assert len(report.authors) == 2
        assert report.first_commit_date is not None
        assert report.last_commit_date is not None
        assert report.commits_by_month  # non-empty
        assert report.phases  # non-empty
        assert report.current_file_count > 0
        assert report.files_added > 0

    def test_build_author_breakdown(self, conn, git_repo):
        """Author breakdown shows Alice (6) > Bob (2)."""
        conn.execute("UPDATE repositories SET name=?, path=? WHERE id=1",
                     ("narrative-test", git_repo))
        conn.commit()

        report = build_narrative(conn, "narrative-test")
        # Alice: 6 commits, Bob: 2 commits
        alice = next((a for a in report.authors if a.name == "Alice"), None)
        bob = next((a for a in report.authors if a.name == "Bob"), None)
        assert alice is not None
        assert bob is not None
        assert alice.commit_count == 6
        assert bob.commit_count == 2

    def test_build_bus_factor(self, conn, git_repo):
        """Bus factor should be 1 (Alice owns 75% of commits)."""
        conn.execute("UPDATE repositories SET name=?, path=? WHERE id=1",
                     ("narrative-test", git_repo))
        conn.commit()

        report = build_narrative(conn, "narrative-test")
        assert report.bus_factor == 1  # Alice owns 6/8 = 75%

    def test_build_commit_patterns(self, conn, git_repo):
        """Batch and single commit counts are populated."""
        conn.execute("UPDATE repositories SET name=?, path=? WHERE id=1",
                     ("narrative-test", git_repo))
        conn.commit()

        report = build_narrative(conn, "narrative-test")
        assert report.batch_count >= 0
        assert report.single_count >= 0
        assert (report.batch_count + report.single_count) > 0


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


class TestFormatting:
    def test_format_summary(self):
        report = NarrativeReport(
            repo_name="test",
            age_days=365,
            total_commits=100,
            total_authors=3,
            bus_factor=2,
            recent_activity="active",
        )
        summary = format_narrative_summary(report)
        assert "365d" in summary
        assert "100" in summary
        assert "active" in summary

    def test_format_summary_minimal(self):
        report = NarrativeReport(repo_name="test")
        summary = format_narrative_summary(report)
        assert summary  # non-empty


# ---------------------------------------------------------------------------
# Narrative CLI summary + timeline (integration-level smoke tests)
# ---------------------------------------------------------------------------


class TestNarrativeSummary:
    """Tests for the --summary flag which uses LLM or structural fallback."""

    def test_structural_summary_fallback(self):
        report = NarrativeReport(
            repo_name="test", age_days=100, total_commits=50, total_authors=2,
            bus_factor=1, recent_activity="active",
        )
        # When LLM is disabled, _llm_narrative_summary should return structural summary.
        from friday.cli_narrative import _llm_narrative_summary
        result = _llm_narrative_summary(report)
        assert isinstance(result, str)
        assert len(result) > 0


class TestFormatTimeline:
    """Tests for the --timeline compact display."""

    def test_empty_timeline(self):
        from friday.narrative import NarrativeReport
        from friday.cli_narrative import _format_timeline
        report = NarrativeReport(repo_name="empty")
        text = _format_timeline(report)
        assert "Timeline: empty" in text

    def test_timeline_with_phases(self):
        from friday.narrative import NarrativeReport, TimelinePhase
        from friday.cli_narrative import _format_timeline
        report = NarrativeReport(
            repo_name="test", age_days=365, total_commits=100, total_authors=3,
            phases=[TimelinePhase(
                label="Initial Build", start_date="2026-01-01",
                end_date="2026-03-01", commit_count=10, author_count=2,
            )],
        )
        text = _format_timeline(report)
        assert "Initial Build" in text
        assert "Phases:" in text
