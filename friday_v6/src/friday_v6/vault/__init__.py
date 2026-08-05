"""Vault — Friday V6's memory as linked markdown (Wave 0).

A faithful port of V5's vault (raw/ wiki/ outputs/ notices/,
``[[links]]``, grep query) plus a rebuildable FTS index cache
(:mod:`.index`) — the index is a cache, grep is the truth.

Import is side-effect free: constructing a :class:`Vault` creates
directories, so callers use :func:`default_vault` (or pass a root)
explicitly. Hermetic tests always pass a tmp root.
"""

from .vault import Vault, default_vault, DEFAULT_VAULT
from .index import VaultIndex, fts5_available
from .memory import MemoryFact, parse_frontmatter

__all__ = [
    "Vault",
    "VaultIndex",
    "MemoryFact",
    "default_vault",
    "DEFAULT_VAULT",
    "fts5_available",
    "parse_frontmatter",
]
