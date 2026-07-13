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

# Relationship-kind -> strength.
# Author/org/language are coincidences, not architecture. Framework/db/deploy/
# architecture are meaningful shared technology. Implementation-level relations
# (computed elsewhere, e.g. from matching import graphs) are Strong.
RELATIONSHIP_STRENGTH: dict[str, str] = {
    "shared-implementation": STRONG,
    "shared-abstraction": STRONG,
    "shared-architecture": MEDIUM,
    "shared-framework": MEDIUM,
    "shared-deployment": MEDIUM,
    "shared-db": MEDIUM,
    "shared-config": MEDIUM,
    "potential-reuse": MEDIUM,
    "duplicated-functionality": MEDIUM,
    "shared-tech": MEDIUM,
    "shared-lang-ecosystem": WEAK,
    "shared-language": WEAK,
    "shared-org": WEAK,
    "shared-author": WEAK,
}

# Components are concepts, not implementations. Detected by filename/import, they
# are Weak: "has a db.py" != "has reusable database logic". They remain listed
# as components but must never drive a reuse recommendation on their own.
COMPONENT_STRENGTH: dict[str, str] = {
    "Authentication": WEAK,
    "Database": WEAK,
    "Configuration": WEAK,
    "Routing": WEAK,
    "Storage": WEAK,
    "Logging": WEAK,
    "CLI": WEAK,
    "LLM interface": WEAK,
    "Caching": WEAK,
    "Networking": WEAK,
    "Testing": WEAK,
}

# Concept names whose mere presence is explicitly forbidden from generating a
# code-reuse recommendation (audit: "Database / Configuration / Authentication /
# Logging / Storage / Routing are concepts, not evidence").
CONCEPT_COMPONENTS = set(COMPONENT_STRENGTH)


def relationship_strength(kind: str) -> str:
    return RELATIONSHIP_STRENGTH.get(kind, MEDIUM)


def component_strength(name: str) -> str:
    return COMPONENT_STRENGTH.get(name, MEDIUM)


def is_weak(strength: str) -> bool:
    return strength == WEAK


def is_strong(strength: str) -> bool:
    return strength == STRONG
