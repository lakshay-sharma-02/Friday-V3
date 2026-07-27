"""Tests for the email communication layer (Pillar C — Communication)."""
from __future__ import annotations

import json
import os
import unittest
from unittest.mock import MagicMock, patch

from friday.services.email import EmailConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task(payload: str = "") -> MagicMock:
    task = MagicMock()
    task.runtime_payload = payload
    task.task_id = "test-email"
    return task


# ===========================================================================
# EmailConfig
# ===========================================================================


class TestEmailConfig(unittest.TestCase):
    def setUp(self):
        # Save original env and clear.
        self._saved = {}
        for key in (
            "FRIDAY_EMAIL_IMAP_SERVER",
            "FRIDAY_EMAIL_IMAP_PORT",
            "FRIDAY_EMAIL_SMTP_SERVER",
            "FRIDAY_EMAIL_SMTP_PORT",
            "FRIDAY_EMAIL_USERNAME",
            "FRIDAY_EMAIL_PASSWORD",
            "FRIDAY_EMAIL_FROM",
        ):
            self._saved[key] = os.environ.get(key)
            os.environ.pop(key, None)

    def tearDown(self):
        for key, val in self._saved.items():
            if val is not None:
                os.environ[key] = val
            else:
                os.environ.pop(key, None)

    def test_defaults_when_no_env(self):
        config = EmailConfig.from_env()
        self.assertEqual(config.imap_server, "imap.gmail.com")
        self.assertEqual(config.imap_port, 993)
        self.assertEqual(config.smtp_server, "smtp.gmail.com")
        self.assertEqual(config.smtp_port, 587)
        self.assertEqual(config.username, "")
        self.assertEqual(config.password, "")
        self.assertFalse(config.configured)

    def test_reads_from_env(self):
        os.environ["FRIDAY_EMAIL_USERNAME"] = "test@example.com"
        os.environ["FRIDAY_EMAIL_PASSWORD"] = "secret"
        os.environ["FRIDAY_EMAIL_IMAP_SERVER"] = "custom.imap.com"
        os.environ["FRIDAY_EMAIL_IMAP_PORT"] = "143"
        os.environ["FRIDAY_EMAIL_SMTP_SERVER"] = "custom.smtp.com"
        os.environ["FRIDAY_EMAIL_SMTP_PORT"] = "25"
        os.environ["FRIDAY_EMAIL_FROM"] = "Friday Bot"

        config = EmailConfig.from_env()
        self.assertEqual(config.username, "test@example.com")
        self.assertEqual(config.password, "secret")
        self.assertEqual(config.imap_server, "custom.imap.com")
        self.assertEqual(config.imap_port, 143)
        self.assertEqual(config.smtp_server, "custom.smtp.com")
        self.assertEqual(config.smtp_port, 25)
        self.assertEqual(config.from_addr, "Friday Bot")
        self.assertTrue(config.configured)

    def test_not_configured_without_password(self):
        os.environ["FRIDAY_EMAIL_USERNAME"] = "test@example.com"
        config = EmailConfig.from_env()
        self.assertFalse(config.configured)
        self.assertIn("NOT CONFIGURED", str(config))

    def test_str_configured(self):
        os.environ["FRIDAY_EMAIL_USERNAME"] = "a@b.com"
        os.environ["FRIDAY_EMAIL_PASSWORD"] = "pw"
        s = str(EmailConfig.from_env())
        self.assertIn("a@b.com", s)
        self.assertIn("imap.gmail.com", s)
        self.assertIn("smtp.gmail.com", s)
        self.assertNotIn("NOT CONFIGURED", s)


# ===========================================================================
# EmailObserver
# ===========================================================================


class TestEmailObserver(unittest.TestCase):
    def setUp(self):
        self._saved = {}
        for key in ("FRIDAY_EMAIL_USERNAME", "FRIDAY_EMAIL_PASSWORD"):
            self._saved[key] = os.environ.get(key)
            os.environ.pop(key, None)

    def tearDown(self):
        for key, val in self._saved.items():
            if val is not None:
                os.environ[key] = val
            else:
                os.environ.pop(key, None)

    def test_health_not_configured(self):
        from friday.services.email import EmailObserver

        obs = EmailObserver()
        h = obs.health(None)
        self.assertTrue(h.healthy)
        self.assertIn("not configured", h.detail.lower())

    @patch("friday.services.email._connect_imap")
    def test_health_imap_down(self, mock_connect):
        from friday.services.email import EmailObserver

        mock_connect.return_value = None
        os.environ["FRIDAY_EMAIL_USERNAME"] = "a@b.com"
        os.environ["FRIDAY_EMAIL_PASSWORD"] = "pw"
        obs = EmailObserver()
        h = obs.health(None)
        self.assertFalse(h.healthy)
        self.assertIn("could not connect", h.detail.lower())

    @patch("friday.services.email._connect_imap")
    def test_health_imap_ok(self, mock_connect):
        from friday.services.email import EmailObserver

        mock_imap = MagicMock()
        mock_imap.search.return_value = ("OK", [b"1 2 3"])
        mock_connect.return_value = mock_imap

        os.environ["FRIDAY_EMAIL_USERNAME"] = "a@b.com"
        os.environ["FRIDAY_EMAIL_PASSWORD"] = "pw"
        obs = EmailObserver()
        h = obs.health(None)
        self.assertTrue(h.healthy)
        self.assertIn("3 unread", h.detail.lower())

    def test_collect_not_configured(self):
        from friday.services.email import EmailObserver

        obs = EmailObserver()
        obs_collected = obs.collect(None)
        self.assertEqual(obs_collected, [])

    @patch("friday.services.email._fetch_inbox_emails")
    def test_collect_with_emails(self, mock_fetch):
        from friday.services.email import EmailObserver

        mock_fetch.return_value = [
            {
                "uid": "42",
                "subject": "Hello Friday",
                "from": "alice@example.com",
                "date": "2026-07-27T12:00:00+00:00",
                "snippet": "Hi there, just checking in...",
                "unread": True,
            },
            {
                "uid": "43",
                "subject": "Meeting reminder",
                "from": "bob@example.com",
                "date": "2026-07-27T10:00:00+00:00",
                "snippet": "Team sync at 3pm...",
                "unread": False,
            },
        ]

        os.environ["FRIDAY_EMAIL_USERNAME"] = "a@b.com"
        os.environ["FRIDAY_EMAIL_PASSWORD"] = "pw"
        obs = EmailObserver()
        observations = obs.collect(None)

        # Should have: 1 unread_count + 5 per email (subject, from, date, unread, snippet) + message_count
        # = 1 + 5 + 5 + 1 = 12
        self.assertGreaterEqual(len(observations), 5)

        # Check unread count.
        unread_obs = [o for o in observations if o.aspect == "unread_count"]
        self.assertEqual(len(unread_obs), 1)
        self.assertEqual(unread_obs[0].value, "1")

        # Check first email observations.
        email_subjects = [o for o in observations if o.aspect == "subject"]
        self.assertTrue(any("Hello Friday" in o.value for o in email_subjects))

        # Check message count.
        count_obs = [o for o in observations if o.aspect == "message_count"]
        self.assertEqual(len(count_obs), 1)
        self.assertEqual(count_obs[0].value, "2")

    def test_summarize_not_configured(self):
        from friday.services.email import EmailObserver

        obs = EmailObserver()
        s = obs.summarize(None)
        self.assertIn("not configured", s.lower())

    @patch("friday.services.email._fetch_inbox_emails")
    def test_summarize_configured(self, mock_fetch):
        from friday.services.email import EmailObserver

        mock_fetch.return_value = [
            {"uid": "1", "subject": "Hi", "from": "a@b.com",
             "date": "", "snippet": "", "unread": True},
        ]

        os.environ["FRIDAY_EMAIL_USERNAME"] = "a@b.com"
        os.environ["FRIDAY_EMAIL_PASSWORD"] = "pw"
        obs = EmailObserver()
        s = obs.summarize(None)
        self.assertIn("Email", s)


# ===========================================================================
# EmailExecutor
# ===========================================================================


class TestEmailExecutor(unittest.TestCase):
    def setUp(self):
        self._saved = {}
        for key in ("FRIDAY_EMAIL_USERNAME", "FRIDAY_EMAIL_PASSWORD"):
            self._saved[key] = os.environ.get(key)
            os.environ.pop(key, None)

    def tearDown(self):
        for key, val in self._saved.items():
            if val is not None:
                os.environ[key] = val
            else:
                os.environ.pop(key, None)

    def test_empty_payload(self):
        from friday.services.email import EmailExecutor

        executor = EmailExecutor()
        result = executor.execute(_make_task(""))

        self.assertFalse(result.success)
        self.assertIn("empty", result.error.lower())

    def test_invalid_json(self):
        from friday.services.email import EmailExecutor

        executor = EmailExecutor()
        result = executor.execute(_make_task("not json"))

        self.assertFalse(result.success)
        self.assertIn("JSON", result.error)

    def test_missing_to(self):
        from friday.services.email import EmailExecutor

        executor = EmailExecutor()
        result = executor.execute(_make_task('{"subject": "hi", "body": "test"}'))

        self.assertFalse(result.success)
        self.assertIn("required", result.error.lower())

    def test_missing_subject(self):
        from friday.services.email import EmailExecutor

        executor = EmailExecutor()
        result = executor.execute(_make_task('{"to": "a@b.com", "body": "test"}'))

        self.assertFalse(result.success)
        self.assertIn("required", result.error.lower())

    def test_not_configured(self):
        from friday.services.email import EmailExecutor

        executor = EmailExecutor()
        result = executor.execute(
            _make_task('{"to": "a@b.com", "subject": "Hi", "body": "test"}')
        )

        self.assertFalse(result.success)
        # Error message mentions environment variables, not 'not configured' directly.
        self.assertTrue(
            "not configured" in result.error.lower()
            or "friday_email_username" in result.error.lower()
        )

    @patch("friday.services.email._send_email")
    def test_send_success(self, mock_send):
        from friday.services.email import EmailExecutor, EmailConfig

        mock_send.return_value = (True, "")
        os.environ["FRIDAY_EMAIL_USERNAME"] = "a@b.com"
        os.environ["FRIDAY_EMAIL_PASSWORD"] = "pw"

        executor = EmailExecutor()
        result = executor.execute(
            _make_task('{"to": "b@b.com", "subject": "Hi", "body": "Hello!"}')
        )

        self.assertTrue(result.success)
        self.assertIn("sent", result.stdout.lower())
        mock_send.assert_called_once()

    @patch("friday.services.email._send_email")
    def test_send_failure(self, mock_send):
        from friday.services.email import EmailExecutor, EmailConfig

        mock_send.return_value = (False, "SMTP error: 550")
        os.environ["FRIDAY_EMAIL_USERNAME"] = "a@b.com"
        os.environ["FRIDAY_EMAIL_PASSWORD"] = "pw"

        executor = EmailExecutor()
        result = executor.execute(
            _make_task('{"to": "b@b.com", "subject": "Hi", "body": "Hello!"}')
        )

        self.assertFalse(result.success)
        self.assertIn("failed", result.error.lower())
        mock_send.assert_called_once()

    def test_verify(self):
        from friday.services.email import EmailExecutor, EmailConfig

        executor = EmailExecutor()
        result = MagicMock()
        result.success = True
        v = executor.verify(None, result)
        self.assertTrue(v.passed)

        result.success = False
        v = executor.verify(None, result)
        self.assertFalse(v.passed)


# ===========================================================================
# send_email / list_recent_emails helpers
# ===========================================================================


class TestEmailHelpers(unittest.TestCase):
    def setUp(self):
        self._saved = {}
        for key in ("FRIDAY_EMAIL_USERNAME", "FRIDAY_EMAIL_PASSWORD"):
            self._saved[key] = os.environ.get(key)
            os.environ.pop(key, None)

    def tearDown(self):
        for key, val in self._saved.items():
            if val is not None:
                os.environ[key] = val
            else:
                os.environ.pop(key, None)

    def test_send_email_not_configured(self):
        from friday.services.email import send_email

        ok, err = send_email("a@b.com", "Hi", "Body")
        self.assertFalse(ok)
        self.assertIn("not configured", err.lower())

    def test_list_recent_not_configured(self):
        from friday.services.email import list_recent_emails

        result = list_recent_emails(limit=5)
        self.assertEqual(result, [])

    @patch("friday.services.email._send_email")
    def test_send_email_configured(self, mock_send):
        from friday.services.email import send_email

        mock_send.return_value = (True, "")
        os.environ["FRIDAY_EMAIL_USERNAME"] = "a@b.com"
        os.environ["FRIDAY_EMAIL_PASSWORD"] = "pw"

        ok, err = send_email("b@b.com", "Test", "Hello")
        self.assertTrue(ok)
        self.assertEqual(err, "")
        mock_send.assert_called_once()

    @patch("friday.services.email._fetch_inbox_emails")
    def test_list_recent_configured(self, mock_fetch):
        from friday.services.email import list_recent_emails

        mock_fetch.return_value = [{"uid": "1", "subject": "Hi"}]
        os.environ["FRIDAY_EMAIL_USERNAME"] = "a@b.com"
        os.environ["FRIDAY_EMAIL_PASSWORD"] = "pw"

        result = list_recent_emails(limit=5)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["uid"], "1")
        mock_fetch.assert_called_once()


# ===========================================================================
# _decode_header_value / _body_snippet
# ===========================================================================


class TestEmailUtilities(unittest.TestCase):
    def test_decode_plain(self):
        from friday.services.email import _decode_header_value

        self.assertEqual(_decode_header_value("Hello"), "Hello")
        self.assertEqual(_decode_header_value(""), "")

    def test_decode_encoded(self):
        from friday.services.email import _decode_header_value

        # RFC 2047 encoded =?UTF-8?Q?Subject?=
        result = _decode_header_value("=?UTF-8?Q?Hello_World?=")
        self.assertEqual(result, "Hello World")

    def test_body_snippet_plain(self):
        from friday.services.email import _body_snippet
        from email.message import EmailMessage

        msg = EmailMessage()
        msg.set_content("Hello World! This is a test email body.")
        snippet = _body_snippet(msg)
        self.assertEqual(snippet, "Hello World! This is a test email body.")

    def test_body_snippet_truncated(self):
        from friday.services.email import _body_snippet
        from email.message import EmailMessage

        long = "word " * 100
        msg = EmailMessage()
        msg.set_content(long)
        snippet = _body_snippet(msg)
        self.assertLessEqual(len(snippet), 200)

    def test_body_snippet_empty(self):
        from friday.services.email import _body_snippet
        from email.message import EmailMessage

        msg = EmailMessage()
        snippet = _body_snippet(msg)
        self.assertEqual(snippet, "")


# ===========================================================================
# CLI commands (smoke tests)
# ===========================================================================


class TestEmailCLI(unittest.TestCase):
    def test_cli_config_exists(self):
        from friday.cli_email import cmd_email_config

        # Just verify the function exists and runs without crashing.
        args = MagicMock()
        try:
            rc = cmd_email_config(args)
            self.assertEqual(rc, 0)
        except Exception as e:
            self.fail(f"cmd_email_config raised: {e}")

    def test_cli_inbox_exists(self):
        from friday.cli_email import cmd_email_inbox

        args = MagicMock()
        args.limit = 5
        try:
            rc = cmd_email_inbox(args)
            self.assertEqual(rc, 0)
        except Exception as e:
            self.fail(f"cmd_email_inbox raised: {e}")

    def test_cli_setup_exists(self):
        from friday.cli_email import cmd_email_setup

        args = MagicMock()
        try:
            rc = cmd_email_setup(args)
            self.assertEqual(rc, 0)
        except Exception as e:
            self.fail(f"cmd_email_setup raised: {e}")

    def test_cli_send_no_recipient(self):
        from friday.cli_email import cmd_email_send

        args = MagicMock()
        args.to = ""
        args.subject = "hi"
        rc = cmd_email_send(args)
        self.assertEqual(rc, 2)

    def test_cli_send_no_subject(self):
        from friday.cli_email import cmd_email_send

        args = MagicMock()
        args.to = "a@b.com"
        args.subject = ""
        rc = cmd_email_send(args)
        self.assertEqual(rc, 2)

    def test_cli_dispatch_config(self):
        from friday.cli_email import cmd_email

        args = MagicMock()
        args.action = "config"
        try:
            rc = cmd_email(args)
            self.assertEqual(rc, 0)
        except Exception as e:
            self.fail(f"cmd_email(config) raised: {e}")

    def test_cli_dispatch_unknown(self):
        from friday.cli_email import cmd_email

        args = MagicMock()
        args.action = "unknown"
        rc = cmd_email(args)
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
