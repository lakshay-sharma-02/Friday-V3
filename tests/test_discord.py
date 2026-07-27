"""Tests for the Discord communication layer (Pillar C — Communication)."""

from __future__ import annotations

import json
import os
import unittest
from unittest.mock import MagicMock, patch

from friday.services.discord import DiscordConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task(payload: str = "") -> MagicMock:
    task = MagicMock()
    task.runtime_payload = payload
    task.task_id = "test-discord"
    return task


def _mock_response(data, status=200):
    """Create a mock urllib response."""
    mock = MagicMock()
    mock.read.return_value = json.dumps(data).encode("utf-8")
    mock.__enter__.return_value = mock
    mock.__exit__.return_value = None
    return mock


# ===========================================================================
# DiscordConfig
# ===========================================================================


class TestDiscordConfig(unittest.TestCase):
    def setUp(self):
        self._saved = {}
        for key in ("FRIDAY_DISCORD_BOT_TOKEN",):
            self._saved[key] = os.environ.get(key)
            os.environ.pop(key, None)

    def tearDown(self):
        for key, val in self._saved.items():
            if val is not None:
                os.environ[key] = val
            else:
                os.environ.pop(key, None)

    def test_defaults_when_no_env(self):
        config = DiscordConfig.from_env()
        self.assertEqual(config.bot_token, "")
        self.assertFalse(config.configured)

    def test_reads_token(self):
        os.environ["FRIDAY_DISCORD_BOT_TOKEN"] = "discord_bot_token_abc"
        config = DiscordConfig.from_env()
        self.assertEqual(config.bot_token, "discord_bot_token_abc")
        self.assertTrue(config.configured)

    def test_not_configured(self):
        config = DiscordConfig.from_env()
        self.assertFalse(config.configured)
        self.assertIn("NOT CONFIGURED", str(config))

    def test_str_configured(self):
        os.environ["FRIDAY_DISCORD_BOT_TOKEN"] = "token_12345"
        s = str(DiscordConfig.from_env())
        self.assertIn("configured", s.lower())
        self.assertNotIn("NOT CONFIGURED", s)


# ===========================================================================
# DiscordObserver
# ===========================================================================


class TestDiscordObserver(unittest.TestCase):
    def setUp(self):
        self._saved = {}
        for key in ("FRIDAY_DISCORD_BOT_TOKEN",):
            self._saved[key] = os.environ.get(key)
            os.environ.pop(key, None)

    def tearDown(self):
        for key, val in self._saved.items():
            if val is not None:
                os.environ[key] = val
            else:
                os.environ.pop(key, None)

    def test_health_not_configured(self):
        from friday.services.discord import DiscordObserver

        obs = DiscordObserver()
        h = obs.health(None)
        self.assertTrue(h.healthy)
        self.assertIn("not configured", h.detail.lower())

    @patch("friday.services.discord._list_guilds")
    def test_health_connected(self, mock_guilds):
        from friday.services.discord import DiscordObserver

        mock_guilds.return_value = [{"id": "123", "name": "TestServer"}]
        os.environ["FRIDAY_DISCORD_BOT_TOKEN"] = "test-token"

        obs = DiscordObserver()
        h = obs.health(None)
        self.assertTrue(h.healthy)
        self.assertIn("connected", h.detail.lower())

    def test_collect_not_configured(self):
        from friday.services.discord import DiscordObserver

        obs = DiscordObserver()
        collected = obs.collect(None)
        self.assertEqual(collected, [])

    @patch("friday.services.discord._list_guilds")
    @patch("friday.services.discord._list_channels")
    @patch("friday.services.discord._fetch_messages")
    def test_collect_with_data(self, mock_msgs, mock_channels, mock_guilds):
        from friday.services.discord import DiscordObserver

        mock_guilds.return_value = [
            {"id": "g001", "name": "My Server"},
        ]
        mock_channels.return_value = [
            {"id": "c001", "name": "general", "type": 0},
        ]
        mock_msgs.return_value = [
            {"id": "m001", "author": "Alice", "content": "Hello!", "channel_id": "c001", "timestamp": "2026-01-01T00:00:00"},
        ]

        os.environ["FRIDAY_DISCORD_BOT_TOKEN"] = "test-token"
        obs = DiscordObserver()
        observations = obs.collect(None)

        # Should have: guild_count + guild name + channel name + channel guild + message x3
        self.assertGreaterEqual(len(observations), 6)

        # Check guild count.
        count_obs = [o for o in observations if o.aspect == "guild_count"]
        self.assertEqual(len(count_obs), 1)
        self.assertEqual(count_obs[0].value, "1")

        # Check guild name.
        name_obs = [o for o in observations if o.aspect == "name"]
        self.assertTrue(any("My Server" in o.value for o in name_obs))
        self.assertTrue(any("general" in o.value for o in name_obs))

        # Check message content.
        content_obs = [o for o in observations if o.aspect == "content"]
        self.assertTrue(any("Hello!" in o.value for o in content_obs))
        self.assertTrue(any("Alice" in o.value for o in [o for o in observations if o.aspect == "author"]))

    def test_summarize_not_configured(self):
        from friday.services.discord import DiscordObserver

        obs = DiscordObserver()
        s = obs.summarize(None)
        self.assertIn("not configured", s.lower())

    @patch("friday.services.discord._list_guilds")
    def test_summarize_configured(self, mock_guilds):
        from friday.services.discord import DiscordObserver

        mock_guilds.return_value = [
            {"id": "g001", "name": "My Server"},
            {"id": "g002", "name": "Other Server"},
        ]
        os.environ["FRIDAY_DISCORD_BOT_TOKEN"] = "test-token"

        obs = DiscordObserver()
        s = obs.summarize(None)
        self.assertIn("Discord", s)
        self.assertIn("My Server", s)
        self.assertIn("Other Server", s)


# ===========================================================================
# DiscordExecutor
# ===========================================================================


class TestDiscordExecutor(unittest.TestCase):
    def setUp(self):
        self._saved = {}
        for key in ("FRIDAY_DISCORD_BOT_TOKEN",):
            self._saved[key] = os.environ.get(key)
            os.environ.pop(key, None)

    def tearDown(self):
        for key, val in self._saved.items():
            if val is not None:
                os.environ[key] = val
            else:
                os.environ.pop(key, None)

    def test_empty_payload(self):
        from friday.services.discord import DiscordExecutor

        executor = DiscordExecutor()
        result = executor.execute(_make_task(""))

        self.assertFalse(result.success)
        self.assertIn("empty", result.error.lower())

    def test_invalid_json(self):
        from friday.services.discord import DiscordExecutor

        executor = DiscordExecutor()
        result = executor.execute(_make_task("not json"))

        self.assertFalse(result.success)
        self.assertIn("JSON", result.error)

    def test_missing_channel(self):
        from friday.services.discord import DiscordExecutor

        executor = DiscordExecutor()
        result = executor.execute(_make_task('{"content": "hello"}'))

        self.assertFalse(result.success)
        self.assertIn("required", result.error.lower())

    def test_missing_content(self):
        from friday.services.discord import DiscordExecutor

        executor = DiscordExecutor()
        result = executor.execute(_make_task('{"channel": "123"}'))

        self.assertFalse(result.success)
        self.assertIn("required", result.error.lower())

    def test_not_configured(self):
        from friday.services.discord import DiscordExecutor

        executor = DiscordExecutor()
        result = executor.execute(
            _make_task('{"channel": "123", "content": "Hello!"}')
        )

        self.assertFalse(result.success)
        self.assertTrue(
            "not configured" in result.error.lower()
            or "discord_bot_token" in result.error.lower()
        )

    @patch("friday.services.discord._post_message")
    def test_send_success(self, mock_post):
        from friday.services.discord import DiscordExecutor

        mock_post.return_value = (True, "")
        os.environ["FRIDAY_DISCORD_BOT_TOKEN"] = "test-token"

        executor = DiscordExecutor()
        result = executor.execute(
            _make_task('{"channel": "123", "content": "Hello!"}')
        )

        self.assertTrue(result.success)
        self.assertIn("posted", result.stdout.lower())
        mock_post.assert_called_once()

    @patch("friday.services.discord._post_message")
    def test_send_failure(self, mock_post):
        from friday.services.discord import DiscordExecutor

        mock_post.return_value = (False, "missing_access")
        os.environ["FRIDAY_DISCORD_BOT_TOKEN"] = "test-token"

        executor = DiscordExecutor()
        result = executor.execute(
            _make_task('{"channel": "123", "content": "Hello!"}')
        )

        self.assertFalse(result.success)
        self.assertIn("failed", result.error.lower())
        mock_post.assert_called_once()

    def test_verify(self):
        from friday.services.discord import DiscordExecutor

        executor = DiscordExecutor()
        result = MagicMock()
        result.success = True
        v = executor.verify(None, result)
        self.assertTrue(v.passed)

        result.success = False
        v = executor.verify(None, result)
        self.assertFalse(v.passed)


# ===========================================================================
# list_guilds / post_message helpers
# ===========================================================================


class TestDiscordHelpers(unittest.TestCase):
    def setUp(self):
        self._saved = {}
        for key in ("FRIDAY_DISCORD_BOT_TOKEN",):
            self._saved[key] = os.environ.get(key)
            os.environ.pop(key, None)

    def tearDown(self):
        for key, val in self._saved.items():
            if val is not None:
                os.environ[key] = val
            else:
                os.environ.pop(key, None)

    def test_list_guilds_not_configured(self):
        from friday.services.discord import list_guilds

        result = list_guilds(limit=5)
        self.assertEqual(result, [])

    def test_list_channels_not_configured(self):
        from friday.services.discord import list_channels_for_guild

        result = list_channels_for_guild("123")
        self.assertEqual(result, [])

    def test_post_message_not_configured(self):
        from friday.services.discord import post_message

        ok, err = post_message("123", "Hello")
        self.assertFalse(ok)
        self.assertIn("not configured", err.lower())

    @patch("friday.services.discord._api_get")
    def test_list_guilds_configured(self, mock_get):
        from friday.services.discord import list_guilds

        mock_get.return_value = [{"id": "g001", "name": "Test"}]
        os.environ["FRIDAY_DISCORD_BOT_TOKEN"] = "test-token"

        result = list_guilds(limit=5)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], "g001")

    @patch("friday.services.discord._api_post")
    def test_post_message_configured(self, mock_post):
        from friday.services.discord import post_message

        mock_post.return_value = {"id": "msg001"}
        os.environ["FRIDAY_DISCORD_BOT_TOKEN"] = "test-token"

        ok, err = post_message("123", "Hello!")
        self.assertTrue(ok)
        self.assertEqual(err, "")


# ===========================================================================
# CLI commands (smoke tests)
# ===========================================================================


class TestDiscordCLI(unittest.TestCase):
    def test_cli_config_exists(self):
        from friday.cli_discord import cmd_discord_config

        args = MagicMock()
        try:
            rc = cmd_discord_config(args)
            self.assertEqual(rc, 0)
        except Exception as e:
            self.fail(f"cmd_discord_config raised: {e}")

    def test_cli_guilds_exists(self):
        from friday.cli_discord import cmd_discord_guilds

        args = MagicMock()
        try:
            rc = cmd_discord_guilds(args)
            self.assertEqual(rc, 0)
        except Exception as e:
            self.fail(f"cmd_discord_guilds raised: {e}")

    def test_cli_send_no_channel(self):
        from friday.cli_discord import cmd_discord_send

        args = MagicMock()
        args.channel = ""
        args.content = "hello"
        rc = cmd_discord_send(args)
        self.assertEqual(rc, 2)

    def test_cli_send_no_content(self):
        from friday.cli_discord import cmd_discord_send

        args = MagicMock()
        args.channel = "123"
        args.content = ""
        rc = cmd_discord_send(args)
        self.assertEqual(rc, 2)

    def test_cli_dispatch_config(self):
        from friday.cli_discord import cmd_discord

        args = MagicMock()
        args.action = "config"
        try:
            rc = cmd_discord(args)
            self.assertEqual(rc, 0)
        except Exception as e:
            self.fail(f"cmd_discord(config) raised: {e}")

    def test_cli_dispatch_unknown(self):
        from friday.cli_discord import cmd_discord

        args = MagicMock()
        args.action = "unknown"
        rc = cmd_discord(args)
        self.assertEqual(rc, 2)

    def test_cli_channels_no_guild(self):
        from friday.cli_discord import cmd_discord_channels

        args = MagicMock()
        args.guild_id = ""
        rc = cmd_discord_channels(args)
        self.assertEqual(rc, 2)

    def test_cli_send_content_as_list(self):
        from friday.cli_discord import cmd_discord_send

        args = MagicMock()
        args.channel = "123"
        args.content = ["Hello", "from", "Discord"]
        rc = cmd_discord_send(args)
        self.assertEqual(rc, 1)  # fails because not configured, but the nargs join works


if __name__ == "__main__":
    unittest.main()
