"""Coordinator — ties discovery, sync, CRDT store, and ACLs together.

This is the single entry point the daemon/CLI interact with. One
Coordinator per Friday instance owns:

    store        — the LWW observation CRDT
    permissions  — the workspace ACL
    discovery    — UDP beacons (presence on the LAN)
    sync         — TCP server + client (observation exchange)

State persists to ``~/.friday/collab/state.json`` so a stopped instance
keeps its merged observations, ACLs, and last-known peers. Every public
method degrades gracefully (never raises) — the daemon law.

Pure stdlib; no dependencies.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import uuid
from pathlib import Path
from typing import Optional

from .crdt import ObservationCRDT
from .peer import PeerDiscovery, PeerInfo
from .permissions import PermissionManager
from .sync import SyncEngine

logger = logging.getLogger("friday_v6.collab.coordinator")

_DEFAULT_STATE_DIR = Path.home() / ".friday" / "collab"
_DEFAULT_SYNC_PORT = 9876
_DEFAULT_BEACON_PORT = 9988
_STATE_FILE = "state.json"
#: Bound on persisted merged entries (protects the state file).
_MAX_ENTRIES = 10_000


def default_peer_id() -> str:
    """Stable local peer id: ``hostname`` (falls back to ``localhost``)."""
    try:
        return socket.gethostname() or "localhost"
    except OSError:
        return "localhost"


class Coordinator:
    """The collaboration surface for one Friday instance."""

    def __init__(self, peer_id: Optional[str] = None,
                 workspace: str = "default",
                 sync_port: int = _DEFAULT_SYNC_PORT,
                 beacon_port: int = _DEFAULT_BEACON_PORT,
                 state_dir: Optional[Path] = None,
                 store: Optional[ObservationCRDT] = None,
                 permissions: Optional[PermissionManager] = None,
                 discovery: Optional[PeerDiscovery] = None,
                 sync: Optional[SyncEngine] = None,
                 bus=None):
        self.state_dir = state_dir or _DEFAULT_STATE_DIR
        self._state_file = self.state_dir / _STATE_FILE
        self._lock = threading.Lock()
        #: Last-known peers, hydrated from disk so `collab peers`/`status`
        #: stay useful after a restart (discovery is not running then).
        self._last_peers: list[PeerInfo] = []
        self._stored_peer_id = self._read_stored_peer_id()
        self.peer_id = peer_id or self._stored_peer_id \
            or self._new_peer_id()
        self.workspace = workspace
        self.sync_port = sync_port
        self.beacon_port = beacon_port
        self.store = store or ObservationCRDT(peer_id=self.peer_id)
        self.permissions = permissions or PermissionManager(
            workspace=workspace, owner_peer_id=self.peer_id)
        self.discovery = discovery
        self.sync = sync
        #: Optional shared Wave 11 AmbientBus — peer observations fan out
        #: to voice/desktop/web without coupling sync to any surface.
        #: Single direction: a short summary is published (permissions are
        #: already respected at merge time), never raw content.
        self._bus = bus
        self._load()
        self.running = False

    # ── Lifecycle ──────────────────────────────────────────────────

    def start(self) -> bool:
        """Start the sync server and peer discovery (idempotent)."""
        if self.running:
            return True
        if self.sync is None:
            self.sync = SyncEngine(
                store=self.store, peer_id=self.peer_id,
                workspace=self.workspace, port=self.sync_port,
                accepts=lambda hello: self.permissions.can_read(
                    hello.get("peer_id", "")),
            )
        if not self.sync.start():
            logger.warning("Collab sync server failed to bind — "
                           "discovery continues but peers cannot connect")
        if self.discovery is None:
            self.discovery = PeerDiscovery(
                peer_id=self.peer_id, workspace=self.workspace,
                sync_port=self.sync_port, beacon_port=self.beacon_port,
            )
        self.discovery.start()
        self.running = True
        return True

    def stop(self) -> None:
        """Stop sync + discovery and persist the merged state."""
        # Capture live peers BEFORE discovery shuts down — once stopped,
        # peers() falls back to _last_peers, and we want the last-known
        # peers persisted so `collab peers`/`status` stay useful after.
        self._last_peers = self.peers() or self._last_peers
        if self.sync is not None:
            self.sync.stop()
        if self.discovery is not None:
            self.discovery.stop()
        self.running = False
        self._save()

    # ── Writes ─────────────────────────────────────────────────────

    def add_observation(self, payload: dict,
                        obs_id: Optional[str] = None) -> Optional[str]:
        """Record a local observation into the CRDT + persist it."""
        with self._lock:
            obs_id = self.store.add(payload, obs_id=obs_id,
                                    peer_id=self.peer_id)
        self._save()
        if obs_id:
            self._publish_collab(payload)
        return obs_id

    def merge_entries(self, entries: list) -> int:
        """Merge a remote batch; returns the number of changes applied."""
        with self._lock:
            applied = self.store.merge(entries)
        if applied:
            self._save()
            self._publish_collab({"applied": applied}, merged=True)
        return applied

    def _publish_collab(self, payload: dict, merged: bool = False) -> None:
        """Publish a short, permission-safe summary onto the shared bus.

        Never raises and never includes raw observation content — the
        bus carries only "who/what/why count" so the web dashboard and
        briefings can say "2 new peer observations" without leaking the
        workspace ACL's protected data.
        """
        if self._bus is None:
            return
        try:
            from ..ambient import Event, Priority
            if merged:
                text = (f"{payload.get('applied', 0)} peer observation(s) "
                        f"merged in {self.workspace}")
            else:
                summary = payload.get("source") or payload.get("subject") \
                    or ""
                if summary:
                    text = (f"observation recorded ({summary}) in "
                            f"{self.workspace}")
                else:
                    text = f"observation recorded in {self.workspace}"
            self._bus.publish(Event(
                topic="collab",
                payload=text,
                priority=Priority.ROUTINE,
                source="collab"))
        except Exception as exc:
            logger.debug(f"collab bus publish failed: {exc}")

    def add_member(self, peer_id: str, role: str = "member") -> None:
        self.permissions.add_member(peer_id, role)
        self._save()

    def remove_member(self, peer_id: str) -> bool:
        changed = self.permissions.remove_member(peer_id)
        if changed:
            self._save()
        return changed

    # ── Reads ──────────────────────────────────────────────────────

    def observations(self, limit: Optional[int] = None) -> list[dict]:
        return self.store.observations(limit)

    def peers(self) -> list[PeerInfo]:
        """Live discovered peers, or last-known peers when discovery is off.

        ``friday6 collab peers``/``status`` run without starting discovery,
        so they surface the last-known peers persisted by a prior run.
        """
        if self.discovery is not None and self.discovery.running:
            return self.discovery.peers()
        return list(self._last_peers)

    # ── Sync ───────────────────────────────────────────────────────

    def sync_once(self) -> dict:
        """Pull from every live peer and merge (aggregate stats)."""
        if self.sync is None or self.discovery is None:
            return {"peers": 0, "sent": 0, "received": 0, "applied": 0,
                    "accepted": 0}
        total = {"peers": 0, "sent": 0, "received": 0, "applied": 0,
                 "accepted": 0}
        for peer in self.discovery.peers():
            if not peer.port:
                continue
            try:
                result = self.sync.sync_with(peer.host, peer.port)
            except Exception as exc:
                logger.debug(f"Sync with {peer.peer_id} failed: {exc}")
                continue
            total["peers"] += 1
            for key in ("sent", "received", "applied"):
                total[key] += result.get(key, 0)
            if result.get("accepted"):
                total["accepted"] += 1
        if total["received"] or total["applied"]:
            self._save()
        return total

    # ── Status ─────────────────────────────────────────────────────

    def status(self) -> dict:
        """Snapshot for ``friday6 collab status`` (never raises)."""
        return {
            "peer_id": self.peer_id,
            "workspace": self.workspace,
            "running": self.running,
            "sync_port": self.sync_port,
            "beacon_port": self.beacon_port,
            "peers": [p.to_dict() for p in self.peers()],
            "observations": len(self.store),
            "live_observations": len(self.store.observations()),
            "permissions": self.permissions.serialize(),
        }

    # ── Persistence ────────────────────────────────────────────────

    def _read_stored_peer_id(self) -> Optional[str]:
        """Peer id persisted by a previous run (stable identity)."""
        try:
            if self._state_file.exists():
                data = json.loads(self._state_file.read_text())
                return data.get("peer_id") or None
        except (OSError, json.JSONDecodeError, ValueError):
            pass
        return None

    def _new_peer_id(self) -> str:
        """Hostname + short random suffix — two machines named the same
        must not collapse into one peer (the peer table is keyed by id)."""
        return f"{default_peer_id()}-{uuid.uuid4().hex[:4]}"

    def _load(self) -> None:
        """Restore entries, ACLs, and last-known peers from disk."""
        try:
            if not self._state_file.exists():
                return
            data = json.loads(self._state_file.read_text())
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            logger.debug(f"Collab state unreadable: {exc}")
            return
        try:
            entries = data.get("entries") or []
            self.store.merge(entries[:_MAX_ENTRIES])
            perms_data = data.get("permissions")
            if perms_data:
                self.permissions.merge(
                    PermissionManager.from_dict(perms_data))
            self._last_peers = [
                PeerInfo.from_dict(p) for p in (data.get("last_peers") or [])
            ]
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug(f"Collab state load failed: {exc}")

    def _save(self) -> None:
        """Persist merged state (bounded, best-effort)."""
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            entries = self.store.state()[-_MAX_ENTRIES:]
            payload = {
                "peer_id": self.peer_id,
                "workspace": self.workspace,
                "entries": entries,
                "permissions": self.permissions.serialize(),
                "last_peers": [p.to_dict() for p in self.peers()],
            }
            self._state_file.write_text(json.dumps(payload))
        except OSError as exc:  # pragma: no cover - defensive
            logger.debug(f"Collab state save failed: {exc}")

    def __repr__(self) -> str:
        return (f"<Coordinator {self.peer_id} ws={self.workspace} "
                f"running={self.running} obs={len(self.store)}>")
