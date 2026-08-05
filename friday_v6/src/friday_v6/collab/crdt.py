"""Observation CRDT — conflict-free merge for collaborative observations.

Wave 5's core primitive. Observations are append-only records with
deterministic ids; a Last-Writer-Wins register per id makes concurrent
sync between Friday instances converge without a central server:

    for a given observation id, the entry with the highest
    (timestamp, peer_id) tuple wins — timestamps break ties and
    peer_id breaks exact-clock collisions deterministically.

Pure stdlib. Entries are plain dicts so they serialize straight to the
JSON-lines wire protocol used by the sync engine::

    {"id": str, "peer_id": str, "ts": int, "payload": dict,
     "deleted": bool}

Tombstones are kept in the merged state (never garbage-collected) so
deletes propagate correctly across peers — the standard LWW CRDT cost.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Iterable, Optional

#: One observation id may live at most this long in the value namespace.
#: (Not a hard cap on the store — just the sha1 prefix width.)
_ID_LEN = 16


def _now_ms() -> int:
    """Wall-clock epoch milliseconds (the LWW timestamp)."""
    return int(time.time() * 1000)


def default_observation_id(peer_id: str, payload: dict) -> str:
    """Deterministic id for a payload — same content, same id.

    Collisions are intentional dedup: two instances that observe the same
    fact produce the same id and LWW-merge into a single record instead
    of duplicating it.
    """
    blob = json.dumps(payload, sort_keys=True, default=str)
    digest = hashlib.sha1(f"{peer_id}:{blob}".encode("utf-8")).hexdigest()
    return f"{peer_id}:{digest[:_ID_LEN]}"


def merge_observations(existing: dict, incoming: dict) -> dict:
    """Resolve two entries for the same id — the LWW winner.

    The winner is the entry with the higher ``(ts, peer_id)`` tuple; the
    tiebreak makes concurrent equal-timestamp writes converge on every
    replica. Always returns one of the two inputs (never a new dict).
    """
    if (incoming.get("ts", 0), incoming.get("peer_id", "")) > (
        existing.get("ts", 0), existing.get("peer_id", "")
    ):
        return incoming
    return existing


class ObservationCRDT:
    """Last-Writer-Wins set of observations, keyed by deterministic id.

    Usage:
        store = ObservationCRDT(peer_id="alice-laptop")
        obs_id = store.add({"kind": "app_open", "app": "kitty"})
        store.merge(other_store.state())   # converges both ways
        store.observations()               # non-deleted, sorted
    """

    def __init__(self, peer_id: str = "local"):
        self.peer_id = peer_id
        self._entries: dict[str, dict] = {}

    # ── Writes ─────────────────────────────────────────────────────

    def add(self, payload: dict, obs_id: Optional[str] = None,
            peer_id: Optional[str] = None, ts: Optional[int] = None,
            deleted: bool = False) -> str:
        """Record an observation; returns its (deterministic) id."""
        writer = peer_id or self.peer_id
        entry = {
            "id": obs_id or default_observation_id(writer, payload),
            "peer_id": writer,
            "ts": _now_ms() if ts is None else int(ts),
            "payload": dict(payload),
            "deleted": bool(deleted),
        }
        self._apply(entry)
        return entry["id"]

    def delete(self, obs_id: str) -> bool:
        """Tombstone an observation (propagates as a delete to peers)."""
        existing = self._entries.get(obs_id)
        if existing is None:
            return False
        tombstone = dict(existing)
        tombstone["deleted"] = True
        tombstone["ts"] = _now_ms()
        tombstone["peer_id"] = self.peer_id
        self._apply(tombstone)
        return True

    def merge(self, entries: Iterable[dict]) -> int:
        """Merge a batch of entries (from sync or a peer) into this store.

        Returns the number of entries that actually changed state.
        """
        applied = 0
        for entry in entries:
            if self._apply(dict(entry)):
                applied += 1
        return applied

    def _apply(self, entry: dict) -> bool:
        """LWW-apply a single entry; True if the store changed."""
        obs_id = entry.get("id")
        if not obs_id:
            return False
        winner = merge_observations(
            self._entries.get(obs_id, {}), entry)
        if winner is self._entries.get(obs_id):
            return False
        self._entries[obs_id] = winner
        return True

    # ── Reads ──────────────────────────────────────────────────────

    def get(self, obs_id: str) -> Optional[dict]:
        """The merged entry for ``obs_id`` (may be a tombstone)."""
        entry = self._entries.get(obs_id)
        return dict(entry) if entry else None

    def state(self) -> list[dict]:
        """Full merged snapshot for sync (includes tombstones)."""
        return [dict(e) for e in sorted(
            self._entries.values(), key=lambda e: (e["ts"], e["id"]))]

    def observations(self, limit: Optional[int] = None) -> list[dict]:
        """Non-deleted observations, newest first."""
        live = [dict(e) for e in self._entries.values()
                if not e.get("deleted")]
        live.sort(key=lambda e: (e["ts"], e["id"]), reverse=True)
        return live[:limit] if limit else live

    def tombstone_count(self) -> int:
        return sum(1 for e in self._entries.values() if e.get("deleted"))

    def __len__(self) -> int:
        return len(self._entries)

    def __repr__(self) -> str:
        return (f"<ObservationCRDT peer={self.peer_id} "
                f"entries={len(self._entries)}>")
