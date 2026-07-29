"""Codebase Narrative — Git archaeology for understanding project evolution.

Tells the story of a project's evolution by analyzing git history, snapshot
changes, and observation patterns.

Usage::

    from friday.narrative import build_narrative

    narrative = build_narrative(conn, repo_name_or_path)
    print(narrative.to_text())

The narrative collects evidence from:
  - Full git log (commit frequency, size, authors, message themes)
  - Git shortlog (author contributions)
  - Git diff --stat between periods (structural changes)
  - Snapshot history (identity, architecture, README evolution)
  - Knowledge store (knowledge about the repo)
  - File extension changes over time
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class GitCommit:
    """One parsed commit from git log."""

    sha: str
    date: str
    author: str
    email: str
    summary: str
    files_changed: int = 0
    insertions: int = 0
    deletions: int = 0


@dataclass
class AuthorStats:
    """Contribution summary for one author."""

    name: str
    commit_count: int
    first_commit: Optional[str] = None
    last_commit: Optional[str] = None
    pct: float = 0.0


@dataclass
class TimelinePhase:
    """A distinct phase in the project's history."""

    label: str
    start_date: str
    end_date: str
    commit_count: int
    author_count: int
    description: str = ""
    avg_commits_per_day: float = 0.0
    top_files_changed: list[str] = field(default_factory=list)
    dominant_author: str = ""


@dataclass
class LanguageSnapshot:
    """Language distribution at a point in time."""

    date: str
    languages: dict[str, int]


@dataclass
class NarrativeReport:
    """Complete codebase narrative for a single repository."""

    # ── Identity ────────────────────────────────────────────────────
    repo_name: str = ""
    repo_path: str = ""
    default_branch: str = "main"
    age_days: int = 0
    total_commits: int = 0
    total_authors: int = 0
    primary_author: str = ""
    bus_factor: int = 1  # authors owning 50%+ of commits
    languages: dict[str, int] = field(default_factory=dict)

    # ── Authors ──────────────────────────────────────────────────────
    authors: list[AuthorStats] = field(default_factory=list)

    # ── Timeline ─────────────────────────────────────────────────────
    first_commit_date: Optional[str] = None
    last_commit_date: Optional[str] = None
    phases: list[TimelinePhase] = field(default_factory=list)
    commits_by_month: dict[str, int] = field(default_factory=dict)
    avg_commits_per_day: float = 0.0
    most_active_month: str = ""

    # ── Activity patterns ────────────────────────────────────────────
    commits_by_hour: dict[int, int] = field(default_factory=dict)
    commits_by_day: dict[str, int] = field(default_factory=dict)
    batch_threshold: int = 5  # commits within 1h window = batch
    batch_count: int = 0
    single_count: int = 0
    has_weekend_work: bool = False

    # ── File / structural evolution ──────────────────────────────────
    current_file_count: int = 0
    files_added: int = 0
    files_removed: int = 0
    files_modified: int = 0
    top_files: list[tuple[str, int]] = field(default_factory=list)

    # ── Snapshot evolution (from DB) ─────────────────────────────────
    snapshot_count: int = 0
    identity_changes: int = 0
    architecture_changes: int = 0
    readme_changes: int = 0

    # ── Milestones ──────────────────────────────────────────────────
    large_commits: list[GitCommit] = field(default_factory=list)
    recent_activity: Optional[str] = None  # dormant / steady / growing

    # ── Errors ───────────────────────────────────────────────────────
    errors: list[str] = field(default_factory=list)

    def to_text(self) -> str:
        """Render the full narrative as human-readable text."""
        sections: list[str] = []

        # ── Header ───────────────────────────────────────────────────
        sections.append(f"Codebase Narrative: {self.repo_name}")
        sections.append("=" * 60)
        sections.append("")

        # ── Summary ──────────────────────────────────────────────────
        sections.append("Summary")
        sections.append("-" * 40)
        sections.append(f"  Age:            {self.age_days} days")
        if self.age_days >= 365:
            sections.append(f"                  ({self.age_days // 365}y {self.age_days % 365}d)")
        sections.append(f"  Total commits:  {self.total_commits}")
        sections.append(f"  Total authors:  {self.total_authors}")
        sections.append(f"  Primary author: {self.primary_author}")
        sections.append(f"  Bus factor:     {self.bus_factor} (authors owning 50%+ of commits)")
        sections.append(f"  Avg commit rate: {self.avg_commits_per_day:.2f}/day")
        if self.first_commit_date:
            sections.append(f"  First commit:   {self.first_commit_date[:10]}")
        if self.last_commit_date:
            sections.append(f"  Last commit:    {self.last_commit_date[:10]}")
        if self.languages:
            top_langs = sorted(self.languages.items(), key=lambda x: -x[1])[:5]
            sections.append(f"  Languages:      {', '.join(f'{l} ({c})' for l, c in top_langs)}")
        sections.append("")

        # ── Activity pattern ─────────────────────────────────────────
        sections.append("Activity Pattern")
        sections.append("-" * 40)
        peak_hour = max(self.commits_by_hour, key=self.commits_by_hour.get) if self.commits_by_hour else 0
        peak_hour_label = f"{peak_hour}:00-{peak_hour + 1}:00"
        sections.append(f"  Peak hour:      {peak_hour_label}")
        if self.commits_by_day:
            top_day = max(self.commits_by_day, key=self.commits_by_day.get)
            sections.append(f"  Most active day: {top_day}")
        sections.append(f"  Weekend work:   {'yes' if self.has_weekend_work else 'no'}")
        total_actions = self.batch_count + self.single_count
        if total_actions:
            batch_pct = self.batch_count / total_actions * 100
            sections.append(f"  Batch commits:  {self.batch_count}/{total_actions} ({batch_pct:.0f}%)")
        sections.append("")

        # ── Authors ──────────────────────────────────────────────────
        if self.authors:
            sections.append("Contributors")
            sections.append("-" * 40)
            bar_width = 30
            for a in self.authors[:10]:
                bar = "█" * int(a.pct / 100 * bar_width) + "░" * (bar_width - int(a.pct / 100 * bar_width))
                sections.append(f"  {a.name:24s} {a.commit_count:5d} ({a.pct:5.1f}%) {bar}")
            if len(self.authors) > 10:
                sections.append(f"  ... and {len(self.authors) - 10} more")
            sections.append("")

        # ── Timeline phases ──────────────────────────────────────────
        if self.phases:
            sections.append("Evolution Timeline")
            sections.append("-" * 40)
            for i, phase in enumerate(self.phases, 1):
                duration_days = max(1, (datetime.fromisoformat(phase.end_date) -
                                       datetime.fromisoformat(phase.start_date)).days)
                sections.append(f"  Phase {i}: {phase.label}")
                sections.append(f"    {phase.start_date[:10]} → {phase.end_date[:10]} ({duration_days}d)")
                sections.append(f"    Commits: {phase.commit_count}, Authors: {phase.author_count}")
                if phase.avg_commits_per_day:
                    sections.append(f"    Rate: {phase.avg_commits_per_day:.2f}/day")
                if phase.dominant_author:
                    sections.append(f"    Lead: {phase.dominant_author}")
                if phase.description:
                    sections.append(f"    {phase.description}")
                if phase.top_files_changed:
                    shown = phase.top_files_changed[:3]
                    sections.append(f"    Top files: {', '.join(shown)}")
                sections.append("")
            sections.append("")

        # ── Monthly activity ─────────────────────────────────────────
        if self.commits_by_month:
            sections.append("Monthly Activity")
            sections.append("-" * 40)
            if self.total_commits:
                max_month = max(self.commits_by_month.values())
                for month, count in sorted(self.commits_by_month.items()):
                    bar_len = max(1, int(count / max_month * 20))
                    bar = "█" * bar_len
                    label = "◀ most active" if month == self.most_active_month else ""
                    sections.append(f"  {month} {bar} {count} {label}")
            sections.append("")

        # ── File evolution ───────────────────────────────────────────
        sections.append("File Structure")
        sections.append("-" * 40)
        sections.append(f"  Current files:  {self.current_file_count}")
        sections.append(f"  Files added:    {self.files_added}")
        sections.append(f"  Files removed:  {self.files_removed}")
        sections.append(f"  Files modified: {self.files_modified}")
        if self.top_files:
            sections.append("  Most changed files:")
            for fn, cnt in self.top_files[:8]:
                sections.append(f"    {fn:<40s} {cnt} changes")
        sections.append("")

        # ── Snapshot history ─────────────────────────────────────────
        sections.append("Observation History")
        sections.append("-" * 40)
        sections.append(f"  Observation snapshots: {self.snapshot_count}")
        sections.append(f"  Identity changes:      {self.identity_changes}")
        sections.append(f"  Architecture changes:  {self.architecture_changes}")
        sections.append(f"  README changes:        {self.readme_changes}")
        sections.append("")

        # ── Milestones ───────────────────────────────────────────────
        if self.large_commits:
            sections.append("Key Milestones")
            sections.append("-" * 40)
            for c in self.large_commits[:5]:
                age_str = f"{c.date[:10]}"
                changes = f"+{c.insertions}/-{c.deletions}" if c.insertions else ""
                sections.append(f"  {c.sha[:8]} {age_str} {c.author:20s} {c.summary[:60]}")
                if changes:
                    sections.append(f"    Changes: {changes} in {c.files_changed} file(s)")
            sections.append("")

        # ── Errors ───────────────────────────────────────────────────
        if self.errors:
            sections.append("Issues")
            sections.append("-" * 40)
            for err in self.errors:
                sections.append(f"  ⚠ {err}")
            sections.append("")

        sections.append("=" * 60)
        return "\n".join(sections)


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def _run_git(repo_path: str, args: list[str], timeout: int = 30) -> Optional[str]:
    """Run a git command in the given repo."""
    try:
        res = subprocess.run(
            ["git", "-C", repo_path, *args],
            capture_output=True, text=True, timeout=timeout,
        )
        if res.returncode != 0:
            return None
        return res.stdout
    except (subprocess.TimeoutExpired, OSError):
        return None


def _parse_log_line(line: str) -> Optional[GitCommit]:
    """Parse a single git log --format line with stat suffix.

    Expected format (on separate lines):
      SHA|DATE|AUTHOR|EMAIL|SUMMARY
      FILES_CHANGED\tINSERTIONS\tDELETIONS
    """
    parts = line.split("|", 4)
    if len(parts) < 5:
        return None
    try:
        return GitCommit(
            sha=parts[0],
            date=parts[1],
            author=parts[2],
            email=parts[3],
            summary=parts[4],
        )
    except (ValueError, IndexError):
        return None


def _load_full_log(repo_path: str, branch: str = "HEAD", max_count: int = 5000) -> list[GitCommit]:
    """Load commit log with stat info."""
    # Get commit metadata.
    raw = _run_git(repo_path, [
        "log", branch, f"--max-count={max_count}",
        "--format=%H|%cI|%an|%ae|%s",
    ])
    if not raw:
        return []

    commits: list[GitCommit] = []
    for raw_line in raw.splitlines():
        c = _parse_log_line(raw_line)
        if c:
            commits.append(c)

    # Get stat info per commit (use dict lookup for O(n) matching).
    stat_raw = _run_git(repo_path, [
        "log", branch, f"--max-count={max_count}",
        "--format=COMMIT:%H",
        "--numstat",
    ])
    if stat_raw:
        commits_by_sha = {c.sha: c for c in commits}
        current_sha = None
        for line in stat_raw.splitlines():
            if line.startswith("COMMIT:"):
                current_sha = line[7:]
                continue
            if current_sha and line.strip():
                parts = line.split("\t")
                if len(parts) >= 3:
                    added = parts[0]
                    removed = parts[1]
                    c = commits_by_sha.get(current_sha)
                    if c is not None:
                        try:
                            c.insertions += int(added) if added != "-" else 0
                            c.deletions += int(removed) if removed != "-" else 0
                            c.files_changed += 1
                        except ValueError:
                            pass

    return commits


def _get_file_change_counts(repo_path: str, branch: str = "HEAD") -> dict[str, int]:
    """Get file change counts from git log --name-only."""
    raw = _run_git(repo_path, [
        "log", branch, "--format=", "--name-only",
        "--diff-filter=M",  # Only modified files
    ])
    if not raw:
        return {}
    counts: Counter = Counter()
    for line in raw.splitlines():
        line = line.strip()
        if line:
            counts[line] += 1
    return dict(counts.most_common(50))


def _get_file_add_remove_counts(repo_path: str, branch: str = "HEAD") -> tuple[int, int]:
    """Get total files added and removed in the repo's history."""
    added_raw = _run_git(repo_path, [
        "log", branch, "--diff-filter=A", "--pretty=tformat:", "--name-only",
    ])
    removed_raw = _run_git(repo_path, [
        "log", branch, "--diff-filter=D", "--pretty=tformat:", "--name-only",
    ])
    added = len(set(line.strip() for line in (added_raw or "").splitlines() if line.strip()))
    removed = len(set(line.strip() for line in (removed_raw or "").splitlines() if line.strip()))
    return added, removed


def _get_large_commits(
    repo_path: str, commits: list[GitCommit], threshold: int = 200,
) -> list[GitCommit]:
    """Find commits above a total change threshold."""
    return [c for c in commits if (c.insertions + c.deletions) >= threshold]


def _classify_commit_pattern(commits: list[GitCommit]) -> tuple[int, int]:
    """Classify commits as batch (within 1h window) or single."""
    if not commits:
        return 0, 0
    batch = 0
    single = 0
    sorted_commits = sorted(commits, key=lambda c: c.date)
    i = 0
    while i < len(sorted_commits):
        window_end = datetime.fromisoformat(sorted_commits[i].date)
        group = 1
        for j in range(i + 1, len(sorted_commits)):
            j_dt = datetime.fromisoformat(sorted_commits[j].date)
            if (j_dt - window_end).total_seconds() <= 3600:
                group += 1
                window_end = j_dt
            else:
                break
        if group >= 3:
            batch += group
        else:
            single += group
        i += group
    return batch, single


# ---------------------------------------------------------------------------
# Phase detection
# ---------------------------------------------------------------------------


def _detect_phases(
    commits: list[GitCommit],
    min_phase_commits: int = 5,
) -> list[TimelinePhase]:
    """Detect distinct development phases from commit history.

    Uses a simple heuristic: split history into equal thirds, then refine
    based on commit density changes.
    """
    if not commits or len(commits) < min_phase_commits:
        return []

    sorted_c = sorted(commits, key=lambda c: c.date)
    total = len(sorted_c)

    # Use commit density to find phase boundaries.
    # Split into 3 initial segments, then merge tiny ones.
    segment_size = max(min_phase_commits, total // 3)
    segments: list[list[GitCommit]] = []
    for i in range(0, total, segment_size):
        seg = sorted_c[i:i + segment_size]
        if seg:
            segments.append(seg)

    if len(segments) < 1:
        return []

    phases = []
    labels = ["Initial Build", "Growth & Features", "Refinement & Polish"]
    if len(segments) >= 4:
        labels = ["Initial Build", "Growth & Features", "Scaling", "Maintenance"]
    elif len(segments) >= 5:
        labels = ["Initial Build", "Rapid Growth", "Feature Expansion", "Refinement", "Maintenance"]

    for i, seg in enumerate(segments):
        label = labels[i] if i < len(labels) else f"Phase {i+1}"
        authors = set(c.author for c in seg)
        avg_daily = 0.0
        if seg:
            first_dt = datetime.fromisoformat(seg[0].date)
            last_dt = datetime.fromisoformat(seg[-1].date)
            days = max(1, (last_dt - first_dt).days)
            avg_daily = len(seg) / days

        # Top files changed in this phase.
        file_counts: Counter = Counter()
        for c in seg:
            file_counts[c.summary.split(":")[0]] += 1
        top_files = [f for f, _ in file_counts.most_common(5)]

        # Dominant author.
        author_counts: Counter = Counter(c.author for c in seg)
        dominant = author_counts.most_common(1)[0][0] if author_counts else ""

        phases.append(TimelinePhase(
            label=label,
            start_date=seg[0].date,
            end_date=seg[-1].date,
            commit_count=len(seg),
            author_count=len(authors),
            description=_phase_description(i, len(segments), len(seg), len(authors)),
            avg_commits_per_day=round(avg_daily, 2),
            top_files_changed=top_files,
            dominant_author=dominant,
        ))

    return phases


def _phase_description(phase_idx: int, total_phases: int, commits: int, authors: int) -> str:
    """Generate a description for a development phase."""
    templates = [
        lambda c, a: f"Project inception with {c} commits by {a} author(s). Laying foundations.",
        lambda c, a: f"Active development with {c} commits across {a} contributor(s). Feature growth.",
        lambda c, a: f"Continued work with {c} commits by {a} author(s). Refinement and expansion.",
        lambda c, a: f"Mature phase with {c} commits by {a} author(s). Maintenance and polish.",
    ]
    if phase_idx < len(templates):
        return templates[phase_idx](commits, authors)
    return f"{commits} commits by {a} author(s)."


# ---------------------------------------------------------------------------
# Main narrative builder
# ---------------------------------------------------------------------------


def build_narrative(
    conn,
    repo_identifier: str,
    max_commits: int = 2000,
) -> NarrativeReport:
    """Build a codebase narrative for a repository.

    Args:
        conn: Database connection.
        repo_identifier: Repository name or path.
        max_commits: Max commits to analyze (default 2000).

    Returns:
        A ``NarrativeReport`` with all available evidence sections populated.
    """
    report = NarrativeReport()

    # ── Resolve repo ─────────────────────────────────────────────────
    try:
        from .db import get_repositories
        repos = get_repositories(conn)
    except Exception as exc:
        report.errors.append(f"Could not load repositories: {exc}")
        return report

    # Find by name or path.
    repo = None
    for r in repos:
        r_name = r.name if hasattr(r, "name") else r.get("name", "")
        r_path = r.path if hasattr(r, "path") else r.get("path", "")
        if r_name == repo_identifier or r_path == repo_identifier:
            repo = r
            break
        # Also match by partial path or name.
        if repo_identifier in r_name or repo_identifier in r_path:
            repo = r
            break

    if repo is None:
        report.errors.append(f"Repository '{repo_identifier}' not found.")
        return report

    repo_name = repo.name if hasattr(repo, "name") else repo.get("name", "")
    repo_path = repo.path if hasattr(repo, "path") else repo.get("path", "")
    repo_id = repo.id if hasattr(repo, "id") else repo.get("id")

    report.repo_name = repo_name
    report.repo_path = repo_path

    if not repo_path or not os.path.isdir(repo_path):
        report.errors.append(f"Repository path '{repo_path}' does not exist on disk.")
        return report

    # ── Basic git metadata ───────────────────────────────────────────
    try:
        from .gitmeta import collect as collect_gitmeta, Metadata
        from .discovery import Repo

        dummy = Repo(path=Path(repo_path))
        meta = collect_gitmeta(dummy)

        report.default_branch = meta.default_branch or "HEAD"
        report.total_commits = meta.commit_count or 0
        report.primary_author = meta.primary_author or ""
        report.languages = meta.languages
        report.first_commit_date = meta.first_commit_date
        report.last_commit_date = meta.last_commit_date

        if meta.first_commit_date and meta.last_commit_date:
            try:
                first = datetime.fromisoformat(meta.first_commit_date)
                last = datetime.fromisoformat(meta.last_commit_date)
                report.age_days = max(1, (last - first).days)
            except (ValueError, TypeError):
                pass
    except Exception as exc:
        report.errors.append(f"Could not collect git metadata: {exc}")

    # ── Full commit log ──────────────────────────────────────────────
    branch = report.default_branch
    commits = _load_full_log(repo_path, branch, max_commits)
    if not commits:
        report.errors.append("No commits found in git history.")
        return report

    sorted_commits = sorted(commits, key=lambda c: c.date)

    # ── Author analysis ──────────────────────────────────────────────
    author_commits: Counter = Counter(c.author for c in commits)
    author_first: dict[str, str] = {}
    author_last: dict[str, str] = {}
    for c in sorted_commits:
        if c.author not in author_first:
            author_first[c.author] = c.date
        author_last[c.author] = c.date

    total = len(commits)
    report.total_authors = len(author_commits)
    report.authors = [
        AuthorStats(
            name=name,
            commit_count=count,
            first_commit=author_first.get(name),
            last_commit=author_last.get(name),
            pct=round(count / total * 100, 1),
        )
        for name, count in author_commits.most_common(30)
    ]

    # Bus factor: how many authors own 50%+ of commits.
    cumulative = 0
    for i, a in enumerate(report.authors):
        cumulative += a.commit_count
        if cumulative >= total * 0.5:
            report.bus_factor = i + 1
            break

    # ── Time-based patterns ──────────────────────────────────────────
    hour_counts: Counter = Counter()
    day_counts: Counter = Counter()
    month_counts: Counter = Counter()

    for c in commits:
        try:
            dt = datetime.fromisoformat(c.date)
            hour_counts[dt.hour] += 1
            day_counts[dt.strftime("%A")] += 1
            month_counts[dt.strftime("%Y-%m")] += 1
            if dt.weekday() >= 5:
                pass  # weekend
        except (ValueError, TypeError):
            pass

    report.commits_by_hour = dict(sorted(hour_counts.items()))
    report.commits_by_day = dict(day_counts)
    report.commits_by_month = dict(sorted(month_counts.items()))
    report.has_weekend_work = any(
        day in ("Saturday", "Sunday") and count > 0
        for day, count in day_counts.items()
    )

    if month_counts:
        report.most_active_month = max(month_counts, key=month_counts.get)

    if report.age_days > 0:
        report.avg_commits_per_day = round(total / report.age_days, 2)

    # ── Batch vs single commit detection ─────────────────────────────
    report.batch_count, report.single_count = _classify_commit_pattern(commits)

    # ── File evolution ───────────────────────────────────────────────
    report.current_file_count = len(meta.languages.values()) if hasattr(meta, "languages") else 0
    try:
        out = _run_git(repo_path, ["ls-files", branch])
        if out:
            report.current_file_count = len(out.splitlines())
    except Exception:
        pass

    report.files_added, report.files_removed = _get_file_add_remove_counts(repo_path, branch)
    report.files_modified = len(commits)  # each commit modified at least one file
    report.top_files = list(_get_file_change_counts(repo_path, branch).items())[:20]

    # ── Milestones (large commits) ───────────────────────────────────
    report.large_commits = _get_large_commits(repo_path, commits, threshold=200)
    # Sort by total changes, take top ones.
    report.large_commits.sort(key=lambda c: -(c.insertions + c.deletions))

    # ── Phases ──────────────────────────────────────────────────────
    report.phases = _detect_phases(commits)

    # ── Snapshot evolution (from DB) ─────────────────────────────────
    if repo_id is not None:
        try:
            snap_rows = conn.execute(
                "SELECT observed_at, readme_hash, architecture_hash, identity_hash "
                "FROM snapshots WHERE repo_path = ? "
                "ORDER BY observed_at",
                (repo_path,),
            ).fetchall()
            report.snapshot_count = len(snap_rows)

            # Count changes between consecutive snapshots.
            prev = None
            for row in snap_rows:
                if prev is not None:
                    if row["readme_hash"] and row["readme_hash"] != prev.get("readme_hash"):
                        report.readme_changes += 1
                    if row["architecture_hash"] and row["architecture_hash"] != prev.get("architecture_hash"):
                        report.architecture_changes += 1
                    if row["identity_hash"] and row["identity_hash"] != prev.get("identity_hash"):
                        report.identity_changes += 1
                prev = dict(row)
        except Exception:
            pass

    # ── Recent activity assessment ──────────────────────────────────
    if report.last_commit_date:
        try:
            last_dt = datetime.fromisoformat(report.last_commit_date)
            now = datetime.now(timezone.utc)
            days_since = (now - last_dt).days
            if days_since > 90:
                report.recent_activity = "dormant"
            elif days_since > 30:
                report.recent_activity = "low"
            elif days_since > 7:
                report.recent_activity = "moderate"
            else:
                report.recent_activity = "active"
        except Exception:
            pass

    return report


def format_narrative_summary(report: NarrativeReport) -> str:
    """Return a one-line summary for CLI display."""
    parts: list[str] = []
    parts.append(f"Age: {report.age_days}d")
    parts.append(f"Commits: {report.total_commits}")
    parts.append(f"Authors: {report.total_authors}")
    if report.phases:
        parts.append(f"Phases: {len(report.phases)}")
    parts.append(f"Bus factor: {report.bus_factor}")
    if report.recent_activity:
        parts.append(f"Activity: {report.recent_activity}")
    return " | ".join(parts)
