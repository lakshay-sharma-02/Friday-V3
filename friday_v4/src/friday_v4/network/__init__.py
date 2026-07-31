"""Network & Remote Access — SSH, webhooks, cloud API integration, DB client.

Extends Friday's reach beyond the local machine. Enables SSH into remote
servers, listening for webhooks, interacting with cloud APIs, and querying
remote databases directly from Friday's execution pipeline.

Capabilities:
    - SSH executor: run commands on remote machines
    - Webhook listener: receive events from GitHub, CI/CD, etc.
    - Cloud API integration: AWS, GCP, Azure
    - Database client: query Postgres, MySQL, SQLite

**Status:** not yet implemented. The imports below are guarded so importing
this package never crashes the rest of Friday V4.
"""

from __future__ import annotations

try:
    from .ssh import SSHExecutor, SSHConnection
    from .webhook import WebhookListener, WebhookEvent
    from .cloud import CloudAPIClient
    from .db_client import DatabaseClient, QueryResult
    _NETWORK_AVAILABLE = True
except ImportError:  # pragma: no cover - stub
    SSHExecutor = None  # type: ignore
    SSHConnection = None  # type: ignore
    WebhookListener = None  # type: ignore
    WebhookEvent = None  # type: ignore
    CloudAPIClient = None  # type: ignore
    DatabaseClient = None  # type: ignore
    QueryResult = None  # type: ignore
    _NETWORK_AVAILABLE = False


def is_available() -> bool:
    """Whether the network & remote access layer is implemented yet."""
    return _NETWORK_AVAILABLE


__all__ = [
    "SSHExecutor",
    "SSHConnection",
    "WebhookListener",
    "WebhookEvent",
    "CloudAPIClient",
    "DatabaseClient",
    "QueryResult",
    "is_available",
    "_NETWORK_AVAILABLE",
]
