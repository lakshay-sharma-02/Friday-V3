"""Capability model + registry — what Friday can do (Wave 16, Law 7).

A :class:`Capability` is one thing Friday can do, described in plain
language with the natural-language intents that reach it. The
:class:`CapabilityRegistry` holds the built-in set and merges learned
skills (self-extension: learning a skill registers a new capability —
Law 2 + Law 7 meet). Every read is guarded — a missing DB or layer
degrades to an empty list, never a crash.

The registry is the answer to "what can you do": the HELP/ASK path
reads it instead of a hardcoded string, so the answer is always the
truth about what Friday can actually do today.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("friday_v4.capability.registry")


@dataclass(frozen=True)
class Capability:
    """One registered capability: what Friday can do + how to reach it.

    ``id`` is stable (``executor:shell``, ``provider:status``,
    ``surface:talk``, ``skill:<name>``). ``intents`` are the NL intents
    that route to it; ``layer`` groups the registry (executor / provider
    / intent / surface / skill). ``permission_level`` mirrors the
    execution gate (auto / confirm / never).
    """

    id: str
    name: str
    description: str
    intents: tuple[str, ...] = ()
    layer: str = "builtin"              # executor | provider | intent | surface | skill
    permission_level: str = "confirm"   # auto | confirm | never
    source: str = "builtin"             # builtin | skill | provider

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "intents": list(self.intents),
            "layer": self.layer,
            "permission_level": self.permission_level,
            "source": self.source,
        }


class CapabilityRegistry:
    """The registry of everything Friday can do (builtins + learned)."""

    def __init__(self, conn=None) -> None:
        self._conn = conn
        self._builtins: dict[str, Capability] = {}
        try:
            from .builtins import register_builtins
            register_builtins(self)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug(f"builtin registration failed: {exc}")

    # ── registration ────────────────────────────────────────────────

    def register(self, cap: Capability) -> None:
        """Register one capability (idempotent by id — last wins)."""
        self._builtins[cap.id] = cap

    # ── reads ───────────────────────────────────────────────────────

    def list(self, include_skills: bool = True) -> list[Capability]:
        """All capabilities: builtins + learned skills (deduped by id).

        Learned skills are dynamic capabilities (self-extension): each
        promoted/verified skill in the V4 DB becomes a ``skill:<name>``
        capability so Friday can say what it learned. A missing DB
        degrades to builtins only.
        """
        caps: dict[str, Capability] = dict(self._builtins)
        if include_skills:
            for skill in self._learned_skills():
                caps[skill.id] = skill
        return sorted(caps.values(), key=lambda c: c.id)

    def get(self, cap_id: str) -> Optional[Capability]:
        for cap in self.list():
            if cap.id == cap_id:
                return cap
        return None

    def by_intent(self, intent: str) -> list[Capability]:
        """Capabilities reachable through one NL intent (e.g. 'execute')."""
        return [c for c in self.list() if intent in c.intents]

    def by_layer(self, layer: str) -> list[Capability]:
        return [c for c in self.list() if c.layer == layer]

    def count(self, include_skills: bool = True) -> int:
        return len(self.list(include_skills=include_skills))

    def search(self, query: str) -> list[Capability]:
        """Case-insensitive substring match on name/description/intents."""
        q = (query or "").strip().lower()
        if not q:
            return self.list()
        return [c for c in self.list()
                if q in c.name.lower() or q in c.description.lower()
                or any(q in i for i in c.intents)]

    def describe(self, cap_id: str) -> Optional[str]:
        cap = self.get(cap_id)
        if cap is None:
            return None
        intents = ", ".join(cap.intents) or "—"
        return (f"{cap.name} — {cap.description} "
                f"[{cap.layer}, {cap.permission_level}; intents: {intents}]")

    def summary(self) -> dict:
        """Counts by layer + the top names, for status/CLI/web surfaces."""
        caps = self.list()
        by_layer: dict[str, int] = {}
        for c in caps:
            by_layer[c.layer] = by_layer.get(c.layer, 0) + 1
        return {
            "total": len(caps),
            "by_layer": by_layer,
            "names": [c.name for c in caps],
        }

    # ── learned skills (self-extension) ─────────────────────────────

    def _learned_skills(self) -> list[Capability]:
        """Promoted/verified skills as capabilities (guarded, never raises)."""
        try:
            from .. import db
            if self._conn is None:
                return []
            rows = db.list_skills(self._conn, limit=100) or []
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug(f"skill capabilities unavailable: {exc}")
            return []
        caps: list[Capability] = []
        for row in rows:
            state = row.get("verification_state") or "shadow"
            name = (row.get("name") or "skill").strip()
            if not name:
                continue
            try:
                steps = row.get("steps") or []
                step_count = len(steps) if isinstance(steps, (list, tuple)) else 0
            except Exception:  # pragma: no cover - defensive
                step_count = 0
            caps.append(Capability(
                id=f"skill:{name}",
                name=name,
                description=(f"learned workflow ({state}, {step_count} "
                             f"step(s))"),
                intents=(name, f"run {name}"),
                layer="skill",
                permission_level="confirm",
                source=state,
            ))
        return caps


# ── module-level helpers (kept next to the registry) ────────────────


def list_capabilities(conn=None, include_skills: bool = True) -> list[Capability]:
    """The full capability list (builtins + skills) — never raises."""
    try:
        return CapabilityRegistry(conn).list(include_skills=include_skills)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug(f"list_capabilities failed: {exc}")
        return []


def capability_count(conn=None, include_skills: bool = True) -> int:
    try:
        return CapabilityRegistry(conn).count(include_skills=include_skills)
    except Exception:  # pragma: no cover - defensive
        return 0


def describe_capabilities(conn=None) -> str:
    """A plain-language list of capabilities for surfaces (never raises)."""
    caps = list_capabilities(conn)
    if not caps:
        return "I don't have any registered capabilities right now."
    by_layer: dict[str, list[str]] = {}
    for c in caps:
        by_layer.setdefault(c.layer, []).append(c.name)
    parts = []
    for layer, names in sorted(by_layer.items()):
        parts.append(f"{layer}: {', '.join(sorted(names)[:12])}")
    return "Here's what I can do: " + "; ".join(parts) + "."


__all__ = ["Capability", "CapabilityRegistry", "capability_count",
           "describe_capabilities", "list_capabilities"]
