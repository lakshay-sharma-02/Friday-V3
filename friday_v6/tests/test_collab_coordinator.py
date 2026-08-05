"""Tests for collab.coordinator — wiring + state persistence (hermetic)."""

from __future__ import annotations

from friday_v6.collab.coordinator import Coordinator
from friday_v6.collab.peer import PeerInfo


class _FakeDiscovery:
    def __init__(self, peers=None):
        self._peers = peers or []
        self.running = False

    def start(self):
        self.running = True
        return True

    def stop(self):
        self.running = False

    def peers(self):
        return list(self._peers)


class _FakeSync:
    def __init__(self, result=None):
        self.started = False
        self.result = result or {"accepted": True, "sent": 2, "received": 3,
                                 "applied": 1}

    def start(self):
        self.started = True
        return True

    def stop(self):
        self.started = False

    def sync_with(self, host, port):
        return dict(self.result)


class TestCoordinator:
    def test_add_observation_persists(self, tmp_path):
        coordinator = Coordinator(state_dir=tmp_path / "state",
                                  workspace="ws")
        obs_id = coordinator.add_observation({"text": "hello"})
        assert obs_id is not None
        assert len(coordinator.observations()) == 1
        state_file = tmp_path / "state" / "state.json"
        assert state_file.exists()

    def test_restart_loads_state(self, tmp_path):
        state_dir = tmp_path / "state"
        first = Coordinator(state_dir=state_dir, workspace="ws")
        first.add_observation({"text": "hello"}, obs_id="k1")
        first.add_member("bob", "member")

        restarted = Coordinator(state_dir=state_dir, workspace="ws")
        assert len(restarted.observations()) == 1
        assert restarted.observations()[0]["id"] == "k1"
        assert restarted.permissions.role_of("bob") == "member"
        # Stable identity: a restarted coordinator keeps its peer id.
        assert restarted.peer_id == first.peer_id

    def test_last_known_peers_hydrate_after_restart(self, tmp_path):
        """`collab peers`/`status` (no live discovery) surface the peers
        persisted by a prior run — the reviewers' regression for the
        half-wired peers feature."""
        state_dir = tmp_path / "state"
        running = Coordinator(state_dir=state_dir, workspace="ws",
                              discovery=_FakeDiscovery(),
                              sync=_FakeSync())
        running.start()
        # A live peer seen while running gets persisted with state.
        peer = PeerInfo(peer_id="bob", host="10.0.0.2", port=9002,
                        workspace="ws")
        running.discovery._peers = [peer]  # type: ignore[attr-defined]
        running.stop()  # stop() persists last_peers

        offline = Coordinator(state_dir=state_dir, workspace="ws")
        peers = offline.peers()  # discovery not running → last-known
        assert [p.peer_id for p in peers] == ["bob"]
        assert peers[0].host == "10.0.0.2"
        assert peers[0].port == 9002

    def test_merge_entries(self, tmp_path):
        coordinator = Coordinator(state_dir=tmp_path / "state",
                                  workspace="ws")
        applied = coordinator.merge_entries([{
            "id": "remote-1", "peer_id": "bob", "ts": 100,
            "payload": {"from": "bob"}, "deleted": False,
        }])
        assert applied == 1
        assert coordinator.observations()[0]["payload"]["from"] == "bob"

    def test_status_shape(self, tmp_path):
        coordinator = Coordinator(state_dir=tmp_path / "state",
                                  workspace="ws")
        status = coordinator.status()
        assert status["workspace"] == "ws"
        assert status["peer_id"] == coordinator.peer_id
        assert "observations" in status
        assert "permissions" in status
        assert "peers" in status

    def test_start_stop_with_fakes(self, tmp_path):
        peers = [PeerInfo(peer_id="bob", host="127.0.0.1", port=9002,
                          workspace="ws")]
        discovery = _FakeDiscovery(peers=peers)
        sync = _FakeSync()
        coordinator = Coordinator(state_dir=tmp_path / "state",
                                  workspace="ws", discovery=discovery,
                                  sync=sync)
        assert coordinator.start() is True
        assert discovery.running is True
        assert sync.started is True
        assert coordinator.running is True
        result = coordinator.sync_once()
        assert result["peers"] == 1
        assert result["accepted"] == 1
        coordinator.stop()
        assert coordinator.running is False
        assert discovery.running is False
        assert sync.started is False

    def test_sync_once_without_network_is_safe(self, tmp_path):
        coordinator = Coordinator(state_dir=tmp_path / "state",
                                  workspace="ws")
        result = coordinator.sync_once()
        assert result == {"peers": 0, "sent": 0, "received": 0,
                          "applied": 0, "accepted": 0}

    def test_remove_member(self, tmp_path):
        coordinator = Coordinator(state_dir=tmp_path / "state",
                                  workspace="ws")
        coordinator.add_member("bob", "member")
        assert coordinator.remove_member("bob") is True
        assert coordinator.remove_member("bob") is False
