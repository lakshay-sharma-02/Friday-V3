"""CLI for Daily Briefing — ``friday briefing``.

Usage::

    friday briefing                          # full briefing (past 24h)
    friday briefing --hours 48               # custom period
    friday briefing --summary                # one-line summary
    friday briefing --json                   # JSON output
"""

from __future__ import annotations

import argparse
import json
import sys

from .briefing import BriefingReport, build_briefing, format_briefing_summary
from .presentation.cli_format import header, green, yellow, red, gray


def cmd_briefing(args: argparse.Namespace) -> int:
    """Build and display a daily briefing.

    Supports morning (default) and evening modes.
    """
    from .db import connect
    from .briefing import (
        build_briefing,
        build_evening_briefing,
        get_cached_briefing,
        format_briefing_summary,
    )

    hours: int = getattr(args, "hours", 24)
    show_json: bool = getattr(args, "json", False)
    summary_only: bool = getattr(args, "summary", False)
    evening_mode: bool = getattr(args, "evening", False)

    if hours < 1:
        print(red("  error: --hours must be >= 1"))
        return 1

    conn = connect()
    try:
        if evening_mode:
            # Try cache first.
            cached = get_cached_briefing(conn, "evening")
            if cached:
                report = cached
            else:
                report = build_evening_briefing(conn)
        else:
            # Try cached morning briefing.
            cached = get_cached_briefing(conn, "morning")
            if cached:
                report = cached
            else:
                report = build_briefing(conn, hours=hours)
    finally:
        conn.close()

    if show_json:
        print(json.dumps(_report_to_dict(report), indent=2, default=str))
        return 0

    if summary_only:
        print(format_briefing_summary(report))
        return 0

    text = report.to_text() if hasattr(report, "to_text") else str(report)
    # If we loaded from cache, the cached raw content is more complete.
    cached_content = getattr(report, "_cached_content", None)
    if cached_content:
        text = cached_content
    print(text)
    return 0


def _report_to_dict(report: BriefingReport) -> dict:
    """Convert BriefingReport to a JSON-serializable dict."""
    return {
        "period_label": report.period_label,
        "briefing_date": report.briefing_date,
        "generated_at": report.generated_at,
        "greet_by_name": report.greet_by_name,
        "total_events": report.total_events,
        "high_priority_events": report.high_priority_events,
        "events_by_type": report.events_by_type,
        "events_by_category": report.events_by_category,
        "total_repos": report.total_repos,
        "active_repos": [
            {
                "repo": r.repo,
                "commit_count": r.commit_count,
                "authors": r.authors,
                "summaries": r.summaries[:3],
                "is_dirty": r.is_dirty,
            }
            for r in report.active_repos
        ],
        "total_commits_yesterday": report.total_commits_yesterday,
        "total_authors_yesterday": report.total_authors_yesterday,
        "dirty_repos": report.dirty_repos,
        "new_pending_initiatives": report.new_pending_initiatives,
        "high_confidence_initiatives": report.high_confidence_initiatives,
        "new_insights": report.new_insights,
        "watchers_fired": report.watchers_fired,
        "watcher_names": report.watcher_names,
        "new_skills": report.new_skills,
        "drifted_skills": report.drifted_skills,
        "new_correlations": report.new_correlations,
        "new_gaps": report.new_gaps,
        "open_gaps": report.open_gaps,
        "auto_dispatched": report.auto_dispatched,
        "kill_switch_active": report.kill_switch_active,
        "cycle_errors": report.cycle_errors,
        "error_summaries": report.error_summaries[:3],
        "pending_reviews": report.pending_reviews,
        "review_severity_high": report.review_severity_high,
        "review_severity_medium": report.review_severity_medium,
        "review_titles": report.review_titles[:5],
    }


# ---------------------------------------------------------------------------
# Subparser registration
# ---------------------------------------------------------------------------


def add_subparser(sub) -> None:
    """Add the ``briefing`` subcommand parser."""
    p = sub.add_parser(
        "briefing",
        help="Daily briefing — morning summary or evening wrap-up of workspace activity.",
    )
    p.add_argument(
        "--hours", type=int, default=24,
        help="Period to cover in hours (default: 24).",
    )
    p.add_argument(
        "--evening", "--eod", action="store_true",
        help="Generate an end-of-day evening briefing instead of morning briefing.",
    )
    p.add_argument(
        "--json", action="store_true",
        help="Output raw JSON instead of formatted text.",
    )
    p.add_argument(
        "--summary", "-s", action="store_true",
        help="Show one-line summary only.",
    )
    p.set_defaults(func=cmd_briefing)
