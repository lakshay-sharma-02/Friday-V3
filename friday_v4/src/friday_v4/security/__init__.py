"""Security & Quality — Vulnerability scanning, secret detection, code quality.

Adds proactive security monitoring to Friday's daemon cycle. Every change to
a project triggers dependency auditing, secret scanning, and quality gate
checks. Findings are pushed to V3's ambient feed as high-priority events.

Scanners:
    - Dependency vulnerability: OSV.dev / Grype
    - Secret detection: truffleHog / Gitleaks
    - Quality gates: linters, formatters, type checkers

**Status:** Wave 3 — not implemented yet. The imports below are guarded so
importing this package never crashes the rest of Friday V4.
"""

from __future__ import annotations

try:
    from .scanner import VulnerabilityScanner
    from .secrets import SecretDetector
    from .deps import DependencyAuditor
    from .quality import QualityGate
    from .reporter import SecurityReport, Finding
    _SECURITY_AVAILABLE = True
except ImportError:  # pragma: no cover - Wave 3 stub
    VulnerabilityScanner = None  # type: ignore
    SecretDetector = None  # type: ignore
    DependencyAuditor = None  # type: ignore
    QualityGate = None  # type: ignore
    SecurityReport = None  # type: ignore
    Finding = None  # type: ignore
    _SECURITY_AVAILABLE = False


def is_available() -> bool:
    """Whether the security & quality layer is implemented yet."""
    return _SECURITY_AVAILABLE


__all__ = [
    "VulnerabilityScanner",
    "SecretDetector",
    "DependencyAuditor",
    "QualityGate",
    "SecurityReport",
    "Finding",
    "is_available",
    "_SECURITY_AVAILABLE",
]
