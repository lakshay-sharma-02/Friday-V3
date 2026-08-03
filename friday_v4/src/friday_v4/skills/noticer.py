"""RepetitionNoticer — notices patterns before being asked (Wave 14 §3.2).

The MCU five-moment *"I noticed you do this every time"*: while the
operator works, Friday notices a repeated ordered sequence in the audit
log and **offers** to form a skill — it does not form one silently.

    noticer = RepetitionNoticer(conn)
    offers = noticer.notice()   # → [{"sequence", "count", "context", "offer"}]

Rules:
- Reuses ``ReplayExecutor.find_patterns`` for the repeated sequences.
- Only *new* patterns are offered — anything already captured as a
  skill (by matching signature prefix) is skipped, so the noticer never
  re-offers what Friday already knows.
- Offers are pure reads: nothing is formed until the operator accepts
  (the CLI/watch NL path does the forming). Never raises.
"""

from __future__ import annotations

import logging
from typing import Optional

from .. import db
from .registry import SkillRegistry
from .replay import ReplayExecutor, _action_signature

logger = logging.getLogger("friday_v4.skills.noticer")


def _signature_prefix(steps: list) -> tuple[str, ...]:
    """The first step signature a skill matches on (for dedup)."""
    for step in steps or []:
        atype = (step.get("action_type") or "").strip()
        if atype:
            cmd = (step.get("command") or "").strip()
            return (f"{atype}:{cmd[:40]}" if cmd else atype,)
    return ()


class RepetitionNoticer:
    """Offers skills for repeated patterns the operator hasn't asked about."""

    def __init__(self, conn, registry: Optional[SkillRegistry] = None,
                 min_occurrences: int = 2,
                 lookback: int = 200) -> None:
        self._conn = conn
        self._registry = registry or SkillRegistry(conn)
        self._min_occurrences = min_occurrences
        self._lookback = lookback

    def _existing_prefixes(self) -> set[tuple[str, ...]]:
        """Signatures already covered by existing skills (skip these)."""
        prefixes: set[tuple[str, ...]] = set()
        for skill in self._registry.list(limit=500):
            prefix = _signature_prefix(skill.steps)
            if prefix:
                prefixes.add(prefix)
        return prefixes

    def notice(self, limit: int = 5) -> list[dict]:
        """Repeated patterns not yet learned, as natural-language offers.

        Returns up to ``limit`` offers; each is a pure-read report —
        nothing is formed here (the operator accepts, then the watch/CLI
        path forms the skill). Never raises.
        """
        try:
            replay = ReplayExecutor(
                self._conn,
                min_occurrences=self._min_occurrences,
                lookback=self._lookback,
                registry=self._registry,
            )
            patterns = replay.find_patterns(length=2)[:limit * 3]
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug(f"noticer pattern scan failed: {exc}")
            return []
        if not patterns:
            return []

        known = self._existing_prefixes()
        offers: list[dict] = []
        for pattern in patterns:
            seq = pattern.get("sequence") or []
            if not seq:
                continue
            if (seq[0],) in known:
                continue  # already a skill — never re-offer
            count = pattern.get("count", 0)
            example = pattern.get("example") or {}
            context = ""
            cwd = (example.get("cwd") or "").strip()
            if cwd:
                from pathlib import Path
                context = Path(cwd).name
            first = _action_signature(example) if example else seq[0]
            offers.append({
                "sequence": seq,
                "count": count,
                "context": context,
                "first": first,
                # The raw audited action that exemplified the pattern —
                # carried so an accepted offer can form a skill with the
                # real evidence trail (cwd, goal, exact commands) instead
                # of a bare signature. Pure read; nothing executes.
                "example": example,
                "offer": _offer_sentence(first, count, context),
            })
            if len(offers) >= limit:
                break
        return offers


def _offer_sentence(first: str, count: int, context: str) -> str:
    """'I noticed you run pytest after editing tests every time…'"""
    where = f" in {context}" if context else ""
    return (f"I noticed you do this every time{where} — you "
            f"{first} (seen {count}×). Want me to form a skill for it?")


__all__ = ["RepetitionNoticer"]
