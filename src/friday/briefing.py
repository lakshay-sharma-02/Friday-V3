"""Daily Briefing — a concise morning summary of workspace activity.

The briefing collates yesterday's events, commits, observations, watcher
activity, and system health into a single readable report.

Usage::

    from friday.briefing import build_briefing

    briefing = build_briefing(conn)
    print(briefing.to_text())
"""

from __future__ import annotations

import subprocess
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional


@dataclass
class CommitSummary:
    """Summary of commits in one repo over the briefing period."""

    repo: str
    commit_count: int
    authors: list[str] = field(default_factory=list)
    summaries: list[str] = field(default_factory=list)
    is_dirty: bool = False


@dataclass
class BriefingReport:
    """Complete daily briefing for the operator's workspace."""

    # ── Period ───────────────────────────────────────────────────────
    period_label: str = ""  # e.g. "Past 24 hours" or "Yesterday"
    generated_at: str = ""
    briefing_date: str = ""

    # ── Event summary (from ambient feed) ───────────────────────────
    total_events: int = 0
    high_priority_events: int = 0
    events_by_type: dict[str, int] = field(default_factory=dict)
    events_by_category: dict[str, int] = field(default_factory=dict)
    recent_events: list[str] = field(default_factory=list)

    # ── Repository activity ─────────────────────────────────────────
    total_repos: int = 0
    active_repos: list[CommitSummary] = field(default_factory=list)
    total_commits_yesterday: int = 0
    total_authors_yesterday: int = 0
    dirty_repos: int = 0
    repos_without_recent_activity: int = 0

    # ── Initiatives ──────────────────────────────────────────────────
    new_pending_initiatives: int = 0
    high_confidence_initiatives: int = 0
    initiative_titles: list[str] = field(default_factory=list)

    # ── Knowledge / Insights ─────────────────────────────────────────
    knowledge_updated: int = 0
    understanding_updated: int = 0
    new_insights: int = 0

    # ── Watchers ─────────────────────────────────────────────────────
    watchers_fired: int = 0
    watcher_names: list[str] = field(default_factory=list)

    # ── Skills ───────────────────────────────────────────────────────
    new_skills: int = 0
    drifted_skills: int = 0

    # ── Correlations / Gaps ──────────────────────────────────────────
    new_correlations: int = 0
    new_gaps: int = 0
    open_gaps: int = 0

    # ── Autonomy ────────────────────────────────────────────────────
    auto_dispatched: int = 0
    kill_switch_active: bool = False

    # ── Errors ──────────────────────────────────────────────────────
    cycle_errors: int = 0
    error_summaries: list[str] = field(default_factory=list)

    # ── Pending reviews (from SpontaneousReviewEngine) ────────────────
    pending_reviews: int = 0
    review_severity_high: int = 0
    review_severity_medium: int = 0
    review_titles: list[str] = field(default_factory=list)

    # ── Operator context ────────────────────────────────────────────
    greet_by_name: Optional[str] = None

    # ── Mode ────────────────────────────────────────────────────────────
    is_evening: bool = False
    mode_label: str = "morning"

    # ── "One thing worth knowing" headline ──────────────────────────────
    headline: str = ""

    def to_text(self) -> str:
        """Render the briefing as a concise morning report."""
        lines: list[str] = []

        # ── Greeting ────────────────────────────────────────────────
        if self.is_evening:
            greeting = "Evening wrap-up"
        else:
            greeting = "Good morning"
            if self.greet_by_name:
                greeting += f", {self.greet_by_name}"
            greeting += "!"
        lines.append(greeting)
        lines.append("=" * 48)
        lines.append(f"{self.period_label} — {self.briefing_date}")
        if self.headline:
            lines.append(f"  ★ {self.headline}")
        lines.append("")

        # ── Pulse (top-line summary) ─────────────────────────────────
        if self.is_evening:
            status = "✅ Day complete" if not self.cycle_errors else "⚠ Issues detected"
        else:
            status = "✅ All clear" if not self.cycle_errors and self.total_events == 0 else "⚠ Issues detected"
        active_count = len(self.active_repos)
        pulse_parts = [
            f"Repos: {active_count}/{self.total_repos} active" if active_count else f"Repos: {self.total_repos}"
        ]
        if self.total_commits_yesterday:
            pulse_parts.append(f"Commits: {self.total_commits_yesterday}")
        if self.total_authors_yesterday:
            pulse_parts.append(f"Authors: {self.total_authors_yesterday}")
        if self.new_pending_initiatives:
            pulse_parts.append(f"Initiatives: {self.new_pending_initiatives}")
        if self.watchers_fired:
            pulse_parts.append(f"Watchers fired: {self.watchers_fired}")
        if self.drifted_skills:
            pulse_parts.append(f"Skills degrading: {self.drifted_skills}")
        if self.new_correlations:
            pulse_parts.append(f"Correlations: {self.new_correlations}")
        if self.cycle_errors:
            pulse_parts.append(f"Errors: {self.cycle_errors}")
        lines.append(f"  Status: {status}")
        lines.append(f"  Pulse: {' · '.join(pulse_parts)}")
        lines.append("")

        # ── Repo Activity ────────────────────────────────────────────
        if self.active_repos:
            lines.append("Repository Activity")
            lines.append("-" * 40)
            for r in self.active_repos:
                dirty_mark = " ⚠ dirty" if r.is_dirty else ""
                author_str = f" by {', '.join(r.authors[:3])}" if r.authors else ""
                lines.append(f"  {r.repo:<30s} {r.commit_count} commit(s){dirty_mark}{author_str}")
                if r.summaries:
                    for s in r.summaries[:3]:
                        lines.append(f"    · {s[:80]}")
            if self.dirty_repos:
                lines.append(f"  ⚠ {self.dirty_repos} repo(s) have uncommitted changes.")
            if self.repos_without_recent_activity:
                lines.append(f"  ○ {self.repos_without_recent_activity} repo(s) had no activity.")
            lines.append("")

        # ── Events ───────────────────────────────────────────────────
        if self.total_events > 0 or self.high_priority_events:
            lines.append("Events & Notifications")
            lines.append("-" * 40)
            lines.append(f"  Total events: {self.total_events} ({self.high_priority_events} high-priority)")
            if self.events_by_type:
                for etype, count in sorted(self.events_by_type.items(), key=lambda x: -x[1])[:8]:
                    lines.append(f"    {etype:<30s} {count}")
            if self.events_by_category:
                cat_str = " · ".join(f"{c}: {n}" for c, n in sorted(self.events_by_category.items()))
                lines.append(f"  Categories: {cat_str}")
            for ev in self.recent_events[:5]:
                lines.append(f"    → {ev[:80]}")
            lines.append("")

        # ── Watchers ─────────────────────────────────────────────────
        if self.watchers_fired:
            lines.append("Watchers Fired")
            lines.append("-" * 40)
            for name in self.watcher_names[:5]:
                lines.append(f"  ✓ {name}")
            lines.append("")

        # ── Initiatives ──────────────────────────────────────────────
        if self.new_pending_initiatives:
            lines.append("Pending Initiatives")
            lines.append("-" * 40)
            lines.append(f"  {self.new_pending_initiatives} pending initiative(s)")
            if self.high_confidence_initiatives:
                lines.append(f"  ({self.high_confidence_initiatives} high-confidence)")
            for t in self.initiative_titles[:5]:
                lines.append(f"  · {t[:80]}")
            lines.append("")

        # ── Pending Reviews ────────────────────────────────────────────
        if self.pending_reviews:
            lines.append("Pending Reviews")
            lines.append("-" * 40)
            lines.append(f"  {self.pending_reviews} review(s) pending")
            if self.review_severity_high:
                lines.append(f"  ⚠ {self.review_severity_high} high-severity")
            if self.review_severity_medium:
                lines.append(f"  · {self.review_severity_medium} medium-severity")
            for t in self.review_titles[:5]:
                lines.append(f"    · {t[:80]}")
            lines.append("")

        # ── System Health ────────────────────────────────────────────
        health_items: list[str] = []
        if self.new_skills:
            health_items.append(f"{self.new_skills} new skill(s) formed")
        if self.drifted_skills:
            health_items.append(f"⚠ {self.drifted_skills} skill(s) degrading")
        if self.new_correlations:
            health_items.append(f"{self.new_correlations} new cross-project correlation(s)")
        if self.new_gaps:
            health_items.append(f"{self.new_gaps} new capability gap(s)")
        if self.open_gaps:
            health_items.append(f"{self.open_gaps} open gap(s)")
        if self.auto_dispatched:
            health_items.append(f"{self.auto_dispatched} auto-dispatched action(s)")
        if self.kill_switch_active:
            health_items.append("🛑 Kill switch ACTIVE")
        if health_items:
            lines.append("System Health")
            lines.append("-" * 40)
            for item in health_items:
                lines.append(f"  · {item}")
            lines.append("")

        # ── Errors ───────────────────────────────────────────────────
        if self.cycle_errors:
            lines.append("⚠ Issues")
            lines.append("-" * 40)
            lines.append(f"  {self.cycle_errors} daemon cycle error(s) recorded:")
            for e in self.error_summaries[:3]:
                lines.append(f"    {e[:100]}")
            lines.append("")

        # ── Footer ───────────────────────────────────────────────────
        lines.append("=" * 48)
        lines.append(f"Generated at {self.generated_at[:19]}")
        if self.kill_switch_active:
            lines.append("🛑 Kill switch is active — no autonomous execution.")
        act = []
        if self.new_pending_initiatives:
            act.append("friday initiative list")
        if self.drifted_skills:
            act.append("friday skills drift")
        if self.watchers_fired:
            act.append("friday wait list")
        if self.new_correlations:
            act.append("friday correlate")
        if self.pending_reviews:
            act.append("friday review pending")
        if act:
            lines.append(f"Suggested: {' | '.join(act)}")

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Briefing builder
# ---------------------------------------------------------------------------


def build_briefing(
    conn,
    hours: int = 24,
) -> BriefingReport:
    """Build a daily briefing covering the past N hours.

    Args:
        conn: Database connection.
        hours: Period to cover (default 24 = daily briefing).

    Returns:
        A ``BriefingReport`` with all available sections.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=hours)
    cutoff_str = cutoff.isoformat()
    now_str = now.isoformat()

    report = BriefingReport(
        period_label=f"Past {hours} hours" if hours <= 48 else "Weekly summary",
        generated_at=now_str,
        briefing_date=now.strftime("%A, %B %d, %Y"),
    )

    # ── Operator name ───────────────────────────────────────────────
    try:
        from .operator import build_operator_profile
        profile = build_operator_profile(conn)
        name = profile.explicit_preferences.get("name")
        if name:
            report.greet_by_name = name
    except Exception:
        pass

    # ── Events from ambient feed ──────────────────────────────────────
    try:
        rows = conn.execute(
            "SELECT id, event_type, title, detail, priority, category, timestamp "
            "FROM ambient_feed WHERE timestamp >= ? "
            "ORDER BY timestamp DESC",
            (cutoff_str,),
        ).fetchall()
        report.total_events = len(rows)
        type_counts: Counter = Counter()
        cat_counts: Counter = Counter()
        high_pri = 0
        for r in rows:
            type_counts[r["event_type"]] += 1
            cat_counts[r["category"]] += 1
            if r["priority"] >= 2:
                high_pri += 1
            if len(report.recent_events) < 10:
                report.recent_events.append(r["title"])
        report.high_priority_events = high_pri
        report.events_by_type = dict(type_counts)
        report.events_by_category = dict(cat_counts)
    except Exception:
        pass

    # ── Repository activity ──────────────────────────────────────────
    try:
        from .db import get_repositories
        repos = get_repositories(conn)
        report.total_repos = len(repos)

        for repo in repos:
            rname = repo.name if hasattr(repo, "name") else repo.get("name", "")
            rpath = repo.path if hasattr(repo, "path") else repo.get("path", "")
            if not rpath or not Path(rpath).exists():
                continue

            # Count commits since cutoff.
            try:
                out = subprocess.run(
                    ["git", "-C", rpath, "log", f"--after={cutoff_str}",
                     "--oneline", "--format=%H|%an|%s"],
                    capture_output=True, text=True, timeout=15,
                )
                if out.returncode != 0 or not out.stdout.strip():
                    continue
                lines = [l.strip() for l in out.stdout.splitlines() if l.strip()]
                if not lines:
                    continue
                authors: set[str] = set()
                summaries: list[str] = []
                for line in lines:
                    parts = line.split("|", 2)
                    if len(parts) >= 3:
                        authors.add(parts[1])
                        summaries.append(parts[2])

                # Check dirty status.
                dirty_out = subprocess.run(
                    ["git", "-C", rpath, "status", "--porcelain"],
                    capture_output=True, text=True, timeout=5,
                )
                is_dirty = bool(dirty_out.stdout.strip())

                report.active_repos.append(CommitSummary(
                    repo=rname,
                    commit_count=len(lines),
                    authors=sorted(authors),
                    summaries=summaries[:5],
                    is_dirty=is_dirty,
                ))
                report.total_commits_yesterday += len(lines)
                report.total_authors_yesterday += len(authors)
                if is_dirty:
                    report.dirty_repos += 1
            except Exception:
                continue

        report.repos_without_recent_activity = (
            report.total_repos - len(report.active_repos)
        )
    except Exception:
        pass

    # ── Initiatives ──────────────────────────────────────────────────
    try:
        rows = conn.execute(
            "SELECT id, title, confidence FROM initiatives "
            "WHERE status = 'pending' AND build_at >= ?",
            (cutoff_str,),
        ).fetchall()
        report.new_pending_initiatives = len(rows)
        for r in rows:
            report.initiative_titles.append(r["title"])
            if r["confidence"] in ("high", "strong"):
                report.high_confidence_initiatives += 1
    except Exception:
        pass

    # ── Knowledge / Understanding / Insights ──────────────────────────
    try:
        rows = conn.execute(
            "SELECT COUNT(*) as cnt FROM knowledge "
            "WHERE updated_at >= ?", (cutoff_str,)
        ).fetchone()
        report.knowledge_updated = rows["cnt"] if rows else 0
    except Exception:
        pass
    try:
        rows = conn.execute(
            "SELECT COUNT(*) as cnt FROM understanding "
            "WHERE updated_at >= ?", (cutoff_str,)
        ).fetchone()
        report.understanding_updated = rows["cnt"] if rows else 0
    except Exception:
        pass
    try:
        rows = conn.execute(
            "SELECT COUNT(*) as cnt FROM insights "
            "WHERE build_at >= ?", (cutoff_str,)
        ).fetchone()
        report.new_insights = rows["cnt"] if rows else 0
    except Exception:
        pass

    # ── Watchers ──────────────────────────────────────────────────────
    try:
        rows = conn.execute(
            "SELECT name FROM persistent_watchers "
            "WHERE last_result = 1 AND last_checked_at >= ?",
            (cutoff_str,),
        ).fetchall()
        report.watchers_fired = len(rows)
        report.watcher_names = [r["name"] for r in rows]
    except Exception:
        pass

    # ── Skills ────────────────────────────────────────────────────────
    try:
        rows = conn.execute(
            "SELECT COUNT(*) as cnt FROM skills "
            "WHERE created_at >= ?", (cutoff_str,)
        ).fetchone()
        report.new_skills = rows["cnt"] if rows else 0
    except Exception:
        pass
    try:
        rows = conn.execute(
            "SELECT COUNT(*) as cnt FROM skills "
            "WHERE health = 'unhealthy' OR health = 'degrading'"
        ).fetchone()
        report.drifted_skills = rows["cnt"] if rows else 0
    except Exception:
        pass

    # ── Correlations / Gaps ───────────────────────────────────────────
    try:
        rows = conn.execute(
            "SELECT COUNT(*) as cnt FROM correlation_results "
            "WHERE 1=1"  # All correlations
        ).fetchone()
        report.new_correlations = rows["cnt"] if rows else 0
    except Exception:
        pass
    try:
        rows = conn.execute(
            "SELECT COUNT(*) as cnt FROM capability_gaps "
            "WHERE created_at >= ?", (cutoff_str,)
        ).fetchone()
        report.new_gaps = rows["cnt"] if rows else 0
    except Exception:
        pass
    try:
        rows = conn.execute(
            "SELECT COUNT(*) as cnt FROM capability_gaps "
            "WHERE status = 'open'"
        ).fetchone()
        report.open_gaps = rows["cnt"] if rows else 0
    except Exception:
        pass

    # ── Autonomy ─────────────────────────────────────────────────────
    try:
        from .autonomy import is_kill_switch_active
        report.kill_switch_active = is_kill_switch_active(conn)
    except Exception:
        pass
    try:
        rows = conn.execute(
            "SELECT COUNT(*) as cnt FROM action_events "
            "WHERE action = 'auto_dispatch' AND timestamp >= ?",
            (cutoff_str,),
        ).fetchone()
        report.auto_dispatched = rows["cnt"] if rows else 0
    except Exception:
        pass

    # ── Pending reviews (from SpontaneousReviewEngine) ────────────────
    try:
        rows = conn.execute(
            "SELECT id, title, confidence FROM pending_initiatives "
            "WHERE initiative_type LIKE 'spontaneous_review:%' "
            "AND reviewed = 0 AND dismissed_at IS NULL"
        ).fetchall()
        report.pending_reviews = len(rows)
        high = 0
        medium = 0
        for r in rows:
            conf = (r["confidence"] or "").lower()
            if conf == "high":
                high += 1
            elif conf == "medium":
                medium += 1
            report.review_titles.append(r["title"])
        report.review_severity_high = high
        report.review_severity_medium = medium
    except Exception:
        pass

    # ── Cycle errors ─────────────────────────────────────────────────
    try:
        rows = conn.execute(
            "SELECT error_detail FROM ambient_feed "
            "WHERE event_type = 'cycle_error' AND timestamp >= ? "
            "ORDER BY timestamp DESC LIMIT 5",
            (cutoff_str,),
        ).fetchall()
        report.cycle_errors = len(rows)
        report.error_summaries = [r["error_detail"][:200] for r in rows if r["error_detail"]]
    except Exception:
        pass

    return report


def build_evening_briefing(conn) -> BriefingReport:
    """Build an end-of-day briefing covering today's activity.

    Runs the same pipeline as ``build_briefing()`` but with evening-appropriate
    labels, a longer format (15-20 lines), and flags incomplete items.
    """
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff_str = today_start.isoformat()

    report = build_briefing(conn, hours=24)
    report.is_evening = True
    report.mode_label = "evening"
    report.period_label = "Today"
    report.generated_at = now.isoformat()

    # ── Blockers (CI failures, unresolved errors) ───────────────────
    try:
        ci_rows = conn.execute(
            "SELECT COUNT(*) AS cnt FROM ambient_feed "
            "WHERE event_type LIKE 'review:ci_failure%' AND timestamp >= ? "
            "AND dismissed = 0", (cutoff_str,)
        ).fetchone()
        report.cycle_errors += ci_rows["cnt"] if ci_rows else 0
    except Exception:
        pass

    # ── Pending PRs from GitHub observer ────────────────────────────
    try:
        from .observation.github_observer import _load_cache
        cache = _load_cache()
        snapshots = cache.get("snapshots", [])
        open_prs = 0
        for snap in snapshots:
            for pr in snap.get("pull_requests", []):
                if (pr.get("state") or "").lower() not in ("merged", "closed"):
                    open_prs += 1
        if open_prs:
            report.error_summaries.append(f"{open_prs} open PR(s) pending review")
    except Exception:
        pass

    # ── Headline for evening is the biggest event ───────────────────
    if not report.headline:
        if report.total_events:
            ev_types = sorted(report.events_by_type.items(), key=lambda x: -x[1])
            top = ev_types[0][0] if ev_types else ""
            report.headline = f"{report.total_events} events today — mostly {top}"
        elif report.total_commits_yesterday:
            report.headline = f"{report.total_commits_yesterday} commits across {len(report.active_repos)} repo(s)"
        else:
            report.headline = "Quiet day — no notable activity detected"

    # ── Mark anything unresolved for tomorrow ───────────────────────
    has_blockers = report.cycle_errors > 0 or report.pending_reviews > 0 or report.watchers_fired > 0
    if not has_blockers:
        # Check for open PRs
        try:
            from .observation.github_observer import _load_cache
            cache = _load_cache()
            for snap in cache.get("snapshots", []):
                for pr in snap.get("pull_requests", []):
                    if (pr.get("state") or "").lower() not in ("merged", "closed"):
                        has_blockers = True
                        break
        except Exception:
            pass

    # Persist to daily_summaries.
    _cache_briefing(conn, report, has_blockers=has_blockers)

    return report


def _compute_headline(report: BriefingReport) -> str:
    """Derive the single most significant finding from the report.

    Returns a short (< 80 char) punchy headline. Deterministic — no LLM.
    Priority order: critical events > errors > big activity > nothing.
    """
    if report.high_priority_events >= 3:
        return f"{report.high_priority_events} critical event(s) need attention"
    if report.cycle_errors:
        return f"⚠ {report.cycle_errors} daemon cycle error(s) recorded"
    if report.drifted_skills:
        return f"⚠ {report.drifted_skills} skill(s) degrading — may need re-formation"
    if report.review_severity_high:
        return f"🔍 {report.review_severity_high} high-severity review(s) pending"
    if report.total_commits_yesterday:
        top_repo = report.active_repos[0].repo if report.active_repos else ""
        if top_repo:
            return f"{report.total_commits_yesterday} commits across {len(report.active_repos)} repo(s) — {top_repo} most active"
        return f"{report.total_commits_yesterday} commits across {len(report.active_repos)} repo(s)"
    if report.new_pending_initiatives:
        return f"{report.new_pending_initiatives} new initiative(s) emerged overnight"
    if report.watchers_fired:
        return f"{report.watchers_fired} watcher(s) fired overnight"
    if report.pending_reviews:
        return f"{report.pending_reviews} review(s) waiting"
    if report.new_skills:
        return f"{report.new_skills} new skill(s) formed"
    if report.new_correlations:
        return f"{report.new_correlations} cross-project correlation(s) detected"
    return "All quiet — no significant changes detected"


def _cache_briefing(conn, report: BriefingReport, has_blockers: bool = False) -> None:
    """Persist a briefing to the daily_summaries table (for caching + history).

    Uses INSERT OR REPLACE so only one entry per (date, summary_type) exists.
    """
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        conn.execute(
            "INSERT OR REPLACE INTO daily_summaries "
            "(date, summary_type, content, headline, event_count, commit_count, "
            " repo_count, has_blockers, generated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                date_str,
                report.mode_label,
                report.to_text(),
                report.headline,
                report.total_events,
                report.total_commits_yesterday,
                report.total_repos,
                1 if has_blockers else 0,
                report.generated_at,
            ),
        )
        conn.commit()
    except Exception:
        pass

    # Also log to briefing_log.
    try:
        conn.execute(
            "INSERT OR REPLACE INTO briefing_log "
            "(date, briefing_type, source, headline, summary, generated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                date_str,
                report.mode_label,
                "cli",
                report.headline,
                format_briefing_summary(report),
                report.generated_at,
            ),
        )
        conn.commit()
    except Exception:
        pass


def get_cached_briefing(conn, summary_type: str = "morning") -> Optional[BriefingReport]:
    """Retrieve a cached briefing from daily_summaries, if one exists for today.

    Returns None when no cached briefing is found for the requested type.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        row = conn.execute(
            "SELECT content, headline, event_count, commit_count, repo_count, "
            "has_blockers, generated_at FROM daily_summaries "
            "WHERE date = ? AND summary_type = ?",
            (today, summary_type),
        ).fetchone()
        if row is None:
            return None
        report = BriefingReport(
            period_label="Today" if summary_type == "evening" else "Past 24 hours",
            generated_at=row["generated_at"],
            briefing_date=today,
            total_events=row["event_count"],
            total_commits_yesterday=row["commit_count"],
            total_repos=row["repo_count"],
            headline=row["headline"],
            is_evening=(summary_type == "evening"),
            mode_label=summary_type,
        )
        # Restore full content for to_text().
        object.__setattr__(report, "_cached_content", row["content"])
        return report
    except Exception:
        return None


def has_briefing_been_delivered(conn, briefing_type: str = "morning") -> bool:
    """Check whether a briefing has already been delivered today.

    Used by the daemon to avoid re-generating the same briefing across
    multiple cycles on the same calendar day.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        row = conn.execute(
            "SELECT 1 FROM briefing_log "
            "WHERE date = ? AND briefing_type = ? AND source = 'daemon' "
            "LIMIT 1",
            (today, briefing_type),
        ).fetchone()
        return row is not None
    except Exception:
        return False


def mark_briefing_delivered(conn, briefing_type: str = "morning") -> None:
    """Mark a briefing as delivered in the daily_summaries table."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        conn.execute(
            "UPDATE daily_summaries SET delivered = 1 "
            "WHERE date = ? AND summary_type = ?",
            (today, briefing_type),
        )
        conn.commit()
    except Exception:
        pass


def format_briefing_summary(report: BriefingReport) -> str:
    """Return a one-line summary for CLI display."""
    parts: list[str] = []
    if report.total_events:
        parts.append(f"Events: {report.total_events}")
    if report.total_commits_yesterday:
        parts.append(f"Commits: {report.total_commits_yesterday}")
    if report.watchers_fired:
        parts.append(f"Watchers: {report.watchers_fired}")
    if report.drifted_skills:
        parts.append(f"Degraded skills: {report.drifted_skills}")
    if report.new_pending_initiatives:
        parts.append(f"Initiatives: {report.new_pending_initiatives}")
    if report.pending_reviews:
        parts.append(f"Reviews: {report.pending_reviews}")
    if report.cycle_errors:
        parts.append(f"Errors: {report.cycle_errors}")
    return " · ".join(parts) if parts else "No data available"
