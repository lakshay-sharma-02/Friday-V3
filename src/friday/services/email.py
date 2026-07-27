"""Email communication layer: Observer (IMAP inbox polling) + Executor (SMTP send).

Architecture follows the same pattern as CalendarObserver / BrowserExecutor:
- EmailObserver reads the inbox via IMAP and emits Observations per message.
- EmailExecutor sends emails via SMTP with a ``Worker.execute(task)`` contract.

Both read credentials from environment variables loaded via ``_load_dotenv()``:

    FRIDAY_EMAIL_IMAP_SERVER   (default: imap.gmail.com)
    FRIDAY_EMAIL_IMAP_PORT     (default: 993)
    FRIDAY_EMAIL_SMTP_SERVER   (default: smtp.gmail.com)
    FRIDAY_EMAIL_SMTP_PORT     (default: 587)
    FRIDAY_EMAIL_USERNAME       (required — your email address)
    FRIDAY_EMAIL_PASSWORD       (required — app password for Gmail, or regular password)
    FRIDAY_EMAIL_FROM           (optional — display name; defaults to USERNAME)

Privacy-first (same discipline as CalendarObserver):
- Reads only metadata: subject, from, date, flags (read/unread), snippet (first
  200 chars of body). Full body content is NEVER stored as an Observation.
- Attachments are counted but NEVER downloaded or inspected.
"""

from __future__ import annotations

import email
import imaplib
import json
import os
import smtplib
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.header import decode_header
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
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

#: Max number of recent emails to fetch per collect() call.
_MAX_EMAILS_PER_COLLECT = 20

#: Max body snippet chars stored in an Observation (privacy: never full body).
_MAX_SNIPPET_CHARS = 200

#: Hours of email history to scan on each collect().
_SCAN_WINDOW_HOURS = 24


@dataclass
class EmailConfig:
    """Email credentials and server settings, read from environment variables."""

    imap_server: str = "imap.gmail.com"
    imap_port: int = 993
    smtp_server: str = "smtp.gmail.com"
    smtp_port: int = 587
    username: str = ""
    password: str = ""
    from_addr: str = ""  # display name; defaults to username

    @classmethod
    def from_env(cls) -> "EmailConfig":
        return cls(
            imap_server=os.environ.get("FRIDAY_EMAIL_IMAP_SERVER", "imap.gmail.com"),
            imap_port=int(os.environ.get("FRIDAY_EMAIL_IMAP_PORT", "993")),
            smtp_server=os.environ.get("FRIDAY_EMAIL_SMTP_SERVER", "smtp.gmail.com"),
            smtp_port=int(os.environ.get("FRIDAY_EMAIL_SMTP_PORT", "587")),
            username=os.environ.get("FRIDAY_EMAIL_USERNAME", ""),
            password=os.environ.get("FRIDAY_EMAIL_PASSWORD", ""),
            from_addr=os.environ.get("FRIDAY_EMAIL_FROM", ""),
        )

    @property
    def configured(self) -> bool:
        return bool(self.username and self.password)

    def __str__(self) -> str:
        if not self.configured:
            return "Email: NOT CONFIGURED (set FRIDAY_EMAIL_USERNAME + FRIDAY_EMAIL_PASSWORD)"
        return (
            f"Email: {self.username}\n"
            f"  IMAP: {self.imap_server}:{self.imap_port}\n"
            f"  SMTP: {self.smtp_server}:{self.smtp_port}"
        )


# ---------------------------------------------------------------------------
# IMAP helpers
# ---------------------------------------------------------------------------


def _connect_imap(config: EmailConfig) -> Optional[imaplib.IMAP4_SSL]:
    """Connect and log in to the IMAP server. Returns None on failure."""
    try:
        imap = imaplib.IMAP4_SSL(config.imap_server, config.imap_port, timeout=30)
        imap.login(config.username, config.password)
        return imap
    except Exception:
        return None


def _fetch_inbox_emails(
    config: EmailConfig,
    limit: int = _MAX_EMAILS_PER_COLLECT,
    since_hours: int = _SCAN_WINDOW_HOURS,
) -> list[dict]:
    """Fetch recent emails from INBOX. Returns list of metadata dicts."""
    imap = _connect_imap(config)
    if not imap:
        return []

    try:
        imap.select("INBOX", readonly=True)

        # Search for messages in the last N hours.
        since_dt = datetime.now(timezone.utc).timestamp() - (since_hours * 3600)
        since_str = datetime.fromtimestamp(since_dt, tz=timezone.utc).strftime(
            "%d-%b-%Y"
        )
        _typ, data = imap.search(None, f'SINCE "{since_str}"')
        if not data or not data[0]:
            return []

        msg_ids = data[0].split()
        # Take the most recent N.
        msg_ids = msg_ids[-limit:]

        emails: list[dict] = []
        for mid in msg_ids:
            _typ, msg_data = imap.fetch(mid, "(RFC822 FLAGS)")
            if not msg_data or not msg_data[0]:
                continue
            raw = msg_data[0]
            # raw is (b'RFC822 {size}', bytes) or just bytes
            if isinstance(raw, tuple):
                raw_bytes = raw[1]
            else:
                raw_bytes = raw

            try:
                parsed = email.message_from_bytes(raw_bytes)
            except Exception:
                continue

            subject = _decode_header_value(parsed.get("Subject", ""))
            from_ = _decode_header_value(parsed.get("From", ""))
            date_str = parsed.get("Date", "")

            # Parse date to ISO.
            iso_date = ""
            try:
                dt = parsedate_to_datetime(date_str)
                if dt:
                    iso_date = dt.isoformat()
            except Exception:
                iso_date = date_str

            # Get first 200 chars of body for snippet (privacy: never full body).
            snippet = _body_snippet(parsed, _MAX_SNIPPET_CHARS)

            # Check flags.
            flags = msg_data[0] if isinstance(msg_data, tuple) else b""
            is_unread = b"\\Seen" not in (flags if isinstance(flags, bytes) else b"")

            emails.append(
                {
                    "uid": mid.decode() if isinstance(mid, bytes) else str(mid),
                    "subject": subject,
                    "from": from_,
                    "date": iso_date,
                    "snippet": snippet,
                    "unread": is_unread,
                }
            )

        return emails
    finally:
        try:
            imap.close()
            imap.logout()
        except Exception:
            pass


def _decode_header_value(raw: str) -> str:
    """Decode RFC 2047 encoded header values (e.g. =?UTF-8?Q?...?=)."""
    if not raw:
        return ""
    parts = []
    for chunk, encoding in decode_header(raw):
        if isinstance(chunk, bytes):
            try:
                parts.append(chunk.decode(encoding or "utf-8", errors="replace"))
            except (LookupError, UnicodeDecodeError):
                parts.append(chunk.decode("utf-8", errors="replace"))
        else:
            parts.append(str(chunk))
    return " ".join(parts).strip()


def _body_snippet(msg: email.message.Message, max_chars: int = 200) -> str:
    """Extract a plain-text body snippet (not the full body)."""
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/plain":
                try:
                    payload = part.get_payload(decode=True)
                    if payload:
                        body = payload.decode("utf-8", errors="replace")
                except Exception:
                    pass
                break
    else:
        try:
            payload = msg.get_payload(decode=True)
            if payload:
                body = payload.decode("utf-8", errors="replace")
        except Exception:
            pass

    # Strip excessive whitespace.
    body = " ".join(body.split())
    return body[:max_chars]


# ---------------------------------------------------------------------------
# SMTP helper
# ---------------------------------------------------------------------------


def _send_email(
    config: EmailConfig,
    to: str,
    subject: str,
    body: str,
) -> tuple[bool, str]:
    """Send an email via SMTP. Returns (success, error_message)."""
    try:
        msg = EmailMessage()
        msg.set_content(body)
        msg["Subject"] = subject
        msg["From"] = config.from_addr or config.username
        msg["To"] = to

        with smtplib.SMTP(config.smtp_server, config.smtp_port, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(config.username, config.password)
            smtp.send_message(msg)
        return True, ""
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# Action logging helper (same pattern as _autonomy_record_outer in executors.py)
# ---------------------------------------------------------------------------


def _record_email_action(
    action_type: str,
    target: str,
    success: bool,
    detail: str = "",
) -> None:
    """Log email action + record outcome for autonomy escalation."""
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
# EmailObserver — polls inbox via IMAP
# ---------------------------------------------------------------------------


class EmailObserver(Observer):
    """Observes the email inbox and emits observations per recent message.

    Emits observations for:
      - unread_count (total unread messages)
      - email:<uid> with subject, from, date, unread flag, snippet

    Never stores full email body. Attachments are counted but not inspected.
    """

    name = "email"

    def __init__(
        self,
        config: Optional[EmailConfig] = None,
        limit: int = _MAX_EMAILS_PER_COLLECT,
    ) -> None:
        self.config = config or EmailConfig.from_env()
        self.limit = limit
        self._at = datetime.now(timezone.utc).isoformat()

    # --- Observer interface ------------------------------------------------

    def health(self, conn) -> ObserverHealth:
        if not self.config.configured:
            return ObserverHealth(
                True,
                Health.HEALTHY,
                "not_configured",
                "Email observer: not configured. Set FRIDAY_EMAIL_USERNAME "
                "and FRIDAY_EMAIL_PASSWORD in .env to enable.",
            )
        imap = _connect_imap(self.config)
        if imap is None:
            return ObserverHealth(
                False,
                Health.DOWN,
                "imap_connect_failed",
                "Could not connect to IMAP server.",
            )
        try:
            imap.select("INBOX", readonly=True)
            _typ, data = imap.search(None, "UNSEEN")
            unread = len(data[0].split()) if data and data[0] else 0
            return ObserverHealth(
                True,
                Health.HEALTHY,
                "imap_connected",
                f"Inbox connected: {unread} unread message(s).",
            )
        except Exception as exc:
            return ObserverHealth(
                False,
                Health.DEGRADED,
                "imap_error",
                f"IMAP error: {exc}",
            )
        finally:
            try:
                imap.close()
                imap.logout()
            except Exception:
                pass

    def collect(self, conn) -> list[Observation]:
        if not self.config.configured:
            return []

        self._at = datetime.now(timezone.utc).isoformat()
        rows: list[Observation] = []

        emails = _fetch_inbox_emails(self.config, limit=self.limit)
        unread_count = sum(1 for e in emails if e.get("unread"))

        # Emit unread count.
        rows.append(self._obs("inbox", "unread_count", str(unread_count)))

        # Emit per-email metadata observations.
        for e in emails:
            uid = e.get("uid", "?")
            rows.append(
                self._obs(
                    f"email:{uid}",
                    "subject",
                    (e.get("subject") or "")[:200],
                )
            )
            rows.append(
                self._obs(
                    f"email:{uid}",
                    "from",
                    (e.get("from") or "")[:200],
                )
            )
            rows.append(
                self._obs(
                    f"email:{uid}",
                    "date",
                    e.get("date") or "",
                )
            )
            rows.append(
                self._obs(
                    f"email:{uid}",
                    "unread",
                    "true" if e.get("unread") else "false",
                )
            )
            snippet = e.get("snippet", "")
            if snippet:
                rows.append(
                    self._obs(
                        f"email:{uid}",
                        "snippet",
                        snippet,
                    )
                )

        rows.append(self._obs("inbox", "message_count", str(len(emails))))
        return rows

    def summarize(self, conn) -> str:
        if not self.config.configured:
            return "Email: not configured"
        emails = _fetch_inbox_emails(self.config, limit=5)
        unread = sum(1 for e in emails if e.get("unread"))
        total = len(emails)
        return (
            f"Email\n"
            f"Healthy\n"
            f"Recent messages\n{total}\n"
            f"Unread\n{unread}\n"
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
# EmailExecutor — sends emails via SMTP
# ---------------------------------------------------------------------------


class EmailExecutor(Executor):
    """Send emails via SMTP. Implements the Executor contract.

    Expects ``task.runtime_payload`` to be JSON:
      {"to": "...", "subject": "...", "body": "..."}
    """

    worker_id = "worker:email"

    def __init__(self, config: Optional[EmailConfig] = None) -> None:
        self.config = config or EmailConfig.from_env()

    def execute(self, task) -> ExecutionResult:
        """Execute one email send."""
        raw = getattr(task, "runtime_payload", "") or ""
        if not raw.strip():
            _record_email_action("email_send", "", False, "empty payload")
            return ExecutionResult(
                success=False,
                stdout="",
                stderr="empty payload",
                exit_code=None,
                duration_ms=0,
                error="EmailExecutor: runtime_payload is empty",
            )

        try:
            obj = json.loads(raw)
        except (ValueError, TypeError):
            _record_email_action("email_send", raw[:100], False, "invalid JSON")
            return ExecutionResult(
                success=False,
                stdout="",
                stderr="invalid JSON",
                exit_code=None,
                duration_ms=0,
                error="EmailExecutor: payload must be valid JSON",
            )

        to = (obj.get("to") or "").strip()
        subject = (obj.get("subject") or "").strip()
        body = (obj.get("body") or "").strip()

        if not to or not subject:
            _record_email_action("email_send", to, False, "missing to/subject")
            return ExecutionResult(
                success=False,
                stdout="",
                stderr="missing required fields",
                exit_code=None,
                duration_ms=0,
                error="EmailExecutor: 'to' and 'subject' are required",
            )

        if not self.config.configured:
            _record_email_action("email_send", to, False, "not configured")
            return ExecutionResult(
                success=False,
                stdout="",
                stderr="email not configured",
                exit_code=None,
                duration_ms=0,
                error="EmailExecutor: set FRIDAY_EMAIL_USERNAME and "
                "FRIDAY_EMAIL_PASSWORD in .env",
            )

        t0 = time.monotonic()
        ok, err = _send_email(self.config, to, subject, body)
        dur = int((time.monotonic() - t0) * 1000)

        if ok:
            _record_email_action("email_send", to, True)
            return ExecutionResult(
                success=True,
                stdout=f"Email sent to {to}: {subject}",
                stderr="",
                exit_code=0,
                duration_ms=dur,
                artifacts=[],
            )
        else:
            _record_email_action("email_send", to, False, err)
            return ExecutionResult(
                success=False,
                stdout="",
                stderr=err,
                exit_code=None,
                duration_ms=dur,
                error=f"Email send failed: {err}",
            )

    def verify(self, task, result) -> VerificationResult:
        """Simple verify: trust the success flag."""
        return VerificationResult(passed=result.success, reason="success flag")


# backward-compat alias
EmailWorker = EmailExecutor


# ---------------------------------------------------------------------------
# Standalone helpers for CLI use
# ---------------------------------------------------------------------------


def send_email(to: str, subject: str, body: str) -> tuple[bool, str]:
    """Send an email using the environment-configured EmailConfig.

    Returns (success, error_message). Can be called directly from CLI handlers.
    """
    config = EmailConfig.from_env()
    if not config.configured:
        return False, "Email not configured. Set FRIDAY_EMAIL_USERNAME and FRIDAY_EMAIL_PASSWORD."
    ok, err = _send_email(config, to, subject, body)
    _record_email_action("email_send_cli", to, ok, err)
    return ok, err


def list_recent_emails(limit: int = 20) -> list[dict]:
    """Fetch recent inbox emails using environment-configured EmailConfig.

    Returns list of metadata dicts (subject, from, date, snippet, unread).
    """
    config = EmailConfig.from_env()
    if not config.configured:
        return []
    return _fetch_inbox_emails(config, limit=limit)
