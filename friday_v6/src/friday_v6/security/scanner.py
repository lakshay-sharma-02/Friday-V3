"""VulnerabilityScanner — orchestrates the Wave 3 security scan.

Runs the dependency auditor, secret detector, and quality gate over a
project directory and aggregates everything into a :class:`SecurityReport`.
Pure stdlib; every scanner degrades gracefully when an external tool is
missing. Never raises.
"""

from __future__ import annotations

import datetime
import logging
import time
from pathlib import Path

from .deps import DependencyAuditor
from .quality import QualityGate
from .reporter import SecurityReport, findings_from_tool
from .secrets import SecretDetector

logger = logging.getLogger("friday_v6.security.scanner")

_CATEGORY_NAMES = {
    "vulnerability": "Dependencies",
    "secret": "Secrets",
    "quality": "Quality",
}


def _iso_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="seconds")


class VulnerabilityScanner:
    """One-command security + quality scan of a project directory.

    Usage:
        report = VulnerabilityScanner().scan(".")
        print(report.summary())          # 'B (82) — 2 high, 3 medium'
        report.to_json()                 # machine-readable
    """

    def __init__(self, subprocess_timeout: float = 120.0):
        self.auditor = DependencyAuditor(subprocess_timeout=subprocess_timeout)
        self.secrets = SecretDetector(subprocess_timeout=subprocess_timeout)
        self.quality = QualityGate(subprocess_timeout=subprocess_timeout)

    def scan(self, path: str | Path = ".",
             enable_deps: bool = True,
             enable_secrets: bool = True,
             enable_quality: bool = True) -> SecurityReport:
        """Scan a path and return an aggregated report."""
        path = Path(path)
        started = time.time()
        report = SecurityReport(path=str(path), scanned_at=_iso_now())

        tools: dict[str, bool] = {}

        if enable_deps:
            try:
                dep_findings, dep_tools = self.auditor.scan(path)
                report.findings.extend(
                    findings_from_tool("vulnerability", "deps", dep_findings))
                tools.update(dep_tools)
            except Exception as exc:
                logger.debug(f"Dependency scan failed: {exc}")

        if enable_secrets:
            try:
                sec_findings, sec_tools = self.secrets.scan(path)
                report.findings.extend(
                    findings_from_tool("secret", "secrets", sec_findings))
                tools.update(sec_tools)
            except Exception as exc:
                logger.debug(f"Secret scan failed: {exc}")

        if enable_quality:
            try:
                q_findings, q_tools = self.quality.scan(path)
                report.findings.extend(
                    findings_from_tool("quality", "quality", q_findings))
                tools.update(q_tools)
            except Exception as exc:
                logger.debug(f"Quality scan failed: {exc}")

        # Number of scanned source/manifest files (best-effort).
        report.scanned_files = self._count_scanned(path)
        report.tools = tools
        report.elapsed_seconds = time.time() - started
        return report.sort_findings()

    def scan_quick(self, path: str | Path = ".",
                   threshold: str = "medium") -> SecurityReport:
        """Scan and keep only findings at/above ``threshold``.

        Convenience for callers that only care about actionable results
        (e.g. the daemon's periodic health checks).
        """
        report = self.scan(path)
        report.findings = report.above_threshold(threshold)
        return report

    @staticmethod
    def _count_scanned(path: Path) -> int:
        """Approximate count of files the scan looked at."""
        count = 0
        path = Path(path)
        skip = {".git", ".venv", "venv", "node_modules", "__pycache__"}
        try:
            if path.is_file():
                return 1
            for p in path.rglob("*"):
                if p.is_file() and not any(
                        part in skip for part in p.relative_to(path).parts):
                    count += 1
                    if count > 10_000:
                        break
        except OSError:
            pass
        return count
