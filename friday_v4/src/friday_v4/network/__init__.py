"""Network & Remote Access — SSH, webhooks, cloud API integration, DB client.

Extends Friday's reach beyond the local machine. Enables SSH into remote
servers, listening for webhooks, interacting with cloud APIs, and querying
remote databases directly from Friday's execution pipeline.

Capabilities:
    - SSH executor: run commands on remote machines ✅ (Wave 12 — lives in
      ``execution/executors.py`` as the ``ssh`` action type, behind the
      same gate → sandbox → audit pipeline)
    - Webhook listener: receive events from GitHub, CI/CD, etc. (future)
    - Cloud API integration: AWS, GCP, Azure (future)
    - Database client: query Postgres, MySQL, SQLite (future)

**Status:** SSH folded into the execution layer (Wave 12 decision in
WAVE_11 doc §8). Cloud/webhook/db-client remain future work. The imports
below are guarded so importing this package never crashes Friday V4.
"""

from __future__ import annotations

try:
    from .cloud import CloudAPIClient
    from .db_client import DatabaseClient, QueryResult
    from .webhook import WebhookEvent, WebhookListener
    _NETWORK_AVAILABLE = True
except ImportError:  # pragma: no cover - stub
    WebhookListener = None  # type: ignore
    WebhookEvent = None  # type: ignore
    CloudAPIClient = None  # type: ignore
    DatabaseClient = None  # type: ignore
    QueryResult = None  # type: ignore
    _NETWORK_AVAILABLE = False

#: SSHExecutor is imported from the execution layer (Wave 12 fold-in),
#: never from this stub — the names stay importable for compatibility.
def _ssh_executor():
    try:
        from ..execution.executors import SSHExecutor
        return SSHExecutor
    except Exception:
        return None

SSHExecutor = _ssh_executor()  # type: ignore
SSHConnection = None  # type: ignore  # SSHConnection class does not exist


def is_available() -> bool:
    """Whether any network & remote-access surface is implemented.

    True when the SSH executor is registered in the execution layer OR
    the cloud/webhook/db-client modules are importable. Consumers (the
    daemon's status, ``friday4 doctor``) treat this as "can Friday reach
    beyond this machine?".
    """
    return ssh_available() or _NETWORK_AVAILABLE


def ssh_available() -> bool:
    """Whether the SSH executor is registered in the execution layer.

    Wave 12 folded SSH into ``execution/`` (the ``ssh`` action type),
    so this reflects the executor registry rather than this stub's
    importability. Never raises.
    """
    try:
        from ..execution.executors import _EXECUTORS
        return "ssh" in _EXECUTORS
    except Exception:
        return False


__all__ = [
    "SSHExecutor",
    "SSHConnection",
    "WebhookListener",
    "WebhookEvent",
    "CloudAPIClient",
    "DatabaseClient",
    "QueryResult",
    "is_available",
    "ssh_available",
    "_NETWORK_AVAILABLE",
]
