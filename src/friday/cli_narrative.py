"""CLI for Codebase Narrative — ``friday narrative <repo>``.

Usage::

    friday narrative my-project        # full narrative report
    friday narrative --summary my-project  # one-line summary
    friday narrative --json my-project     # JSON output
"""

from __future__ import annotations

import argparse
import json
import sys

from .narrative import NarrativeReport, build_narrative, format_narrative_summary
from .presentation.cli_format import header, green, yellow, red, gray


_LLM_SUMMARY_PROMPT = (
    "You are a codebase historian. Given the following data about a repository, "
    "write a 3-5 sentence narrative explaining its evolution — birth, major changes, "
    "current state, and who the key contributors are.\n\n"
    "Rules:\n"
    "- Be concise (3-5 sentences max)\n"
    "- Focus on the story, not just data dumps\n"
    "- Mention the project's age, total commits, primary author, and bus factor\n"
    "- Highlight the most active development phase\n"
    "- End with the current activity state (active/dormant/growing)\n\n"
    "Repository data:\n{data}\n\n"
    "Narrative:"
)


def _llm_narrative_summary(report) -> str:
    """Generate an LLM-powered 3-5 sentence narrative summary.

    Falls back to structural summary if LLM is unavailable.
    """
    try:
        from .services.llm import _call as llm_call, _enabled as llm_enabled
        if not llm_enabled():
            return format_narrative_summary(report)

        from .narrative import format_narrative_summary
        data_str = format_narrative_summary(report)
        if report.phases:
            phase_strs = []
            for p in report.phases:
                phase_strs.append(f"  - {p.label}: {p.commit_count} commits, {p.author_count} authors")
            data_str += "\nPhases:\n" + "\n".join(phase_strs)
        if report.large_commits:
            big_ones = [f"    {c.sha[:8]} {c.summary[:60]} (+{c.insertions}/-{c.deletions})" for c in report.large_commits[:3]]
            data_str += "\nMajor milestones:\n" + "\n".join(big_ones)

        result = llm_call(_LLM_SUMMARY_PROMPT.format(data=data_str), "")
        if result and len(result.strip()) > 20:
            return result.strip()
    except Exception:
        pass
    return format_narrative_summary(report)


def cmd_narrative(args: argparse.Namespace) -> int:
    """Build and display a codebase narrative."""
    from .db import connect

    repo_identifier: str = args.repo
    show_json: bool = getattr(args, "json", False)
    summary_only: bool = getattr(args, "summary", False)
    timeline_only: bool = getattr(args, "timeline", False)

    conn = connect()
    try:
        report = build_narrative(conn, repo_identifier, max_commits=2000)
    finally:
        conn.close()

    if show_json:
        print(json.dumps(_report_to_json(report), indent=2, default=str))
        return 0

    if timeline_only:
        print(_format_timeline(report))
        return 0

    if summary_only:
        print(_llm_narrative_summary(report))
        return 0

    print(report.to_text())
    return 0


def _format_timeline(report) -> str:
    """Render a compact timeline of commits and key events."""
    from .presentation.cli_format import gray, green, yellow
    lines: list[str] = []
    lines.append(f"Timeline: {report.repo_name}")
    lines.append("=" * 50)
    lines.append(f"  {report.age_days}d old | {report.total_commits} commits | {report.total_authors} author(s)")
    lines.append("")
    if report.phases:
        lines.append("  Phases:")
        for i, p in enumerate(report.phases, 1):
            lines.append(f"    {i}. {p.start_date[:10]} \u2192 {p.end_date[:10]} | {p.label} | {p.commit_count}c {p.author_count}a")
        lines.append("")
    if report.large_commits:
        lines.append("  Milestones:")
        for c in report.large_commits[:5]:
            color = green if c.insertions > 0 else yellow
            lines.append(f"    {c.sha[:8]} {c.date[:10]} {c.author:20s} {c.summary[:60]} {gray(f'+{c.insertions}/-{c.deletions}')}")
    return "\n".join(lines)


def _report_to_json(report: NarrativeReport) -> dict:
    """Convert NarrativeReport to a JSON-serializable dict."""
    return {
        "repo_name": report.repo_name,
        "repo_path": report.repo_path,
        "age_days": report.age_days,
        "total_commits": report.total_commits,
        "total_authors": report.total_authors,
        "primary_author": report.primary_author,
        "bus_factor": report.bus_factor,
        "avg_commits_per_day": report.avg_commits_per_day,
        "first_commit_date": report.first_commit_date,
        "last_commit_date": report.last_commit_date,
        "languages": report.languages,
        "recent_activity": report.recent_activity,
        "authors": [
            {"name": a.name, "commits": a.commit_count, "pct": a.pct}
            for a in report.authors[:15]
        ],
        "phases": [
            {
                "label": p.label,
                "start_date": p.start_date,
                "end_date": p.end_date,
                "commit_count": p.commit_count,
                "author_count": p.author_count,
                "description": p.description,
            }
            for p in report.phases
        ],
        "commits_by_month": report.commits_by_month,
        "large_commits": [
            {
                "sha": c.sha[:8],
                "date": c.date,
                "author": c.author,
                "summary": c.summary,
                "insertions": c.insertions,
                "deletions": c.deletions,
                "files_changed": c.files_changed,
            }
            for c in report.large_commits[:10]
        ],
        "errors": report.errors,
    }


# ---------------------------------------------------------------------------
# Subparser registration
# ---------------------------------------------------------------------------


def add_subparser(sub) -> None:
    """Add the ``narrative`` subcommand parser."""
    p = sub.add_parser(
        "narrative",
        help="Codebase narrative — git archaeology for understanding project evolution.",
    )
    p.add_argument(
        "repo",
        help="Repository name or path to analyze.",
    )
    p.add_argument(
        "--json", action="store_true",
        help="Output raw JSON instead of formatted text.",
    )
    p.add_argument(
        "--summary", "-s", action="store_true",
        help="Show one-line summary only.",
    )
    p.set_defaults(func=cmd_narrative)
