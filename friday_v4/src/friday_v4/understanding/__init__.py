"""Understanding Layer — now a thin shim onto the ONE NLU point (Wave 13a).

The Wave 9 deterministic NLU was replaced by :mod:`friday_v4.nlu` —
**LLM-first, deterministic fallback, one ``resolve()`` entry point**
(the Wave 13a directive: no keyword-matching as the product).

This package is kept as a compatibility shim so existing importers
(``missions/planner.py``, tests) keep working — every name re-exports
from ``nlu``. New code imports :mod:`friday_v4.nlu` directly.
"""

from __future__ import annotations

try:
    from ..nlu import (
        Assessment,
        Entity,
        EntityType,
        Intent,
        IntentResult,
        ResolvedAction,
        assess,
        classify,
        extract,
        find_type,
        resolve,
    )
    _UNDERSTANDING_AVAILABLE = True
except ImportError:  # pragma: no cover - defensive stub
    Assessment = None  # type: ignore
    Entity = None  # type: ignore
    EntityType = None  # type: ignore
    Intent = None  # type: ignore
    IntentResult = None  # type: ignore
    ResolvedAction = None  # type: ignore
    assess = None  # type: ignore
    classify = None  # type: ignore
    extract = None  # type: ignore
    find_type = None  # type: ignore
    resolve = None  # type: ignore
    _UNDERSTANDING_AVAILABLE = False


def is_available() -> bool:
    """Whether the NLU layer is implemented (now via ``nlu``)."""
    return _UNDERSTANDING_AVAILABLE


#: Kept for back-compat — the LLM-first default resolver lives in ``nlu``.
def resolve_with_llm(text: str, llm=None):
    """Resolve through the ONE NLU point with an explicit LLM client."""
    from ..nlu import resolve as _resolve
    return _resolve(text, llm=llm)


__all__ = [
    "Assessment",
    "Entity",
    "EntityType",
    "Intent",
    "IntentResult",
    "ResolvedAction",
    "assess",
    "classify",
    "extract",
    "find_type",
    "resolve",
    "resolve_with_llm",
    "is_available",
    "_UNDERSTANDING_AVAILABLE",
]
