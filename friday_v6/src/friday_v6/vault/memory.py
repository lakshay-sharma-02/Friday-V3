"""MemoryFact — the single bridge between vault prose and SQLite facts (W1).

V6 law: *DB = structured truth, vault = prose memory.* Missions, asks,
audit, sessions stay in SQLite (they need queries and transactions);
the vault carries what a human wants to *read*. :class:`MemoryFact` is
the ONE code path that keeps a fact on both sides in sync:

- **remember()** writes the SQLite ``memories`` row (via the existing
  ``FactMemory`` — value, source, confidence, decay policy unchanged)
  AND writes a wiki note carrying the V3 evidence convention
  (``sources:`` frontmatter). One call, two surfaces.
- **recall()** reads the structured rows (source of truth); the note
  path is derivable, so any surface can show the prose too.
- **forget()** removes the DB rows AND the matching wiki notes.

Never-crash: each side is guarded independently — a missing DB still
writes the note, an unwritable vault still stores the fact. Hermetic
tests always pass a tmp vault root + tmp DB.
"""

from __future__ import annotations

import datetime
import logging
from pathlib import Path
from typing import Optional

from .vault import Vault, _slug

logger = logging.getLogger("friday_v6.vault.memory")

try:  # never-crash — FactMemory is pure stdlib, but stay defensive
    from friday_v6.memory import DECAY_USAGE, Fact, FactMemory
    _FACTS_AVAILABLE = True
except Exception:  # pragma: no cover - defensive stub
    FactMemory = None  # type: ignore
    Fact = None  # type: ignore
    DECAY_USAGE = "usage"  # type: ignore
    _FACTS_AVAILABLE = False

def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _note_name(subject: str, predicate: str) -> str:
    """Wiki note file name for a fact: ``operator-prefers_rust.md``."""
    return f"{_slug(f'{subject}-{predicate}') or 'fact'}.md"


def _parse_frontmatter(text: str) -> dict:
    """Parse a ``---``-delimited frontmatter block (pure stdlib)."""
    out: dict = {}
    body = text or ""
    if body.startswith("---"):
        end = body.find("\n---", 3)
        if end != -1:
            block = body[3:end]
            body = body[end + 4:]
            for line in block.splitlines():
                if ":" in line:
                    key, _, value = line.partition(":")
                    out[key.strip()] = value.strip()
    return out


class MemoryFact:
    """One write path keeping SQLite facts and vault notes in sync."""

    def __init__(self, conn=None, vault: Optional[Vault] = None) -> None:
        self._conn = conn
        self.vault = vault or Vault()

    # ── the one write path ───────────────────────────────────────────

    def remember(self, subject: str, predicate: str, value: str,
                 source: str = "", confidence: float = 0.6,
                 decay_policy: str = DECAY_USAGE) -> Optional[dict]:
        """Store a fact on BOTH sides. Returns ``{fact, note}`` on
        success (None only when both sides failed). Never raises."""
        subject = (subject or "operator").strip()
        predicate = (predicate or "note").strip()
        value = (value or "").strip()
        # An empty value is not a fact — honest no-op, never a stored lie.
        if not value:
            return None
        fact = None
        note = None

        # Structured side — SQLite row (queries, decay, persona).
        if self._conn is not None and _FACTS_AVAILABLE:
            try:
                facts = FactMemory(self._conn)
                facts.remember(subject, predicate, value,
                               source=source, confidence=confidence,
                               decay_policy=decay_policy)
                fact = facts.recall_one(subject, predicate)
            except Exception as exc:
                logger.debug(f"MemoryFact db write failed: {exc}")

        # Prose side — vault wiki note with the evidence convention.
        try:
            note = self._write_note(subject, predicate, value,
                                    source=source, confidence=confidence)
        except Exception as exc:
            logger.debug(f"MemoryFact vault write failed: {exc}")

        if fact is None and note is None:
            return None
        return {"fact": fact, "note": str(note) if note else None}

    def _write_note(self, subject: str, predicate: str, value: str,
                    source: str, confidence: float) -> Path:
        """The wiki note for a fact, with ``sources:`` frontmatter."""
        updated = _now()
        frontmatter = "\n".join(
            f"{key}: {val}"
            for key, val in (
                ("subject", subject),
                ("predicate", predicate),
                ("source", source or ""),
                ("sources", source or ""),
                ("confidence", f"{confidence:.3f}"),
                ("updated", updated),
            ))
        body = (
            f"---\n{frontmatter}\n---\n\n"
            f"# {subject}.{predicate}\n\n"
            f"{value}\n\n"
            f"<!-- prose note — the SQLite row is the fact of record; "
            f"re-storing via `friday6 fact store` rewrites this -->\n")
        path = self.vault.wiki / _note_name(subject, predicate)
        path.write_text(body, encoding="utf-8")
        return path

    # ── reads ────────────────────────────────────────────────────────

    def recall(self, subject: Optional[str] = None,
               predicate: Optional[str] = None,
               limit: int = 50) -> list:
        """Structured recall — the SQLite rows are the truth. Returns
        [] (never raises) when the DB is missing."""
        if self._conn is None or not _FACTS_AVAILABLE:
            return []
        try:
            return FactMemory(self._conn).recall(
                subject=subject, predicate=predicate, limit=limit)
        except Exception as exc:
            logger.debug(f"MemoryFact recall failed: {exc}")
            return []

    def recall_one(self, subject: str, predicate: str):
        """One fact or None (never raises)."""
        try:
            facts = self.recall(subject=subject, predicate=predicate,
                                limit=1)
            return facts[0] if facts else None
        except Exception:
            return None

    def count(self, subject: Optional[str] = None) -> int:
        try:
            return FactMemory(self._conn).count(subject=subject)
        except Exception:
            return 0

    # ── notes ────────────────────────────────────────────────────────

    def note_path(self, subject: str, predicate: str) -> Path:
        """The wiki note path for a fact (derivable, even if absent)."""
        return self.vault.wiki / _note_name(subject, predicate)

    def read_note(self, subject: str, predicate: str) -> Optional[str]:
        """The note's markdown, or None when it doesn't exist."""
        path = self.note_path(subject, predicate)
        try:
            if not path.exists():
                return None
            return path.read_text(encoding="utf-8")
        except OSError:
            return None

    def sources_for(self, subject: str, predicate: str) -> list[str]:
        """The ``sources:`` evidence list from the note (V3 discipline)."""
        text = self.read_note(subject, predicate)
        if not text:
            return []
        raw = _parse_frontmatter(text).get("sources", "")
        return [s.strip() for s in raw.split(",") if s.strip()]

    # ── forget ───────────────────────────────────────────────────────

    def forget(self, subject: str,
               predicate: Optional[str] = None) -> bool:
        """Remove DB rows AND their wiki notes. Returns True when
        anything was removed. Never raises.

        Note deletion is glob-based and never depends on the DB: a
        missing DB, or a drifted row, still cleans the orphan notes.
        """
        removed = False
        # Structured side first.
        if self._conn is not None and _FACTS_AVAILABLE:
            try:
                removed = FactMemory(self._conn).forget(subject, predicate) \
                    or removed
            except Exception as exc:
                logger.debug(f"MemoryFact db forget failed: {exc}")
        # Prose side — the note for (subject, predicate), or every note
        # under ``<subject>-`` when forgetting a whole subject.
        subject = (subject or "").strip()
        if not subject:
            return removed
        if predicate:
            targets = [(subject, predicate)]
        else:
            prefix = f"{_slug(subject)}-"
            targets = []
            for path in sorted(self.vault.wiki.glob(f"{prefix}*.md")):
                rest = path.stem[len(prefix):]
                if rest:
                    targets.append((subject, rest))
        for s, p in targets:
            try:
                path = self.note_path(s, p)
                if path.exists():
                    path.unlink()
                    removed = True
            except OSError as exc:
                logger.debug(f"MemoryFact note delete failed: {exc}")
        return removed


#: Public alias (used by the tests).
parse_frontmatter = _parse_frontmatter

__all__ = ["MemoryFact", "parse_frontmatter"]
