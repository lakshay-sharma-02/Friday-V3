"""Security report model — findings, severity, scoring, serialization.

Wave 3 (Security & Quality) — the shared data model for every scanner.
Pure stdlib, no external services.

A :class:`Finding` is one issue found by any scanner (dependency
vulnerability, exposed secret, or code-quality defect). A
:class:`SecurityReport` aggregates findings for one scan, computes a
0-100 score and A-F grade (mirroring the intelligence layer's health
report conventions), and can serialize to JSON for `--json` output or
persistence.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

#: Severity ranking, most severe first.
SEVERITY_ORDER = ("critical", "high", "medium", "low", "info")

#: Score deductions per finding severity (score starts at 100).
_SEVERITY_PENALTY = {
    "critical": 20,
    "high": 10,
    "medium": 4,
    "low": 1,
    "info": 0,
}

#: Score thresholds for the A-F grade.
_GRADE_BANDS = (
    (90, "A"),
    (75, "B"),
    (60, "C"),
    (40, "D"),
    (0, "F"),
)

_SEVERITY_COLOR = {
    "critical": "\033[91m",  # red
    "high": "\033[93m",      # yellow
    "medium": "\033[96m",    # cyan
    "low": "\033[0m",        # reset
    "info": "\033[2m",       # dim
}


@dataclass
class Finding:
    """One security or quality issue found by a scanner.

    Attributes:
        category: ``vulnerability`` | ``secret`` | ``quality``
        severity: one of :data:`SEVERITY_ORDER`
        title: short human-readable headline
        detail: longer explanation / remediation hint
        file: relative path of the affected file ("" for repo-level deps)
        line: line number, 0 when not applicable
        package: affected dependency name (vulnerabilities)
        installed_version: version found (vulnerabilities)
        fixed_version: patched version (vulnerabilities)
        cve: advisory identifier, e.g. ``CVE-2024-23334`` (may be empty)
        detector: source of the finding (``builtin``, ``pip-audit``, …)
        snippet: matched text (secrets) or issue code (quality)
    """

    category: str = "quality"
    severity: str = "low"
    title: str = ""
    detail: str = ""
    file: str = ""
    line: int = 0
    package: str = ""
    installed_version: str = ""
    fixed_version: str = ""
    cve: str = ""
    detector: str = "builtin"
    snippet: str = ""
    id: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            self.id = self._make_id()

    def _make_id(self) -> str:
        """Stable id from the finding's identity fields (dedup + persistence)."""
        import hashlib
        raw = "|".join((self.category, self.severity, self.title,
                        self.file, str(self.line), self.package,
                        self.installed_version))
        return hashlib.sha1(raw.encode()).hexdigest()[:12]

    @property
    def severity_rank(self) -> int:
        return SEVERITY_ORDER.index(self.severity) \
            if self.severity in SEVERITY_ORDER else len(SEVERITY_ORDER) - 1

    def to_dict(self) -> dict:
        return asdict(self)


def severity_color(severity: str) -> str:
    return _SEVERITY_COLOR.get(severity, "\033[0m")


@dataclass
class SecurityReport:
    """Result of one security scan of a directory/project."""

    path: str = "."
    scanned_at: str = ""
    elapsed_seconds: float = 0.0
    findings: list[Finding] = field(default_factory=list)
    scanned_files: int = 0
    tools: dict[str, bool] = field(default_factory=dict)  # detector -> available

    # -- aggregation ---------------------------------------------------

    def counts_by_severity(self) -> dict[str, int]:
        out = {s: 0 for s in SEVERITY_ORDER}
        for f in self.findings:
            out[f.severity] = out.get(f.severity, 0) + 1
        return out

    def counts_by_category(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for f in self.findings:
            out[f.category] = out.get(f.category, 0) + 1
        return out

    def highest_severity(self) -> Optional[str]:
        if not self.findings:
            return None
        return min((f for f in self.findings),
                   key=lambda f: f.severity_rank).severity

    # -- scoring -------------------------------------------------------

    def score(self) -> int:
        """0-100 score: start at 100, subtract per-finding penalties."""
        total = 100
        for f in self.findings:
            total -= _SEVERITY_PENALTY.get(f.severity, 0)
        return max(total, 0)

    def grade(self) -> str:
        score = self.score()
        for threshold, letter in _GRADE_BANDS:
            if score >= threshold:
                return letter
        return "F"

    # -- filtering -----------------------------------------------------

    def above_threshold(self, threshold: str = "medium") -> list[Finding]:
        """Findings at or above a severity threshold (default: medium)."""
        if threshold not in SEVERITY_ORDER:
            threshold = "medium"
        rank = SEVERITY_ORDER.index(threshold)
        return [f for f in self.findings if f.severity_rank <= rank]

    def by_category(self, category: str) -> list[Finding]:
        return [f for f in self.findings if f.category == category]

    # -- serialization -------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "scanned_at": self.scanned_at,
            "elapsed_seconds": round(self.elapsed_seconds, 2),
            "score": self.score(),
            "grade": self.grade(),
            "scanned_files": self.scanned_files,
            "counts_by_severity": self.counts_by_severity(),
            "counts_by_category": self.counts_by_category(),
            "tools": self.tools,
            "findings": [f.to_dict() for f in self.findings],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, default=str)

    def summary(self) -> str:
        """One-line human summary, e.g. ``B (82) — 2 high, 3 medium``."""
        counts = self.counts_by_severity()
        parts = [f"{s} {counts[s]}" for s in SEVERITY_ORDER if counts[s] > 0]
        label = ", ".join(parts) if parts else "clean"
        return f"{self.grade()} ({self.score()}) — {label}"

    def sort_findings(self) -> "SecurityReport":
        """Return a copy with findings sorted most-severe first."""
        out = SecurityReport(
            path=self.path, scanned_at=self.scanned_at,
            elapsed_seconds=self.elapsed_seconds,
            scanned_files=self.scanned_files, tools=self.tools)
        out.findings = sorted(self.findings,
                              key=lambda f: (f.severity_rank, f.category))
        return out


def _to_int(value: Any) -> int:
    """Best-effort int conversion (0 on failure) — tool JSON is untrusted."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def findings_from_tool(category: str, detector: str,
                       items: list[dict[str, Any]]) -> list[Finding]:
    """Build :class:`Finding` objects from a scanner's raw dict results.

    The per-item ``detector`` (e.g. ``builtin``, ``pip-audit``,
    ``trufflehog``) is preserved when present; ``detector`` is the
    fallback for items that don't carry one.
    """
    out: list[Finding] = []
    for item in items:
        severity = str(item.get("severity", "low")).lower()
        if severity not in SEVERITY_ORDER:
            severity = "low"
        out.append(Finding(
            category=category,
            severity=severity,
            title=str(item.get("title") or "Issue"),
            detail=str(item.get("detail") or ""),
            file=str(item.get("file") or ""),
            line=_to_int(item.get("line")),
            package=str(item.get("package") or ""),
            installed_version=str(item.get("installed_version") or ""),
            fixed_version=str(item.get("fixed_version") or ""),
            cve=str(item.get("cve") or ""),
            detector=str(item.get("detector") or detector),
            snippet=str(item.get("snippet") or ""),
        ))
    return out
