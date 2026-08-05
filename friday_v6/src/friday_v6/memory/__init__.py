"""Memory Layer — long-term facts with provenance + session context (Wave 10).

Rebuilds V3's memory stack V4-native on the existing ``memories`` table
(plus the new ``working_memory`` table): Friday *remembers* you.

    facts (facts.py)    — propositions with provenance, confidence, decay
    store (store.py)    — typed DB access + decay sweeps over memories
    working (working.py)— ephemeral "right now" context with TTL

Facts are never raw strings: ``("operator", "prefers_python_for_tooling",
confidence=0.9, source="voice:2026-08-01")``. ``decay()`` fades unused
facts and forgets stale ones below the confidence floor; re-stating a
fact strengthens it. Nothing is written to the real ``~/.friday`` in
tests (hermetic via ``tmp_path``).

Usage:
    from friday_v6.memory import FactMemory, WorkingMemory
    facts = FactMemory(conn)
    facts.remember("operator", "prefers_python_for_tooling", "True",
                   source="voice:2026-08-01", confidence=0.9)
    wm = WorkingMemory(conn)
    wm.set("current_task", "Refactoring auth module", priority=3)
"""

from __future__ import annotations

try:
    from .facts import Fact, FactMemory
    from .store import (
        DECAY_NONE,
        DECAY_TIME,
        DECAY_USAGE,
        DecayReport,
        MemoryFact,
        MemoryStore,
        is_valid_decay_policy,
    )
    from .working import DEFAULT_TTL_SECONDS, MAX_ENTRIES, WorkingMemory
    _MEMORY_AVAILABLE = True
except ImportError:  # pragma: no cover - defensive stub
    Fact = None  # type: ignore
    FactMemory = None  # type: ignore
    DECAY_NONE = "none"  # type: ignore
    DECAY_TIME = "time"  # type: ignore
    DECAY_USAGE = "usage"  # type: ignore
    DecayReport = None  # type: ignore
    MemoryFact = None  # type: ignore
    MemoryStore = None  # type: ignore
    is_valid_decay_policy = None  # type: ignore
    DEFAULT_TTL_SECONDS = 3600  # type: ignore
    MAX_ENTRIES = 50  # type: ignore
    WorkingMemory = None  # type: ignore
    _MEMORY_AVAILABLE = False


def is_available() -> bool:
    """Whether the memory layer is implemented yet."""
    return _MEMORY_AVAILABLE


__all__ = [
    "Fact",
    "FactMemory",
    "DECAY_NONE",
    "DECAY_TIME",
    "DECAY_USAGE",
    "DecayReport",
    "MemoryFact",
    "MemoryStore",
    "is_valid_decay_policy",
    "DEFAULT_TTL_SECONDS",
    "MAX_ENTRIES",
    "WorkingMemory",
    "is_available",
    "_MEMORY_AVAILABLE",
]
