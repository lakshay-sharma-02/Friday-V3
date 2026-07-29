"""Telegram communication layer: Observer (message polling) + Executor (message sending).

Architecture follows the same pattern as the Slack/Discord/Email modules:
- TelegramObserver polls for new updates (messages) via getUpdates and emits Observations.
- TelegramExecutor sends messages via sendMessage.

Both authenticate via a Bot Token:

    FRIDAY_TELEGRAM_BOT_TOKEN  (required — from @BotFather on Telegram)

Uses the Telegram Bot API directly via urllib — zero dependencies.
Privacy-first: only stores message text snippet up to 200 chars.
"""

from __future__ import annotations

import json
import os
import time
import urllib.parse
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

#: Telegram Bot API base URL.
_API_BASE = "https://api.telegram.org/bot"

#: Max messages to fetch per collect() call.
_MAX_MESSAGES = 20

#: Max message text chars stored in an Observation.
_MAX_TEXT_CHARS = 200

#: Long poll timeout for getUpdates (seconds).
_POLL_TIMEOUT = 10


@dataclass
class TelegramConfig:
    """Telegram Bot credentials, read from environment variables."""

    bot_token: str = ""

    @classmethod
    def from_env(cls) -> "TelegramConfig":
        return cls(
            bot_token=os.environ.get("FRIDAY_TELEGRAM_BOT_TOKEN", ""),
        )

    @property
    def configured(self) -> bool:
        return bool(self.bot_token)

    @property
    def api_url(self) -> str:
        return f"{_API_BASE}{self.bot_token}"

    def __str__(self) -> str:
        if not self.configured:
            return "Telegram: NOT CONFIGURED (set FRIDAY_TELEGRAM_BOT_TOKEN in .env)"
        masked = self.bot_token[:12] + "..." if len(self.bot_token) > 15 else self.bot_token
        return f"Telegram: configured\n  Bot token: {masked}"


# ---------------------------------------------------------------------------
# Bot API helpers (stdlib urllib, zero dependencies)
# ---------------------------------------------------------------------------


def _api_get(config: TelegramConfig, method: str, params: Optional[dict] = None) -> Optional[dict]:
    """Make a GET request to the Telegram Bot API."""
    if not config.configured:
        return None
    url = f"{config.api_url}/{method}"
    if params:
        qs = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items() if v is not None)
        url = f"{url}?{qs}"
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": "Friday/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, json.JSONDecodeError):
        return None


def _api_post(config: TelegramConfig, method: str, data: dict) -> Optional[dict]:
    """Make a POST request to the Telegram Bot API."""
    if not config.configured:
        return None
    url = f"{config.api_url}/{method}"
    body = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "Friday/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, json.JSONDecodeError):
        return None


def _get_me(config: TelegramConfig) -> Optional[dict]:
    """Get bot info. Returns None on failure."""
    result = _api_get(config, "getMe")
    if result and result.get("ok"):
        return result.get("result")
    return None


def _get_updates(
    config: TelegramConfig,
    offset: Optional[int] = None,
    limit: int = _MAX_MESSAGES,
    timeout: int = _POLL_TIMEOUT,
) -> list[dict]:
    """Fetch new updates (messages) since the given offset."""
    params = {"limit": limit, "timeout": timeout}
    if offset:
        params["offset"] = offset
    result = _api_get(config, "getUpdates", params)
    if result and result.get("ok"):
        updates = result.get("result", [])
        parsed: list[dict] = []
        for u in updates:
            msg = u.get("message", {}) or {}
            if not msg or not msg.get("text"):
                continue
            chat = msg.get("chat", {}) or {}
            from_ = msg.get("from", {}) or {}
            parsed.append({
                "update_id": u.get("update_id"),
                "message_id": str(msg.get("message_id", "?")),
                "chat_id": str(chat.get("id", "?")),
                "chat_title": chat.get("title", "") or chat.get("username", ""),
                "from_user": from_.get("username", "?"),
                "text": (msg.get("text", "") or "")[:_MAX_TEXT_CHARS],
                "date": str(msg.get("date", "")),
            })
        return parsed
    return []


def _send_message(config: TelegramConfig, chat_id: str, text: str, parse_mode: str = "") -> tuple[bool, str]:
    """Send a message to a Telegram chat. Returns (success, message_id_or_error).

    On success, the second element is the sent message_id (as a string) so
    callers can use ``_edit_message()`` to edit it in-place later.

    Args:
        config: Telegram bot configuration.
        chat_id: Target chat ID.
        text: Message text.
        parse_mode: Optional parse mode ("HTML" or "MarkdownV2").
    """
    payload: dict = {
        "chat_id": chat_id,
        "text": text,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    result = _api_post(config, "sendMessage", payload)
    if result and result.get("ok"):
        sent_msg = result.get("result", {}) or {}
        msg_id = sent_msg.get("message_id", None)
        if msg_id is not None:
            return True, str(msg_id)
        return True, ""
    error = (result or {}).get("description", "unknown error") if result else "API request failed"
    return False, error


def _edit_message(config: TelegramConfig, chat_id: str, message_id: int, text: str,
                   parse_mode: str = "") -> tuple[bool, str]:
    """Edit an existing Telegram message in-place.

    Uses the ``editMessageText`` API. Returns (success, error_message).

    Args:
        config: Telegram bot configuration.
        chat_id: Target chat ID.
        message_id: ID of the message to edit (returned by ``_send_message``).
        text: New message text.
        parse_mode: Optional parse mode ("HTML" or "MarkdownV2").
    """
    payload: dict = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    result = _api_post(config, "editMessageText", payload)
    if result and result.get("ok"):
        return True, ""
    error = (result or {}).get("description", "unknown error") if result else "API request failed"
    return False, error


# ---------------------------------------------------------------------------
# Action logging helper (same pattern as other modules)
# ---------------------------------------------------------------------------


def _record_telegram_action(
    action_type: str,
    target: str,
    success: bool,
    detail: str = "",
) -> None:
    """Log Telegram action + record outcome for autonomy escalation."""
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
# TelegramObserver — polls for new messages
# ---------------------------------------------------------------------------


class TelegramObserver(Observer):
    """Observes Telegram for new messages via long-polling.

    Emits observations for:
      - unread_count (number of new messages since last poll)
      - message:<id> with text, from_user, chat_id, chat_title

    Never stores full message body (only first 200 chars).
    The ``offset`` for getUpdates is tracked across collect() calls.
    """

    name = "telegram"

    def __init__(
        self,
        config: Optional[TelegramConfig] = None,
    ) -> None:
        self.config = config or TelegramConfig.from_env()
        self._offset: Optional[int] = None  # tracks last seen update_id
        self._at = datetime.now(timezone.utc).isoformat()

    # --- Observer interface ------------------------------------------------

    def health(self, conn) -> ObserverHealth:
        if not self.config.configured:
            return ObserverHealth(
                True,
                Health.HEALTHY,
                "not_configured",
                "Telegram observer: not configured. Set FRIDAY_TELEGRAM_BOT_TOKEN "
                "in .env to enable.",
            )
        me = _get_me(self.config)
        if me is None:
            return ObserverHealth(
                False,
                Health.DOWN,
                "api_error",
                "Could not connect to Telegram API.",
            )
        username = me.get("username", "?")
        return ObserverHealth(
            True,
            Health.HEALTHY,
                "telegram_connected",
                f"Connected as @{username}.",
        )

    def collect(self, conn) -> list[Observation]:
        if not self.config.configured:
            return []

        self._at = datetime.now(timezone.utc).isoformat()
        rows: list[Observation] = []

        updates = _get_updates(self.config, offset=self._offset)

        if updates:
            # Track the highest update_id as the offset for next poll.
            self._offset = max(u["update_id"] for u in updates) + 1

        # Emit unread count.
        rows.append(self._obs("telegram", "unread_count", str(len(updates))))

        for msg in updates:
            mid = str(msg.get("message_id", "?"))
            text = msg.get("text", "")
            from_user = msg.get("from_user", "?")
            chat_id = str(msg.get("chat_id", "?"))
            chat_title = msg.get("chat_title", "")

            rows.append(self._obs(f"message:{mid}", "text", text))
            rows.append(self._obs(f"message:{mid}", "from", from_user))
            rows.append(self._obs(f"message:{mid}", "chat_id", chat_id))
            if chat_title:
                rows.append(self._obs(f"message:{mid}", "chat_title", chat_title))

        return rows

    def summarize(self, conn) -> str:
        if not self.config.configured:
            return "Telegram: not configured"
        me = _get_me(self.config)
        username = me.get("username", "?") if me else "?"
        return (
            f"Telegram\n"
            f"Healthy\n"
            f"Bot\n@{username}\n"
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
# TelegramExecutor — sends messages
# ---------------------------------------------------------------------------


class TelegramExecutor(Executor):
    """Send messages via Telegram Bot API. Implements the Executor contract.

    Expects ``task.runtime_payload`` to be JSON:
      {"chat_id": "123456789", "text": "Hello world!"}
    """

    worker_id = "worker:telegram"

    def __init__(self, config: Optional[TelegramConfig] = None) -> None:
        self.config = config or TelegramConfig.from_env()

    def execute(self, task) -> ExecutionResult:
        """Execute one Telegram message send."""
        raw = getattr(task, "runtime_payload", "") or ""
        if not raw.strip():
            _record_telegram_action("telegram_send", "", False, "empty payload")
            return ExecutionResult(
                success=False,
                stdout="",
                stderr="empty payload",
                exit_code=None,
                duration_ms=0,
                error="TelegramExecutor: runtime_payload is empty",
            )

        try:
            obj = json.loads(raw)
        except (ValueError, TypeError):
            _record_telegram_action("telegram_send", raw[:100], False, "invalid JSON")
            return ExecutionResult(
                success=False,
                stdout="",
                stderr="invalid JSON",
                exit_code=None,
                duration_ms=0,
                error="TelegramExecutor: payload must be valid JSON",
            )

        chat_id = (obj.get("chat_id") or "").strip()
        text = (obj.get("text") or "").strip()

        if not chat_id or not text:
            _record_telegram_action("telegram_send", chat_id, False, "missing chat_id/text")
            return ExecutionResult(
                success=False,
                stdout="",
                stderr="missing required fields",
                exit_code=None,
                duration_ms=0,
                error="TelegramExecutor: 'chat_id' and 'text' are required",
            )

        if not self.config.configured:
            _record_telegram_action("telegram_send", chat_id, False, "not configured")
            return ExecutionResult(
                success=False,
                stdout="",
                stderr="telegram not configured",
                exit_code=None,
                duration_ms=0,
                error="TelegramExecutor: set FRIDAY_TELEGRAM_BOT_TOKEN in .env",
            )

        t0 = time.monotonic()
        ok, err = _send_message(self.config, chat_id, text)
        dur = int((time.monotonic() - t0) * 1000)

        if ok:
            _record_telegram_action("telegram_send", chat_id, True)
            return ExecutionResult(
                success=True,
                stdout=f"Message sent to chat {chat_id}",
                stderr="",
                exit_code=0,
                duration_ms=dur,
                artifacts=[],
            )
        else:
            _record_telegram_action("telegram_send", chat_id, False, err)
            return ExecutionResult(
                success=False,
                stdout="",
                stderr=err,
                exit_code=None,
                duration_ms=dur,
                error=f"Telegram send failed: {err}",
            )

    def verify(self, task, result) -> VerificationResult:
        """Simple verify: trust the success flag."""
        return VerificationResult(passed=result.success, reason="success flag")


# backward-compat alias
TelegramWorker = TelegramExecutor


# ---------------------------------------------------------------------------
# Standalone helpers for CLI use
# ---------------------------------------------------------------------------


def get_bot_info() -> Optional[dict]:
    """Get bot info using environment config."""
    config = TelegramConfig.from_env()
    if not config.configured:
        return None
    return _get_me(config)


def send_message(chat_id: str, text: str) -> tuple[bool, str]:
    """Send a message to a Telegram chat using environment config.

    Returns (success, error_message).
    """
    config = TelegramConfig.from_env()
    if not config.configured:
        return False, "Telegram not configured. Set FRIDAY_TELEGRAM_BOT_TOKEN in .env."
    ok, err = _send_message(config, chat_id, text)
    _record_telegram_action("telegram_send_cli", chat_id, ok, err)
    return ok, err
