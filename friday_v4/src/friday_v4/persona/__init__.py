"""Persona Layer — Friday knows who you are (Wave 10).

Rebuilds V3's personality stack V4-native on top of the ``memory/``
facts layer: name learning, preference extraction, and tone — all
explicit-consent-first (only learns what the operator states) and all
stored as facts with provenance (never a hidden profile store).

    engine (engine.py) — IdentityEngine: learn → facts, profile view
    learn (learn.py)   — pure explicit-consent extraction rules
    prompts (prompts.py)— persona context block (name/tone/preferences)

Usage:
    from friday_v4.persona import IdentityEngine
    engine = IdentityEngine(conn)
    engine.learn("Call me Lakshay, by the way.")  # → ack, stores fact
    profile = engine.profile()                    # → view over facts
"""

from __future__ import annotations

try:
    from .engine import IdentityEngine
    from .learn import recent_statements, record_statement, statement_count
    from .prompts import build_persona_context
    _PERSONA_AVAILABLE = True
except ImportError:  # pragma: no cover - defensive stub
    IdentityEngine = None  # type: ignore
    recent_statements = None  # type: ignore
    record_statement = None  # type: ignore
    statement_count = None  # type: ignore
    build_persona_context = None  # type: ignore
    _PERSONA_AVAILABLE = False


def is_available() -> bool:
    """Whether the persona layer is implemented yet."""
    return _PERSONA_AVAILABLE


__all__ = [
    "IdentityEngine",
    "recent_statements",
    "record_statement",
    "statement_count",
    "build_persona_context",
    "is_available",
    "_PERSONA_AVAILABLE",
]
