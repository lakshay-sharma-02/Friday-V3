"""Tests for the Spontaneous Code Review engine — proactive code review notes."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from friday.spontaneous_review import (
    ReviewNote,
    SpontaneousReviewEngine,
    _days_since,
)
from friday.impact import scan_dirty_patterns


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def conn():
    """In-memory SQLite connection with minimal schema."""
    from friday.db import connect
    c = connect(":memory:")

    # Drop and re-create pending_initiatives WITHOUT watch_run_id NOT NULL,
    # since spontaneous review notes don't originate from watch cycles.
    c.execute("DROP TABLE IF EXISTS pending_initiatives")
    c.execute(
        "CREATE TABLE IF NOT EXISTS pending_initiatives ("
        "  id TEXT PRIMARY KEY,"
        "  title TEXT NOT NULL,"
        "  statement TEXT NOT NULL,"
        "  initiative_type TEXT NOT NULL,"
        "  confidence TEXT NOT NULL,"
        "  understanding_ids TEXT NOT NULL DEFAULT '',"
        "  knowledge_ids TEXT NOT NULL DEFAULT '',"
        "  detected_at TEXT NOT NULL,"
        "  watch_run_id INTEGER,"
        "  reviewed INTEGER NOT NULL DEFAULT 0,"
        "  reviewed_at TEXT,"
        "  dismissed_at TEXT,"
        "  action_taken TEXT"
        ")"
    )
    c.commit()

    # Build the minimal tables trigger resolvers need.
    c.executescript("""
        CREATE TABLE IF NOT EXISTS watch_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL, finished_at TEXT,
            outcome TEXT NOT NULL DEFAULT 'running'
        );
        CREATE TABLE IF NOT EXISTS repositories (
            id INTEGER PRIMARY KEY, name TEXT, path TEXT
        );
        CREATE TABLE IF NOT EXISTS skills (
            name TEXT, health TEXT, success_rate REAL, error_rate REAL,
            drift_reason TEXT, last_executed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS ambient_feed (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT, event_type TEXT, title TEXT, detail TEXT,
            source TEXT, project TEXT, payload TEXT, confidence REAL,
            priority INTEGER, category TEXT, dismissed INTEGER,
            actionable INTEGER, action_label TEXT, action_command TEXT,
            mission_id TEXT, graph_id TEXT
        );
    """)
    c.commit()
    yield c
    c.close()


@pytest.fixture
def git_repo():
    """Create a temporary git repository with some test files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = Path(tmpdir) / "test_repo"
        repo_path.mkdir(parents=True)

        import subprocess

        # Initialize git repo.
        subprocess.run(["git", "init"], cwd=repo_path, capture_output=True, timeout=30)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=repo_path, capture_output=True, timeout=30,
        )
        subprocess.run(
            ["git", "config", "user.name", "Tester"],
            cwd=repo_path, capture_output=True, timeout=30,
        )

        # Create an initial commit with some files.
        (repo_path / "README.md").write_text("# Test Repo")
        (repo_path / "main.py").write_text("def main():\n    print('hello')\n")
        (repo_path / "utils.py").write_text("def helper():\n    return 42\n")

        subprocess.run(["git", "add", "-A"], cwd=repo_path, capture_output=True, timeout=30)
        subprocess.run(
            ["git", "commit", "-m", "Initial commit"],
            cwd=repo_path, capture_output=True, timeout=30,
        )

        yield str(repo_path)


# ---------------------------------------------------------------------------
# ReviewNote model
# ---------------------------------------------------------------------------


class TestReviewNote:
    def test_basic_note(self):
        note = ReviewNote(
            title="Test finding",
            severity="medium",
            category="dirty_repo",
            repo="test-repo",
            detail="Something worth reviewing.",
        )
        assert note.title == "Test finding"
        assert note.severity == "medium"
        assert note.category == "dirty_repo"
        assert note.repo == "test-repo"
        assert note.content_hash  # computed automatically
        assert len(note.content_hash) == 16

    def test_content_hash_deterministic(self):
        n1 = ReviewNote(
            title="Same finding",
            severity="high",
            category="ci_failure",
            repo="repo",
            detail="some detail",
        )
        n2 = ReviewNote(
            title="Same finding",
            severity="high",
            category="ci_failure",
            repo="repo",
            detail="some detail",
        )
        assert n1.content_hash == n2.content_hash

    def test_content_hash_differs_on_different_content(self):
        n1 = ReviewNote(
            title="Finding A",
            severity="high",
            category="ci_failure",
            repo="repo",
            detail="detail A",
        )
        n2 = ReviewNote(
            title="Finding B",
            severity="high",
            category="ci_failure",
            repo="repo",
            detail="detail B",
        )
        assert n1.content_hash != n2.content_hash

    def test_to_pending_row(self):
        note = ReviewNote(
            title="Test finding",
            severity="high",
            category="dirty_repo",
            repo="test-repo",
            file="main.py",
            detail="Something worth reviewing.",
            action_command="friday impact main.py",
        )
        row = note.to_pending_row()
        assert row["title"] == "Test finding"
        assert "spontaneous_review:" in row["initiative_type"]
        assert row["confidence"] == "high"
        blob = json.loads(row["knowledge_ids"])
        assert blob["content_hash"] == note.content_hash
        assert blob["category"] == "dirty_repo"
        assert blob["repo"] == "test-repo"


# ---------------------------------------------------------------------------
# SpontaneousReviewEngine
# ---------------------------------------------------------------------------


class TestSpontaneousReviewEngine:
    def test_init_loads_known_hashes(self, conn):
        """Engine should load existing hashes from the pending queue."""
        # Seed a pending item.
        conn.execute(
            "INSERT INTO pending_initiatives "
            "(id, title, statement, initiative_type, confidence, knowledge_ids, detected_at, reviewed) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
            (
                "test_note",
                "Existing note",
                "Some detail",
                "spontaneous_review:dirty_repo",
                "medium",
                json.dumps({"content_hash": "abcdef1234567890"}),
                "2026-07-29T00:00:00",
            ),
        )
        conn.commit()

        engine = SpontaneousReviewEngine(conn)
        assert "abcdef1234567890" in engine._known_hashes

    def test_dirty_repo_trigger_finds_nothing_on_clean_repo(self, conn):
        """A clean repo should produce no review notes."""
        engine = SpontaneousReviewEngine(conn)
        notes = engine._check_dirty_repos()
        assert len(notes) == 0

    def test_push_to_feed(self, conn):
        """High-severity notes should be pushed to the ambient feed."""
        engine = SpontaneousReviewEngine(conn)
        note = ReviewNote(
            title="High severity finding",
            severity="high",
            category="ci_failure",
            repo="test-repo",
            detail="Something urgent.",
            action_command="friday impact test",
        )
        pushed = engine.push_to_feed([note])
        assert pushed >= 1

        # Verify it appeared in the feed.
        from friday.ambient import get_feed
        events = get_feed(conn, limit=5)
        titles = [e.title for e in events]
        assert "High severity finding" in titles

    def test_push_to_pending(self, conn):
        """Notes should be insertable into the pending queue."""
        engine = SpontaneousReviewEngine(conn)
        note = ReviewNote(
            title="Pending finding",
            severity="medium",
            category="skill_drift",
            repo="test-repo",
            detail="A skill is degrading.",
        )
        inserted = engine.push_to_pending([note])
        assert inserted == 1

        # Verify it's in the table.
        row = conn.execute(
            "SELECT * FROM pending_initiatives WHERE title = ?",
            ("Pending finding",),
        ).fetchone()
        assert row is not None
        assert row["initiative_type"] == "spontaneous_review:skill_drift"

    def test_dedup_pending_queue(self, conn):
        """Same note should not be inserted twice."""
        engine = SpontaneousReviewEngine(conn)
        note = ReviewNote(
            title="Dedup test",
            severity="low",
            category="blast_radius",
            repo="r",
            detail="Test.",
        )
        first = engine.push_to_pending([note])
        second = engine.push_to_pending([note])
        assert first == 1
        assert second == 0  # deduped

    def test_skill_drift_trigger_empty(self, conn):
        """No drifted skills -> no notes."""
        engine = SpontaneousReviewEngine(conn)
        notes = engine._check_skill_drift()
        assert len(notes) == 0, "Expected no drift notes when skills table is empty"

    def test_pr_signals_trigger_empty(self, conn):
        """No GitHub cache -> no notes."""
        engine = SpontaneousReviewEngine(conn)
        notes = engine._check_pr_signals()
        assert len(notes) == 0

    def test_ci_failures_trigger_empty(self, conn):
        """No GitHub cache -> no notes."""
        engine = SpontaneousReviewEngine(conn)
        notes = engine._check_ci_failures()
        assert len(notes) == 0

    def test_blast_radius_trigger_empty(self, conn):
        """No dirty repos -> no notes."""
        engine = SpontaneousReviewEngine(conn)
        notes = engine._check_blast_radius()
        assert len(notes) == 0


# ---------------------------------------------------------------------------
# impact.scan_dirty_patterns
# ---------------------------------------------------------------------------


class TestScanDirtyPatterns:
    def test_clean_repo_no_findings(self, git_repo):
        """A clean repo (no uncommitted changes) should produce no findings."""
        findings = scan_dirty_patterns(git_repo)
        assert len(findings) == 0

    @staticmethod
    def _git_add(repo: str, filename: str) -> None:
        import subprocess
        subprocess.run(["git", "add", filename], cwd=repo, capture_output=True, timeout=10)

    def test_detect_todo(self, git_repo):
        """A dirty file with TODO should be detected."""
        repo_path = Path(git_repo)
        (repo_path / "main.py").write_text("def main():\n    # TODO: implement this\n    pass\n")
        self._git_add(git_repo, "main.py")

        findings = scan_dirty_patterns(git_repo)
        todos = [f for f in findings if "TODO" in f["label"]]
        assert len(todos) >= 1
        assert todos[0]["severity"] == "medium"

    def test_detect_merge_conflict(self, git_repo):
        """Merge conflict markers should be detected as high severity."""
        repo_path = Path(git_repo)
        (repo_path / "main.py").write_text("def main():\n<<<<<<< HEAD\n    print('old')\n=======\n    print('new')\n>>>>>>> branch\n")
        self._git_add(git_repo, "main.py")

        findings = scan_dirty_patterns(git_repo)
        conflicts = [f for f in findings if "conflict" in f["label"].lower()]
        assert len(conflicts) >= 1
        assert conflicts[0]["severity"] == "high"

    def test_detect_debug_print(self, git_repo):
        """Debug print statements should be detected."""
        repo_path = Path(git_repo)
        (repo_path / "utils.py").write_text("def helper():\n    print('debug')\n    return 42\n")
        self._git_add(git_repo, "utils.py")

        findings = scan_dirty_patterns(git_repo)
        debug_prints = [f for f in findings if "print" in f["label"].lower()]
        assert len(debug_prints) >= 1

    def test_detect_debugger_left_in(self, git_repo):
        """Breakpoint/pdb left in code should be high severity."""
        repo_path = Path(git_repo)
        (repo_path / "utils.py").write_text("def helper():\n    import pdb; pdb.set_trace()\n    return 42\n")
        self._git_add(git_repo, "utils.py")

        findings = scan_dirty_patterns(git_repo)
        debuggers = [f for f in findings if "Debugger" in f["label"]]
        assert len(debuggers) >= 1
        assert debuggers[0]["severity"] == "high"

    def test_detect_very_large_change(self, git_repo):
        """A very large hunk should be flagged."""
        repo_path = Path(git_repo)
        big_content = "\n".join(f"line_{i} = {i}" for i in range(220))
        (repo_path / "big_file.py").write_text(big_content)
        self._git_add(git_repo, "big_file.py")

        findings = scan_dirty_patterns(git_repo)
        large = [f for f in findings if "Large" in f["label"]]
        assert len(large) >= 1

    def test_detect_fixme(self, git_repo):
        """FIXME comments should be detected."""
        repo_path = Path(git_repo)
        (repo_path / "main.py").write_text("def main():\n    # FIXME: this is broken\n    pass\n")
        self._git_add(git_repo, "main.py")

        findings = scan_dirty_patterns(git_repo)
        fixmes = [f for f in findings if "FIXME" in f["label"]]
        assert len(fixmes) >= 1


# ---------------------------------------------------------------------------
# _days_since utility
# ---------------------------------------------------------------------------


class TestDaysSince:
    def test_none_input(self):
        assert _days_since(None) is None

    def test_empty_input(self):
        assert _days_since("") is None

    def test_recent_date(self):
        from datetime import datetime, timezone, timedelta
        recent = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
        days = _days_since(recent)
        assert days is not None
        assert days == 0  # less than 1 day

    def test_old_date(self):
        from datetime import datetime, timezone, timedelta
        old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        days = _days_since(old)
        assert days is not None
        assert days >= 28  # approximately 30 days, accounting for test timing
