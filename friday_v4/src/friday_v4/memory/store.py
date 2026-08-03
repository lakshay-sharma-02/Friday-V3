"""Typed memory access + decay sweeps over the V4 memories table (Wave 10).

``MemoryStore`` is the typed, guarded layer over ``db.py``'s ``memories``
table (provenance + confidence + decay policy). ``decay()`` implements the
sweep: unused facts fade, confirmed facts strengthen, facts below the
confidence floor are forgotten.

Decay policies (values match the ``memories.decay_policy`` schema comment):

- ``none``  — never fades
- ``time``  — fades by age (anchor: ``created_at``)
- ``usage`` — fades by idle time since last use (anchor: ``updated_at``,
  which ``db.recall_memory`` touches on every recall)

Usage::

    store = MemoryStore(conn)
    store.store("operator.prefers_python_for_tooling", "True",
                source="voice:2026-08-01", confidence=0.9,
                decay_policy=DECAY_USAGE)
    fact = store.recall("operator.prefers_python_for_tooling")
    report = store.decay()   # sweep — stale facts fade, low ones die
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from friday_v4 import db

# Decay policies (must match the schema comment on memories.decay_policy)
DECAY_NONE = "none"
DECAY_TIME = "time"
DECAY_USAGE = "usage"
_VALID_POLICIES = (DECAY_NONE, DECAY_TIME, DECAY_USAGE)


def is_valid_decay_policy(policy: str) -> bool:
    """Whether ``policy`` is one of the three schema decay policies."""
    return policy in _VALID_POLICIES


@dataclass
class MemoryFact:
    """A memory row with provenance (typed view over the table)."""

    key: str
    value: str
    source: str = ""
    confidence: float = 0.5
    decay_policy: str = DECAY_NONE
    created_at: str = ""
    updated_at: str = ""
    id: str = ""

    @classmethod
    def from_row(cls, row: dict) -> "MemoryFact":
        return cls(
            id=row.get("id", ""),
            key=row.get("mem_key", ""),
            value=row.get("value", ""),
            source=row.get("source", ""),
            confidence=row.get("confidence", 0.5),
            decay_policy=row.get("decay_policy", DECAY_NONE),
            created_at=row.get("created_at", ""),
            updated_at=row.get("updated_at", ""),
        )


@dataclass
class DecayReport:
    """Result of one decay sweep."""

    decayed: int = 0  # facts whose confidence was reduced
    removed: int = 0  # facts forgotten (dropped below the floor)
    total: int = 0    # facts examined


def _coerce_iso(now: Optional[str]) -> str:
    return now or db.now_iso()


def _days_between(now_iso: str, then_iso: str) -> float:
    """Whole days between two ISO timestamps (0 if either is unparseable)."""
    try:
        now = datetime.fromisoformat(now_iso)
        then = datetime.fromisoformat(then_iso)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
        return max(0.0, (now - then).total_seconds() / 86400.0)
    except (TypeError, ValueError):
        return 0.0


class MemoryStore:
    """Typed, guarded access to the ``memories`` table.

    - ``store`` upserts with provenance; re-affirming an existing fact
      strengthens it (confidence ``+= strengthen_delta``, capped at 1.0) —
      *confirmed facts strengthen*.
    - ``recall`` returns a typed :class:`MemoryFact` (and touches
      ``updated_at`` as the usage-decay signal).
    - ``decay`` implements the sweep — stale facts fade, facts below the
      floor are forgotten.
    """

    def __init__(self, conn) -> None:
        self._conn = conn

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def store(self, key: str, value: str, source: str = "",
              confidence: float = 0.5, decay_policy: str = DECAY_NONE,
              strengthen_delta: float = 0.1) -> Optional[str]:
        """Upsert a fact with provenance; returns its id (None on failure).

        If the key already exists, confidence becomes
        ``max(confidence, existing + strengthen_delta)`` capped at 1.0 —
        the operator re-stating a preference makes Friday more sure of it.
        """
        if not is_valid_decay_policy(decay_policy):
            decay_policy = DECAY_NONE
        existing = db.recall_memory(self._conn, key)
        if existing:
            merged = min(1.0, existing["confidence"] + strengthen_delta)
            confidence = max(confidence, merged)
        return db.store_memory(self._conn, key, value, source=source,
                               confidence=confidence, decay_policy=decay_policy)

    def forget(self, key: str) -> bool:
        """Delete a fact by key."""
        return db.forget_memory(self._conn, key)

    def strengthen(self, key: str, delta: float = 0.1) -> Optional[MemoryFact]:
        """Boost a fact's confidence (a confirmation signal)."""
        fact = self.recall(key)
        if fact is None:
            return None
        new_conf = min(1.0, fact.confidence + delta)
        db.set_memory_confidence(self._conn, key, new_conf)
        return self.recall(key)

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def recall(self, key: str) -> Optional[MemoryFact]:
        """Fetch a fact by exact key (touches ``updated_at`` = usage)."""
        row = db.recall_memory(self._conn, key)
        return MemoryFact.from_row(row) if row else None

    def list(self, prefix: Optional[str] = None,
             limit: int = 100) -> list[MemoryFact]:
        """List facts, optionally scoped to a key prefix, newest-first."""
        rows = db.list_memories(self._conn, limit=limit, mem_key_prefix=prefix)
        return [MemoryFact.from_row(r) for r in rows]

    def count(self, prefix: Optional[str] = None) -> int:
        """Count facts, optionally scoped to a key prefix."""
        return len(self.list(prefix=prefix, limit=100000))

    # ------------------------------------------------------------------
    # Decay sweep
    # ------------------------------------------------------------------

    def decay(self, now: Optional[str] = None,
              time_age_days: int = 30,
              usage_idle_days: int = 14,
              decay_rate: float = 0.2,
              floor: float = 0.15) -> DecayReport:
        """One decay sweep over all facts.

        - ``time``  policy: fade facts older than ``time_age_days``
          (measured from ``created_at``).
        - ``usage`` policy: fade facts idle for ``usage_idle_days``
          (measured from ``updated_at``; recall refreshes it).
        - ``none``  policy: never fades.
        - Any fact whose confidence drops below ``floor`` is forgotten.

        Pass ``now`` (ISO) for deterministic tests; defaults to the clock.
        """
        base = _coerce_iso(now)
        report = DecayReport()
        for fact in self.list(limit=100000):
            report.total += 1
            if fact.decay_policy == DECAY_NONE:
                continue
            anchor = fact.created_at if fact.decay_policy == DECAY_TIME \
                else fact.updated_at
            threshold = time_age_days if fact.decay_policy == DECAY_TIME \
                else usage_idle_days
            if _days_between(base, anchor) < threshold:
                continue
            new_conf = fact.confidence - decay_rate
            if new_conf < floor:
                if self.forget(fact.key):
                    report.removed += 1
            else:
                db.set_memory_confidence(self._conn, fact.key, new_conf)
                report.decayed += 1
        return report
