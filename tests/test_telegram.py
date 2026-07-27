"""Tests for the Telegram communication layer (Pillar C — Communication)."""

from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

from friday.services.telegram import TelegramConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task(payload: str = "") -> MagicMock:
    task = MagicMock()
    task.runtime_payload = payload
    task.task_id = "test-telegram"
    return task


# ===========================================================================
# TelegramConfig
# ===========================================================================


class TestTelegramConfig(unittest.TestCase):
    def setUp(self):
        self._saved = {}
        for key in ("FRIDAY_TELEGRAM_BOT_TOKEN",):
            self._saved[key] = os.environ.get(key)
            os.environ.pop(key, None)

    def tearDown(self):
        for key, val in self._saved.items():
            if val is not None:
                os.environ[key] = val
            else:
                os.environ.pop(key, None)

    def test_defaults_when_no_env(self):
        config = TelegramConfig.from_env()
        self.assertEqual(config.bot_token, "")
        self.assertFalse(config.configured)

    def test_reads_token(self):
        os.environ["FRIDAY_TELEGRAM_BOT_TOKEN"] = "123456:ABC-DEF1234"
        config = TelegramConfig.from_env()
        self.assertEqual(config.bot_token, "123456:ABC-DEF1234")
        self.assertTrue(config.configured)

    def test_not_configured(self):
        config = TelegramConfig.from_env()
        self.assertFalse(config.configured)
        self.assertIn("NOT CONFIGURED", str(config))

    def test_str_configured(self):
        os.environ["FRIDAY_TELEGRAM_BOT_TOKEN"] = "token_123"
        s = str(TelegramConfig.from_env())
        self.assertIn("configured", s.lower())
        self.assertNotIn("NOT CONFIGURED", s)

    def test_api_url(self):
        os.environ["FRIDAY_TELEGRAM_BOT_TOKEN"] = "token:abc"
        config = TelegramConfig.from_env()
        self.assertEqual(config.api_url, "https://api.telegram.org/bottoken:abc")


# ===========================================================================
# TelegramObserver
# ===========================================================================


class TestTelegramObserver(unittest.TestCase):
    def setUp(self):
        self._saved = {}
        for key in ("FRIDAY_TELEGRAM_BOT_TOKEN",):
            self._saved[key] = os.environ.get(key)
            os.environ.pop(key, None)

    def tearDown(self):
        for key, val in self._saved.items():
            if val is not None:
                os.environ[key] = val
            else:
                os.environ.pop(key, None)

    def test_health_not_configured(self):
        from friday.services.telegram import TelegramObserver

        obs = TelegramObserver()
        h = obs.health(None)
        self.assertTrue(h.healthy)
        self.assertIn("not configured", h.detail.lower())

    @patch("friday.services.telegram._get_me")
    def test_health_connected(self, mock_me):
        from friday.services.telegram import TelegramObserver

        mock_me.return_value = {"username": "FridayBot", "first_name": "Friday"}
        os.environ["FRIDAY_TELEGRAM_BOT_TOKEN"] = "test-token"

        obs = TelegramObserver()
        h = obs.health(None)
        self.assertTrue(h.healthy)
        self.assertIn("connected", h.detail.lower())

    @patch("friday.services.telegram._get_me")
    def test_health_api_down(self, mock_me):
        from friday.services.telegram import TelegramObserver

        mock_me.return_value = None
        os.environ["FRIDAY_TELEGRAM_BOT_TOKEN"] = "test-token"

        obs = TelegramObserver()
        h = obs.health(None)
        self.assertFalse(h.healthy)
        self.assertIn("could not connect", h.detail.lower())

    def test_collect_not_configured(self):
        from friday.services.telegram import TelegramObserver

        obs = TelegramObserver()
        collected = obs.collect(None)
        self.assertEqual(collected, [])

    @patch("friday.services.telegram._get_updates")
    def test_collect_with_messages(self, mock_updates):
        from friday.services.telegram import TelegramObserver

        mock_updates.return_value = [
            {
                "update_id": 1001,
                "message_id": "42",
                "text": "Hello Friday!",
                "from_user": "testuser",
                "chat_id": "123",
                "chat_title": "Test Chat",
                "date": "1700000000",
            },
            {
                "update_id": 1002,
                "message_id": "43",
                "text": "How are you?",
                "from_user": "testuser",
                "chat_id": "123",
                "chat_title": "Test Chat",
                "date": "1700000001",
            },
        ]

        os.environ["FRIDAY_TELEGRAM_BOT_TOKEN"] = "test-token"
        obs = TelegramObserver()
        observations = obs.collect(None)

        # unread_count + 2 messages * (text + from + chat_id) = 7
        self.assertGreaterEqual(len(observations), 7)

        # Check unread count.
        count_obs = [o for o in observations if o.aspect == "unread_count"]
        self.assertEqual(len(count_obs), 1)
        self.assertEqual(count_obs[0].value, "2")

        # Check message text.
        text_obs = [o for o in observations if o.aspect == "text"]
        self.assertTrue(any("Hello Friday!" in o.value for o in text_obs))
        self.assertTrue(any("How are you?" in o.value for o in text_obs))

        # Check that tracking offset was updated (max update_id + 1)
        self.assertEqual(obs._offset, 1003)

    @patch("friday.services.telegram._get_updates")
    def test_collect_offsets_tracking(self, mock_updates):
        from friday.services.telegram import TelegramObserver

        # First call returns updates.
        mock_updates.return_value = [
            {"update_id": 500, "message_id": "1", "text": "Hi",
             "from_user": "u", "chat_id": "c", "chat_title": "", "date": ""},
        ]

        os.environ["FRIDAY_TELEGRAM_BOT_TOKEN"] = "test-token"
        obs = TelegramObserver()
        obs.collect(None)

        # Second call should use offset=501.
        mock_updates.assert_called_with(obs.config, offset=None)  # first call
        self.assertEqual(obs._offset, 501)

    def test_summarize_not_configured(self):
        from friday.services.telegram import TelegramObserver

        obs = TelegramObserver()
        s = obs.summarize(None)
        self.assertIn("not configured", s.lower())

    @patch("friday.services.telegram._get_me")
    def test_summarize_configured(self, mock_me):
        from friday.services.telegram import TelegramObserver

        mock_me.return_value = {"username": "FridayBot", "first_name": "Friday"}
        os.environ["FRIDAY_TELEGRAM_BOT_TOKEN"] = "test-token"

        obs = TelegramObserver()
        s = obs.summarize(None)
        self.assertIn("Telegram", s)
        self.assertIn("FridayBot", s)


# ===========================================================================
# TelegramExecutor
# ===========================================================================


class TestTelegramExecutor(unittest.TestCase):
    def setUp(self):
        self._saved = {}
        for key in ("FRIDAY_TELEGRAM_BOT_TOKEN",):
            self._saved[key] = os.environ.get(key)
            os.environ.pop(key, None)

    def tearDown(self):
        for key, val in self._saved.items():
            if val is not None:
                os.environ[key] = val
            else:
                os.environ.pop(key, None)

    def test_empty_payload(self):
        from friday.services.telegram import TelegramExecutor

        executor = TelegramExecutor()
        result = executor.execute(_make_task(""))

        self.assertFalse(result.success)
        self.assertIn("empty", result.error.lower())

    def test_invalid_json(self):
        from friday.services.telegram import TelegramExecutor

        executor = TelegramExecutor()
        result = executor.execute(_make_task("not json"))

        self.assertFalse(result.success)
        self.assertIn("JSON", result.error)

    def test_missing_chat_id(self):
        from friday.services.telegram import TelegramExecutor

        executor = TelegramExecutor()
        result = executor.execute(_make_task('{"text": "hello"}'))

        self.assertFalse(result.success)
        self.assertIn("required", result.error.lower())

    def test_missing_text(self):
        from friday.services.telegram import TelegramExecutor

        executor = TelegramExecutor()
        result = executor.execute(_make_task('{"chat_id": "123"}'))

        self.assertFalse(result.success)
        self.assertIn("required", result.error.lower())

    def test_not_configured(self):
        from friday.services.telegram import TelegramExecutor

        executor = TelegramExecutor()
        result = executor.execute(
            _make_task('{"chat_id": "123", "text": "Hello!"}')
        )

        self.assertFalse(result.success)
        self.assertTrue(
            "not configured" in result.error.lower()
            or "telegram_bot_token" in result.error.lower()
        )

    @patch("friday.services.telegram._send_message")
    def test_send_success(self, mock_send):
        from friday.services.telegram import TelegramExecutor

        mock_send.return_value = (True, "")
        os.environ["FRIDAY_TELEGRAM_BOT_TOKEN"] = "test-token"

        executor = TelegramExecutor()
        result = executor.execute(
            _make_task('{"chat_id": "123", "text": "Hello!"}')
        )

        self.assertTrue(result.success)
        self.assertIn("sent", result.stdout.lower())
        mock_send.assert_called_once()

    @patch("friday.services.telegram._send_message")
    def test_send_failure(self, mock_send):
        from friday.services.telegram import TelegramExecutor

        mock_send.return_value = (False, "chat not found")
        os.environ["FRIDAY_TELEGRAM_BOT_TOKEN"] = "test-token"

        executor = TelegramExecutor()
        result = executor.execute(
            _make_task('{"chat_id": "123", "text": "Hello!"}')
        )

        self.assertFalse(result.success)
        self.assertIn("failed", result.error.lower())
        mock_send.assert_called_once()

    def test_verify(self):
        from friday.services.telegram import TelegramExecutor

        executor = TelegramExecutor()
        result = MagicMock()
        result.success = True
        v = executor.verify(None, result)
        self.assertTrue(v.passed)

        result.success = False
        v = executor.verify(None, result)
        self.assertFalse(v.passed)


# ===========================================================================
# get_bot_info / send_message helpers
# ===========================================================================


class TestTelegramHelpers(unittest.TestCase):
    def setUp(self):
        self._saved = {}
        for key in ("FRIDAY_TELEGRAM_BOT_TOKEN",):
            self._saved[key] = os.environ.get(key)
            os.environ.pop(key, None)

    def tearDown(self):
        for key, val in self._saved.items():
            if val is not None:
                os.environ[key] = val
            else:
                os.environ.pop(key, None)

    def test_get_bot_info_not_configured(self):
        from friday.services.telegram import get_bot_info

        result = get_bot_info()
        self.assertIsNone(result)

    def test_send_message_not_configured(self):
        from friday.services.telegram import send_message

        ok, err = send_message("123", "Hello")
        self.assertFalse(ok)
        self.assertIn("not configured", err.lower())

    @patch("friday.services.telegram._get_me")
    def test_get_bot_info_configured(self, mock_me):
        from friday.services.telegram import get_bot_info

        mock_me.return_value = {"username": "FridayBot"}
        os.environ["FRIDAY_TELEGRAM_BOT_TOKEN"] = "test-token"

        result = get_bot_info()
        self.assertEqual(result["username"], "FridayBot")

    @patch("friday.services.telegram._send_message")
    def test_send_message_configured(self, mock_send):
        from friday.services.telegram import send_message

        mock_send.return_value = (True, "")
        os.environ["FRIDAY_TELEGRAM_BOT_TOKEN"] = "test-token"

        ok, err = send_message("123", "Hello!")
        self.assertTrue(ok)
        self.assertEqual(err, "")


# ===========================================================================
# CLI commands (smoke tests)
# ===========================================================================


class TestTelegramCLI(unittest.TestCase):
    def test_cli_config_exists(self):
        from friday.cli_telegram import cmd_telegram_config

        args = MagicMock()
        try:
            rc = cmd_telegram_config(args)
            self.assertEqual(rc, 0)
        except Exception as e:
            self.fail(f"cmd_telegram_config raised: {e}")

    def test_cli_me_exists(self):
        from friday.cli_telegram import cmd_telegram_me

        args = MagicMock()
        try:
            rc = cmd_telegram_me(args)
            self.assertEqual(rc, 0)
        except Exception as e:
            self.fail(f"cmd_telegram_me raised: {e}")

    def test_cli_send_no_chat(self):
        from friday.cli_telegram import cmd_telegram_send

        args = MagicMock()
        args.chat_id = ""
        args.text = "hello"
        rc = cmd_telegram_send(args)
        self.assertEqual(rc, 2)

    def test_cli_send_no_text(self):
        from friday.cli_telegram import cmd_telegram_send

        args = MagicMock()
        args.chat_id = "123"
        args.text = ""
        rc = cmd_telegram_send(args)
        self.assertEqual(rc, 2)

    def test_cli_dispatch_config(self):
        from friday.cli_telegram import cmd_telegram

        args = MagicMock()
        args.action = "config"
        try:
            rc = cmd_telegram(args)
            self.assertEqual(rc, 0)
        except Exception as e:
            self.fail(f"cmd_telegram(config) raised: {e}")

    def test_cli_dispatch_unknown(self):
        from friday.cli_telegram import cmd_telegram

        args = MagicMock()
        args.action = "unknown"
        rc = cmd_telegram(args)
        self.assertEqual(rc, 2)

    def test_cli_send_text_as_list(self):
        from friday.cli_telegram import cmd_telegram_send

        args = MagicMock()
        args.chat_id = "123"
        args.text = ["Hello", "from", "Telegram"]
        rc = cmd_telegram_send(args)
        self.assertEqual(rc, 1)  # fails because not configured, but nargs join works


if __name__ == "__main__":
    unittest.main()
