"""Hermetic tests for the Wave 10 memory layer (friday_v6.memory).

Covers:
- MemoryStore: provenance round-trip, reaffirm strengthening, prefix
  listing, count, forget
- decay sweeps: time fades by age, usage fades by idle, none never fades,
  floor removal, deterministic via injected ``now``
- FactMemory: subject/predicate recall, recall_one, strengthen, forget,
  decay delegation, summary rendering (empty → "")
- WorkingMemory: set/get, TTL expiry, priority ordering, eviction, clear

Every test uses a tmp_path DB — never the real ~/.friday.
"""

from __future__ import annotations

import pytest

from friday_v6 import db
from friday_v6.memory import (
    DECAY_NONE,
    DECAY_TIME,
    DECAY_USAGE,
    FactMemory,
    MemoryStore,
    WorkingMemory,
)

NOW = "2026-08-01T12:00:00+00:00"


def _conn(tmp_path):
    return db.connect(tmp_path / "v4.db")


def _backdate(conn, key: str, days: float):
    """Move a memory's created_at/updated_at back by ``days`` (determinism)."""
    from datetime import datetime, timedelta, timezone
    ts = (datetime.fromisoformat(NOW) - timedelta(days=days)).isoformat(
        timespec="seconds")
    conn.execute("UPDATE memories SET created_at = ?, updated_at = ? "
                 "WHERE mem_key = ?", (ts, ts, key))
    conn.commit()


# ==========================================================================
# MemoryStore — provenance + reaffirm
# ==========================================================================


class TestMemoryStore:
    def test_store_recall_roundtrip_with_provenance(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            store = MemoryStore(conn)
            mid = store.store("operator.prefers_python_for_tooling", "True",
                              source="voice:2026-08-01", confidence=0.9,
                              decay_policy=DECAY_USAGE)
            assert mid
            fact = store.recall("operator.prefers_python_for_tooling")
            assert fact.value == "True"
            assert fact.source == "voice:2026-08-01"
            assert fact.confidence == 0.9
            assert fact.decay_policy == DECAY_USAGE
            assert fact.created_at and fact.updated_at
        finally:
            conn.close()

    def test_reaffirm_strengthens_confidence(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            store = MemoryStore(conn)
            store.store("operator.name", "Lakshay", source="voice:1",
                        confidence=0.6, decay_policy=DECAY_USAGE)
            # Re-stating the same fact boosts confidence (capped at 1.0).
            store.store("operator.name", "Lakshay", source="voice:2",
                        confidence=0.6, decay_policy=DECAY_USAGE)
            fact = store.recall("operator.name")
            assert fact.confidence == pytest.approx(0.7)  # 0.6 + 0.1
            assert store.count() == 1  # single row, upserted
            # ... and stays capped.
            for _ in range(10):
                store.store("operator.name", "Lakshay", source="voice:x",
                            confidence=0.99, decay_policy=DECAY_USAGE)
            assert store.recall("operator.name").confidence == 1.0
        finally:
            conn.close()

    def test_strengthen_method(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            store = MemoryStore(conn)
            store.store("pref.verbose", "False", confidence=0.5)
            fact = store.strengthen("pref.verbose", delta=0.25)
            assert fact.confidence == pytest.approx(0.75)
            assert store.strengthen("nope", delta=0.1) is None
        finally:
            conn.close()

    def test_list_prefix_and_count(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            store = MemoryStore(conn)
            store.store("operator.name", "Lakshay")
            store.store("operator.location", "Berlin")
            store.store("project.active", "friday")
            assert store.count() == 3
            assert store.count(prefix="operator") == 2
            keys = [f.key for f in store.list(prefix="operator")]
            assert "operator.name" in keys and "operator.location" in keys
        finally:
            conn.close()

    def test_forget(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            store = MemoryStore(conn)
            store.store("operator.name", "Lakshay")
            assert store.forget("operator.name")
            assert store.recall("operator.name") is None
            assert not store.forget("operator.name")  # already gone
        finally:
            conn.close()

    def test_invalid_policy_degrades_to_none(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            store = MemoryStore(conn)
            store.store("k", "v", decay_policy="bogus")
            assert store.recall("k").decay_policy == DECAY_NONE
        finally:
            conn.close()


# ==========================================================================
# Decay sweeps
# ==========================================================================


class TestDecay:
    def test_time_policy_fades_old_facts(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            store = MemoryStore(conn)
            store.store("project.old", "v1", confidence=0.9,
                        decay_policy=DECAY_TIME)
            store.store("project.fresh", "v2", confidence=0.9,
                        decay_policy=DECAY_TIME)
            _backdate(conn, "project.old", days=60)  # older than 30-day ttl
            report = store.decay(now=NOW, time_age_days=30)
            assert report.total == 2
            assert report.decayed == 1
            assert report.removed == 0
            assert store.recall("project.old").confidence == pytest.approx(0.7)
            assert store.recall("project.fresh").confidence == 0.9
        finally:
            conn.close()

    def test_usage_policy_fades_idle_facts(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            store = MemoryStore(conn)
            store.store("pref.lang", "rust", confidence=0.9,
                        decay_policy=DECAY_USAGE)
            # A recall touches updated_at — the usage signal.
            store.recall("pref.lang")
            assert store.decay(now=NOW, usage_idle_days=14).decayed == 0
            # Idle for 30 days → fades.
            _backdate(conn, "pref.lang", days=30)
            assert store.decay(now=NOW, usage_idle_days=14).decayed == 1
        finally:
            conn.close()

    def test_none_never_fades(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            store = MemoryStore(conn)
            store.store("operator.name", "Lakshay", confidence=0.9,
                        decay_policy=DECAY_NONE)
            _backdate(conn, "operator.name", days=400)
            report = store.decay(now=NOW)
            assert report.decayed == 0
            assert report.removed == 0
            assert store.recall("operator.name").confidence == 0.9
        finally:
            conn.close()

    def test_floor_removes_facts(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            store = MemoryStore(conn)
            store.store("project.old", "v", confidence=0.2,
                        decay_policy=DECAY_TIME)
            _backdate(conn, "project.old", days=60)
            report = store.decay(now=NOW, time_age_days=30, decay_rate=0.2,
                                 floor=0.15)
            # 0.2 - 0.2 = 0.0 < 0.15 → forgotten entirely.
            assert report.removed == 1
            assert store.recall("project.old") is None
        finally:
            conn.close()

    def test_decay_skips_empty_and_guarded(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            report = MemoryStore(conn).decay(now=NOW)
            assert report.total == 0 and report.decayed == 0
        finally:
            conn.close()


# ==========================================================================
# FactMemory — propositions
# ==========================================================================


class TestFactMemory:
    def test_remember_and_recall_by_subject(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            facts = FactMemory(conn)
            facts.remember("operator", "prefers_python_for_tooling", "True",
                           source="voice:2026-08-01", confidence=0.9)
            facts.remember("operator", "name", "Lakshay",
                           source="cli:2026-08-01", confidence=0.95)
            ops = facts.recall(subject="operator")
            assert len(ops) == 2
            names = {f.predicate for f in ops}
            assert names == {"prefers_python_for_tooling", "name"}
            one = facts.recall_one("operator", "name")
            assert one.value == "Lakshay"
            assert one.source == "cli:2026-08-01"
            assert facts.recall_one("operator", "nope") is None
        finally:
            conn.close()

    def test_recall_by_predicate_across_subjects(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            facts = FactMemory(conn)
            facts.remember("operator", "timezone", "Europe/Berlin")
            facts.remember("project", "timezone", "UTC")
            hits = facts.recall(predicate="timezone")
            assert {f.subject for f in hits} == {"operator", "project"}
        finally:
            conn.close()

    def test_strengthen_and_forget(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            facts = FactMemory(conn)
            facts.remember("operator", "name", "Lakshay", confidence=0.5)
            boosted = facts.strengthen("operator", "name", delta=0.2)
            assert boosted.confidence == pytest.approx(0.7)
            # Forget whole subject.
            facts.remember("operator", "location", "Berlin")
            assert facts.forget("operator") is True
            assert facts.recall(subject="operator") == []
            # Forget single predicate.
            facts.remember("project", "active", "yes")
            assert facts.forget("project", "active")
            assert facts.recall(subject="project") == []
        finally:
            conn.close()

    def test_summary_block_and_empty(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            facts = FactMemory(conn)
            assert facts.summary() == ""
            facts.remember("operator", "name", "Lakshay",
                           source="voice:2026-08-01", confidence=0.95)
            block = facts.summary()
            assert "Lakshay" in block
            assert "voice:2026-08-01" in block
            assert "Things I remember about you:" in block
        finally:
            conn.close()

    def test_decay_delegates(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            facts = FactMemory(conn)
            facts.remember("operator", "location", "Berlin",
                           confidence=0.9, decay_policy=DECAY_TIME)
            _backdate(conn, "operator.location", days=60)
            report = facts.decay(now=NOW, time_age_days=30)
            assert report.decayed == 1
        finally:
            conn.close()


# ==========================================================================
# WorkingMemory — ephemeral context
# ==========================================================================


class TestWorkingMemory:
    def test_set_get_roundtrip(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            wm = WorkingMemory(conn)
            wid = wm.set("current_task", "Refactoring auth module",
                         category="working", source="planner", priority=3)
            assert wid
            row = wm.get("current_task")
            assert row["value"] == "Refactoring auth module"
            assert row["priority"] == 3
            assert row["source"] == "planner"
            assert wm.get("missing") is None
        finally:
            conn.close()

    def test_ttl_expiry(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            wm = WorkingMemory(conn)
            wm.set("task", "x", ttl_seconds=3600,
                   now="2026-08-01T10:00:00+00:00")
            assert wm.count() == 1
            # Before expiry → still live.
            assert wm.clear_expired(now="2026-08-01T10:30:00+00:00") == 0
            assert wm.get("task", now="2026-08-01T10:30:00+00:00") is not None
            # After expiry → pruned on access.
            assert wm.clear_expired(now="2026-08-01T12:00:00+00:00") == 1
            assert wm.count() == 0
        finally:
            conn.close()

    def test_priority_ordering_and_min_priority(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            wm = WorkingMemory(conn)
            wm.set("low", "1", priority=0)
            wm.set("high", "2", priority=4)
            wm.set("mid", "3", priority=2)
            keys = [r["context_key"] for r in wm.all()]
            assert keys == ["high", "mid", "low"]
            block = wm.current_context(min_priority=3)
            assert "high" in block and "low" not in block
        finally:
            conn.close()

    def test_eviction_keeps_highest_priority(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            wm = WorkingMemory(conn)
            for i in range(60):
                wm.set(f"k{i}", str(i), priority=i % 5)
            assert wm.count() <= 50  # MAX_ENTRIES enforced
            remaining = wm.all()
            # The highest-priority entries (priority 4) survive.
            assert any(r["priority"] == 4 for r in remaining)
            assert all(r["priority"] >= 0 for r in remaining)
        finally:
            conn.close()

    def test_clear_all_and_by_category(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            wm = WorkingMemory(conn)
            wm.set("a", "1", category="task")
            wm.set("b", "2", category="status")
            assert len(wm.by_category("task")) == 1
            assert wm.clear_all() == 2
            assert wm.count() == 0
        finally:
            conn.close()
