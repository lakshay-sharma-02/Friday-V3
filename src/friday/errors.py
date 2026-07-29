"""Friday's typed error envelope — structured context at every layer boundary.

Every exception caught in the codebase should be wrapped in a ``FridayError``
before it propagates. This lets Friday say things like:

    "Auth token expired, requesting new one, ETA 30s."
    "Shell command timed out after 60s. The build script is hanging on a
     network call. Try adding a shorter timeout to the fetch step."
    "File not found: /tmp/build.yml. Did you delete the build directory?"

Instead of:

    "Exception: command timed out"
    "Error: [Errno 2] No such file or directory"

Usage::

    from .errors import FridayError, ErrorType, friday_error

    try:
        ...
    except subprocess.TimeoutExpired as exc:
        raise friday_error(
            ErrorType.TIMEOUT,
            action="run_build",
            target="./build.sh",
            message="Build script timed out after 60s.",
            eta_hint="30s if interrupted and restarted",
            recovery_hint="Add a shorter timeout to the network fetch step",
            cause=exc,
        )
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ErrorType(str, Enum):
    """Classification of the kind of error that occurred.

    Maps to the human-readable error type Friday should mention first.
    """

    # ── Authentication / authorization ──
    AUTH_ERROR = "auth_error"
    TOKEN_EXPIRED = "token_expired"
    PERMISSION_DENIED = "permission_denied"
    RATE_LIMITED = "rate_limited"

    # ── Network / connectivity ──
    NETWORK_ERROR = "network_error"
    TIMEOUT = "timeout"
    API_ERROR = "api_error"
    CONNECTION_REFUSED = "connection_refused"
    DNS_FAILURE = "dns_failure"

    # ── Filesystem ──
    NOT_FOUND = "not_found"
    FILE_EXISTS = "file_exists"
    PERMISSION_ERROR = "permission_error"
    DISK_FULL = "disk_full"

    # ── Subprocess / execution ──
    SUBPROCESS_ERROR = "subprocess_error"
    EXIT_CODE = "exit_code"
    COMMAND_NOT_FOUND = "command_not_found"

    # ── Data / validation ──
    VALIDATION_ERROR = "validation_error"
    PARSE_ERROR = "parse_error"
    SCHEMA_ERROR = "schema_error"

    # ── LLM / AI ──
    LLM_ERROR = "llm_error"
    LLM_DISABLED = "llm_disabled"
    LLM_PARSE_ERROR = "llm_parse_error"

    # ── Internal ──
    UNEXPECTED = "unexpected"
    NOT_IMPLEMENTED = "not_implemented"
    BUG = "bug"


@dataclass
class FridayError(Exception):
    """Structured error envelope for every layer boundary.

    Every caught exception across daemon.py, workers.py, dispatcher.py, llm.py,
    and the notification engine should be wrapped in this type so Friday can:

    1. State the problem precisely ("auth token expired")
    2. State what she's doing about it ("requesting new one")
    3. Give a time estimate ("ETA 30s")
    4. Offer a recovery hint ("try adding a shorter timeout")

    Fields
    ------
    error_type:
        Classification — one of the ``ErrorType`` values.
    action:
        What Friday was doing when the error occurred
        (e.g. ``"run_build"``, ``"observe_workspace"``).
    target:
        What was being acted upon (e.g. ``"./build.sh"``, ``"workspace:3"``).
    message:
        Human-readable description of what happened. This is the primary
        string shown to the operator.
    eta_hint:
        Optional recovery time estimate (e.g. ``"30s"``, ``"~2 minutes"``).
        When set, ``format_friday_error()`` includes it as "ETA {eta_hint}".
    recovery_hint:
        Optional suggestion for what the operator can do next. Displayed
        after the primary message.
    cause:
        The original exception that triggered this error. Preserved for
        traceback introspection.
    details:
        Optional dict of structured key-value pairs for programmatic
        consumers (notifications, feed events, dashboards).
    """

    error_type: ErrorType = ErrorType.UNEXPECTED
    action: str = ""
    target: str = ""
    message: str = ""
    eta_hint: str = ""
    recovery_hint: str = ""
    cause: Optional[BaseException] = None
    details: dict = field(default_factory=dict)

    def __str__(self) -> str:
        """Return the canonical formatted message.

        ``str(friday_error)`` is what eventually lands in ``str(exc)[:500]``
        in the daemon log — so it's concise but informative.
        """
        return format_friday_error(self)

    def to_dict(self) -> dict:
        """Serialise to a plain dict for JSON/logging."""
        return {
            "error_type": self.error_type.value,
            "action": self.action,
            "target": self.target,
            "message": self.message,
            "eta_hint": self.eta_hint,
            "recovery_hint": self.recovery_hint,
            "details": dict(self.details),
        }


def friday_error(
    error_type: ErrorType,
    action: str = "",
    target: str = "",
    message: str = "",
    eta_hint: str = "",
    recovery_hint: str = "",
    cause: Optional[BaseException] = None,
    details: Optional[dict] = None,
) -> FridayError:
    """Factory helper — creates a ``FridayError`` with a short message.

    ``message`` is the primary user-facing description. Where possible, include
    the error type and actionable context in the message so the operator knows
    what happened and what to do next, even before reading the typed fields.

    Example::

        raise friday_error(
            ErrorType.TOKEN_EXPIRED,
            action="telegram_send",
            target="chat:12345",
            message="Telegram bot token expired. Requesting new one, ETA 30s.",
            eta_hint="30s",
            recovery_hint="Run `friday doctor` to refresh credentials.",
            cause=exc,
        )
    """
    return FridayError(
        error_type=error_type,
        action=action,
        target=target,
        message=message,
        eta_hint=eta_hint,
        recovery_hint=recovery_hint,
        cause=cause,
        details=details or {},
    )


def format_friday_error(err: FridayError) -> str:
    """Format a ``FridayError`` for user-facing display.

    Produces a concise, structured message like:

        Auth token expired. Action: telegram_send. ETA 30s. Recovery: Run
        `friday doctor` to refresh credentials.

    Or when fields are sparse:

        Subprocess error (shell): command timed out after 60s.
    """
    parts: list[str] = []

    if err.message:
        parts.append(err.message.rstrip("."))

    # Add action/target context (not redundant with message).
    if err.action and err.action not in (err.message or ""):
        ctx = err.action
        if err.target:
            ctx += f" ({err.target})"
        parts.append(f"Action: {ctx}")

    if err.eta_hint:
        parts.append(f"ETA: {err.eta_hint}")

    if err.recovery_hint:
        recovery = err.recovery_hint.rstrip(".")
        parts.append(f"Recovery: {recovery}")

    if not parts:
        # Fallback: at least show the error type.
        parts.append(f"Error: {err.error_type.value}")

    return ". ".join(parts) + "."


def error_from_exception(
    exc: BaseException,
    action: str = "",
    target: str = "",
    recovery_hint: str = "",
) -> FridayError:
    """Wrap an arbitrary exception into a ``FridayError`` with auto-classification.

    Attempts to classify common exception types (``TimeoutExpired``,
    ``FileNotFoundError``, ``PermissionError``, etc.) into the appropriate
    ``ErrorType``.

    This is the function to use in blanket ``except Exception:`` blocks that
    want to preserve as much context as possible::

        try:
            ...
        except Exception as exc:
            raise error_from_exception(
                exc,
                action="observe_workspace",
                target="repo:my-project",
            ) from exc
    """
    exc_type = type(exc).__name__
    exc_str = str(exc) or f"{exc_type} (no message)"

    # Classify by type name --- works across stdlib and common wrapping.
    type_name = exc_type.lower()

    is_timeout = (
        "timeout" in type_name
        or "timeout" in exc_str.lower()
    )
    is_not_found = (
        isinstance(exc, FileNotFoundError)
        or "not found" in exc_str.lower()
        or "no such file" in exc_str.lower()
    )
    is_permission = (
        isinstance(exc, PermissionError)
        or "permission" in type_name
        or "permission denied" in exc_str.lower()
    )
    is_auth = (
        "auth" in type_name
        or "token" in type_name
        or "unauthorized" in exc_str.lower()
        or "401" in exc_str
        or "403" in exc_str
    )
    is_connection = (
        isinstance(exc, ConnectionError)
        or "connection" in type_name
        or "connection refused" in exc_str.lower()
        or "connection reset" in exc_str.lower()
    )

    if is_auth:
        error_type = ErrorType.AUTH_ERROR
    elif is_timeout:
        error_type = ErrorType.TIMEOUT
    elif is_connection:
        error_type = ErrorType.CONNECTION_REFUSED
    elif is_not_found:
        error_type = ErrorType.NOT_FOUND
    elif is_permission:
        error_type = ErrorType.PERMISSION_ERROR
    else:
        error_type = ErrorType.UNEXPECTED

    return friday_error(
        error_type=error_type,
        action=action,
        target=target,
        message=f"{exc_type}: {exc_str[:300]}",
        recovery_hint=recovery_hint,
        cause=exc,
    )


# ── Deprecated alias compatibility ──
ErrorEnvelope = FridayError
