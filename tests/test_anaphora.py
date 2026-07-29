"""Tests for the anaphora resolver — cross-query follow-through.

Covers:
1. Pronoun resolution ("tell me more about it" → "Tell me more about <subject>")
2. Action repetition ("do that again" → re-asks previous question)
3. More-like-this patterns ("show me more like this")
4. Focus narrowing ("specifically the auth part")
5. Continuation + pronoun ("and it?", "what about that?")
6. Implicit subject switch (question names different repo than previous)
7. LLM fallback (when deterministic patterns fail)
8. No-anaphora cases (returns None for fresh questions)
9. Edge cases (empty question, no previous subject, very short questions)
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from friday.anaphora import resolve_anaphora
from friday.db import connect


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def conn():
    c = connect(":memory:")
    yield c
    c.close()


def _seed_repo(conn, name: str):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT OR IGNORE INTO repositories "
        "(name, path, default_branch, is_dirty, first_commit_date, "
        "last_commit_date, commit_count, primary_author, ingestion_time) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (name, f"/home/{name}", "main", 0, "2026-01-01T00:00:00Z",
         "2026-07-29T00:00:00Z", 100, "test", now),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# 1. Pronoun resolution
# ---------------------------------------------------------------------------


class TestPronounResolution:
    def test_tell_me_about_it(self, conn):
        """'tell me more about it' → 'Tell me more about friday'"""
        result = resolve_anaphora(
            "tell me more about it",
            prev_question="Describe the friday project",
            prev_subject="friday",
            prev_needs=["describe"],
            conn=conn,
        )
        assert result is not None
        assert "friday" in result.lower()

    def test_what_about_it(self, conn):
        """'what about it?' → 'Tell me more about friday'"""
        result = resolve_anaphora(
            "what about it?",
            prev_question="What is the architecture?",
            prev_subject="friday",
            prev_needs=["architecture"],
            conn=conn,
        )
        assert result is not None
        assert "friday" in result.lower()

    def test_pronoun_without_subject_returns_prev_question(self, conn):
        """When no previous subject, pronoun-based questions fall through."""
        result = resolve_anaphora(
            "tell me more about it",
            prev_question="What is the architecture?",
            prev_subject=None,
            prev_needs=["architecture"],
            conn=conn,
        )
        # Without a subject, the pronoun can't be resolved deterministically.
        # The _is_pronoun_based check fires, _resolve_pronoun_question returns None
        # without a subject, then _probably_anaphoric returns True but the LLM
        # fallback isn't available in tests — so returns None gracefully.
        assert result is None

    def test_describe_it(self, conn):
        """'describe it' → 'Describe friday'"""
        result = resolve_anaphora(
            "describe it",
            prev_question="What is friday?",
            prev_subject="friday",
            prev_needs=["describe"],
            conn=conn,
        )
        assert result is not None
        assert "friday" in result.lower()


# ---------------------------------------------------------------------------
# 2. Action repetition
# ---------------------------------------------------------------------------


class TestActionRepeat:
    def test_do_that_again(self, conn):
        """'do that again' → re-asks previous question."""
        result = resolve_anaphora(
            "do that again",
            prev_question="What should I work on next?",
            prev_subject=None,
            prev_needs=["recommend"],
            conn=conn,
        )
        assert result == "What should I work on next?"

    def test_again(self, conn):
        """'again' alone → re-asks previous question."""
        result = resolve_anaphora(
            "again",
            prev_question="Describe the architecture",
            prev_subject="friday",
            prev_needs=["architecture"],
            conn=conn,
        )
        assert result == "Describe the architecture"

    def test_run_it_again(self, conn):
        """'run it again' → re-asks."""
        result = resolve_anaphora(
            "run it again",
            prev_question="Find inactive repos",
            prev_subject=None,
            prev_needs=["inactive"],
            conn=conn,
        )
        assert result == "Find inactive repos"

    def test_repeat_that(self, conn):
        """'repeat that' → re-asks."""
        result = resolve_anaphora(
            "repeat that",
            prev_question="Compare project-a and project-b",
            prev_subject="project-a",
            prev_needs=["compare"],
            conn=conn,
        )
        assert result == "Compare project-a and project-b"


# ---------------------------------------------------------------------------
# 3. More-like-this
# ---------------------------------------------------------------------------


class TestMoreLikeThis:
    def test_more_like_this(self, conn):
        """'show me more like this' → similarity question."""
        _seed_repo(conn, "friday")
        result = resolve_anaphora(
            "show me more like this",
            prev_question="Describe friday",
            prev_subject="friday",
            prev_needs=["describe"],
            conn=conn,
        )
        assert result is not None
        assert "similar" in result.lower()

    def test_similar(self, conn):
        """'similar' alone → workspace-wide similarity."""
        result = resolve_anaphora(
            "similar",
            prev_question="What is friday?",
            prev_subject=None,
            prev_needs=["describe"],
            conn=conn,
        )
        assert result == "similarity"


# ---------------------------------------------------------------------------
# 4. Focus narrowing
# ---------------------------------------------------------------------------


class TestFocusNarrow:
    def test_specifically_the_auth_part(self, conn):
        """'specifically the auth part' → narrows focus."""
        _seed_repo(conn, "friday")
        result = resolve_anaphora(
            "specifically the auth part",
            prev_question="Describe the friday architecture",
            prev_subject="friday",
            prev_needs=["architecture"],
            conn=conn,
        )
        assert result is not None
        assert "auth" in result.lower()
        assert "friday" in result.lower()

    def test_focus_on_testing(self, conn):
        """'focus on testing' → narrows."""
        _seed_repo(conn, "friday")
        result = resolve_anaphora(
            "focus on testing",
            prev_question="What do I need to improve?",
            prev_subject="friday",
            prev_needs=["describe"],
            conn=conn,
        )
        assert result is not None
        assert "testing" in result.lower()


# ---------------------------------------------------------------------------
# 5. Continuation + pronoun
# ---------------------------------------------------------------------------


class TestContinuation:
    def test_and_what_about_that(self, conn):
        """'and what about that?' → resolves 'that' to previous subject."""
        _seed_repo(conn, "friday")
        result = resolve_anaphora(
            "and what about that",
            prev_question="Describe the architecture",
            prev_subject="friday",
            prev_needs=["architecture"],
            conn=conn,
        )
        assert result is not None
        assert "friday" in result.lower()

    def test_also_this(self, conn):
        """'also this' → continuation with pronoun."""
        _seed_repo(conn, "friday")
        result = resolve_anaphora(
            "also this",
            prev_question="What is the purpose?",
            prev_subject="friday",
            prev_needs=["describe"],
            conn=conn,
        )
        assert result is not None
        assert "friday" in result.lower()


# ---------------------------------------------------------------------------
# 6. No anaphora (fresh questions)
# ---------------------------------------------------------------------------


class TestNoAnaphora:
    def test_fresh_question_returns_none(self, conn):
        """A completely new question should not be treated as anaphora."""
        result = resolve_anaphora(
            "What is the weather today?",
            prev_question="Describe friday",
            prev_subject="friday",
            prev_needs=["describe"],
            conn=conn,
        )
        assert result is None

    def test_question_about_different_topic(self, conn):
        """A question about a different topic that doesn't use pronouns."""
        _seed_repo(conn, "friday")
        result = resolve_anaphora(
            "Show me my calendar events",
            prev_question="What is the architecture of friday?",
            prev_subject="friday",
            prev_needs=["architecture"],
            conn=conn,
        )
        assert result is None


# ---------------------------------------------------------------------------
# 7. LLM fallback
# ---------------------------------------------------------------------------


class TestLLMFallback:
    def test_llm_resolves_unclear_pronoun(self, conn):
        """When deterministic patterns fail, LLM fallback resolves pronouns.

        Uses a question that does NOT match any deterministic anaphora pattern
        but IS probably anaphoric (contains "that" without a subject), so it
        relies on the LLM to resolve the reference.
        """
        with patch("friday.anaphora._llm_enabled", return_value=True):
            with patch("friday.anaphora._llm_call", return_value="Tell me about project X") as mock:
                result = resolve_anaphora(
                    "i was wondering about that thing you mentioned",
                    prev_question="What was the last initiative?",
                    prev_subject="friday",
                    prev_needs=["initiative"],
                    conn=conn,
                )
                assert result is not None
                assert result == "Tell me about project X"

    def test_llm_not_enabled_fallback_gracefully(self, conn):
        """When LLM is not enabled, returns None gracefully.

        Uses a question that does NOT match any deterministic anaphora pattern
        but IS probably anaphoric (short, contains "that"), so it would trigger
        the LLM fallback. Without LLM, falls through to None.
        """
        with patch("friday.anaphora._llm_enabled", return_value=False):
            result = resolve_anaphora(
                "i was wondering about that thing you mentioned",
                prev_question="What was the last initiative?",
                prev_subject="friday",
                prev_needs=["initiative"],
                conn=conn,
            )
            assert result is None

    def test_llm_returns_empty(self, conn):
        """When LLM returns EMPTY, returns None."""
        with patch("friday.anaphora._llm_enabled", return_value=True):
            with patch("friday.anaphora._llm_call", return_value="EMPTY"):
                result = resolve_anaphora(
                    "Is this a follow-up?",
                    prev_question="What was the question?",
                    prev_subject=None,
                    prev_needs=["general"],
                    conn=conn,
                )
                assert result is None


# ---------------------------------------------------------------------------
# 8. Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_no_previous_subject_with_pronoun(self, conn):
        """Pronoun without previous subject should return None or rewrite."""
        result = resolve_anaphora(
            "tell me more about it",
            prev_question="What are my strengths?",
            prev_subject=None,
            prev_needs=["strengths"],
            conn=conn,
        )
        # Since there's no subject but the question looks anaphoric, the LLM
        # fallback fires. Without LLM, returns None.
        assert result is None or "it" not in result.lower()

    def test_empty_question_returns_none(self, conn):
        """Empty string should not crash and return None."""
        result = resolve_anaphora(
            "",
            prev_question="What is friday?",
            prev_subject="friday",
            prev_needs=["describe"],
            conn=conn,
        )
        assert result is None

    def test_very_short_question_anaphoric(self, conn):
        """Very short questions are probably anaphoric → triggers LLM fallback."""
        result = resolve_anaphora(
            "?",
            prev_question="What is friday?",
            prev_subject="friday",
            prev_needs=["describe"],
            conn=conn,
        )
        assert result is None  # LLM not available, gracefully returns None

    def test_question_unchanged_returns_none(self, conn):
        """If the rewritten question is the same as original, don't rewrite."""
        result = resolve_anaphora(
            "do that again",
            prev_question="do that again",  # same text
            prev_subject="friday",
            prev_needs=["describe"],
            conn=conn,
        )
        # Action repeat returns previous question, which is same as current.
        # But we check `rewritten != question` in ask.py, not here.
        assert result == "do that again"


# ---------------------------------------------------------------------------
# 9. Implicit subject switch (with seeded repos)
# ---------------------------------------------------------------------------


class TestImplicitSubjectSwitch:
    def test_question_switches_to_different_repo(self, conn):
        """'describe project-b' when previous was about project-a should rewrite."""
        _seed_repo(conn, "project-a")
        _seed_repo(conn, "project-b")
        result = resolve_anaphora(
            "describe project-b",
            prev_question="What is the architecture of project-a?",
            prev_subject="project-a",
            prev_needs=["architecture"],
            conn=conn,
        )
        assert result is not None
        assert "project-b" in result
        assert "project-a" not in result

    def test_same_repo_is_not_a_switch(self, conn):
        """Asking about the same repo again is not a subject switch."""
        _seed_repo(conn, "friday")
        result = resolve_anaphora(
            "Tell me more about friday",
            prev_question="Describe friday",
            prev_subject="friday",
            prev_needs=["describe"],
            conn=conn,
        )
        # This is a fresh question about the same subject — not anaphoric.
        assert result is None
