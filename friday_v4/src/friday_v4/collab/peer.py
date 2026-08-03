"""Peer discovery — pure-stdlib UDP beacons for the collaboration layer.

The roadmap named zeroconf (mDNS) here; V4's pure-stdlib law (the same
reason the web dashboard is stdlib ``http.server``) wins, so discovery is
a tiny UDP beacon protocol instead — no dependencies, works on any LAN:

- Every instance announces itself on a broadcast address every few
  seconds with a JSON beacon::

      {"type": "friday-collab", "version": 1, "peer_id": "...",
       "hostname": "...", "port": 9876, "workspace": "default",
       "sent_at": 1754000000.0}

- A listener thread bound to ``beacon_port`` collects beacons into a
  peer table; entries expire after ``peer_ttl`` seconds so dead peers
  drop out on their own.

The socket is injectable/optional so tests can drive ``_handle_beacon``
and ``announce_to`` without kernel multicast semantics, and two
instances on loopback can discover each other deterministically.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("friday_v4.collab.peer")

#: Beacon wire protocol marker (guards against stray UDP noise).
_BEACON_TYPE = "friday-collab"
_BEACON_VERSION = 1

#: Where to broadcast on a normal LAN (configurable).
_DEFAULT_BROADCAST = "255.255.255.255"
#: Default beacon listener port.
_DEFAULT_BEACON_PORT = 9988


@dataclass
class PeerInfo:
    """A live Friday instance discovered on the LAN."""

    peer_id: str
    host: str = ""
    port: int = 0                 # the peer's sync (TCP) port
    workspace: str = "default"
    hostname: str = ""
    version: int = _BEACON_VERSION
    last_seen: float = 0.0
    extra: dict = field(default_factory=dict)

    def is_fresh(self, ttl: float, now: Optional[float] = None) -> bool:
        """Whether the peer was heard from within ``ttl`` seconds."""
        now = time.time() if now is None else now
        return (now - self.last_seen) < ttl

    def to_dict(self) -> dict:
        return {
            "peer_id": self.peer_id,
            "host": self.host,
            "port": self.port,
            "workspace": self.workspace,
            "hostname": self.hostname,
            "version": self.version,
            "last_seen": self.last_seen,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PeerInfo":
        return cls(
            peer_id=str(data.get("peer_id", "")),
            host=str(data.get("host", "")),
            port=int(data.get("port", 0) or 0),
            workspace=str(data.get("workspace", "default")),
            hostname=str(data.get("hostname", "")),
            version=int(data.get("version", _BEACON_VERSION)),
            last_seen=float(data.get("last_seen", 0.0) or 0.0),
        )

    def __repr__(self) -> str:
        return (f"<PeerInfo {self.peer_id} @ {self.host}:{self.port} "
                f"ws={self.workspace} seen={self.last_seen:.0f}>")


class PeerDiscovery:
    """UDP beacon announcer + listener with a self-expiring peer table."""

    def __init__(self, peer_id: str, workspace: str = "default",
                 sync_port: int = 9876, beacon_port: int = _DEFAULT_BEACON_PORT,
                 broadcast_addr: str = _DEFAULT_BROADCAST,
                 listen_addr: str = "",
                 announce_interval: float = 3.0, peer_ttl: float = 12.0,
                 sock: Optional[socket.socket] = None):
        self.peer_id = peer_id
        self.workspace = workspace
        self.sync_port = sync_port
        self.beacon_port = beacon_port
        self.broadcast_addr = broadcast_addr
        self.listen_addr = listen_addr
        self.announce_interval = announce_interval
        self.peer_ttl = peer_ttl
        self._sock = sock
        #: The port actually bound (real port when ``beacon_port=0``),
        #: useful for tests and for telling peers where to reach us.
        self.bound_beacon_port: int = beacon_port
        self._peers: dict[str, PeerInfo] = {}
        self._lock = threading.Lock()
        self.running = False
        self._announcer: Optional[threading.Thread] = None
        self._listener: Optional[threading.Thread] = None
        self._owns_sock = sock is None

    # ── Beacon building ────────────────────────────────────────────

    def _beacon(self) -> dict:
        return {
            "type": _BEACON_TYPE,
            "version": _BEACON_VERSION,
            "peer_id": self.peer_id,
            "hostname": socket.gethostname(),
            "port": self.sync_port,
            "workspace": self.workspace,
            "sent_at": time.time(),
        }

    def _datagram(self) -> bytes:
        return json.dumps(self._beacon()).encode("utf-8")

    # ── Sending ────────────────────────────────────────────────────

    def announce(self) -> bool:
        """Broadcast one beacon to the configured broadcast address."""
        if not self._sock:
            return False
        return self._send_to(self.broadcast_addr, self.beacon_port)

    def _send_to(self, host: str, port: int) -> bool:
        try:
            self._sock.sendto(self._datagram(), (host, port))
            return True
        except OSError as exc:
            logger.debug(f"Beacon send failed: {exc}")
            return False

    # ── Receiving ──────────────────────────────────────────────────

    def _handle_beacon(self, data: bytes, addr: tuple) -> bool:
        """Parse + upsert a beacon from ``addr``; True if accepted."""
        try:
            msg = json.loads(data.decode("utf-8", "replace"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return False
        if msg.get("type") != _BEACON_TYPE:
            return False
        peer_id = msg.get("peer_id")
        if not peer_id or peer_id == self.peer_id:
            return False  # ignore self-beacons
        info = PeerInfo(
            peer_id=str(peer_id),
            host=str(addr[0]),
            port=int(msg.get("port", 0) or 0),
            workspace=str(msg.get("workspace", "default")),
            hostname=str(msg.get("hostname", "")),
            version=int(msg.get("version", 1)),
            last_seen=float(msg.get("sent_at", time.time()) or time.time()),
        )
        with self._lock:
            self._peers[peer_id] = info
        logger.debug(f"Discovered peer {peer_id} @ {addr[0]}:{info.port}")
        return True

    def _listen_loop(self) -> None:
        while self.running:
            try:
                data, addr = self._sock.recvfrom(4096)
            except OSError:
                return  # socket closed on stop()
            self._handle_beacon(data, addr)

    def _announce_loop(self) -> None:
        while self.running:
            self.announce()
            # Short waits keep stop() responsive.
            for _ in range(max(int(self.announce_interval / 0.25), 1)):
                if not self.running:
                    return
                time.sleep(0.25)

    # ── Reads ──────────────────────────────────────────────────────

    def peers(self) -> list[PeerInfo]:
        """Live (non-expired) discovered peers, oldest last_seen first."""
        now = time.time()
        with self._lock:
            live = [p for p in self._peers.values()
                    if p.is_fresh(self.peer_ttl, now)]
        live.sort(key=lambda p: p.last_seen)
        return live

    def peer_count(self) -> int:
        return len(self.peers())

    # ── Lifecycle ──────────────────────────────────────────────────

    def start(self) -> bool:
        """Open the UDP socket and start announcer + listener threads."""
        if self.running:
            return True
        if self._owns_sock:
            try:
                self._sock = socket.socket(socket.AF_INET,
                                           socket.SOCK_DGRAM)
                self._sock.setsockopt(socket.SOL_SOCKET,
                                      socket.SO_REUSEADDR, 1)
                self._sock.bind((self.listen_addr, self.beacon_port))
                self.bound_beacon_port = self._sock.getsockname()[1]
            except OSError as exc:
                logger.warning(f"Beacon bind failed: {exc}")
                return False
        self.running = True
        self._listener = threading.Thread(
            target=self._listen_loop, name="friday-collab-listen",
            daemon=True)
        self._listener.start()
        self._announcer = threading.Thread(
            target=self._announce_loop, name="friday-collab-announce",
            daemon=True)
        self._announcer.start()
        return True

    def stop(self) -> None:
        self.running = False
        if self._owns_sock and self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        for thread in (self._listener, self._announcer):
            if thread is not None:
                thread.join(timeout=max(self.announce_interval + 1, 3))

    def __repr__(self) -> str:
        return (f"<PeerDiscovery {self.peer_id} ws={self.workspace} "
                f"running={self.running} peers={self.peer_count()}>")
