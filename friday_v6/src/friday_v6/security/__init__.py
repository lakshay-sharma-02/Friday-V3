"""Security & Quality — vulnerability scanning, secret detection, quality gates.

Wave 3 — implemented. Every scanner has a **built-in, pure-stdlib**
implementation that always works (the machine has no pip-audit /
trufflehog / ruff / bandit installed), plus **optional subprocess**
integrations for those tools when they are present.

Scanners:
    - DependencyAuditor   (``deps.py``)      curated advisory DB + optional pip-audit
    - SecretDetector      (``secrets.py``)   regex/entropy scanning + optional trufflehog
    - QualityGate         (``quality.py``)   AST checks + optional ruff/mypy
    - VulnerabilityScanner (``scanner.py``)  orchestrates all three → SecurityReport
    - SecurityReport      (``reporter.py``)  severity model, scoring, JSON

Design laws (inherited from the rest of V4):
    - Never crash: every external call is wrapped; missing tools degrade
      silently to built-in checks.
    - V4-native: findings are owned by V4 and surfaced via V4's own
      channels (CLI, desktop notification). V3's DB is never written.
"""

from __future__ import annotations

from .deps import DependencyAuditor
from .quality import QualityGate
from .reporter import SEVERITY_ORDER, Finding, SecurityReport
from .scanner import VulnerabilityScanner
from .secrets import SecretDetector

_SECURITY_AVAILABLE = True


def is_available() -> bool:
    """Whether the security & quality layer is implemented."""
    return _SECURITY_AVAILABLE


__all__ = [
    "VulnerabilityScanner",
    "SecretDetector",
    "DependencyAuditor",
    "QualityGate",
    "SecurityReport",
    "Finding",
    "SEVERITY_ORDER",
    "is_available",
    "_SECURITY_AVAILABLE",
]
