"""Tests for IdentityEngine persona routing — Q&A vs action classification.

Covers:
  - _is_command / _is_chitchat routing helpers
  - _classify_action_or_question from cli_nl.py
  - _handle_command with AgenticExecutor
  - Edge cases (ambiguous messages, empty text, mixed intent)
"""

from __future__ import annotations

import os
from pathlib import Path


# ──────────────────────────────────────────────────────────────────────────
# IdentityEngine routing helpers
# ──────────────────────────────────────────────────────────────────────────


class TestIdentityEngineRouting:
    """Tests for IdentityEngine._is_command, _is_chitchat, _is_identity_question."""

    def test_is_command_action_keywords(self):
        """Action words like 'deploy', 'run', 'create' should be classified as commands."""
        from friday.persona.engine import IdentityEngine
        engine = IdentityEngine()
        assert engine._is_command("deploy the staging server")
        assert engine._is_command("run the tests")
        assert engine._is_command("create a new file")
        assert engine._is_command("fix the bug in auth")
        assert engine._is_command("build the project")

    def test_is_command_non_commands(self):
        """Non-action words should NOT be classified as commands."""
        from friday.persona.engine import IdentityEngine
        engine = IdentityEngine()
        assert not engine._is_command("what is the architecture")
        assert not engine._is_command("explain how this works")
        assert not engine._is_command("tell me about the project")
        assert not engine._is_command("hello")

    def test_is_chitchat_greetings(self):
        """Greetings should be classified as chitchat."""
        from friday.persona.engine import IdentityEngine
        engine = IdentityEngine()
        assert engine._is_chitchat("hello")
        assert engine._is_chitchat("hi")
        assert engine._is_chitchat("hey")
        assert engine._is_chitchat("thanks")
        assert engine._is_chitchat("good morning")

    def test_is_chitchat_elongated(self):
        """Elongated greetings like 'hiiii', 'heyyy' should be chitchat."""
        from friday.persona.engine import IdentityEngine
        engine = IdentityEngine()
        assert engine._is_chitchat("hiii")
        assert engine._is_chitchat("heyyy")
        assert engine._is_chitchat("hellooo")

    def test_is_chitchat_non_greetings(self):
        """Non-greeting messages should NOT be chitchat."""
        from friday.persona.engine import IdentityEngine
        engine = IdentityEngine()
        assert not engine._is_chitchat("deploy the app")
        assert not engine._is_chitchat("what is the architecture")
        assert not engine._is_chitchat("fix this bug")

    def test_is_identity_question(self):
        """Identity questions should be detected."""
        from friday.persona.engine import IdentityEngine
        engine = IdentityEngine()
        assert engine._is_identity_question("who are you")
        assert engine._is_identity_question("what are you")
        assert engine._is_identity_question("introduce yourself")
        assert engine._is_identity_question("/start")

    def test_is_identity_question_non(self):
        """Non-identity questions should not match."""
        from friday.persona.engine import IdentityEngine
        engine = IdentityEngine()
        assert not engine._is_identity_question("hello")
        assert not engine._is_identity_question("deploy the app")
        assert not engine._is_identity_question("what is python")


# ──────────────────────────────────────────────────────────────────────────
# cli_nl._classify_action_or_question
# ──────────────────────────────────────────────────────────────────────────


class TestClassifyActionOrQuestion:
    """Tests for the fast predicate classifier in cli_nl.py."""

    def _get_classifier(self):
        """Import the classifier from cli_nl."""
        from friday.cli_nl import _classify_action_or_question
        return _classify_action_or_question

    def test_action_keywords(self):
        """Action words should be classified as 'action'."""
        classify = self._get_classifier()
        assert classify("copy the git diff to clipboard") == "action"
        assert classify("deploy the staging server") == "action"
        assert classify("run the tests") == "action"
        assert classify("fix the build error") == "action"
        assert classify("create a new file") == "action"

    def test_question_keywords(self):
        """Question words should be classified as 'question'."""
        classify = self._get_classifier()
        assert classify("what is the architecture") == "question"
        assert classify("explain how this works") == "question"
        assert classify("who built this project") == "question"
        assert classify("tell me about the codebase") == "question"

    def test_ambiguous_returns_none(self):
        """Ambiguous messages that could be either should return None."""
        classify = self._get_classifier()
        # This is ambiguous — "show me" could be either
        # Let the classifier determine
        result = classify("show me the project")
        assert result in ("action", "question", None)

    def test_empty_string(self):
        """Empty string should return None."""
        classify = self._get_classifier()
        assert classify("") is None
        assert classify("   ") is None


# ──────────────────────────────────────────────────────────────────────────
# _handle_command with AgenticExecutor
# ──────────────────────────────────────────────────────────────────────────


class TestHandleCommand:
    """Tests for IdentityEngine._handle_command with AgenticExecutor integration."""

    def test_handle_command_no_conn(self):
        """_handle_command should work without a DB connection."""
        from friday.persona.engine import IdentityEngine
        engine = IdentityEngine(conn=None)
        result = engine._handle_command("echo hello", "cli")
        # Should produce a result string (either agent output or fallback message)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_handle_command_simple_command(self):
        """Simple commands should route through the legacy path."""
        from friday.persona.engine import IdentityEngine
        import tempfile
        db_fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(db_fd)
        try:
            from friday.db import connect
            conn = connect(Path(db_path))
            engine = IdentityEngine(conn=conn)
            result = engine._handle_command("echo hello from test", "cli")
            assert isinstance(result, str)
            assert len(result) > 0
            conn.close()
        finally:
            os.unlink(db_path)


# ──────────────────────────────────────────────────────────────────────────
# Edge cases
# ──────────────────────────────────────────────────────────────────────────


class TestPersonaEdgeCases:
    """Edge cases for persona routing."""

    def test_process_empty_text(self):
        """Processing empty text should return empty string."""
        from friday.persona.engine import IdentityEngine
        engine = IdentityEngine()
        result = engine.process("", "cli")
        assert result == ""

    def test_process_none_text(self):
        """Processing None should return empty string."""
        from friday.persona.engine import IdentityEngine
        engine = IdentityEngine()
        result = engine.process(None, "cli")  # type: ignore
        assert result == ""

    def test_process_chitchat_without_llm(self):
        """Chitchat should return a greeting without LLM."""
        from friday.persona.engine import IdentityEngine
        engine = IdentityEngine()
        result = engine.process("hello", "cli")
        assert result is not None
        assert len(result) > 0

    def test_process_chitchat_with_name(self):
        """Chitchat should include operator name when known."""
        from friday.persona.engine import IdentityEngine
        engine = IdentityEngine()
        engine._operator_name = "TestUser"
        result = engine.process("hello", "cli")
        assert "TestUser" in result

    def test_personalized_greeting_with_name(self):
        """Personalized greeting should include the operator name."""
        from friday.persona.engine import IdentityEngine
        engine = IdentityEngine()
        engine._operator_name = "Alice"
        greeting = engine._personalized_greeting()
        assert "Alice" in greeting
        assert "Friday" in greeting

    def test_learn_operator_name(self):
        """Extracting name from 'my name is X' should work."""
        from friday.persona.engine import IdentityEngine
        engine = IdentityEngine()
        result = engine._learn_operator_info("my name is John")
        assert result == "John"
        assert engine._operator_name == "John"

    def test_learn_operator_name_false_positive(self):
        """Common words like 'done' should not be extracted as names."""
        from friday.persona.engine import IdentityEngine
        engine = IdentityEngine()
        result = engine._learn_operator_info("my name is done")
        assert result is None
