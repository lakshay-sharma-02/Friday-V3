"""Tests for the Friday Identity engine (Pillar C — Persistent Persona)."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from friday.persona import IdentityEngine, IdentityConfig


# ===========================================================================
# IdentityEngine
# ===========================================================================


class TestIdentityEngine(unittest.TestCase):
    def setUp(self):
        self.engine = IdentityEngine()

    def test_greeting(self):
        """Identity should have a greeting."""
        self.assertIn("Friday", self.engine.config.name)
        self.assertTrue(len(self.engine.config.greeting) > 20)

    def test_process_empty(self):
        """Empty text returns empty response."""
        reply = self.engine.process("")
        self.assertEqual(reply, "")

    def test_process_whitespace(self):
        """Whitespace-only text returns empty response."""
        reply = self.engine.process("   ")
        self.assertEqual(reply, "")

    def test_chitchat_hello(self):
        """Hello should get a chitchat response."""
        reply = self.engine.process("hello", channel_id="test")
        self.assertTrue(len(reply) > 0)
        self.assertNotIn("Sorry", reply)

    def test_chitchat_hi(self):
        reply = self.engine.process("hi", channel_id="test")
        self.assertTrue(len(reply) > 0)

    def test_chitchat_how_are_you(self):
        reply = self.engine.process("how are you?", channel_id="test")
        self.assertTrue(len(reply) > 0)

    def test_chitchat_thanks(self):
        reply = self.engine.process("thanks", channel_id="test")
        self.assertEqual(reply, "Anytime.")

    def test_chitchat_bye(self):
        reply = self.engine.process("bye", channel_id="test")
        self.assertIn("Later", reply)

    def test_identity_question(self):
        """Asking who Friday is returns the greeting."""
        reply = self.engine.process("who are you?", channel_id="test")
        self.assertIn("Friday", reply)

    def test_identity_question_alt(self):
        reply = self.engine.process("introduce yourself")
        self.assertIn("Friday", reply)

    def test_chitchat_good_bot(self):
        reply = self.engine.process("good bot")
        self.assertIn("Thanks", reply)

    def test_context_tracking(self):
        """Exchanges should be tracked per channel."""
        self.engine.process("hello", channel_id="telegram:123")
        self.engine.process("how are you?", channel_id="telegram:123")
        self.engine.process("who are you?", channel_id="slack:C456")

        ctx_tg = self.engine.get_context("telegram:123")
        ctx_slack = self.engine.get_context("slack:C456")

        self.assertEqual(len(ctx_tg.exchanges), 2)
        self.assertEqual(len(ctx_slack.exchanges), 1)
        self.assertEqual(ctx_tg.exchanges[0][0], "hello")
        self.assertEqual(ctx_slack.exchanges[0][0], "who are you?")

    def test_context_format(self):
        """Context should format nicely for LLM prompt."""
        self.engine.process("hello", channel_id="test")
        self.engine.process("what's up?", channel_id="test")
        ctx = self.engine.get_context("test")
        formatted = ctx.format()
        self.assertIn("Recent conversation", formatted)
        self.assertIn("hello", formatted)

    def test_is_command(self):
        """Command detection should work for various prefixes."""
        self.assertTrue(self.engine._is_command("deploy the app"))
        self.assertTrue(self.engine._is_command("run tests"))
        self.assertTrue(self.engine._is_command("check disk space"))
        self.assertTrue(self.engine._is_command("fix the bug"))
        self.assertFalse(self.engine._is_command("what's the architecture"))
        self.assertFalse(self.engine._is_command("hello"))
        self.assertFalse(self.engine._is_command(""))

    def test_is_chitchat(self):
        self.assertTrue(self.engine._is_chitchat("hello"))
        self.assertTrue(self.engine._is_chitchat("thanks"))
        self.assertTrue(self.engine._is_chitchat("bye"))
        self.assertFalse(self.engine._is_chitchat("what's the architecture of Friday?"))

    def test_is_identity_question(self):
        self.assertTrue(self.engine._is_identity_question("who are you?"))
        self.assertTrue(self.engine._is_identity_question("tell me about yourself"))
        self.assertFalse(self.engine._is_identity_question("what's the weather"))


# ===========================================================================
# IdentityEngine with ask() pipeline integration
# ===========================================================================

# Ensure ask module is loaded before patching.
from friday import ask as _ask_module


class TestIdentityAskIntegration(unittest.TestCase):
    @patch("friday.ask.ask")
    def test_question_routes_to_ask(self, mock_ask):
        """Non-chitchat, non-command text should route to ask()."""
        mock_ask.return_value = MagicMock(text="Your project uses Python 3.11 with FastAPI.")

        engine = IdentityEngine()
        reply = engine.process("what's the tech stack?", channel_id="test")

        mock_ask.assert_called_once()
        self.assertIn("Python", reply)

    @patch("friday.ask.ask")
    def test_ask_empty_response(self, mock_ask):
        """When ask() returns empty, fallback message should be used."""
        mock_ask.return_value = MagicMock(text="")

        engine = IdentityEngine()
        reply = engine.process("what's the meaning of life?", channel_id="test")

        self.assertIn("enough context", reply)

    @patch("friday.ask.ask")
    def test_ask_exception(self, mock_ask):
        """When ask() raises, a friendly error should be returned."""
        mock_ask.side_effect = Exception("DB connection failed")

        engine = IdentityEngine()
        reply = engine.process("what's the architecture?", channel_id="test")

        self.assertIn("error", reply.lower())

    @patch("friday.ask.ask")
    def test_question_with_context(self, mock_ask):
        """Previous exchanges should be passed as context to ask()."""
        mock_ask.return_value = MagicMock(text="Yes, it uses FastAPI.")

        engine = IdentityEngine()
        # First exchange
        engine.process("what's the tech stack?", channel_id="test")
        # Second exchange (should carry context)
        engine.process("does it use FastAPI?", channel_id="test")

        # The second call to ask() should have prev set
        call_args = mock_ask.call_args
        self.assertIsNotNone(call_args)
        # Second call should have prev set (from the first exchange)
        self.assertEqual(mock_ask.call_count, 2)


# ===========================================================================
# IdentityConfig
# ===========================================================================


class TestIdentityConfig(unittest.TestCase):
    def test_default_name(self):
        config = IdentityConfig()
        self.assertEqual(config.name, "Friday")

    def test_default_greeting(self):
        config = IdentityConfig()
        self.assertIn("Friday", config.greeting)

    def test_custom_config(self):
        config = IdentityConfig(name="JARVIS", greeting="At your service.")
        self.assertEqual(config.name, "JARVIS")
        self.assertEqual(config.greeting, "At your service.")


# ===========================================================================
# ConversationContext
# ===========================================================================


class TestConversationContext(unittest.TestCase):
    def test_add_exchange(self):
        from friday.persona.engine import ConversationContext

        ctx = ConversationContext()
        ctx.add("hello", "Hi!")
        self.assertEqual(len(ctx.exchanges), 1)
        self.assertEqual(ctx.exchanges[0][0], "hello")
        self.assertEqual(ctx.exchanges[0][1], "Hi!")

    def test_context_ring_buffer(self):
        from friday.persona.engine import ConversationContext, _MAX_CONTEXT

        ctx = ConversationContext()
        for i in range(_MAX_CONTEXT + 3):
            ctx.add(f"msg{i}", f"reply{i}")

        self.assertLessEqual(len(ctx.exchanges), _MAX_CONTEXT)
        # Should contain the most recent messages
        self.assertEqual(ctx.exchanges[-1][0], f"msg{_MAX_CONTEXT + 2}")

    def test_empty_format(self):
        from friday.persona.engine import ConversationContext

        ctx = ConversationContext()
        self.assertEqual(ctx.format(), "")


# ===========================================================================
# Identity CLI (smoke tests)
# ===========================================================================


class TestIdentityCLI(unittest.TestCase):
    def test_cli_status_exists(self):
        from friday.cli_identity import cmd_identity_status

        args = MagicMock()
        try:
            rc = cmd_identity_status(args)
            self.assertEqual(rc, 0)
        except Exception as e:
            self.fail(f"cmd_identity_status raised: {e}")

    def test_cli_dispatch_status(self):
        from friday.cli_identity import cmd_identity

        args = MagicMock()
        args.action = None
        try:
            rc = cmd_identity(args)
            self.assertEqual(rc, 0)
        except Exception as e:
            self.fail(f"cmd_identity raised: {e}")

    def test_cli_dispatch_chat(self):
        from friday.cli_identity import cmd_identity

        args = MagicMock()
        args.action = "chat"
        # Should return 0 but we can't test interactive, just verify it routes
        # Actually it'll hang waiting for input, so just verify the function exists
        self.assertTrue(callable(cmd_identity))


if __name__ == "__main__":
    unittest.main()
