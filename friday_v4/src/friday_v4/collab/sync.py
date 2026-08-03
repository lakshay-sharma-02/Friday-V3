"""Sync engine — pure-stdlib TCP JSON-lines exchange for collaboration.

The roadmap named WebSockets here; V4's pure-stdlib law wins again, so
sync is a minimal TCP protocol with newline-delimited JSON frames. It is
deliberately small — observations are the only payload — and converges
because every replica speaks the same LWW CRDT (``collab.crdt``).

Wire protocol (one JSON object per line, ASCII-escaped, line-bounded):

    -> {"type": "hello", "peer_id", "workspace", "version": 1}
    <- {"type": "hello_ack", "accepted": true, "reason": ""}
    <- {"type": "obs_batch", "entries": [...]}      (server state)
    -> {"type": "obs_batch", "entries": [...]}      (client state)
    -> {"type": "obs_ack", "count": N, "applied": N}
    <- {"type": "bye", "reason": ""}

Both sides merge the other's batch after the handshake, so a single
``sync_with`` call converges two stores in both directions.
"""

from __future__ import annotations

import json
import logging
import socket
import socketserver
import threading
from typing import Callable, Optional

logger = logging.getLogger("friday_v4.collab.sync")

_PROTOCOL_VERSION = 1
#: Upper bound on a single frame (16 MB) — guards against memory bombs.
_MAX_FRAME = 16 * 1024 * 1024


class SyncError(Exception):
    """Raised when a sync exchange fails (handshake or frame errors)."""


# ---------------------------------------------------------------------------
# Frame I/O
# ---------------------------------------------------------------------------


class _LineReader:
    """Buffered newline frame reader for one connection.

    Holds leftover bytes across calls — critical, because two frames can
    arrive inside a single ``recv`` (very common on loopback), and a
    naive read-one-line implementation would silently drop the tail of
    the first frame and deadlock both peers.
    """

    def __init__(self, sock: socket.socket, max_len: int = _MAX_FRAME):
        self._sock = sock
        self._max_len = max_len
        self._buf = bytearray()

    def readline(self) -> str:
        while True:
            nl = self._buf.find(b"\n")
            if nl != -1:
                line = bytes(self._buf[:nl])
                del self._buf[:nl + 1]
                return line.decode("utf-8", "replace")
            if len(self._buf) > self._max_len:
                raise SyncError("frame too large")
            chunk = self._sock.recv(4096)
            if not chunk:
                raise SyncError("connection closed")
            self._buf.extend(chunk)


def _send_frame(sock: socket.socket, msg: dict) -> None:
    payload = json.dumps(msg, default=str).encode("utf-8")
    if len(payload) > _MAX_FRAME:
        raise SyncError("frame too large")
    sock.sendall(payload + b"\n")


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class _SyncHandler(socketserver.StreamRequestHandler):
    """One peer connection: handshake, exchange batches, merge, close."""

    #: A silent/malicious peer must not hold a handler thread forever.
    _READ_TIMEOUT = 15.0

    def handle(self) -> None:
        engine = self.server.engine  # type: ignore[attr-defined]
        self.connection.settimeout(self._READ_TIMEOUT)
        reader = _LineReader(self.connection)
        try:
            hello = json.loads(reader.readline())
        except (SyncError, OSError, json.JSONDecodeError) as exc:
            logger.debug(f"Collab handshake failed: {exc}")
            return
        if hello.get("type") != "hello":
            return
        peer_ok = engine.accepts(hello)
        _send_frame(self.connection, {
            "type": "hello_ack", "accepted": peer_ok,
            "reason": "" if peer_ok else "workspace mismatch or read denied",
        })
        if not peer_ok:
            return
        # Send our merged state first, then ingest theirs.
        _send_frame(self.connection, {
            "type": "obs_batch", "entries": engine.store.state(),
        })
        try:
            frame = json.loads(reader.readline())
        except (SyncError, OSError, json.JSONDecodeError) as exc:
            logger.debug(f"Collab frame read failed: {exc}")
            return
        if frame.get("type") != "obs_batch":
            return
        entries = frame.get("entries") or []
        applied = engine.store.merge(entries)
        _send_frame(self.connection, {
            "type": "obs_ack", "count": len(entries), "applied": applied,
        })
        engine.after_sync(applied, entries)


class _SyncServer(socketserver.ThreadingTCPServer):
    """Threaded TCP server that hands each peer to ``_SyncHandler``."""

    allow_reuse_address = True
    daemon_threads = True


# ---------------------------------------------------------------------------
# SyncEngine
# ---------------------------------------------------------------------------


class SyncEngine:
    """TCP server + client that converges a CRDT store across peers.

    Usage:
        engine = SyncEngine(store, peer_id="a", workspace="default")
        engine.start()                       # serve on port 9876
        other.sync_with("127.0.0.1", engine.bound_port)  # converge
        engine.stop()
    """

    def __init__(self, store, peer_id: str = "local",
                 workspace: str = "default", host: str = "0.0.0.0",
                 port: int = 9876,
                 accepts: Optional[Callable[[dict], bool]] = None,
                 after_sync: Optional[Callable[[int, list], None]] = None):
        self.store = store
        self.peer_id = peer_id
        self.workspace = workspace
        self.host = host
        self.port = port
        self._accepts = accepts
        self._after_sync = after_sync or (lambda n, entries: None)
        self._server: Optional[_SyncServer] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def bound_port(self) -> Optional[int]:
        """The actual bound port (real port when ``port=0``)."""
        if self._server is None:
            return None
        return self._server.server_address[1]

    def accepts(self, hello: dict) -> bool:
        """Whether to accept a peer's hello (workspace + ACL gate)."""
        if hello.get("workspace") != self.workspace:
            return False
        if self._accepts is not None:
            try:
                return bool(self._accepts(hello))
            except Exception as exc:
                logger.debug(f"Collab accept check failed: {exc}")
                return False
        return True

    # ── Server ─────────────────────────────────────────────────────

    def start(self) -> bool:
        if self._server is not None:
            return True
        try:
            self._server = _SyncServer((self.host, self.port),
                                       _SyncHandler)
            self._server.engine = self  # type: ignore[attr-defined]
        except OSError as exc:
            logger.warning(f"Collab sync bind failed: {exc}")
            self._server = None
            return False
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="friday-collab-sync", daemon=True)
        self._thread.start()
        logger.info(f"Collab sync serving on {self.host}:{self.bound_port}")
        return True

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None

    # ── Client ─────────────────────────────────────────────────────

    def sync_with(self, host: str, port: int,
                  timeout: float = 10.0) -> dict:
        """One bidirectional sync against a peer; returns exchange stats.

        Raises :class:`SyncError` on handshake/frame failure. A peer that
        rejects us (workspace mismatch / read denied) returns
        ``{"accepted": False, ...}`` instead of raising.
        """
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.settimeout(timeout)
        reader = _LineReader(sock)
        result: dict = {"accepted": False, "sent": 0, "received": 0,
                        "applied": 0}
        try:
            _send_frame(sock, {
                "type": "hello", "peer_id": self.peer_id,
                "workspace": self.workspace,
                "version": _PROTOCOL_VERSION,
            })
            ack = json.loads(reader.readline())
            if ack.get("type") != "hello_ack" or not ack.get("accepted"):
                result["reason"] = ack.get("reason", "rejected")
                return result
            result["accepted"] = True
            remote_batch = json.loads(reader.readline())
            if remote_batch.get("type") != "obs_batch":
                raise SyncError("expected obs_batch from peer")
            entries = remote_batch.get("entries") or []
            result["received"] = len(entries)
            result["applied"] = self.store.merge(entries)
            my_batch = self.store.state()
            _send_frame(sock, {"type": "obs_batch", "entries": my_batch})
            result["sent"] = len(my_batch)
            ack2 = json.loads(reader.readline())
            if ack2.get("type") != "obs_ack":
                raise SyncError("expected obs_ack from peer")
            self._after_sync(result["applied"], entries)
            return result
        finally:
            sock.close()

    def __repr__(self) -> str:
        return (f"<SyncEngine {self.peer_id} ws={self.workspace} "
                f"port={self.bound_port or self.port}>")
