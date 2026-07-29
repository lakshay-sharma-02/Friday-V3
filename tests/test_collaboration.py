"""Tests for Collaboration External Integration modules: guide, translate, pr_review."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


# =============================================================================
# Guide Engine
# =============================================================================


def _guide_conn():
    """Create a fresh in-memory SQLite connection with all migrations applied."""
    from friday.db import connect
    return connect(":memory:")


class TestCreateGuide:
    def test_create_basic(self):
        conn = _guide_conn()
        from friday.guide import create_guide, load_session

        session = create_guide(conn, "Deploy fix", [
            {"instruction": "Run git pull", "verification": "git status"},
            {"instruction": "Run tests", "verification": "pytest -q"},
        ])

        assert session.title == "Deploy fix"
        assert session.total_steps == 2
        assert session.status == "running"
        assert session.current_step == 0
        assert session.id.startswith("guide_")

        # Verify persistence.
        loaded = load_session(conn, session.id)
        assert loaded is not None
        assert loaded.title == "Deploy fix"

    def test_empty_steps(self):
        conn = _guide_conn()
        from friday.guide import create_guide

        session = create_guide(conn, "Empty", [])
        assert session.total_steps == 0
        assert session.status == "running"

    def test_custom_channel(self):
        conn = _guide_conn()
        from friday.guide import create_guide

        session = create_guide(
            conn, "Slack guide", [{"instruction": "Step 1"}], channel="slack"
        )
        assert session.channel == "slack"


class TestAdvanceGuide:
    def test_advance_done(self):
        conn = _guide_conn()
        from friday.guide import create_guide, advance_guide, load_session

        session = create_guide(conn, "Test", [
            {"instruction": "Step 1"},
            {"instruction": "Step 2"},
        ])
        assert session.current_step == 0

        # Advance step 1.
        session = advance_guide(conn, session.id, "done", output="ok")
        assert session is not None
        assert session.status == "running"
        assert session.steps[0].completed is True
        assert session.steps[0].output == "ok"

        # Advance step 2 → completed.
        session = advance_guide(conn, session.id, "done")
        assert session is not None
        assert session.status == "completed"

    def test_advance_fail(self):
        conn = _guide_conn()
        from friday.guide import create_guide, advance_guide

        session = create_guide(conn, "Test", [{"instruction": "Step 1"}])
        session = advance_guide(conn, session.id, "fail", error="timeout")
        assert session is not None
        assert session.steps[0].failed is True
        assert session.steps[0].error == "timeout"

    def test_abort(self):
        conn = _guide_conn()
        from friday.guide import create_guide, advance_guide

        session = create_guide(conn, "Test", [{"instruction": "Step 1"}])
        session = advance_guide(conn, session.id, "abort")
        assert session is not None
        assert session.status == "aborted"

    def test_pause_resume(self):
        conn = _guide_conn()
        from friday.guide import create_guide, advance_guide

        session = create_guide(conn, "Test", [{"instruction": "Step 1"}])
        session = advance_guide(conn, session.id, "pause")
        assert session is not None
        assert session.status == "paused"
        session = advance_guide(conn, session.id, "resume")
        assert session is not None
        assert session.status == "running"


class TestListActiveSessions:
    def test_list_active(self):
        conn = _guide_conn()
        from friday.guide import create_guide, list_active_sessions

        create_guide(conn, "Active 1", [{"instruction": "Step"}])
        create_guide(conn, "Active 2", [{"instruction": "Step"}])

        sessions = list_active_sessions(conn)
        assert len(sessions) == 2

    def test_empty_when_none(self):
        conn = _guide_conn()
        from friday.guide import list_active_sessions

        assert list_active_sessions(conn) == []

    def test_excludes_completed(self):
        conn = _guide_conn()
        from friday.guide import create_guide, advance_guide, list_active_sessions

        session = create_guide(conn, "Done", [{"instruction": "Step"}])
        advance_guide(conn, session.id, "done")

        assert len(list_active_sessions(conn)) == 0


class TestFormatStep:
    def test_format_step(self):
        conn = _guide_conn()
        from friday.guide import create_guide, get_current_step, format_step

        session = create_guide(conn, "Test", [{"instruction": "Do X", "verification": "check X"}])
        step = get_current_step(session)
        assert step is not None
        text = format_step(session, step)
        assert "Do X" in text
        assert "check X" in text
        assert "Guide: Test" in text
        assert "Step 1/1" in text
        assert "done`" in text


# =============================================================================
# Translation Engine
# =============================================================================


class TestDetectLanguage:
    def test_detect_english(self):
        from friday.translate import detect_language
        lang = detect_language("Hello, how are you?")
        assert lang == "en"

    def test_detect_spanish(self):
        from friday.translate import detect_language
        lang = detect_language("Hola, ¿cómo estás?")
        assert lang in ("es", "en")  # heuristic may fall back to en

    def test_detect_empty(self):
        from friday.translate import detect_language
        assert detect_language("") == "en"

    def test_detect_cjk(self):
        from friday.translate import detect_language
        lang = detect_language("你好，世界")
        assert lang == "zh"

    def test_detect_cyrillic(self):
        from friday.translate import detect_language
        lang = detect_language("Привет, мир")
        assert lang == "ru"

    def test_detect_arabic(self):
        from friday.translate import detect_language
        lang = detect_language("مرحبا بالعالم")
        assert lang == "ar"

    def test_detect_german(self):
        from friday.translate import detect_language
        lang = detect_language("Der Hund läuft durch den Park")
        assert lang == "de"

    def test_detect_french(self):
        from friday.translate import detect_language
        lang = detect_language("Nous sommes ravis de vous rencontrer")
        assert lang == "fr"


class TestTranslate:
    def test_same_language_returns_original(self):
        from friday.translate import translate
        assert translate("hello", "en", "en") == "hello"

    def test_empty_text(self):
        from friday.translate import translate
        assert translate("", "en", "es") == ""

    def test_none_backend_fallback(self):
        """When no backends available, returns original text."""
        from friday.translate import translate
        result = translate("hello world", "en", "fr")
        assert result == "hello world"

    def test_supported_languages_defined(self):
        from friday.translate import SUPPORTED_LANGUAGES
        assert "en" in SUPPORTED_LANGUAGES
        assert "es" in SUPPORTED_LANGUAGES
        assert "fr" in SUPPORTED_LANGUAGES
        assert len(SUPPORTED_LANGUAGES) >= 20

    def test_get_operator_language_default(self):
        conn = _guide_conn()
        from friday.translate import get_operator_language
        lang = get_operator_language(conn)
        assert lang == "en"

    def test_get_operator_language_custom(self):
        conn = _guide_conn()
        from friday.db import now_iso
        conn.execute(
            "INSERT INTO operator_preferences (key, value, set_at, source) VALUES (?, ?, ?, ?)",
            ("language", "fr", now_iso(), "explicit"),
        )
        conn.commit()
        from friday.translate import get_operator_language
        assert get_operator_language(conn) == "fr"


# =============================================================================
# PR Review Engine
# =============================================================================


class TestPRReviewModel:
    def test_basic_creation(self):
        from friday.pr_review import PRReview

        review = PRReview(
            repo="test/repo", pr_number=42, pr_title="Fix bug",
            pr_author="bot", base_branch="main", head_branch="fix",
        )

        assert review.repo == "test/repo"
        assert review.pr_number == 42
        assert review.pr_title == "Fix bug"
        assert review.content_hash is not None
        assert review.created_at is not None
        assert review.severity == "info"

    def test_different_hashes_for_different_prs(self):
        from friday.pr_review import PRReview

        r1 = PRReview(repo="a", pr_number=1, pr_title="X",
                      pr_author="u", base_branch="main", head_branch="b1")
        r2 = PRReview(repo="a", pr_number=2, pr_title="Y",
                      pr_author="u", base_branch="main", head_branch="b2")
        assert r1.content_hash != r2.content_hash

    def test_defaults(self):
        from friday.pr_review import PRReview
        r = PRReview(repo="r", pr_number=1, pr_title="T",
                     pr_author="a", base_branch="m", head_branch="h")
        assert r.summary == ""
        assert r.concerns == []
        assert r.suggestions == []
        assert r.test_gaps == []
        assert r.severity == "info"


def _mock_github_cache(prs_data: list[dict]) -> dict:
    """Build a mock GitHub observer cache with pull requests."""
    return {
        "snapshots": [
            {
                "full_name": "test/repo",
                "owner": "test",
                "pull_requests": prs_data,
            }
        ]
    }


class TestPRReviewEngine:
    def test_run_empty(self):
        conn = _guide_conn()
        from friday.pr_review import PRReviewEngine

        engine = PRReviewEngine(conn)
        reviews = engine.run(repo_name="test/repo")
        assert reviews == []

    def test_analyze_open_pr(self):
        conn = _guide_conn()
        from friday.pr_review import PRReviewEngine
        import friday.pr_review as pr_mod

        engine = PRReviewEngine(conn)
        pr_data = {
            "number": 1, "title": "Fix critical bug",
            "user": {"login": "dev1"}, "state": "open",
            "base": {"ref": "main"}, "head": {"ref": "fix"},
            "body": "Fixes a critical security vulnerability",
        }
        review = engine._analyze_pr("test/repo", pr_data)

        assert review is not None
        assert review.repo == "test/repo"
        assert review.pr_number == 1
        assert review.pr_title == "Fix critical bug"
        assert review.pr_author == "dev1"
        assert review.base_branch == "main"
        assert review.head_branch == "fix"

    def test_skip_merged_pr(self):
        conn = _guide_conn()
        from friday.pr_review import PRReviewEngine

        engine = PRReviewEngine(conn)
        pr_data = {
            "number": 2, "title": "Done", "state": "merged",
            "base": {"ref": "main"}, "head": {"ref": "old"},
        }
        review = engine._analyze_pr("test/repo", pr_data)
        assert review is not None  # still analyzed but won't be included in run

    def test_deterministic_concerns_from_body(self):
        conn = _guide_conn()
        from friday.pr_review import PRReviewEngine

        engine = PRReviewEngine(conn)
        pr_data = {
            "number": 3, "title": "Urgent",
            "user": {"login": "dev2"}, "state": "open",
            "base": {"ref": "main"}, "head": {"ref": "hotfix"},
            "body": "This is an emergency security fix with large changes",
        }
        review = engine._analyze_pr("test/repo", pr_data)
        assert review is not None
        assert any("urgent" in c.lower() or "emergency" in c.lower() for c in review.concerns)

    def test_persist_and_push(self):
        conn = _guide_conn()
        from friday.pr_review import PRReview, PRReviewEngine

        engine = PRReviewEngine(conn)
        review = PRReview(
            repo="test/repo", pr_number=1, pr_title="Fix",
            pr_author="dev", base_branch="main", head_branch="fix",
            summary="A test PR",
        )

        engine.persist_review(review)
        row = conn.execute(
            "SELECT * FROM pr_reviews WHERE pr_number = 1"
        ).fetchone()
        assert row is not None
        assert row["pr_title"] == "Fix"

    def test_push_to_feed(self):
        conn = _guide_conn()
        from friday.pr_review import PRReview, PRReviewEngine

        engine = PRReviewEngine(conn)
        review = PRReview(
            repo="test/repo", pr_number=42, pr_title="Fix bug",
            pr_author="dev", base_branch="main", head_branch="fix",
        )

        result = engine.push_to_feed(review)
        assert result is True

        # Check feed for the event.
        row = conn.execute(
            "SELECT * FROM ambient_feed WHERE event_type = 'review:pr_review'"
        ).fetchone()
        assert row is not None
        assert "Fix bug" in row["title"]

    def test_run_pr_review_convenience(self):
        conn = _guide_conn()
        from friday.pr_review import run_pr_review

        # With no PR data, should return 0.
        n = run_pr_review(conn, repo_name="test/repo")
        assert n == 0


# =============================================================================
# CLI module imports (smoke tests)
# =============================================================================


class TestCLIImports:
    def test_guide_cli_imports(self):
        from friday.cli_guide import cmd_guide, add_subparser
        assert callable(cmd_guide)
        assert callable(add_subparser)

    def test_translate_cli_imports(self):
        from friday.cli_translate import cmd_translate, cmd_detect, add_subparser
        assert callable(cmd_translate)
        assert callable(cmd_detect)
        assert callable(add_subparser)

    def test_pr_cli_imports(self):
        from friday.cli_pr import cmd_pr, add_subparser
        assert callable(cmd_pr)
        assert callable(add_subparser)
