"""Collaboration Layer — multi-instance coordination, team workspaces.

Solves V3's single-instance limitation by enabling multiple Friday instances
to observe, share, and sync across machines. Uses CRDT-based merge for
conflict-free observation synchronization.

Key capabilities:
    - Peer discovery via pure-stdlib UDP beacons
    - CRDT-based observation merge (Last-Writer-Wins)
    - Team workspace management
    - Permission and access control
    - Real-time sync via a pure-stdlib TCP protocol

**Status:** Wave 5 — shipped (2026-08). Pure stdlib: discovery and sync
replace the roadmap's zeroconf/WebSocket choices deliberately, matching
V4's "pure-stdlib, always works" law. The imports below stay guarded so
importing this package never crashes the rest of Friday V4.
"""

from __future__ import annotations

try:
    from .coordinator import Coordinator
    from .crdt import ObservationCRDT, merge_observations
    from .peer import PeerDiscovery, PeerInfo
    from .permissions import PermissionManager
    from .sync import SyncEngine
    _COLLAB_AVAILABLE = True
except ImportError:  # pragma: no cover - Wave 5 stub
    Coordinator = None  # type: ignore
    ObservationCRDT = None  # type: ignore
    merge_observations = None  # type: ignore
    PeerDiscovery = None  # type: ignore
    PeerInfo = None  # type: ignore
    PermissionManager = None  # type: ignore
    SyncEngine = None  # type: ignore
    _COLLAB_AVAILABLE = False


def is_available() -> bool:
    """Whether the collaboration layer is implemented yet."""
    return _COLLAB_AVAILABLE


__all__ = [
    "Coordinator",
    "ObservationCRDT",
    "merge_observations",
    "PeerDiscovery",
    "PeerInfo",
    "PermissionManager",
    "SyncEngine",
    "is_available",
    "_COLLAB_AVAILABLE",
]
