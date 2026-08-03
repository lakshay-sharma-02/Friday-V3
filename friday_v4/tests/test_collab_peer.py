"""Tests for collab.peer — pure-stdlib UDP beacon discovery."""

from __future__ import annotations

import json
import time

from friday_v4.collab.peer import PeerDiscovery, PeerInfo


def _beacon(peer_id, port=9876, workspace="default"):
    return json.dumps({
        "type": "friday-collab", "version": 1, "peer_id": peer_id,
        "hostname": peer_id, "port": port, "workspace": workspace,
        "sent_at": time.time(),
    }).encode("utf-8")


class TestPeerDiscoveryUnit:
    def test_beacon_shape(self):
        discovery = PeerDiscovery(peer_id="a", workspace="ws", sync_port=9)
        beacon = discovery._beacon()
        assert beacon["type"] == "friday-collab"
        assert beacon["peer_id"] == "a"
        assert beacon["workspace"] == "ws"
        assert beacon["port"] == 9
        assert "sent_at" in beacon

    def test_handle_beacon_upserts_peer(self):
        discovery = PeerDiscovery(peer_id="a", workspace="ws")
        assert discovery._handle_beacon(
            _beacon("b", port=9800, workspace="ws"),
            ("192.168.1.5", 9988)) is True
        peers = discovery.peers()
        assert len(peers) == 1
        peer = peers[0]
        assert peer.peer_id == "b"
        assert peer.host == "192.168.1.5"
        assert peer.port == 9800
        assert peer.workspace == "ws"

    def test_ignores_self_beacon(self):
        discovery = PeerDiscovery(peer_id="a", workspace="ws")
        assert discovery._handle_beacon(
            _beacon("a"), ("127.0.0.1", 9988)) is False
        assert discovery.peers() == []

    def test_ignores_garbage(self):
        discovery = PeerDiscovery(peer_id="a", workspace="ws")
        assert discovery._handle_beacon(b"not json", ("1.1.1.1", 1)) is False
        assert discovery._handle_beacon(
            json.dumps({"type": "other"}).encode(), ("1.1.1.1", 1)) is False
        assert discovery.peers() == []

    def test_peer_expiry(self):
        discovery = PeerDiscovery(peer_id="a", workspace="ws", peer_ttl=1.0)
        discovery._handle_beacon(
            _beacon("b", port=9800), ("192.168.1.5", 9988))
        assert len(discovery.peers()) == 1
        # Backdate the peer's last_seen past the TTL.
        with discovery._lock:
            discovery._peers["b"].last_seen = time.time() - 10.0
        assert discovery.peers() == []

    def test_peer_info_roundtrip(self):
        info = PeerInfo(peer_id="b", host="10.0.0.2", port=9800,
                        workspace="ws", last_seen=123.0)
        clone = PeerInfo.from_dict(info.to_dict())
        assert clone.peer_id == "b"
        assert clone.host == "10.0.0.2"
        assert clone.port == 9800
        assert clone.workspace == "ws"

    def test_announce_without_socket_is_noop(self):
        discovery = PeerDiscovery(peer_id="a", workspace="ws")
        assert discovery.announce() is False  # no socket yet


class TestPeerDiscoveryIntegration:
    """Two instances on loopback discover each other over real UDP."""

    def test_two_instances_discover_each_other(self):
        # Ephemeral ports (0) so parallel pytest processes never collide
        # on fixed beacon ports; bound_beacon_port exposes the real ones.
        a = PeerDiscovery(peer_id="alice", workspace="ws", sync_port=9001,
                          beacon_port=0, listen_addr="127.0.0.1",
                          broadcast_addr="127.0.0.1",
                          announce_interval=60.0)
        b = PeerDiscovery(peer_id="bob", workspace="ws", sync_port=9002,
                          beacon_port=0, listen_addr="127.0.0.1",
                          broadcast_addr="127.0.0.1",
                          announce_interval=60.0)
        try:
            assert a.start() is True
            assert b.start() is True
            # Manually direct each beacon (no kernel multicast needed).
            assert a._send_to("127.0.0.1", b.bound_beacon_port) is True
            assert b._send_to("127.0.0.1", a.bound_beacon_port) is True
            deadline = time.time() + 3.0
            while time.time() < deadline:
                if len(a.peers()) == 1 and len(b.peers()) == 1:
                    break
                time.sleep(0.05)
            assert len(a.peers()) == 1
            assert len(b.peers()) == 1
            assert a.peers()[0].peer_id == "bob"
            assert b.peers()[0].peer_id == "alice"
            assert b.peers()[0].port == 9001  # alice's sync port
        finally:
            a.stop()
            b.stop()
        assert a.running is False
        assert b.running is False
