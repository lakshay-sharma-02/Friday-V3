"""Operator Profile — who you are and what you consistently prefer.

Package structure mirrors knowledge/, understanding/, initiative/, insight/:
- models.py:      OperatorProfile dataclass
- derivation.py:  Evidence-derived preference computation
- engine.py:      Build profile + integration helpers
"""

from __future__ import annotations

from .engine import build_operator_profile, derive_preferences, get_active_repos
from .models import OperatorProfile

__all__ = [
    "OperatorProfile",
    "build_operator_profile",
    "derive_preferences",
    "get_active_repos",
]
