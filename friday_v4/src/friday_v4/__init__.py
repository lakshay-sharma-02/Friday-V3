"""Friday V4 — Ambient, Proactive, Multi-Modal AI Operating Partner.

V4 builds on V3's frozen core with:
- Voice-first interaction (STT + TTS + hotword)
- Cross-platform desktop integration
- Mobile companion app
- Multi-instance collaboration
- Security & quality scanning
- Advanced intelligence (drift, anomaly, prediction)
- Proactive anticipation

V4 depends on V3 (friday package) but V3 never depends on V4.
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
}


def __getattr__(name):
    if name in _SUBMODULES:
        try:
            return importlib.import_module(f"friday_v4.{name}")
        except ImportError as e:
            _logger.debug(f"Submodule 'friday_v4.{name}' not available: {e}")
            raise ModuleNotFoundError(
                f"Submodule 'friday_v4.{name}' is not installed. "
                f"Install it with: pip install friday-v4[{name}]"
            ) from e
    raise AttributeError(f"module 'friday_v4' has no attribute {name!r}")


def __dir__():
    return sorted(_SUBMODULES | {"__version__", "__author__", "__email__"})
