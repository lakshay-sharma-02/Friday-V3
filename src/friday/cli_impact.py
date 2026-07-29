"""CLI for Change Impact Analysis — ``friday impact <file>``.

Usage::

    friday impact src/myfile.py                    # full report
    friday impact --summary src/myfile.py           # one-line summary
    friday impact --json src/myfile.py              # JSON output
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from .impact import ImpactReport, analyze_impact, format_impact_summary
from .presentation.cli_format import header, green, yellow, red, gray


def cmd_impact(args: argparse.Namespace) -> int:
    """Run change impact analysis on a file."""
    from .db import connect

    file_path: str = args.file
    show_json: bool = getattr(args, "json", False)
    summary_only: bool = getattr(args, "summary", False)

    # Resolve relative paths relative to CWD.
    if not file_path.startswith("/"):
        file_path = os.path.join(os.getcwd(), file_path)

    if not args.file:
        print(red("  error: specify a file path to analyze"))
        print(gray("  Usage: friday impact <file>"))
        return 1

    if not os.path.exists(file_path):
        print(red(f"  error: File '{file_path}' does not exist"))
        return 1

    conn = connect()
    try:
        report = analyze_impact(conn, file_path, max_commits=10)
    finally:
        conn.close()

    if show_json:
        print(json.dumps(_report_to_dict(report), indent=2, default=str))
        return 0

    if summary_only:
        print(format_impact_summary(report))
        return 0

    print(report.to_text())
    return 0


def _report_to_dict(report: ImpactReport) -> dict:
    """Convert an ImpactReport to a JSON-serializable dict."""
    return {
        "file_path": report.file_path,
        "resolved_repo": report.resolved_repo,
        "repo_root": report.repo_root,
        "relative_path": report.relative_path,
        "last_modified": report.last_modified,
        "last_author": report.last_author,
        "commit_count": report.commit_count,
        "total_authors": report.total_authors,
        "blame_authors": report.blame_authors,
        "repo_commit_count": report.repo_commit_count,
        "repo_is_dirty": report.repo_is_dirty,
        "repo_last_commit": report.repo_last_commit,
        "repo_primary_author": report.repo_primary_author,
        "repo_languages": report.repo_languages,
        "related_repos": [
            {"name": r.name, "reason": r.reason, "strength": r.strength, "detail": r.detail}
            for r in report.related_repos
        ],
        "correlations": report.correlations,
        "knowledge": [
            {"type": k.type, "statement": k.statement, "confidence": k.confidence, "status": k.status}
            for k in report.knowledge
        ],
        "co_occurring_repos": [
            {"name": n, "detail": d} for n, d in report.co_occurring_repos
        ],
        "architecture": report.architecture,
        "known_patterns": report.known_patterns,
        "components": report.components,
        "errors": report.errors,
    }


# ---------------------------------------------------------------------------
# Subparser registration
# ---------------------------------------------------------------------------


def add_subparser(sub) -> None:
    """Add the ``impact`` subcommand parser."""
    p = sub.add_parser(
        "impact",
        help="Change impact analysis — what breaks if I modify this file?",
    )
    p.add_argument(
        "file",
        help="Path to the file to analyze.",
    )
    p.add_argument(
        "--json", action="store_true",
        help="Output raw JSON instead of formatted text.",
    )
    p.add_argument(
        "--summary", "-s", action="store_true",
        help="Show one-line summary only.",
    )
    p.set_defaults(func=cmd_impact)
