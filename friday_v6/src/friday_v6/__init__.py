"""Friday V6 — the product. Ambient, Proactive, Multi-Modal AI Operating Partner.

V6 is the synthesis: the verified V4 product (frozen reference), re-seeded
into this package, with the V5 wins (vault memory, Textual HUD, markdown
skills) and the V3 evidence discipline layered on top.

Inherited from V4 (unchanged, re-verified green):
- Voice-first interaction (STT + TTS + hotword)
- Cross-platform desktop integration
- Mobile companion app + PWA
- Multi-instance collaboration
- Security & quality scanning
- Advanced intelligence (drift, anomaly, prediction)
- Proactive anticipation
- Gated, sandboxed, audited execution (Wave 9)

V6 is the product — its own daemon, CLI, config, state, and tests. V3 is
optional legacy *data*, read through the read-only ``proactive/v3source.py``
bridge when present; V4 is frozen and never imported by V6.
"""

__version__ = "0.1.0.dev0"
__author__ = "Friday Team"
__email__ = "friday@example.com"

__all__ = [
    "__version__",
    "__author__",
    "__email__",
    "voice",
    "desktop",
    "mobile",
    "collab",
    "security",
    "intelligence",
    "network",
    "proactive",
    "execution",
    "understanding",
    "missions",
    "reasoning",
    "memory",
    "persona",
    "relationship",
    "skills",
    "autonomy",
    "vault",
]

# Lazy submodule loading — only import modules that exist
# Each module is loaded on first access via __getattr__
import importlib
import logging

_logger = logging.getLogger(__name__)

_SUBMODULES = {
    "voice",
    "desktop",
    "mobile",
    "collab",
    "security",
    "intelligence",
    "network",
    "proactive",
    "execution",
    "understanding",
    "missions",
    "reasoning",
    "memory",
    "persona",
    "relationship",
    "skills",
    "autonomy",
    "vault",
}


def __getattr__(name):
    if name in _SUBMODULES:
        try:
            return importlib.import_module(f"friday_v6.{name}")
        except ImportError as e:
            _logger.debug(f"Submodule 'friday_v6.{name}' not available: {e}")
            raise ModuleNotFoundError(
                f"Submodule 'friday_v6.{name}' is not installed. "
                f"Install it with: pip install friday-v6[{name}]"
            ) from e
    raise AttributeError(f"module 'friday_v6' has no attribute {name!r}")


def __dir__():
    return sorted(_SUBMODULES | {"__version__", "__author__", "__email__"})
