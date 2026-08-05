"""Natural Language Understanding — the ONE NLU point (Wave 13a).

**The directive:** every surface (voice, `friday6 talk`, web chat) routes
through ONE parser — ``resolve()`` — which is **LLM-first**. The
deterministic rules are a *fallback only when the LLM is absent/offline*,
never the primary path, and never a keyword-matching substitute for
understanding.

```
utterance ──► resolve() ──► LLM parse (primary)
                     └──► deterministic rules (fallback: no LLM/offline)
                            └──► canonical ResolvedAction
```

Design laws:
- **One point:** voice router, `friday6 talk`, web chat all call
  ``resolve()``. No surface parses language itself.
- **LLM primary:** intent + entities + confidence come from the LLM
  through the 9router proxy (``localhost:20128/v1``, configurable model).
- **Deterministic fallback:** only when the LLM is unavailable — keeps
  the never-crash law without keyword-matching being the product.
- **Never crash:** every branch guarded; unknown input → clarification,
  never an error.

**Status:** Wave 13a — built (2026-08). Pure stdlib (urllib LLM client,
rules fallback), hermetic tests, never-crash.

Usage:
    from friday_v6.nlu import resolve
    action = resolve("run the tests")       # LLM-first, rules fallback
"""

from __future__ import annotations

try:
    from .llm import LLMClient, config_from_env
    from .resolver import resolve, ResolvedAction
    from .intent import Intent, IntentResult, classify
    from .entities import Entity, EntityType, extract, find_type
    from .confidence import Assessment, assess
    _NLU_AVAILABLE = True
except ImportError:  # pragma: no cover - defensive stub
    LLMClient = None  # type: ignore
    config_from_env = None  # type: ignore
    resolve = None  # type: ignore
    ResolvedAction = None  # type: ignore
    Intent = None  # type: ignore
    IntentResult = None  # type: ignore
    classify = None  # type: ignore
    Entity = None  # type: ignore
    EntityType = None  # type: ignore
    extract = None  # type: ignore
    find_type = None  # type: ignore
    Assessment = None  # type: ignore
    assess = None  # type: ignore
    _NLU_AVAILABLE = False


def is_available() -> bool:
    return _NLU_AVAILABLE


__all__ = [
    "LLMClient",
    "config_from_env",
    "resolve",
    "ResolvedAction",
    "Intent",
    "IntentResult",
    "classify",
    "Entity",
    "EntityType",
    "extract",
    "find_type",
    "Assessment",
    "assess",
    "is_available",
    "_NLU_AVAILABLE",
]
