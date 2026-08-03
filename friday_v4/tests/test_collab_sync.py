"""Tests for collab.sync — TCP JSON-lines sync engine (real loopback)."""

from __future__ import annotations

from friday_v4.collab.crdt import ObservationCRDT
from friday_v4.collab.sync import SyncEngine, SyncError


def _store(peer_id, payloads):
    store = ObservationCRDT(peer_id=peer_id)
    for i, payload in enumerate(payloads):
        store.add(payload, ts=100 + i)
    return store


class _FakeSock:
    """A socket whose recv behavior is scriptable.

    ``chunk_size=None`` returns all queued bytes at once — the exact
    condition that used to deadlock the protocol (two frames in one
    recv losing the second frame's tail). A small ``chunk_size`` makes
    recv dribble bytes so a single frame genuinely splits across calls.
    """

    def __init__(self, data: bytes, chunk_size: int | None = None):
        self._data = data
        self._chunk_size = chunk_size

    def recv(self, _n):
        if not self._data:
            return b""
        if self._chunk_size is None:
            chunk = self._data
            self._data = b""
        else:
            chunk = self._data[:self._chunk_size]
            self._data = self._data[self._chunk_size:]
        return chunk


def test_line_reader_handles_coalesced_frames():
    """Two frames arriving in a single recv must both be readable — a
    naive read-one-line implementation drops the second frame's tail and
    deadlocks both peers."""
    from friday_v4.collab.sync import _LineReader
    data = (
        b'{"type": "hello_ack", "accepted": true}\n'
        b'{"type": "obs_batch", "entries": []}\n'
    )
    reader = _LineReader(_FakeSock(data))
    assert '"hello_ack"' in reader.readline()
    assert '"obs_batch"' in reader.readline()


def test_line_reader_frame_split_across_recvs():
    """A single frame dribbled across many recv chunks reassembles — the
    persistent buffer must hold partial bytes between reads."""
    from friday_v4.collab.sync import _LineReader
    reader = _LineReader(_FakeSock(b'{"type": "ping"}\n', chunk_size=3))
    assert '"ping"' in reader.readline()
    # The reader must consume the whole buffer — no trailing leftovers
    # that would corrupt the next frame.
    assert reader._buf == bytearray()


class TestSyncEngine:
    def test_two_engines_converge(self):
        server = SyncEngine(_store("srv", [{"from": "srv"}]), peer_id="srv",
                            workspace="ws", host="127.0.0.1", port=0)
        client = SyncEngine(_store("cli", [{"from": "cli"}]), peer_id="cli",
                            workspace="ws", host="127.0.0.1", port=0)
        assert server.start() is True
        try:
            result = client.sync_with("127.0.0.1", server.bound_port)
            assert result["accepted"] is True
            assert result["received"] == 1  # server's observation
            assert result["applied"] == 1
            assert result["sent"] == 2  # client's own + the merged one
            # Both sides converge to the same merged state.
            assert len(client.store.state()) == 2
            assert len(server.store.state()) == 2
            assert client.store.state() == server.store.state()
        finally:
            server.stop()

    def test_workspace_mismatch_rejected(self):
        server = SyncEngine(_store("srv", []), peer_id="srv",
                            workspace="ws-a", host="127.0.0.1", port=0)
        client = SyncEngine(_store("cli", []), peer_id="cli",
                            workspace="ws-b", host="127.0.0.1", port=0)
        assert server.start() is True
        try:
            result = client.sync_with("127.0.0.1", server.bound_port)
            assert result["accepted"] is False
            assert "workspace" in result.get("reason", "")
        finally:
            server.stop()

    def test_read_denied_rejected(self):
        server = SyncEngine(_store("srv", []), peer_id="srv",
                            workspace="ws", host="127.0.0.1", port=0,
                            accepts=lambda hello: False)
        client = SyncEngine(_store("cli", []), peer_id="cli",
                            workspace="ws", host="127.0.0.1", port=0)
        assert server.start() is True
        try:
            result = client.sync_with("127.0.0.1", server.bound_port)
            assert result["accepted"] is False
        finally:
            server.stop()

    def test_connect_failure_raises(self):
        engine = SyncEngine(_store("cli", []), peer_id="cli",
                            workspace="ws")
        # Nothing is listening on this ephemeral port.
        import socket as _socket
        with _socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            dead_port = probe.getsockname()[1]
        try:
            engine.sync_with("127.0.0.1", dead_port, timeout=1.0)
            assert False, "expected SyncError"
        except (SyncError, OSError):
            pass

    def test_start_idempotent_and_stop(self):
        engine = SyncEngine(_store("srv", []), peer_id="srv",
                            workspace="ws", host="127.0.0.1", port=0)
        assert engine.start() is True
        assert engine.start() is True  # idempotent
        assert engine.bound_port is not None
        engine.stop()
        assert engine._server is None
