"""Evidence-strength model for Friday's inferences (hardening sprint).

Every cross-repository inference and every component carries a strength so the
rest of the system can refuse to make engineering recommendations from weak
evidence alone. This is the single source of truth for the Weak/Medium/Strong
classification required by the audit.
"""

from __future__ import annotations

# Canonical strength levels, ordered weakest -> strongest.
WEAK = "Weak"
MEDIUM = "Medium"
STRONG = "Strong"

# Consolidated vocabulary (source of truth in vocabulary.py).
from .vocabulary import RELATIONSHIP_STRENGTH, COMPONENT_STRENGTH, CONCEPT_COMPONENTS


def relationship_strength(kind: str) -> str:
    return RELATIONSHIP_STRENGTH.get(kind, MEDIUM)


def component_strength(name: str) -> str:
    return COMPONENT_STRENGTH.get(name, MEDIUM)


def is_weak(strength: str) -> bool:
    return strength == WEAK


def is_strong(strength: str) -> bool:
    return strength == STRONG
