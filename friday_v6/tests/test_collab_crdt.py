"""Tests for collab.crdt — the Last-Writer-Wins observation CRDT."""

from __future__ import annotations

from friday_v6.collab.crdt import (
    ObservationCRDT,
    default_observation_id,
    merge_observations,
)


class TestObservationCRDT:
    def test_add_returns_deterministic_id(self):
        store = ObservationCRDT(peer_id="alice")
        obs_id = store.add({"kind": "app_open", "app": "kitty"})
        again = store.add({"kind": "app_open", "app": "kitty"})
        assert obs_id == again
        assert obs_id.startswith("alice:")

    def test_same_payload_dedups(self):
        store = ObservationCRDT(peer_id="alice")
        store.add({"kind": "app_open", "app": "kitty"})
        store.add({"kind": "app_open", "app": "kitty"})
        assert len(store) == 1

    def test_explicit_id_respected(self):
        store = ObservationCRDT(peer_id="alice")
        obs_id = store.add({"text": "x"}, obs_id="custom-1")
        assert obs_id == "custom-1"
        assert store.get("custom-1") is not None

    def test_merge_converges_both_ways(self):
        a = ObservationCRDT(peer_id="a")
        b = ObservationCRDT(peer_id="b")
        a.add({"text": "from-a"}, ts=100)
        b.add({"text": "from-b"}, ts=200)
        b.merge(a.state())
        a.merge(b.state())
        texts = sorted(e["payload"]["text"] for e in a.state())
        assert texts == ["from-a", "from-b"]
        assert a.state() == b.state()

    def test_lww_newer_timestamp_wins(self):
        store = ObservationCRDT(peer_id="alice")
        store.add({"text": "old"}, obs_id="k", ts=100)
        applied = store.merge([{
            "id": "k", "peer_id": "bob", "ts": 300,
            "payload": {"text": "new"}, "deleted": False,
        }])
        assert applied == 1
        assert store.get("k")["payload"]["text"] == "new"

    def test_equal_timestamp_ties_break_by_peer_id(self):
        store = ObservationCRDT(peer_id="alice")
        older_peer = {"id": "k", "peer_id": "aaa", "ts": 500,
                      "payload": {"from": "aaa"}, "deleted": False}
        newer_peer = {"id": "k", "peer_id": "zzz", "ts": 500,
                      "payload": {"from": "zzz"}, "deleted": False}
        store.merge([older_peer])
        store.merge([newer_peer])
        assert store.get("k")["payload"]["from"] == "zzz"

    def test_merge_returns_applied_count(self):
        store = ObservationCRDT(peer_id="a")
        entries = [{
            "id": f"k{i}", "peer_id": "b", "ts": 100 + i,
            "payload": {"n": i}, "deleted": False,
        } for i in range(3)]
        assert store.merge(entries) == 3
        assert store.merge(entries) == 0  # no-op second time

    def test_delete_tombstones_but_keeps_id(self):
        store = ObservationCRDT(peer_id="alice")
        obs_id = store.add({"text": "bye"}, ts=100)
        assert store.delete(obs_id) is True
        assert len(store) == 1  # tombstone retained for sync
        assert store.observations() == []
        assert store.tombstone_count() == 1

    def test_observations_newest_first(self):
        store = ObservationCRDT(peer_id="a")
        store.add({"n": 1}, ts=100)
        store.add({"n": 2}, ts=200)
        store.add({"n": 3}, ts=300)
        assert [e["payload"]["n"] for e in store.observations()] == [3, 2, 1]

    def test_state_roundtrip_into_fresh_store(self):
        a = ObservationCRDT(peer_id="a")
        a.add({"text": "hello"}, ts=100)
        a.add({"text": "world"}, ts=200)
        b = ObservationCRDT(peer_id="b")
        applied = b.merge(a.state())
        assert applied == 2
        assert b.state() == a.state()

    def test_default_id_stable_across_peers(self):
        assert default_observation_id("a", {"x": 1}) == \
            default_observation_id("a", {"x": 1})
        assert default_observation_id("a", {"x": 1}) != \
            default_observation_id("b", {"x": 1})

    def test_merge_observations_returns_winner(self):
        old = {"id": "k", "peer_id": "a", "ts": 1, "payload": {},
               "deleted": False}
        new = {"id": "k", "peer_id": "b", "ts": 2, "payload": {},
               "deleted": False}
        assert merge_observations(old, new) is new
        assert merge_observations(new, old) is new
