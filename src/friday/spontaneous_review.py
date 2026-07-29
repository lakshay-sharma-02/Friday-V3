"""Spontaneous Code Review — proactive review notes for the workspace.

On every daemon cycle, the ``SpontaneousReviewEngine`` checks five trigger
sources for code worth reviewing and produces ``ReviewNote`` instances.
High-severity notes become ambient feed events; all notes accumulate in the
"pending review" queue visible via ``friday review pending``.

Trigger sources (each a private method):
  A. **Dirty repos** — uncommitted changes with problematic patterns.
  B. **PR signals** — long-lived PRs, stale reviews, unreviewed changes.
  C. **CI failures** — workflow runs that failed recently.
  D. **Skill drift** — formed skills whose health is degrading.
  E. **Blast radius** — high-impact files modified recently.

Design:
  - ``ReviewNote`` is the sole output model — immutable, evidence-backed.
  - Each trigger resolver is a pure function: reads DB / git, returns notes.
  - Dedup by content hash: the same note never fires twice across cycles.
  - Respects the existing ``pending_initiatives`` table for the review queue.
  - All LLM-free; every finding is deterministic.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .db import connect, get_repositories, now_iso
from .impact import analyze_impact, scan_dirty_patterns


# ---------------------------------------------------------------------------
# ReviewNote — the sole output model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ReviewNote:
    """One evidence-backed review finding.

    Every note has a deterministic ``content_hash`` derived from its fields
    so the engine can skip duplicates across cycles. The hash is computed
    at construction time and is part of the frozen identity.
    """

    title: str
    severity: str  # "high" | "medium" | "low"
    category: str  # "dirty_repo" | "pr_review" | "ci_failure" | "skill_drift" | "blast_radius"
    repo: str
    file: Optional[str] = None
    detail: str = ""
    action_command: str = ""
    created_at: str = ""
    content_hash: str = ""

    def __post_init__(self) -> None:
        # Compute content hash deterministically.
        raw = json.dumps({
            "title": self.title,
            "category": self.category,
            "repo": self.repo,
            "file": self.file,
            "detail": self.detail[:200],
        }, sort_keys=True)
        object.__setattr__(self, "content_hash",
                           hashlib.sha256(raw.encode()).hexdigest()[:16])
        if not self.created_at:
            object.__setattr__(self, "created_at", now_iso())

    def to_pending_row(self) -> dict[str, Any]:
        """Convert to a row for the ``pending_initiatives`` table.

        Fits into the existing schema:
          - ``statement`` holds the detail text.
          - ``knowledge_ids`` holds the JSON evidence blob (content_hash,
            category, repo, file, action_command, severity). This field
            is unused by spontaneous review notes, making it a convient
            structured storage slot.
          - ``action_taken`` is not set (no action taken yet).
        """
        blob = json.dumps({
            "content_hash": self.content_hash,
            "category": self.category,
            "repo": self.repo,
            "file": self.file,
            "action_command": self.action_command,
            "severity": self.severity,
        })
        return {
            "title": self.title,
            "statement": self.detail[:500],
            "initiative_type": f"spontaneous_review:{self.category}",
            "confidence": self.severity,
            "knowledge_ids": blob,
            "detected_at": self.created_at,
        }


# ---------------------------------------------------------------------------
# SpontaneousReviewEngine
# ---------------------------------------------------------------------------


class SpontaneousReviewEngine:
    """Proactively identifies code worth reviewing each daemon cycle.

    Usage::

        engine = SpontaneousReviewEngine(conn)
        notes = engine.run()
        engine.push_to_feed(notes)
    """

    def __init__(self, conn) -> None:
        self.conn = conn
        # Cache known content hashes so we never re-emit the same note.
        self._known_hashes: set[str] = set()
        self._load_known_hashes()

    # ── Public API ─────────────────────────────────────────────────────────

    def run(self) -> list[ReviewNote]:
        """Run all five trigger resolvers and return **new** review notes.

        Only notes whose ``content_hash`` has not been seen before are
        returned. Run this once per daemon cycle.
        """
        notes: list[ReviewNote] = []
        notes.extend(self._check_dirty_repos())
        notes.extend(self._check_pr_signals())
        notes.extend(self._check_ci_failures())
        notes.extend(self._check_skill_drift())
        notes.extend(self._check_blast_radius())

        # Filter to only truly new notes.
        new_notes = [n for n in notes if n.content_hash not in self._known_hashes]
        for n in new_notes:
            self._known_hashes.add(n.content_hash)
        return new_notes

    def push_to_feed(self, notes: list[ReviewNote]) -> int:
        """Push high-severity notes to the ambient feed.

        Returns the number of events pushed.
        """
        from .ambient import AmbientEvent, push_event

        pushed = 0
        for note in notes:
            if note.severity in ("high", "medium"):
                pri = 3 if note.severity == "high" else 2 if note.severity == "medium" else 1
                ev = AmbientEvent(
                    timestamp=note.created_at,
                    event_type=f"review:{note.category}",
                    title=note.title,
                    detail=note.detail[:300],
                    source="spontaneous_review",
                    project=note.repo,
                    priority=pri,
                    category="quality" if note.severity == "high" else "intelligence",
                    actionable=bool(note.action_command),
                    action_command=note.action_command,
                    action_label="Review",
                )
                push_event(self.conn, ev, dedup_hours=24)
                pushed += 1
        return pushed

    def push_to_pending(self, notes: list[ReviewNote]) -> int:
        """Insert new notes into the ``pending_initiatives`` table.

        Returns the number of rows inserted.

        Uses the existing ``pending_initiatives`` table columns:
          - ``id``: derived from title (slugified).
          - ``statement``: detail text.
          - ``knowledge_ids``: JSON evidence blob.
          - ``watch_run_id``: not set (not a watch-harvested initiative).
        """
        inserted = 0
        for note in notes:
            row = note.to_pending_row()
            # Check if a pending item with the same title already exists.
            existing = self.conn.execute(
                "SELECT id FROM pending_initiatives WHERE title = ? AND reviewed = 0 "
                "AND dismissed_at IS NULL",
                (row["title"],),
            ).fetchone()
            if existing is not None:
                continue
            try:
                self.conn.execute(
                    "INSERT INTO pending_initiatives "
                    "(id, title, statement, initiative_type, confidence, "
                    " knowledge_ids, detected_at, reviewed, dismissed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 0, NULL)",
                    (
                        row["title"].replace(" ", "_").lower()[:64],
                        row["title"],
                        row["statement"],
                        row["initiative_type"],
                        row["confidence"],
                        row["knowledge_ids"],
                        row["detected_at"],
                    ),
                )
                inserted += 1
            except Exception:
                # Duplicate title (PK collision) — skip silently.
                pass
        if inserted:
            self.conn.commit()
        return inserted

    # ── Trigger A: Dirty repos with problematic patterns ──────────────────

    def _check_dirty_repos(self) -> list[ReviewNote]:
        """Scan repos with uncommitted changes for review-worthy patterns.

        Uses ``impact.scan_dirty_patterns()`` on each dirty repo.
        """
        notes: list[ReviewNote] = []
        try:
            repos = get_repositories(self.conn)
        except Exception:
            return notes

        for repo in repos:
            repo_path = repo.path if hasattr(repo, "path") else repo.get("path", "")
            repo_name = repo.name if hasattr(repo, "name") else repo.get("name", "?")
            if not repo_path or not Path(repo_path).exists():
                continue

            # Quick dirty check.
            try:
                status = subprocess.run(
                    ["git", "-C", repo_path, "status", "--porcelain"],
                    capture_output=True, text=True, timeout=10,
                )
                if not status.stdout.strip():
                    continue
            except Exception:
                continue

            # Scan for patterns.
            try:
                findings = scan_dirty_patterns(repo_path)
            except Exception:
                findings = []

            for finding in findings:
                notes.append(ReviewNote(
                    title=f"{repo_name}: {finding['label']}",
                    severity=finding.get("severity", "medium"),
                    category="dirty_repo",
                    repo=repo_name,
                    file=finding.get("file"),
                    detail=finding.get("detail", ""),
                    action_command=f"friday impact {finding.get('file', repo_path)}",
                ))

        return notes

    # ── Trigger B: PR signals from GitHub observer ────────────────────────

    def _check_pr_signals(self) -> list[ReviewNote]:
        """Analyze GitHub observer data for PRs needing attention.
        
        Reads the most recent ``RepositorySnapshot`` data from the GitHub
        observer's cache and produces notes for:
        - Long-lived PRs (>14 days open)
        - PRs with changes requested and no updates
        - Stale issues (>90 days without activity)
        """
        notes: list[ReviewNote] = []
        try:
            from .observation.github_observer import _load_cache
            from .observation.github_observer import LONG_LIVED_PR_DAYS, STALE_ISSUE_DAYS
        except Exception:
            return notes

        try:
            cache = _load_cache()
        except Exception:
            return notes

        snapshots = cache.get("snapshots", [])
        if not snapshots:
            return notes

        for snap in snapshots:
            full_name = snap.get("full_name", "?")
            pull_requests = snap.get("pull_requests", [])
            issues = snap.get("issues", [])

            # Check PRs.
            for pr in pull_requests:
                state = (pr.get("state") or "").lower()
                if state == "merged" or state == "closed":
                    continue

                created = pr.get("created_at", "")
                days_open = _days_since(created)
                if days_open is not None and days_open >= LONG_LIVED_PR_DAYS:
                    notes.append(ReviewNote(
                        title=f"Long-lived PR: {pr.get('title', '?')[:60]}",
                        severity="medium",
                        category="pr_review",
                        repo=full_name,
                        detail=(f"PR #{pr.get('number', '?')} has been open "
                                f"for {days_open} days — review or close."),
                        action_command=f"friday review {full_name}",
                    ))

                # PR with changes requested but no updates.
                review_state = (pr.get("review_state") or "").lower()
                if review_state == "changes_requested":
                    notes.append(ReviewNote(
                        title=f"PR waiting on changes: {pr.get('title', '?')[:60]}",
                        severity="medium",
                        category="pr_review",
                        repo=full_name,
                        detail=f"PR #{pr.get('number', '?')} has changes requested.",
                        action_command=f"friday review {full_name}",
                    ))

            # Check issues.
            for issue in issues:
                created = issue.get("created_at", "")
                days_open = _days_since(created)
                if days_open is not None and days_open >= STALE_ISSUE_DAYS:
                    notes.append(ReviewNote(
                        title=f"Stale issue: {issue.get('title', '?')[:60]}",
                        severity="low",
                        category="pr_review",
                        repo=full_name,
                        detail=(f"Issue #{issue.get('number', '?')} has been open "
                                f"for {days_open} days without activity."),
                    ))

        return notes

    # ── Trigger C: CI failures from GitHub observer ───────────────────────

    def _check_ci_failures(self) -> list[ReviewNote]:
        """Check for CI workflow failures from the GitHub observer.
        
        Reads ``RepositorySnapshot.workflows`` and flags failures.
        """
        notes: list[ReviewNote] = []
        try:
            from .observation.github_observer import _load_cache
            from .observation.github_observer import REPEATED_CI_FAILURES
        except Exception:
            return notes

        try:
            cache = _load_cache()
        except Exception:
            return notes

        snapshots = cache.get("snapshots", [])
        for snap in snapshots:
            full_name = snap.get("full_name", "?")
            workflows = snap.get("workflows", [])

            failures: dict[str, int] = {}
            for wf in workflows:
                name = wf.get("name", "?")
                conclusion = (wf.get("conclusion", "") or "").lower()
                if conclusion in ("failure", "cancelled"):
                    failures[name] = failures.get(name, 0) + 1

            for wf_name, count in failures.items():
                if count >= 1:
                    severity = "high" if count >= REPEATED_CI_FAILURES else "medium"
                    notes.append(ReviewNote(
                        title=f"CI failure: {wf_name} in {full_name}",
                        severity=severity,
                        category="ci_failure",
                        repo=full_name,
                        detail=(f"Workflow '{wf_name}' failed {count} time(s) "
                                f"recently."),
                        action_command=f"friday impact {full_name}",
                    ))

        return notes

    # ── Trigger D: Skill drift affecting a repo's patterns ────────────────

    def _check_skill_drift(self) -> list[ReviewNote]:
        """Check if any formed skills have degraded health.
        
        Reads the skills table and flags unhealthy or degrading skills.
        """
        notes: list[ReviewNote] = []
        try:
            rows = self.conn.execute(
                "SELECT name, health, success_rate, error_rate, last_executed_at, "
                "drift_reason FROM skills "
                "WHERE health IN ('unhealthy', 'degrading') "
                "ORDER BY health DESC"
            ).fetchall()
        except Exception:
            return notes

        for row in rows:
            name = row["name"]
            health = row["health"]
            success = row["success_rate"] or 0
            error = row["error_rate"] or 0
            reason = row.get("drift_reason", "") or ""

            detail = (f"Skill '{name}' is {health} "
                      f"(success rate: {success:.0%}, error rate: {error:.0%}).")
            if reason:
                detail += f" {reason}"

            notes.append(ReviewNote(
                title=f"Skill degrading: {name}",
                severity="high" if health == "unhealthy" else "medium",
                category="skill_drift",
                repo="",
                detail=detail,
                action_command="friday skills drift",
            ))

        return notes

    # ── Trigger E: Blast radius — high-impact files modified recently ────

    def _check_blast_radius(self) -> list[ReviewNote]:
        """Check if files with high author-count or many dependents were
        recently modified.
        
        Uses ``impact.analyze_impact()`` on recently-modified files from
        dirty repos.
        """
        notes: list[ReviewNote] = []
        try:
            repos = get_repositories(self.conn)
        except Exception:
            return notes

        for repo in repos:
            repo_path = repo.path if hasattr(repo, "path") else repo.get("path", "")
            repo_name = repo.name if hasattr(repo, "name") else repo.get("name", "?")
            if not repo_path or not Path(repo_path).exists():
                continue

            # Get recently modified tracked files.
            try:
                out = subprocess.run(
                    ["git", "-C", repo_path, "diff", "--name-only", "@{1.day.ago}"],
                    capture_output=True, text=True, timeout=10,
                )
                if not out.stdout.strip():
                    # Fallback: last 5 commits' files.
                    out = subprocess.run(
                        ["git", "-C", repo_path, "diff", "--name-only", "@~5", "@"],
                        capture_output=True, text=True, timeout=10,
                    )
                recent_files = [f for f in out.stdout.splitlines() if f.strip()]
            except Exception:
                recent_files = []

            for rel_path in recent_files[:10]:  # cap at 10 files
                if not rel_path.strip():
                    continue
                full_path = str(Path(repo_path) / rel_path)
                if not Path(full_path).is_file():
                    continue

                try:
                    report = analyze_impact(self.conn, full_path, max_commits=3)
                except Exception:
                    continue

                # Flag if file has many authors or recent commits.
                if report.commit_count >= 20 and report.total_authors >= 3:
                    notes.append(ReviewNote(
                        title=f"High-impact file changed: {rel_path}",
                        severity="medium",
                        category="blast_radius",
                        repo=repo_name,
                        file=rel_path,
                        detail=(f"File has {report.commit_count} commits by "
                                f"{report.total_authors} authors — "
                                f"changes here may have wide impact."),
                        action_command=f"friday impact {full_path}",
                    ))

                # Flag if the file has many related repos.
                if len(report.related_repos) >= 3:
                    notes.append(ReviewNote(
                        title=f"File touches many repos: {rel_path}",
                        severity="medium",
                        category="blast_radius",
                        repo=repo_name,
                        file=rel_path,
                        detail=(f"File is related to "
                                f"{len(report.related_repos)} other repos — "
                                f"changes may have cross-project impact."),
                        action_command=f"friday impact {full_path}",
                    ))

        return notes

    # ── Internal helpers ──────────────────────────────────────────────────

    def _load_known_hashes(self) -> None:
        """Load previously-seen content hashes from the pending queue.

        Reads the ``knowledge_ids`` field (which stores the JSON evidence
        blob for spontaneous review notes) to find existing content hashes.
        This prevents re-emitting the same note across daemon restarts.
        """
        try:
            rows = self.conn.execute(
                "SELECT knowledge_ids FROM pending_initiatives "
                "WHERE initiative_type LIKE 'spontaneous_review:%'"
            ).fetchall()
            for row in rows:
                try:
                    blob = json.loads(row["knowledge_ids"])
                    h = blob.get("content_hash")
                    if h:
                        self._known_hashes.add(h)
                except (json.JSONDecodeError, TypeError):
                    pass
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------

def _days_since(iso_timestamp: Optional[str]) -> Optional[int]:
    """Return the number of whole days since an ISO timestamp, or None."""
    if not iso_timestamp:
        return None
    try:
        dt = datetime.fromisoformat(iso_timestamp)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0, (datetime.now(timezone.utc) - dt).days)
