"""Rich Interactive Reports — formatted daily/weekly/impact reports.

Provides:
  - `friday report daily` → HTML/Markdown daily summary
  - `friday report weekly` → weekly engineering summary
  - `friday report impact <symbol>` → impact analysis report

Uses briefing.py for daily report content, and renders in multiple
output formats: HTML (for email/web), Markdown (for GitHub).

Design:
  - No LLM needed — all content from DB + deterministic formatting
  - Each report format is a separate renderer
  - Reports generated on demand, not automatic
  - Includes ASCII sparklines in terminal, SVG in HTML
"""

from __future__ import annotations

import html as html_module
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any, Optional


# ──────────────────────────────────────────────────────────────────────────
# Report data models
# ──────────────────────────────────────────────────────────────────────────


@dataclass
class ReportSection:
    """A section within a report."""
    title: str
    content: list[str]  # Lines of content
    kind: str = "text"  # text | table | list | code | sparkline


@dataclass
class Report:
    """A complete report ready for rendering."""
    title: str
    date: str
    period: str  # "daily" | "weekly" | "impact"
    sections: list[ReportSection] = field(default_factory=list)
    summary: str = ""


# ──────────────────────────────────────────────────────────────────────────
# Data collectors
# ──────────────────────────────────────────────────────────────────────────


def _daily_data() -> Report:
    """Collect data for a daily report."""
    from ..db import connect
    conn = connect()
    
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    report = Report(
        title="Friday Daily Report",
        date=today,
        period="daily",
    )
    
    try:
        # Repositories.
        repo_count = conn.execute("SELECT COUNT(*) AS cnt FROM repositories").fetchone()["cnt"]
        report.sections.append(ReportSection(
            title=f"Workspace Overview ({repo_count} repos)",
            kind="list",
            content=[
                f"Repositories tracked: {repo_count}",
            ],
        ))
        
        # Today's activity.
        cycles = conn.execute(
            """SELECT outcome, repos_scanned FROM watch_history
               WHERE date(started_at) = ? ORDER BY started_at DESC""",
            (today,),
        ).fetchall()
        
        if cycles:
            succeeded = sum(1 for c in cycles if c["outcome"] == "succeeded")
            failed = sum(1 for c in cycles if c["outcome"] == "failed")
            total_repos = sum(c.get("repos_scanned", 0) for c in cycles)
            report.sections.append(ReportSection(
                title="Daemon Activity",
                kind="list",
                content=[
                    f"Daemon cycles: {len(cycles)} ({succeeded} succeeded, {failed} failed)",
                    f"Repos scanned: {total_repos} total",
                ],
            ))
        
        # Today's events.
        events = conn.execute(
            """SELECT event_type, priority, category, title, timestamp
               FROM ambient_feed WHERE date(timestamp) = ? AND dismissed = 0
               ORDER BY timestamp DESC LIMIT 20""",
            (today,),
        ).fetchall()
        
        if events:
            high_pri = sum(1 for e in events if e["priority"] >= 3)
            report.sections.append(ReportSection(
                title=f"Events ({len(events)} total, {high_pri} high priority)",
                kind="list",
                content=[
                    f"  [{e['timestamp'][11:16] if e['timestamp'] else '?'}] {e['title']}"
                    for e in events[:10]
                ],
            ))
        else:
            report.sections.append(ReportSection(
                title="Events",
                kind="text",
                content=["No events today."],
            ))
        
        # Today's actions.
        actions = conn.execute(
            """SELECT action_type, COUNT(*) AS cnt FROM actions
               WHERE date(created_at) = ? GROUP BY action_type ORDER BY cnt DESC""",
            (today,),
        ).fetchall()
        
        if actions:
            report.sections.append(ReportSection(
                title="Actions Performed",
                kind="table",
                content=[f"{a['action_type']}: {a['cnt']}" for a in actions],
            ))
        
        # Knowledge changes today.
        knowledge = conn.execute(
            """SELECT COUNT(*) AS cnt FROM knowledge_evolution
               WHERE date(changed_at) = ?""",
            (today,),
        ).fetchone()
        if knowledge and knowledge["cnt"] > 0:
            report.sections.append(ReportSection(
                title="Knowledge Changes",
                kind="text",
                content=[f"{knowledge['cnt']} knowledge entries updated today."],
            ))
        
    finally:
        conn.close()
    
    return report


def _weekly_data() -> Report:
    """Collect data for a weekly report."""
    from ..db import connect
    conn = connect()
    
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    
    report = Report(
        title="Friday Weekly Engineering Summary",
        date=today,
        period="weekly",
    )
    
    try:
        # Total repos.
        repo_count = conn.execute("SELECT COUNT(*) AS cnt FROM repositories").fetchone()["cnt"]
        
        # Weekly cycles.
        cycles = conn.execute(
            """SELECT outcome, repos_scanned FROM watch_history
               WHERE date(started_at) >= ?""",
            (week_ago,),
        ).fetchall()
        
        cycle_count = len(cycles)
        succeeded = sum(1 for c in cycles if c["outcome"] == "succeeded")
        
        # Weekly events.
        events = conn.execute(
            """SELECT COUNT(*) AS cnt FROM ambient_feed
               WHERE date(timestamp) >= ? AND dismissed = 0""",
            (week_ago,),
        ).fetchone()
        
        high_pri = conn.execute(
            """SELECT COUNT(*) AS cnt FROM ambient_feed
               WHERE date(timestamp) >= ? AND dismissed = 0 AND priority >= 3""",
            (week_ago,),
        ).fetchone()
        
        # Weekly actions by type.
        actions = conn.execute(
            """SELECT action_type, COUNT(*) AS cnt FROM actions
               WHERE date(created_at) >= ? GROUP BY action_type ORDER BY cnt DESC""",
            (week_ago,),
        ).fetchall()
        
        # Initiatives changed.
        initiatives = conn.execute(
            """SELECT COUNT(*) AS cnt FROM initiative_history
               WHERE date(changed_at) >= ?""",
            (week_ago,),
        ).fetchone()
        
        report.sections.append(ReportSection(
            title="Weekly Summary",
            kind="list",
            content=[
                f"Period: {week_ago} to {today}",
                f"Repositories tracked: {repo_count}",
                f"Daemon cycles: {cycle_count} ({succeeded} succeeded)",
                f"Events: {events['cnt'] if events else 0} ({high_pri['cnt'] if high_pri else 0} high priority)",
                f"Initiatives changed: {initiatives['cnt'] if initiatives else 0}",
            ],
        ))
        
        if actions:
            total_actions = sum(a["cnt"] for a in actions)
            report.sections.append(ReportSection(
                title=f"Actions ({total_actions} total)",
                kind="list",
                content=[f"  {a['action_type']}: {a['cnt']}" for a in actions],
            ))
        
    finally:
        conn.close()
    
    return report


def _impact_data(symbol: str) -> Report:
    """Collect impact data for a symbol."""
    from ..impact import analyze_symbol
    from ..db import connect
    conn = connect()
    
    report = Report(
        title=f"Impact Analysis: {symbol}",
        date=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        period="impact",
    )
    
    try:
        result = analyze_symbol(symbol, conn, depth=3)
        if result:
            report.sections.append(ReportSection(
                title=f"Affected Items ({len(result.affected)})",
                kind="list",
                content=result.affected[:30],
            ))
            if result.change_summary:
                report.sections.append(ReportSection(
                    title="Change Summary",
                    kind="text",
                    content=[result.change_summary],
                ))
        else:
            report.sections.append(ReportSection(
                title="No Data",
                kind="text",
                content=[f"No impact data found for '{symbol}'."],
            ))
    finally:
        conn.close()
    
    return report


# ──────────────────────────────────────────────────────────────────────────
# Renderers
# ──────────────────────────────────────────────────────────────────────────


def _render_markdown_section(section: ReportSection) -> str:
    """Render a single section in Markdown."""
    lines = [f"### {section.title}", ""]
    
    if section.kind in ("text", "list"):
        for item in section.content:
            if section.kind == "list" and not item.startswith("  "):
                lines.append(f"- {item}")
            else:
                lines.append(item)
    elif section.kind == "table":
        for item in section.content:
            lines.append(f"- {item}")
    elif section.kind == "code":
        lines.append("```")
        lines.extend(section.content)
        lines.append("```")
    
    lines.append("")
    return "\n".join(lines)


def markdown_report(report: Report) -> str:
    """Render a Report as Markdown."""
    lines = [f"# {report.title}", ""]
    lines.append(f"**Date:** {report.date}")
    lines.append("")
    
    for section in report.sections:
        lines.append(_render_markdown_section(section))
    
    if report.summary:
        lines.append(f"**Summary:** {report.summary}")
    
    return "\n".join(lines)


def _render_html_section(section: ReportSection) -> str:
    """Render a single section in HTML."""
    parts = [f'<h3>{html_module.escape(section.title)}</h3>']
    
    if section.kind in ("text", "list"):
        parts.append('<ul>')
        for item in section.content:
            parts.append(f'<li>{html_module.escape(item)}</li>')
        parts.append('</ul>')
    elif section.kind == "table":
        parts.append('<table><tr><th>Item</th><th>Value</th></tr>')
        for item in section.content:
            if ":" in item:
                k, v = item.split(":", 1)
                parts.append(f'<tr><td>{html_module.escape(k.strip())}</td><td>{html_module.escape(v.strip())}</td></tr>')
            else:
                parts.append(f'<tr><td colspan="2">{html_module.escape(item)}</td></tr>')
        parts.append('</table>')
    elif section.kind == "code":
        parts.append(f'<pre><code>{html_module.escape("\\n".join(section.content))}</code></pre>')
    
    return "\n".join(parts)


def html_report(report: Report) -> str:
    """Render a Report as a standalone HTML document."""
    parts = [
        '<!DOCTYPE html>',
        '<html lang="en"><head><meta charset="UTF-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">',
        f'<title>{html_module.escape(report.title)}</title>',
        '<style>',
        'body { font-family: -apple-system, sans-serif; max-width: 800px; margin: 0 auto; padding: 20px; background: #0d1117; color: #e6edf3; }',
        'h1 { color: #58a6ff; } h2 { color: #e6edf3; border-bottom: 1px solid #30363d; padding-bottom: 4px; }',
        'h3 { color: #8b949e; } a { color: #58a6ff; }',
        'ul { padding-left: 20px; } li { margin: 4px 0; }',
        'table { width: 100%; border-collapse: collapse; margin: 8px 0; }',
        'th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid #30363d; }',
        'th { color: #8b949e; font-weight: 500; }',
        'pre { background: #161b22; padding: 12px; border-radius: 6px; overflow-x: auto; }',
        '.meta { color: #8b949e; font-size: 0.9em; }',
        '</style></head><body>',
        f'<h1>{html_module.escape(report.title)}</h1>',
        f'<p class="meta">Date: {html_module.escape(report.date)}</p>',
    ]
    
    for section in report.sections:
        parts.append(_render_html_section(section))
    
    if report.summary:
        parts.append(f'<p><strong>Summary:</strong> {html_module.escape(report.summary)}</p>')
    
    parts.append('</body></html>')
    return "\n".join(parts)


# ──────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────


def generate_report(kind: str, fmt: str = "markdown",
                    output: Optional[Path] = None,
                    symbol: Optional[str] = None) -> str:
    """Generate a report.

    Args:
        kind: "daily", "weekly", or "impact".
        fmt: "markdown" or "html".
        output: Optional path to save the report.
        symbol: Required for "impact" kind.

    Returns:
        The rendered report as a string.
    
    If output is given, the report is also saved to that path.
    """
    # Collect data.
    if kind == "daily":
        report = _daily_data()
    elif kind == "weekly":
        report = _weekly_data()
    elif kind == "impact":
        if not symbol:
            return "Error: symbol required for impact report."
        report = _impact_data(symbol)
    else:
        return f"Error: unknown report kind '{kind}'."
    
    # Render.
    if fmt == "html":
        result = html_report(report)
    else:
        result = markdown_report(report)
    
    # Save if requested.
    if output:
        output.write_text(result, encoding="utf-8")
    
    return result
