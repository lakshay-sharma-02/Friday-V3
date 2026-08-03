"""Long-term memory — propositions with provenance (Wave 10).

``FactMemory`` is the engine behind *"Friday, who am I?"*: facts are
**propositions with provenance**, never raw strings::

    ("operator", "prefers_python_for_tooling",
     confidence=0.9, source="voice:2026-08-01")

stored under ``mem_key = "operator.prefers_python_for_tooling"`` in the
V4 ``memories`` table. Recall is subject-scoped; ``summary()`` renders
the natural-language block persona/briefing surfaces consume.

MCU Friday feel::

    🎤 [You]: "I prefer Rust for performance-critical code."
    🎧 [Friday]: "Noted — storing that. I'll keep briefings shorter in the
                 morning going forward."
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from friday_v4.memory.store import (
    DECAY_NONE,
    DECAY_TIME,
    DECAY_USAGE,
    DecayReport,
    MemoryStore,
)

# Common subjects (wave-10 vocabulary)
SUBJECT_OPERATOR = "operator"
SUBJECT_PROJECT = "project"
SUBJECT_PREFERENCE = "preference"


@dataclass
class Fact:
    """A proposition: ``subject.predicate = value`` with provenance."""

    subject: str
    predicate: str
    value: str
    source: str = ""
    confidence: float = 0.6
    decay_policy: str = DECAY_USAGE
    created_at: str = ""
    updated_at: str = ""

    @property
    def key(self) -> str:
        return f"{self.subject}.{self.predicate}"

    @classmethod
    def from_memory_fact(cls, mf) -> "Fact":
        subject, _, predicate = mf.key.partition(".")
        if not predicate:
            subject, predicate = mf.key, ""
        return cls(
            subject=subject, predicate=predicate, value=mf.value,
            source=mf.source, confidence=mf.confidence,
            decay_policy=mf.decay_policy,
            created_at=mf.created_at, updated_at=mf.updated_at,
        )


class FactMemory:
    """Long-term memory engine over the V4 memories table.

    Usage::

        facts = FactMemory(conn)
        facts.remember("operator", "prefers_python_for_tooling", "True",
                       source="voice:2026-08-01", confidence=0.9)
        ops = facts.recall(subject="operator")
        block = facts.summary()   # → prompt-ready natural language
    """

    def __init__(self, conn, store: Optional[MemoryStore] = None) -> None:
        self._conn = conn
        self._store = store or MemoryStore(conn)

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def remember(self, subject: str, predicate: str, value: str,
                 source: str = "", confidence: float = 0.6,
                 decay_policy: str = DECAY_USAGE) -> Optional[str]:
        """Store (or strengthen) a proposition; returns its memory id."""
        return self._store.store(f"{subject}.{predicate}", value,
                                 source=source, confidence=confidence,
                                 decay_policy=decay_policy)

    def strengthen(self, subject: str, predicate: str,
                   delta: float = 0.1) -> Optional[Fact]:
        """Boost a fact's confidence (operator confirmed it)."""
        mf = self._store.strengthen(f"{subject}.{predicate}", delta=delta)
        return Fact.from_memory_fact(mf) if mf else None

    def forget(self, subject: str, predicate: Optional[str] = None) -> bool:
        """Forget one fact (``predicate`` given) or a whole subject."""
        if predicate:
            return self._store.forget(f"{subject}.{predicate}")
        removed = False
        for fact in self.recall(subject=subject, limit=100000):
            removed = self._store.forget(fact.key) or removed
        return removed

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def recall(self, subject: Optional[str] = None,
               predicate: Optional[str] = None,
               limit: int = 50) -> list[Fact]:
        """Recall facts, filtered by subject and/or predicate.

        - ``subject`` only → prefix ``subject.``
        - ``predicate`` only → matches the predicate suffix across subjects
        - both → exact key lookup
        - neither → all facts, newest-first
        """
        if subject and predicate:
            mf = self._store.recall(f"{subject}.{predicate}")
            return [Fact.from_memory_fact(mf)] if mf else []
        prefix = f"{subject}." if subject else None
        if predicate:
            suffix = f".{predicate}"
            return [Fact.from_memory_fact(m)
                    for m in self._store.list(prefix=prefix, limit=100000)
                    if m.key.endswith(suffix)][:limit]
        return [Fact.from_memory_fact(m)
                for m in self._store.list(prefix=prefix, limit=limit)]

    def recall_one(self, subject: str, predicate: str) -> Optional[Fact]:
        """Recall exactly one fact by subject.predicate, or None."""
        facts = self.recall(subject=subject, predicate=predicate, limit=1)
        return facts[0] if facts else None

    def count(self, subject: Optional[str] = None) -> int:
        return self._store.count(prefix=f"{subject}." if subject else None)

    # ------------------------------------------------------------------
    # Decay
    # ------------------------------------------------------------------

    def decay(self, now: Optional[str] = None, **kwargs) -> DecayReport:
        """Run the decay sweep (see ``MemoryStore.decay``)."""
        return self._store.decay(now=now, **kwargs)

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def summary(self, max_facts: int = 15,
                subject: Optional[str] = None) -> str:
        """Natural-language memory block for prompts/briefings.

        Empty when there is nothing remembered (never a fabricated line).
        """
        facts = self.recall(subject=subject, limit=max_facts)
        if not facts:
            return ""
        lines = ["Things I remember about you:"]
        for f in facts:
            source = f" (from {f.source})" if f.source else ""
            lines.append(f"- {f.predicate}: {f.value}{source}")
        return "\n".join(lines)
