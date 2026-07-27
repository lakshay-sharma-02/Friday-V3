"""Tests for the Slack communication layer (Pillar C — Communication)."""

from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

from friday.services.slack import SlackConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task(payload: str = "") -> MagicMock:
    task = MagicMock()
    task.runtime_payload = payload
    task.task_id = "test-slack"
    return task


# ===========================================================================
# SlackConfig
# ===========================================================================


class TestSlackConfig(unittest.TestCase):
    def setUp(self):
        self._saved = {}
        for key in ("FRIDAY_SLACK_BOT_TOKEN", "FRIDAY_SLACK_APP_TOKEN"):
            self._saved[key] = os.environ.get(key)
            os.environ.pop(key, None)

    def tearDown(self):
        for key, val in self._saved.items():
            if val is not None:
                os.environ[key] = val
            else:
                os.environ.pop(key, None)

    def test_defaults_when_no_env(self):
        config = SlackConfig.from_env()
        self.assertEqual(config.bot_token, "")
        self.assertEqual(config.app_token, "")
        self.assertFalse(config.configured)

    def test_reads_token(self):
        os.environ["FRIDAY_SLACK_BOT_TOKEN"] = "xoxb-abc123"
        os.environ["FRIDAY_SLACK_APP_TOKEN"] = "xapp-def456"

        config = SlackConfig.from_env()
        self.assertEqual(config.bot_token, "xoxb-abc123")
        self.assertEqual(config.app_token, "xapp-def456")
        self.assertTrue(config.configured)

    def test_not_configured_without_token(self):
        config = SlackConfig.from_env()
        self.assertFalse(config.configured)
        self.assertIn("NOT CONFIGURED", str(config))

    def test_str_configured(self):
        os.environ["FRIDAY_SLACK_BOT_TOKEN"] = "xoxb-test-token-12345"
        s = str(SlackConfig.from_env())
        self.assertIn("configured", s.lower())
        self.assertNotIn("NOT CONFIGURED", s)


# ===========================================================================
# SlackObserver
# ===========================================================================


class TestSlackObserver(unittest.TestCase):
    def setUp(self):
        self._saved = {}
        for key in ("FRIDAY_SLACK_BOT_TOKEN",):
            self._saved[key] = os.environ.get(key)
            os.environ.pop(key, None)

    def tearDown(self):
        for key, val in self._saved.items():
            if val is not None:
                os.environ[key] = val
            else:
                os.environ.pop(key, None)

    def test_health_not_configured(self):
        from friday.services.slack import SlackObserver

        obs = SlackObserver()
        h = obs.health(None)
        self.assertTrue(h.healthy)
        self.assertIn("not configured", h.detail.lower())

    @patch("friday.services.slack._get_client")
    def test_health_sdk_missing(self, mock_get_client):
        from friday.services.slack import SlackObserver

        mock_get_client.return_value = None
        os.environ["FRIDAY_SLACK_BOT_TOKEN"] = "xoxb-test"
        obs = SlackObserver()
        h = obs.health(None)
        self.assertFalse(h.healthy)
        self.assertIn("sdk", h.detail.lower())

    @patch("friday.services.slack._get_client")
    def test_health_connected(self, mock_get_client):
        from friday.services.slack import SlackObserver

        mock_client = MagicMock()
        mock_client.auth_test.return_value = {
            "ok": True, "team": "TestTeam", "user": "FridayBot"
        }
        mock_get_client.return_value = mock_client

        os.environ["FRIDAY_SLACK_BOT_TOKEN"] = "xoxb-test"
        obs = SlackObserver()
        h = obs.health(None)
        self.assertTrue(h.healthy)
        self.assertIn("connected", h.detail.lower())

    def test_collect_not_configured(self):
        from friday.services.slack import SlackObserver

        obs = SlackObserver()
        collected = obs.collect(None)
        self.assertEqual(collected, [])

    @patch("friday.services.slack._list_channels")
    @patch("friday.services.slack._fetch_channel_messages")
    def test_collect_with_channels(self, mock_fetch_msgs, mock_list_chs):
        from friday.services.slack import SlackObserver

        mock_list_chs.return_value = [
            {"id": "C001", "name": "general", "topic": {"value": "Team chat"},
             "num_members": 42},
            {"id": "C002", "name": "random", "topic": {"value": ""},
             "num_members": 10},
        ]
        mock_fetch_msgs.return_value = [
            {"ts": "123.456", "user": "U001", "text": "Hello!", "channel": "C001"},
        ]

        os.environ["FRIDAY_SLACK_BOT_TOKEN"] = "xoxb-test"
        obs = SlackObserver()
        observations = obs.collect(None)

        # Should have: channel_count + per-channel: name, member_count, topic
        # + per-message: user, channel, text
        self.assertGreaterEqual(len(observations), 8)

        # Check channel count.
        count_obs = [o for o in observations if o.aspect == "channel_count"]
        self.assertEqual(len(count_obs), 1)
        self.assertEqual(count_obs[0].value, "2")

        # Check channel names.
        name_obs = [o for o in observations if o.aspect == "name"]
        self.assertTrue(any("general" in o.value for o in name_obs))
        self.assertTrue(any("random" in o.value for o in name_obs))

        # Check message text.
        text_obs = [o for o in observations if o.aspect == "text"]
        self.assertTrue(any("Hello!" in o.value for o in text_obs))

    def test_summarize_not_configured(self):
        from friday.services.slack import SlackObserver

        obs = SlackObserver()
        s = obs.summarize(None)
        self.assertIn("not configured", s.lower())

    @patch("friday.services.slack._list_channels")
    def test_summarize_configured(self, mock_list_chs):
        from friday.services.slack import SlackObserver

        mock_list_chs.return_value = [
            {"id": "C001", "name": "general"},
            {"id": "C002", "name": "random"},
        ]

        os.environ["FRIDAY_SLACK_BOT_TOKEN"] = "xoxb-test"
        obs = SlackObserver()
        s = obs.summarize(None)
        self.assertIn("Slack", s)
        self.assertIn("general", s)


# ===========================================================================
# SlackExecutor
# ===========================================================================


class TestSlackExecutor(unittest.TestCase):
    def setUp(self):
        self._saved = {}
        for key in ("FRIDAY_SLACK_BOT_TOKEN",):
            self._saved[key] = os.environ.get(key)
            os.environ.pop(key, None)

    def tearDown(self):
        for key, val in self._saved.items():
            if val is not None:
                os.environ[key] = val
            else:
                os.environ.pop(key, None)

    def test_empty_payload(self):
        from friday.services.slack import SlackExecutor

        executor = SlackExecutor()
        result = executor.execute(_make_task(""))

        self.assertFalse(result.success)
        self.assertIn("empty", result.error.lower())

    def test_invalid_json(self):
        from friday.services.slack import SlackExecutor

        executor = SlackExecutor()
        result = executor.execute(_make_task("not json"))

        self.assertFalse(result.success)
        self.assertIn("JSON", result.error)

    def test_missing_channel(self):
        from friday.services.slack import SlackExecutor

        executor = SlackExecutor()
        result = executor.execute(_make_task('{"text": "hello"}'))

        self.assertFalse(result.success)
        self.assertIn("required", result.error.lower())

    def test_missing_text(self):
        from friday.services.slack import SlackExecutor

        executor = SlackExecutor()
        result = executor.execute(_make_task('{"channel": "#general"}'))

        self.assertFalse(result.success)
        self.assertIn("required", result.error.lower())

    def test_not_configured(self):
        from friday.services.slack import SlackExecutor

        executor = SlackExecutor()
        result = executor.execute(
            _make_task('{"channel": "#general", "text": "Hello!"}')
        )

        self.assertFalse(result.success)
        self.assertTrue(
            "not configured" in result.error.lower()
            or "slack_bot_token" in result.error.lower()
        )

    @patch("friday.services.slack._post_message")
    def test_send_success(self, mock_post):
        from friday.services.slack import SlackExecutor

        mock_post.return_value = (True, "")
        os.environ["FRIDAY_SLACK_BOT_TOKEN"] = "xoxb-test"

        executor = SlackExecutor()
        result = executor.execute(
            _make_task('{"channel": "#general", "text": "Hello!"}')
        )

        self.assertTrue(result.success)
        self.assertIn("posted", result.stdout.lower())
        mock_post.assert_called_once()

    @patch("friday.services.slack._post_message")
    def test_send_failure(self, mock_post):
        from friday.services.slack import SlackExecutor

        mock_post.return_value = (False, "not_in_channel")
        os.environ["FRIDAY_SLACK_BOT_TOKEN"] = "xoxb-test"

        executor = SlackExecutor()
        result = executor.execute(
            _make_task('{"channel": "#general", "text": "Hello!"}')
        )

        self.assertFalse(result.success)
        self.assertIn("failed", result.error.lower())
        mock_post.assert_called_once()

    def test_verify(self):
        from friday.services.slack import SlackExecutor

        executor = SlackExecutor()
        result = MagicMock()
        result.success = True
        v = executor.verify(None, result)
        self.assertTrue(v.passed)

        result.success = False
        v = executor.verify(None, result)
        self.assertFalse(v.passed)


# ===========================================================================
# list_channels / post_message helpers
# ===========================================================================


class TestSlackHelpers(unittest.TestCase):
    def setUp(self):
        self._saved = {}
        for key in ("FRIDAY_SLACK_BOT_TOKEN",):
            self._saved[key] = os.environ.get(key)
            os.environ.pop(key, None)

    def tearDown(self):
        for key, val in self._saved.items():
            if val is not None:
                os.environ[key] = val
            else:
                os.environ.pop(key, None)

    def test_list_channels_not_configured(self):
        from friday.services.slack import list_channels

        result = list_channels(limit=5)
        self.assertEqual(result, [])

    def test_post_message_not_configured(self):
        from friday.services.slack import post_message

        ok, err = post_message("#general", "Hello")
        self.assertFalse(ok)
        self.assertIn("not configured", err.lower())

    @patch("friday.services.slack._list_channels")
    def test_list_channels_configured(self, mock_list):
        from friday.services.slack import list_channels

        mock_list.return_value = [
            {"id": "C001", "name": "general", "num_members": 42}
        ]
        os.environ["FRIDAY_SLACK_BOT_TOKEN"] = "xoxb-test"

        result = list_channels(limit=5)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], "C001")
        mock_list.assert_called_once()

    @patch("friday.services.slack._post_message")
    def test_post_message_configured(self, mock_post):
        from friday.services.slack import post_message

        mock_post.return_value = (True, "")
        os.environ["FRIDAY_SLACK_BOT_TOKEN"] = "xoxb-test"

        ok, err = post_message("#general", "Hello!")
        self.assertTrue(ok)
        self.assertEqual(err, "")
        mock_post.assert_called_once()


# ===========================================================================
# CLI commands (smoke tests)
# ===========================================================================


class TestSlackCLI(unittest.TestCase):
    def test_cli_config_exists(self):
        from friday.cli_slack import cmd_slack_config

        args = MagicMock()
        try:
            rc = cmd_slack_config(args)
            self.assertEqual(rc, 0)
        except Exception as e:
            self.fail(f"cmd_slack_config raised: {e}")

    def test_cli_channels_exists(self):
        from friday.cli_slack import cmd_slack_channels

        args = MagicMock()
        args.limit = 5
        try:
            rc = cmd_slack_channels(args)
            self.assertEqual(rc, 0)
        except Exception as e:
            self.fail(f"cmd_slack_channels raised: {e}")

    def test_cli_setup_exists(self):
        from friday.cli_slack import cmd_slack_setup

        args = MagicMock()
        try:
            rc = cmd_slack_setup(args)
            self.assertEqual(rc, 0)
        except Exception as e:
            self.fail(f"cmd_slack_setup raised: {e}")

    def test_cli_send_no_channel(self):
        from friday.cli_slack import cmd_slack_send

        args = MagicMock()
        args.channel = ""
        args.text = "hello"
        rc = cmd_slack_send(args)
        self.assertEqual(rc, 2)

    def test_cli_send_no_text(self):
        from friday.cli_slack import cmd_slack_send

        args = MagicMock()
        args.channel = "#general"
        args.text = ""
        rc = cmd_slack_send(args)
        self.assertEqual(rc, 2)

    def test_cli_dispatch_config(self):
        from friday.cli_slack import cmd_slack

        args = MagicMock()
        args.action = "config"
        try:
            rc = cmd_slack(args)
            self.assertEqual(rc, 0)
        except Exception as e:
            self.fail(f"cmd_slack(config) raised: {e}")

    def test_cli_dispatch_unknown(self):
        from friday.cli_slack import cmd_slack

        args = MagicMock()
        args.action = "unknown"
        rc = cmd_slack(args)
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
