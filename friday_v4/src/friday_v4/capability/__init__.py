"""Capability Registry — what Friday can do, known to Friday (Wave 16, Law 7).

The registry makes self-extension real: every executor, provider, skill,
and surface is registered with the natural-language intents that reach
it. Friday *knows what it can do* — and can say so ("what can you do"
is answered from the real registry, never a hardcoded list), and can
compose capabilities later (MCU test #5 groundwork).

    registry (registry.py) — Capability model + CapabilityRegistry
    builtins (builtins.py) — the built-in capabilities (executors,
        providers, intents, surfaces, learned skills)

Usage:
    from friday_v4.capability import CapabilityRegistry
    reg = CapabilityRegistry(conn)
    caps = reg.list()          # builtins + learned skills
    reg.describe("executor:shell")

**Status:** Wave 16 — built (2026-08). Pure stdlib, hermetic tests,
never-crash (a missing registry degrades to an honest empty answer).
"""

from __future__ import annotations

try:
    from .builtins import BUILTIN_CAPABILITIES, register_builtins
    from .registry import (
        Capability,
        CapabilityRegistry,
        capability_count,
        describe_capabilities,
        list_capabilities,
    )
    _CAPABILITY_AVAILABLE = True
except ImportError:  # pragma: no cover - defensive stub
    BUILTIN_CAPABILITIES = ()  # type: ignore
    register_builtins = None  # type: ignore
    Capability = None  # type: ignore
    CapabilityRegistry = None  # type: ignore
    capability_count = lambda conn: 0  # type: ignore
    describe_capabilities = None  # type: ignore
    list_capabilities = None  # type: ignore
    _CAPABILITY_AVAILABLE = False


def is_available() -> bool:
    """Whether the capability registry layer is implemented yet."""
    return _CAPABILITY_AVAILABLE


__all__ = [
    "BUILTIN_CAPABILITIES",
    "register_builtins",
    "Capability",
    "CapabilityRegistry",
    "capability_count",
    "describe_capabilities",
    "list_capabilities",
    "is_available",
    "_CAPABILITY_AVAILABLE",
]
