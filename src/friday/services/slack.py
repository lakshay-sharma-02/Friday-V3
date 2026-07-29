"""Slack communication layer: Observer (channel message polling) + Executor (message posting).

Architecture follows the same pattern as the Email module:
- SlackObserver lists conversations and fetches recent messages as Observations.
- SlackExecutor posts messages to channels via the Web API.

Both read credentials from environment variables loaded via ``_load_dotenv()``:

    FRIDAY_SLACK_BOT_TOKEN   (required — starts with ``xoxb-``)
    FRIDAY_SLACK_APP_TOKEN   (optional — for Socket Mode, starts with ``xapp-``)

Privacy-first (same discipline as CalendarObserver):
- Reads only metadata: channel name, message text snippet (first 200 chars),
  user, timestamp.
- Full message history is NEVER stored as Observations.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from ..action_log import ActionEvent, log_action, now_iso as _now_action
from ..autonomy import record_action_outcome
from ..db import connect as _resolve_connect
from ..observation.interface import Health, Observer, ObserverHealth
from ..observation.model import Confidence, Observation
from ..runtime.models import Executor, ExecutionResult, VerificationResult

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

#: Max messages to fetch per channel per collect() call.
_MAX_MESSAGES_PER_CHANNEL = 10

#: Max channels to observe per collect() call.
_MAX_CHANNELS = 20

#: Max message text chars stored in an Observation.
_MAX_TEXT_CHARS = 200


@dataclass
class SlackConfig:
    """Slack credentials and settings, read from environment variables."""

    bot_token: str = ""
    app_token: str = ""  # optional, for Socket Mode

    @classmethod
    def from_env(cls) -> "SlackConfig":
        return cls(
            bot_token=os.environ.get("FRIDAY_SLACK_BOT_TOKEN", ""),
            app_token=os.environ.get("FRIDAY_SLACK_APP_TOKEN", ""),
        )

    @property
    def configured(self) -> bool:
        return bool(self.bot_token)

    def __str__(self) -> str:
        if not self.configured:
            return "Slack: NOT CONFIGURED (set FRIDAY_SLACK_BOT_TOKEN in .env)"
        masked = self.bot_token[:12] + "..." if len(self.bot_token) > 15 else self.bot_token
        has_app = "yes" if self.app_token else "no"
        return (
            f"Slack: configured\n"
            f"  Bot token: {masked}\n"
            f"  App token: {has_app}"
        )


# ---------------------------------------------------------------------------
# Slack SDK wrapper (lazy import so module loads without slack_sdk installed)
# ---------------------------------------------------------------------------


def _get_client(config: SlackConfig):
    """Create a Slack WebClient from config. Returns None on failure."""
    if not config.configured:
        return None
    try:
        from slack_sdk import WebClient
        return WebClient(token=config.bot_token)
    except ImportError:
        return None


def _list_channels(config: SlackConfig, limit: int = _MAX_CHANNELS) -> list[dict]:
    """List public channels the bot has access to."""
    client = _get_client(config)
    if client is None:
        return []
    try:
        result = client.conversations_list(
            types="public_channel,private_channel",
            limit=limit,
            exclude_archived=True,
        )
        return result.get("channels", [])
    except Exception:
        return []


def _fetch_channel_messages(
    config: SlackConfig,
    channel_id: str,
    limit: int = _MAX_MESSAGES_PER_CHANNEL,
) -> list[dict]:
    """Fetch recent messages from a channel."""
    client = _get_client(config)
    if client is None:
        return []
    try:
        result = client.conversations_history(
            channel=channel_id,
            limit=limit,
        )
        messages = result.get("messages", [])
        # Only return text messages with real content.
        return [
            {
                "ts": m.get("ts", ""),
                "user": m.get("user", ""),
                "text": (m.get("text", "") or "")[:_MAX_TEXT_CHARS],
                "channel": channel_id,
            }
            for m in messages
            if m.get("text") and m.get("type") == "message"
        ]
    except Exception:
        return []


def _post_message(
    config: SlackConfig,
    channel: str,
    text: str,
) -> tuple[bool, str]:
    """Post a message to a Slack channel. Returns (success, error_message)."""
    client = _get_client(config)
    if client is None:
        return False, "Slack not configured"
    try:
        result = client.chat_postMessage(channel=channel, text=text)
        if result.get("ok"):
            return True, result.get("ts", "")
        return False, result.get("error", "unknown error")
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _edit_message(
    config: SlackConfig,
    channel: str,
    ts: str,
    text: str,
) -> tuple[bool, str]:
    """Edit an existing Slack message in-place via ``chat_update``.

    Returns (success, error_message).

    Args:
        config: Slack bot configuration.
        channel: Channel ID.
        ts: Timestamp of the message to edit (returned by ``_post_message``).
        text: New message text.
    """
    client = _get_client(config)
    if client is None:
        return False, "Slack not configured"
    try:
        result = client.chat_update(channel=channel, ts=ts, text=text)
        if result.get("ok"):
            return True, ""
        return False, result.get("error", "unknown error")
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# Action logging helper (same pattern as email module)
# ---------------------------------------------------------------------------


def _record_slack_action(
    action_type: str,
    target: str,
    success: bool,
    detail: str = "",
) -> None:
    """Log Slack action + record outcome for autonomy escalation."""
    try:
        conn = _resolve_connect()
        status = "success" if success else "failure"
        log_action(
            conn,
            ActionEvent(
                source="friday",
                action_type=action_type,
                target=(target or "")[:200],
                detail=json.dumps({"status": status, "error": detail}),
                confidence="observed",
                observed_at=_now_action(),
            ),
        )
        record_action_outcome(action_type, success, conn=conn)
        conn.close()
    except Exception:
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# SlackObserver — polls channels for messages
# ---------------------------------------------------------------------------


class SlackObserver(Observer):
    """Observes Slack channels and emits observations per recent message.

    Emits observations for:
      - channel_count (total accessible channels)
      - channel:<id> with name, topic, member_count
      - message:<channel>:<ts> with text snippet, user, channel

    Never stores full message body (only first 200 chars).
    """

    name = "slack"

    def __init__(
        self,
        config: Optional[SlackConfig] = None,
        max_channels: int = _MAX_CHANNELS,
        max_messages: int = _MAX_MESSAGES_PER_CHANNEL,
    ) -> None:
        self.config = config or SlackConfig.from_env()
        self.max_channels = max_channels
        self.max_messages = max_messages
        self._at = datetime.now(timezone.utc).isoformat()

    # --- Observer interface ------------------------------------------------

    def health(self, conn) -> ObserverHealth:
        if not self.config.configured:
            return ObserverHealth(
                True,
                Health.HEALTHY,
                "not_configured",
                "Slack observer: not configured. Set FRIDAY_SLACK_BOT_TOKEN "
                "in .env to enable.",
            )
        client = _get_client(self.config)
        if client is None:
            return ObserverHealth(
                False,
                Health.DOWN,
                "sdk_missing",
                "Slack SDK not available. Install: pip install slack_sdk",
            )
        try:
            result = client.auth_test()
            if result.get("ok"):
                team = result.get("team", "?")
                user = result.get("user", "?")
                return ObserverHealth(
                    True,
                    Health.HEALTHY,
                    "slack_connected",
                    f"Connected as {user} to {team}.",
                )
            return ObserverHealth(
                False,
                Health.DOWN,
                "auth_failed",
                "Slack auth test failed.",
            )
        except Exception as exc:
            return ObserverHealth(
                False,
                Health.DEGRADED,
                "slack_error",
                f"Slack error: {exc}",
            )

    def collect(self, conn) -> list[Observation]:
        if not self.config.configured:
            return []

        self._at = datetime.now(timezone.utc).isoformat()
        rows: list[Observation] = []

        channels = _list_channels(self.config, limit=self.max_channels)

        # Emit channel count.
        rows.append(self._obs("slack", "channel_count", str(len(channels))))

        for ch in channels[: self.max_channels]:
            ch_id = ch.get("id", "?")
            ch_name = ch.get("name", "?")
            topic = (ch.get("topic", {}) or {}).get("value", "") or ""
            member_count = ch.get("num_members", 0)

            # Emit channel metadata.
            rows.append(self._obs(f"channel:{ch_id}", "name", ch_name))
            rows.append(
                self._obs(f"channel:{ch_id}", "member_count", str(member_count))
            )
            if topic:
                rows.append(self._obs(f"channel:{ch_id}", "topic", topic[:_MAX_TEXT_CHARS]))

            # Fetch recent messages.
            messages = _fetch_channel_messages(
                self.config, ch_id, limit=self.max_messages
            )
            for msg in messages:
                ts = msg.get("ts", "?")
                user = msg.get("user", "?")
                text = msg.get("text", "")

                rows.append(
                    self._obs(
                        f"message:{ch_id}:{ts}",
                        "user",
                        user,
                    )
                )
                rows.append(
                    self._obs(
                        f"message:{ch_id}:{ts}",
                        "channel",
                        ch_id,
                    )
                )
                if text:
                    rows.append(
                        self._obs(
                            f"message:{ch_id}:{ts}",
                            "text",
                            text[:_MAX_TEXT_CHARS],
                        )
                    )

        return rows

    def summarize(self, conn) -> str:
        if not self.config.configured:
            return "Slack: not configured"
        channels = _list_channels(self.config, limit=self.max_channels)
        total = len(channels)
        names = ", ".join(c.get("name", "?") for c in channels[:5])
        extra = f" and {total - 5} more" if total > 5 else ""
        return (
            f"Slack\n"
            f"Healthy\n"
            f"Channels\n{total}\n"
            f"Names\n{names}{extra}\n"
        )

    # --- internals ---------------------------------------------------------

    def _obs(
        self,
        subject: str,
        aspect: str,
        value: str,
        cause: Optional[str] = None,
    ) -> Observation:
        return Observation(
            source=self.name,
            subject=subject,
            aspect=aspect,
            value=value,
            confidence=Confidence.OBSERVED,
            observed_at=self._at,
            scope="",
            cause=cause,
        )


# ---------------------------------------------------------------------------
# SlackExecutor — posts messages to channels
# ---------------------------------------------------------------------------


class SlackExecutor(Executor):
    """Post messages to Slack channels. Implements the Executor contract.

    Expects ``task.runtime_payload`` to be JSON:
      {"channel": "C12345678 or #general", "text": "Hello world!"}
    """

    worker_id = "worker:slack"

    def __init__(self, config: Optional[SlackConfig] = None) -> None:
        self.config = config or SlackConfig.from_env()

    def execute(self, task) -> ExecutionResult:
        """Execute one Slack message post."""
        raw = getattr(task, "runtime_payload", "") or ""
        if not raw.strip():
            _record_slack_action("slack_post", "", False, "empty payload")
            return ExecutionResult(
                success=False,
                stdout="",
                stderr="empty payload",
                exit_code=None,
                duration_ms=0,
                error="SlackExecutor: runtime_payload is empty",
            )

        try:
            obj = json.loads(raw)
        except (ValueError, TypeError):
            _record_slack_action("slack_post", raw[:100], False, "invalid JSON")
            return ExecutionResult(
                success=False,
                stdout="",
                stderr="invalid JSON",
                exit_code=None,
                duration_ms=0,
                error="SlackExecutor: payload must be valid JSON",
            )

        channel = (obj.get("channel") or "").strip()
        text = (obj.get("text") or "").strip()

        if not channel or not text:
            _record_slack_action("slack_post", channel, False, "missing channel/text")
            return ExecutionResult(
                success=False,
                stdout="",
                stderr="missing required fields",
                exit_code=None,
                duration_ms=0,
                error="SlackExecutor: 'channel' and 'text' are required",
            )

        if not self.config.configured:
            _record_slack_action("slack_post", channel, False, "not configured")
            return ExecutionResult(
                success=False,
                stdout="",
                stderr="slack not configured",
                exit_code=None,
                duration_ms=0,
                error="SlackExecutor: set FRIDAY_SLACK_BOT_TOKEN in .env",
            )

        t0 = time.monotonic()
        ok, err = _post_message(self.config, channel, text)
        dur = int((time.monotonic() - t0) * 1000)

        if ok:
            _record_slack_action("slack_post", channel, True)
            return ExecutionResult(
                success=True,
                stdout=f"Message posted to {channel}",
                stderr="",
                exit_code=0,
                duration_ms=dur,
                artifacts=[],
            )
        else:
            _record_slack_action("slack_post", channel, False, err)
            return ExecutionResult(
                success=False,
                stdout="",
                stderr=err,
                exit_code=None,
                duration_ms=dur,
                error=f"Slack post failed: {err}",
            )

    def verify(self, task, result) -> VerificationResult:
        """Simple verify: trust the success flag."""
        return VerificationResult(passed=result.success, reason="success flag")


# backward-compat alias
SlackWorker = SlackExecutor


# ---------------------------------------------------------------------------
# Standalone helpers for CLI use
# ---------------------------------------------------------------------------


def list_channels(limit: int = 20) -> list[dict]:
    """List accessible Slack channels using environment config."""
    config = SlackConfig.from_env()
    if not config.configured:
        return []
    return _list_channels(config, limit=limit)


def post_message(channel: str, text: str) -> tuple[bool, str]:
    """Post a message to a Slack channel using environment config.

    Returns (success, error_message).
    """
    config = SlackConfig.from_env()
    if not config.configured:
        return False, "Slack not configured. Set FRIDAY_SLACK_BOT_TOKEN in .env."
    ok, err = _post_message(config, channel, text)
    _record_slack_action("slack_post_cli", channel, ok, err)
    return ok, err
