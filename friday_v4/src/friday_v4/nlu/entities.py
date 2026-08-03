"""Entities — LLM-first extraction with deterministic fallback (Wave 13a).

The LLM returns entities as part of its canonical action JSON (see
``intent.py``); the deterministic extractor (kept from Wave 9) runs only
when the LLM is unavailable, producing the same shape.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class EntityType(str, Enum):
    """The entity kinds the resolver understands."""

    PATH = "path"
    FILE = "file"
    REPO = "repo"
    APP = "app"
    TIME = "time"
    PERSON = "person"


@dataclass
class Entity:
    """One extracted entity: type + value."""

    type: EntityType
    value: str

    def to_dict(self) -> dict:
        return {"type": self.type.value, "value": self.value}


# ── deterministic fallback (ONLY when the LLM is unavailable) ─────────

_PATH_RE = re.compile(r"(?:/[\w.\-/]+)+")
_REPO_RE = re.compile(r"\b[\w.-]+(?:-|_)?[\w.-]+\b", re.IGNORECASE)


def _fallback_extract(text: str) -> list[Entity]:
    out: list[Entity] = []
    seen: set[str] = set()

    def add(t: EntityType, v: str):
        key = f"{t.value}:{v}"
        if v and key not in seen:
            seen.add(key)
            out.append(Entity(t, v))

    # Absolute paths first.
    for m in _PATH_RE.finditer(text):
        add(EntityType.PATH, m.group(0))
    # Named after "the X repo"/"repo X".
    for m in re.finditer(r"\brepo\s+([\w.-]+)", text, re.IGNORECASE):
        add(EntityType.REPO, m.group(1))
    # Window/app after desktop verbs.
    for verb in ("focus", "switch", "open", "launch"):
        m = re.search(rf"\b{verb}\s+([\w\s.-]+)", text, re.IGNORECASE)
        if m:
            add(EntityType.APP, m.group(1).strip()[:40])
    return out


def find_type(entities: list[Entity], etype: EntityType) -> Optional[Entity]:
    return next((e for e in entities if e.type == etype), None)


def extract(text: str, entity_values: Optional[list[dict]] = None) -> list[Entity]:
    """Entities from the LLM's canonical JSON, or the rules fallback.

    Args:
        text: The utterance (used by the fallback).
        entity_values: The LLM's ``entities`` list
            ([{"type": "...", "value": "..."}]).
    """
    if entity_values:
        out: list[Entity] = []
        for e in entity_values:
            t = e.get("type") if isinstance(e, dict) else None
            v = e.get("value") if isinstance(e, dict) else None
            if not t or not v:
                continue
            try:
                et = EntityType(t)
            except ValueError:
                continue
            out.append(Entity(et, str(v)))
        if out:
            return out
    return _fallback_extract(text or "")


__all__ = ["Entity", "EntityType", "extract", "find_type"]
