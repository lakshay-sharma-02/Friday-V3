"""Synthesis Layer — deterministic, evidence-cited reports (Wave 11 §3.2).

Synthesis is **composition of evidence, never invention**: every
paragraph maps to cited findings, and the same evidence set always
produces the same report (deterministic — testable).

**Status:** Wave 11 — built (2026-08). Pure stdlib, hermetic tests.

Usage:
    from friday_v6.synthesis import synthesize
    report = synthesize(title="Security digest", sections={
        "vulnerabilities": [
            "2 high-sev: CVE-2026-0001 (auth)", "1 medium: CVE-2026-0002",
        ],
    })
"""

from __future__ import annotations

try:
    from .reports import build_daily_report, build_weekly_report
    from .synthesis import synthesize, SynthesisReport
    _SYNTHESIS_AVAILABLE = True
except ImportError:  # pragma: no cover - defensive stub
    synthesize = None  # type: ignore
    SynthesisReport = None  # type: ignore
    build_daily_report = None  # type: ignore
    build_weekly_report = None  # type: ignore
    _SYNTHESIS_AVAILABLE = False


def is_available() -> bool:
    return _SYNTHESIS_AVAILABLE


__all__ = ["synthesize", "SynthesisReport", "build_daily_report",
           "build_weekly_report", "is_available", "_SYNTHESIS_AVAILABLE"]
