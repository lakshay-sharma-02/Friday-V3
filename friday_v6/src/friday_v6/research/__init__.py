"""Research Layer — architecture, cross-project correlation, impact,
code search, README purpose recovery (Wave 11).

The "researched before asked" layer: ``analyze(repo)`` produces a
cached, evidence-cited analysis of a repo; ``correlate(a, b)`` estimates
integration cost with a range + confidence; ``impact`` estimates blast
radius of a change. Every finding carries the repo/file/line it came
from (evidence — the Wave 9 contract).

Design laws (Wave 11 doc §3.1):
- Analysis is evidence-cited — every claim carries its source.
- Integration-cost estimates are range + confidence, never false
  precision ("~3 days, confidence: medium").
- Analysis runs are cached with invalidation (repo hash + time) —
  Friday "already did that" only when it actually did.

**Status:** Wave 11 — built (2026-08). Pure stdlib, hermetic tests,
never-crash.

Usage:
    from friday_v6.research import analyze, correlate, CodeSearch
    rep = analyze("~/Projects/vivaha")        # cached
    est = correlate("~/Projects/vivaha", "~/Projects/MindWell")
"""

from __future__ import annotations

try:
    from .architecture import analyze
    from .cross_project import correlate
    from .code_search import CodeSearch
    from .impact import impact
    from .readme import readme_purpose
    _RESEARCH_AVAILABLE = True
except ImportError:  # pragma: no cover - defensive stub
    analyze = None  # type: ignore
    correlate = None  # type: ignore
    CodeSearch = None  # type: ignore
    impact = None  # type: ignore
    readme_purpose = None  # type: ignore
    _RESEARCH_AVAILABLE = False


def is_available() -> bool:
    """Whether the research layer is implemented yet."""
    return _RESEARCH_AVAILABLE


__all__ = [
    "analyze",
    "correlate",
    "CodeSearch",
    "impact",
    "readme_purpose",
    "is_available",
    "_RESEARCH_AVAILABLE",
]
