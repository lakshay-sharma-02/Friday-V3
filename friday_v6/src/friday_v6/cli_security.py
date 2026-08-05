"""CLI commands for `friday6 security` — Wave 3 security & quality.

Usage:
    friday6 security scan [path]               # scan a project (default: cwd)
    friday6 security scan --json               # machine-readable output
    friday6 security scan --threshold high     # only critical/high findings
    friday6 security scan --no-quality         # skip the quality gate
    friday6 security status                    # tool availability overview
"""

from __future__ import annotations

import argparse
import logging

logger = logging.getLogger("friday_v6.cli_security")

_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_GREEN = "\033[92m"
_YELLOW = "\033[93m"
_CYAN = "\033[96m"
_RED = "\033[91m"

_SEV_ICON = {
    "critical": f"{_RED}✗ CRITICAL{_RESET}",
    "high": f"{_YELLOW}⚠ high{_RESET}",
    "medium": f"{_CYAN}◈ medium{_RESET}",
    "low": f"{_DIM}• low{_RESET}",
    "info": f"{_DIM}ℹ info{_RESET}",
}

_GRADE_COLORS = {"A": _GREEN, "B": _GREEN, "C": _YELLOW,
                 "D": _YELLOW, "F": _RED}


def _print_logo():
    print()
    print(f"  {_BOLD}{_CYAN}◆ FRIDAY{_RESET} {_DIM}V4 — Security & Quality{_RESET}")
    print(f"  {_DIM}{'─' * 40}{_RESET}")
    print()


def _grade_color(grade: str) -> str:
    return _GRADE_COLORS.get(grade, _RESET)


def _print_issue(text: str):
    print(f"  {_RED}✗ {text}{_RESET}")


def _print_dim(text: str):
    print(f"  {_DIM}{text}{_RESET}")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_security_scan(args: argparse.Namespace) -> int:
    """Scan a project for vulnerabilities, secrets, and quality issues."""
    from .security import VulnerabilityScanner

    path = args.path or "."
    threshold = args.threshold or "low"

    scanner = VulnerabilityScanner()
    report = scanner.scan(
        path,
        enable_deps=not args.no_deps,
        enable_secrets=not args.no_secrets,
        enable_quality=not args.no_quality,
    )

    # Machine-readable mode must emit ONLY the JSON document — no logo,
    # no header, no progress lines (they would corrupt downstream parsing).
    if args.json:
        print(report.to_json())
        return 0 if not report.above_threshold(threshold) else 1

    _print_logo()
    print(f"  {_BOLD}Scanning{_RESET} {_DIM}{path}{_RESET} "
          f"(threshold: {threshold})")
    print(f"  {_DIM}{'─' * 40}{_RESET}")

    # Tools used
    available = [name for name, ok in report.tools.items() if ok]
    missing = [name for name, ok in report.tools.items()
               if not ok and name != "builtin-deps"
               and name != "builtin-secrets" and name != "builtin-quality"]
    if available:
        print(f"  {_GREEN}✓{_RESET} tools: {_DIM}{', '.join(sorted(available))}{_RESET}")
    if missing:
        print(f"  {_YELLOW}•{_RESET} not installed (built-in checks used): "
              f"{_DIM}{', '.join(sorted(missing))}{_RESET}")

    counts = report.counts_by_severity()
    summary = ", ".join(f"{counts[s]} {s}" for s in
                        ("critical", "high", "medium", "low", "info")
                        if counts[s] > 0) or "clean"
    grade = report.grade()
    color = _grade_color(grade)
    print(f"\n  Overall:  {color}{grade}{_RESET} ({report.score()}/100) "
          f"— {_DIM}{summary}{_RESET}")
    print(f"  {_DIM}{'─' * 40}{_RESET}")

    if not report.findings:
        print(f"\n  {_GREEN}✓ No security or quality issues found.{_RESET}")
        print()
        return 0

    # Only print findings at/above the threshold; the counts above stay
    # complete so the headline reflects the whole scan.
    from .security.reporter import SEVERITY_ORDER
    threshold_rank = SEVERITY_ORDER.index(threshold) \
        if threshold in SEVERITY_ORDER else len(SEVERITY_ORDER) - 1

    by_cat = report.counts_by_category()
    for cat, label in (("vulnerability", "Dependencies"),
                       ("secret", "Secrets"),
                       ("quality", "Quality")):
        shown = [f for f in report.by_category(cat)
                 if f.severity_rank <= threshold_rank]
        if not shown:
            continue
        print(f"\n  {_BOLD}{label} ({len(shown)} shown / {by_cat.get(cat, 0)} total){_RESET}")
        for finding in shown:
            sev_label = _SEV_ICON.get(finding.severity, finding.severity)
            loc = f"{finding.file}:{finding.line}" if finding.file else finding.package
            fixed = f" → {finding.fixed_version}" if finding.fixed_version else ""
            cve = f" [{finding.cve}]" if finding.cve else ""
            print(f"  {sev_label} {finding.title}{cve}{fixed}")
            print(f"  {_DIM}      {loc} · {finding.detail[:110]}{_RESET}")

    above = report.above_threshold(threshold)
    print()
    if above:
        print(f"  {_YELLOW}{len(above)} finding(s) at or above "
              f"'{threshold}' severity.{_RESET}")
        return 1
    print(f"  {_GREEN}✓ No findings at or above '{threshold}' severity.{_RESET}")
    print()
    return 0


def cmd_security_status(args: argparse.Namespace) -> int:
    """Show security tool availability and config."""
    from .config import load_config
    from .security.tooling import find_tool

    _print_logo()
    print(f"  {_BOLD}Tools{_RESET}")
    print(f"  {_DIM}{'─' * 30}{_RESET}")
    for tool in ("pip-audit", "trufflehog", "ruff", "mypy"):
        present = find_tool(tool) is not None
        print(f"  {'✓' if present else '✗'} {tool:<12} "
              f"{_DIM}{'(found)' if present else '(not installed — built-in checks used)'}{_RESET}")
    print(f"  {'✓' if True else '✗'} {'builtin':<12} "
          f"{_DIM}(always available){_RESET}")

    config = load_config()
    sec = config.security
    print(f"\n  {_BOLD}Config{_RESET}")
    print(f"  {_DIM}{'─' * 30}{_RESET}")
    print(f"  enabled                    {sec.enabled}")
    print(f"  scan_on_change              {sec.scan_on_change}")
    print(f"  scan_interval_minutes       {sec.scan_interval_minutes}")
    print(f"  vulnerability_severity_threshold  {sec.vulnerability_severity_threshold}")
    print(f"  secret_detection            {sec.secret_detection}")
    print()
    return 0


# ---------------------------------------------------------------------------
# Argument parsers
# ---------------------------------------------------------------------------


def build_security_parser(subparsers) -> None:
    """Build the `friday6 security` subparser (used by the integrated CLI)."""
    parser = subparsers.add_parser(
        "security",
        help="Security & quality: vulnerabilities, secrets, quality gates",
        description="Friday's security layer — dependency vulnerability "
                    "scanning, secret detection, and code quality gates.",
    )
    security_sub = parser.add_subparsers(dest="security_command")

    # friday6 security scan [path]
    p = security_sub.add_parser("scan", help="Scan a project for issues")
    p.add_argument("path", nargs="?", default=".",
                   help="Project directory to scan (default: cwd)")
    p.add_argument("--threshold", choices=["critical", "high", "medium",
                                           "low", "info"],
                   default="low",
                   help="Only report findings at/above this severity "
                        "(default: low — report everything)")
    p.add_argument("--json", action="store_true",
                   help="Machine-readable JSON output")
    p.add_argument("--no-deps", action="store_true",
                   help="Skip dependency vulnerability scan")
    p.add_argument("--no-secrets", action="store_true",
                   help="Skip secret detection")
    p.add_argument("--no-quality", action="store_true",
                   help="Skip the code quality gate")
    p.set_defaults(func=cmd_security_scan)

    # friday6 security status
    p = security_sub.add_parser("status", help="Security tool availability")
    p.set_defaults(func=cmd_security_status)


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Standalone entry point for `python -m friday_v6.cli_security`."""
    logging.basicConfig(level=logging.WARNING)

    parser = argparse.ArgumentParser(prog="friday6 security")
    subparsers = parser.add_subparsers(dest="security_command")
    build_security_parser(subparsers)

    args = parser.parse_args(argv)

    if hasattr(args, "func"):
        return args.func(args) or 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
