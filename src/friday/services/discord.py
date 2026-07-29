"""Discord communication layer: Observer (channel polling) + Executor (message posting).

Architecture follows the same pattern as the Slack/Email modules:
- DiscordObserver lists guilds/channels and fetches recent messages as Observations.
- DiscordExecutor posts messages to channels via the REST API.

Both authenticate via a Bot Token:

    FRIDAY_DISCORD_BOT_TOKEN  (required — starts with bot token from Discord Developer Portal)

Uses the Discord REST API (v10) directly via urllib — zero dependencies.
Privacy-first: only stores metadata (channel name, message text snippet up to 200 chars).
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
import urllib.error
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

#: Discord REST API base URL.
_API_BASE = "https://discord.com/api/v10"

#: Max messages to fetch per channel per collect() call.
_MAX_MESSAGES_PER_CHANNEL = 10

#: Max channels to observe per collect() call.
_MAX_CHANNELS = 20

#: Max guilds to observe.
_MAX_GUILDS = 5

#: Max message text chars stored in an Observation.
_MAX_TEXT_CHARS = 200


@dataclass
class DiscordConfig:
    """Discord credentials, read from environment variables."""

    bot_token: str = ""

    @classmethod
    def from_env(cls) -> "DiscordConfig":
        return cls(
            bot_token=os.environ.get("FRIDAY_DISCORD_BOT_TOKEN", ""),
        )

    @property
    def configured(self) -> bool:
        return bool(self.bot_token)

    def __str__(self) -> str:
        if not self.configured:
            return "Discord: NOT CONFIGURED (set FRIDAY_DISCORD_BOT_TOKEN in .env)"
        masked = self.bot_token[:12] + "..." if len(self.bot_token) > 15 else self.bot_token
        return f"Discord: configured\n  Bot token: {masked}"


# ---------------------------------------------------------------------------
# REST API helpers (stdlib urllib, zero dependencies)
# ---------------------------------------------------------------------------


def _headers(config: DiscordConfig) -> dict[str, str]:
    return {
        "Authorization": f"Bot {config.bot_token}",
        "Content-Type": "application/json",
        "User-Agent": "Friday/1.0",
    }


def _api_get(config: DiscordConfig, path: str) -> Optional[list | dict]:
    """Make a GET request to the Discord REST API."""
    if not config.configured:
        return None
    url = f"{_API_BASE}{path}"
    req = urllib.request.Request(url, headers=_headers(config), method="GET")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, json.JSONDecodeError):
        return None


def _api_post(config: DiscordConfig, path: str, data: dict) -> Optional[dict]:
    """Make a POST request to the Discord REST API."""
    if not config.configured:
        return None
    url = f"{_API_BASE}{path}"
    body = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=_headers(config), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, json.JSONDecodeError):
        return None


def _list_guilds(config: DiscordConfig, limit: int = _MAX_GUILDS) -> list[dict]:
    """List guilds (servers) the bot is in."""
    result = _api_get(config, "/users/@me/guilds")
    if not isinstance(result, list):
        return []
    return result[:limit]


def _list_channels(config: DiscordConfig, guild_id: str) -> list[dict]:
    """List text channels in a guild."""
    result = _api_get(config, f"/guilds/{guild_id}/channels")
    if not isinstance(result, list):
        return []
    # Only return text channels (type 0 = GUILD_TEXT).
    return [c for c in result if c.get("type") == 0]


def _fetch_messages(
    config: DiscordConfig,
    channel_id: str,
    limit: int = _MAX_MESSAGES_PER_CHANNEL,
) -> list[dict]:
    """Fetch recent messages from a channel."""
    result = _api_get(config, f"/channels/{channel_id}/messages?limit={limit}")
    if not isinstance(result, list):
        return []
    return [
        {
            "id": m.get("id", "?"),
            "author": (m.get("author", {}) or {}).get("username", "?"),
            "content": (m.get("content", "") or "")[:_MAX_TEXT_CHARS],
            "channel_id": channel_id,
            "timestamp": m.get("timestamp", ""),
        }
        for m in result
        if m.get("content")
    ]


def _post_message(
    config: DiscordConfig,
    channel_id: str,
    content: str,
) -> tuple[bool, str]:
    """Post a message to a Discord channel. Returns (success, error_message)."""
    result = _api_post(config, f"/channels/{channel_id}/messages", {"content": content})
    if result and result.get("id"):
        return True, result.get("id", "")
    return False, "failed to post message"


def _edit_message(
    config: DiscordConfig,
    channel_id: str,
    message_id: str,
    content: str,
) -> tuple[bool, str]:
    """Edit an existing Discord message in-place via PATCH.

    Uses the Discord REST API ``PATCH /channels/{channel_id}/messages/{message_id}``.
    Returns (success, error_message).

    Args:
        config: Discord bot configuration.
        channel_id: Channel ID.
        message_id: ID of the message to edit.
        content: New message content.
    """
    if not config.configured:
        return False, "Discord not configured"
    url = f"{_API_BASE}/channels/{channel_id}/messages/{message_id}"
    body = json.dumps({"content": content}).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={
            "Authorization": f"Bot {config.bot_token}",
            "Content-Type": "application/json",
            "User-Agent": "Friday/1.0",
        },
        method="PATCH",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            if result and result.get("id"):
                return True, ""
            return False, "edit failed"
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        return False, f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# Action logging helper (same pattern as slack/email modules)
# ---------------------------------------------------------------------------


def _record_discord_action(
    action_type: str,
    target: str,
    success: bool,
    detail: str = "",
) -> None:
    """Log Discord action + record outcome for autonomy escalation."""
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
# DiscordObserver — polls channels for messages
# ---------------------------------------------------------------------------


class DiscordObserver(Observer):
    """Observes Discord guilds and channels, emits observations per message.

    Emits observations for:
      - guild_count (total accessible guilds)
      - guild:<id> with name
      - channel:<id> with name, guild_id, guild_name
      - message:<id> with author, content, channel

    Never stores full message body (only first 200 chars).
    """

    name = "discord"

    def __init__(
        self,
        config: Optional[DiscordConfig] = None,
        max_guilds: int = _MAX_GUILDS,
        max_channels: int = _MAX_CHANNELS,
        max_messages: int = _MAX_MESSAGES_PER_CHANNEL,
    ) -> None:
        self.config = config or DiscordConfig.from_env()
        self.max_guilds = max_guilds
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
                "Discord observer: not configured. Set FRIDAY_DISCORD_BOT_TOKEN "
                "in .env to enable.",
            )
        guilds = _list_guilds(self.config, limit=1)
        if guilds is None:
            return ObserverHealth(
                False,
                Health.DOWN,
                "api_error",
                "Could not connect to Discord API.",
            )
        return ObserverHealth(
            True,
            Health.HEALTHY,
            "discord_connected",
            f"Connected to {len(guilds)} guild(s).",
        )

    def collect(self, conn) -> list[Observation]:
        if not self.config.configured:
            return []

        self._at = datetime.now(timezone.utc).isoformat()
        rows: list[Observation] = []

        guilds = _list_guilds(self.config, limit=self.max_guilds)
        rows.append(self._obs("discord", "guild_count", str(len(guilds))))

        for guild in guilds:
            gid = guild.get("id", "?")
            gname = guild.get("name", "?")
            rows.append(self._obs(f"guild:{gid}", "name", gname))

            channels = _list_channels(self.config, gid)
            # Limit to first N channels to avoid noise.
            for ch in channels[: self.max_channels]:
                cid = ch.get("id", "?")
                cname = ch.get("name", "?")
                rows.append(
                    self._obs(f"channel:{cid}", "name", cname)
                )
                rows.append(self._obs(f"channel:{cid}", "guild", gname))

                # Fetch recent messages.
                messages = _fetch_messages(
                    self.config, cid, limit=self.max_messages
                )
                for msg in messages:
                    mid = msg.get("id", "?")
                    author = msg.get("author", "?")
                    content = msg.get("content", "")

                    rows.append(
                        self._obs(f"message:{mid}", "author", author)
                    )
                    rows.append(
                        self._obs(f"message:{mid}", "channel", cname)
                    )
                    if content:
                        rows.append(
                            self._obs(f"message:{mid}", "content", content)
                        )

        return rows

    def summarize(self, conn) -> str:
        if not self.config.configured:
            return "Discord: not configured"
        guilds = _list_guilds(self.config, limit=self.max_guilds)
        gnames = ", ".join(g.get("name", "?") for g in guilds)
        return (
            f"Discord\n"
            f"Healthy\n"
            f"Guilds\n{len(guilds)}\n"
            f"Names\n{gnames}\n"
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
# DiscordExecutor — posts messages to channels
# ---------------------------------------------------------------------------


class DiscordExecutor(Executor):
    """Post messages to Discord channels. Implements the Executor contract.

    Expects ``task.runtime_payload`` to be JSON:
      {"channel": "123456789", "content": "Hello world!"}
    """

    worker_id = "worker:discord"

    def __init__(self, config: Optional[DiscordConfig] = None) -> None:
        self.config = config or DiscordConfig.from_env()

    def execute(self, task) -> ExecutionResult:
        """Execute one Discord message post."""
        raw = getattr(task, "runtime_payload", "") or ""
        if not raw.strip():
            _record_discord_action("discord_post", "", False, "empty payload")
            return ExecutionResult(
                success=False,
                stdout="",
                stderr="empty payload",
                exit_code=None,
                duration_ms=0,
                error="DiscordExecutor: runtime_payload is empty",
            )

        try:
            obj = json.loads(raw)
        except (ValueError, TypeError):
            _record_discord_action("discord_post", raw[:100], False, "invalid JSON")
            return ExecutionResult(
                success=False,
                stdout="",
                stderr="invalid JSON",
                exit_code=None,
                duration_ms=0,
                error="DiscordExecutor: payload must be valid JSON",
            )

        channel = (obj.get("channel") or "").strip()
        content = (obj.get("content") or "").strip()

        if not channel or not content:
            _record_discord_action("discord_post", channel, False, "missing channel/content")
            return ExecutionResult(
                success=False,
                stdout="",
                stderr="missing required fields",
                exit_code=None,
                duration_ms=0,
                error="DiscordExecutor: 'channel' and 'content' are required",
            )

        if not self.config.configured:
            _record_discord_action("discord_post", channel, False, "not configured")
            return ExecutionResult(
                success=False,
                stdout="",
                stderr="discord not configured",
                exit_code=None,
                duration_ms=0,
                error="DiscordExecutor: set FRIDAY_DISCORD_BOT_TOKEN in .env",
            )

        t0 = time.monotonic()
        ok, err = _post_message(self.config, channel, content)
        dur = int((time.monotonic() - t0) * 1000)

        if ok:
            _record_discord_action("discord_post", channel, True)
            return ExecutionResult(
                success=True,
                stdout=f"Message posted to channel {channel}",
                stderr="",
                exit_code=0,
                duration_ms=dur,
                artifacts=[],
            )
        else:
            _record_discord_action("discord_post", channel, False, err)
            return ExecutionResult(
                success=False,
                stdout="",
                stderr=err,
                exit_code=None,
                duration_ms=dur,
                error=f"Discord post failed: {err}",
            )

    def verify(self, task, result) -> VerificationResult:
        """Simple verify: trust the success flag."""
        return VerificationResult(passed=result.success, reason="success flag")


# backward-compat alias
DiscordWorker = DiscordExecutor


# ---------------------------------------------------------------------------
# Standalone helpers for CLI use
# ---------------------------------------------------------------------------


def list_guilds(limit: int = 5) -> list[dict]:
    """List Discord guilds using environment config."""
    config = DiscordConfig.from_env()
    if not config.configured:
        return []
    return _list_guilds(config, limit=limit)


def list_channels_for_guild(guild_id: str) -> list[dict]:
    """List text channels in a Discord guild."""
    config = DiscordConfig.from_env()
    if not config.configured:
        return []
    return _list_channels(config, guild_id)


def post_message(channel_id: str, content: str) -> tuple[bool, str]:
    """Post a message to a Discord channel using environment config.

    Returns (success, error_message).
    """
    config = DiscordConfig.from_env()
    if not config.configured:
        return False, "Discord not configured. Set FRIDAY_DISCORD_BOT_TOKEN in .env."
    ok, err = _post_message(config, channel_id, content)
    _record_discord_action("discord_post_cli", channel_id, ok, err)
    return ok, err
