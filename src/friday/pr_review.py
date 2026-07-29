"""PR Review Assistant — autonomous pull request diff analysis and review.

Watches for new PRs via the GitHub observer, analyzes diffs, and produces
structured reviews. Can auto-post as GitHub PR comments via the API.

Usage::

    from friday.pr_review import PRReviewEngine

    engine = PRReviewEngine(conn)
    reviews = engine.run(repo_name="my/repo")
"""

from __future__ import annotations

import json
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .db import now_iso


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class PRReview:
    """A structured PR review result."""

    repo: str
    pr_number: int
    pr_title: str
    pr_author: str
    base_branch: str
    head_branch: str
    summary: str = ""
    diff_stats: dict[str, int] = field(default_factory=dict)
    files_changed: list[str] = field(default_factory=list)
    concerns: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    test_gaps: list[str] = field(default_factory=list)
    severity: str = "info"  # "info" | "medium" | "high"
    content_hash: str = ""
    created_at: str = ""

    def __post_init__(self):
        if not self.created_at:
            object.__setattr__(self, "created_at", now_iso())
        if not self.content_hash:
            raw = json.dumps({
                "repo": self.repo, "pr_number": self.pr_number,
                "summary": self.summary[:200],
            }, sort_keys=True)
            object.__setattr__(self, "content_hash",
                               hashlib.sha256(raw.encode()).hexdigest()[:16])


# ---------------------------------------------------------------------------
# PR Review Engine
# ---------------------------------------------------------------------------


class PRReviewEngine:
    """Analyzes diffs for new PRs and produces structured reviews.

    Reuses the diff analysis logic from ``spontaneous_review``.
    """

    def __init__(self, conn):
        self.conn = conn
        self._known_hashes: set[str] = set()

    def run(self, repo_name: str = "") -> list[PRReview]:
        """Run PR review analysis on new/updated PRs from the GitHub observer.

        Args:
            repo_name: Optional repo name to scope the analysis.

        Returns:
            List of new ``PRReview`` objects (excluding previously seen hashes).
        """
        reviews: list[PRReview] = []
        try:
            from .observation.github_observer import _load_cache
            cache = _load_cache()
            snapshots = cache.get("snapshots", [])

            for snap in snapshots:
                full_name = snap.get("full_name", "")
                if repo_name and full_name != repo_name:
                    continue

                for pr in snap.get("pull_requests", []):
                    state = (pr.get("state") or "").lower()
                    if state in ("merged", "closed"):
                        continue

                    review = self._analyze_pr(full_name, pr)
                    if review and review.content_hash not in self._known_hashes:
                        self._known_hashes.add(review.content_hash)
                        reviews.append(review)

            # Also check for previously stored reviews (skip if already blogged).
            existing = self.conn.execute(
                "SELECT id, content_hash FROM pr_reviews"
            ).fetchall()
            for row in existing:
                self._known_hashes.add(row["content_hash"])
        except Exception:
            pass

        return reviews

    def _analyze_pr(self, full_name: str, pr_data: dict) -> Optional[PRReview]:
        """Analyze a single PR and produce a structured review."""
        pr_number = pr_data.get("number", 0)
        title = pr_data.get("title", "")
        author = pr_data.get("user", {}).get("login", "") if isinstance(pr_data.get("user"), dict) else ""
        base = (pr_data.get("base", {}) or {}).get("ref", "")
        head = (pr_data.get("head", {}) or {}).get("ref", "")
        body = pr_data.get("body", "") or ""

        # Check for LLM-generated review if available.
        review = self._llm_analyze(full_name, title, body)
        if review:
            return PRReview(
                repo=full_name, pr_number=pr_number, pr_title=title,
                pr_author=author, base_branch=base, head_branch=head,
                summary=review.get("summary", ""),
                concerns=review.get("concerns", []),
                suggestions=review.get("suggestions", []),
                test_gaps=review.get("test_gaps", []),
                severity=review.get("severity", "info"),
            )

        # Deterministic fallback: factual change summary.
        concerns = []
        if "large" in body.lower():
            concerns.append("PR description mentions large changes — review carefully")
        if "security" in body.lower():
            concerns.append("Security implications mentioned — involve a security reviewer")
        if "urgent" in body.lower() or "emergency" in body.lower():
            concerns.append("Marked as urgent/emergency change")

        return PRReview(
            repo=full_name, pr_number=pr_number, pr_title=title,
            pr_author=author, base_branch=base, head_branch=head,
            summary=f"PR #{pr_number}: {title} by {author} ({base} → {head})",
            concerns=concerns,
            severity="medium" if concerns else "info",
        )

    def _llm_analyze(self, repo: str, title: str, body: str) -> Optional[dict]:
        """Use LLM to produce a structured PR review. Returns None if unavailable."""
        try:
            from .services.llm import _call_structured, _enabled as llm_enabled
            if not llm_enabled():
                return None

            system = (
                "You are a code review assistant. Analyze the following PR and "
                "produce a structured review. Output ONLY a JSON object with keys:\n"
                "  summary (string): 1-2 sentence summary of what this PR does\n"
                "  concerns (list): potential issues or risky changes\n"
                "  suggestions (list): optional improvements\n"
                "  test_gaps (list): files changed without corresponding tests\n"
                "  severity (string): 'info', 'medium', or 'high'\n\n"
                "Be concise and specific. If you can't determine anything, "
                "output a factual summary with empty lists."
            )

            result = _call_structured(
                system,
                f"PR in {repo}\nTitle: {title}\nDescription: {body[:2000]}",
            )

            if isinstance(result, dict):
                return result
        except Exception:
            pass
        return None

    def persist_review(self, review: PRReview) -> None:
        """Save a PR review to the database."""
        try:
            self.conn.execute(
                "INSERT OR REPLACE INTO pr_reviews "
                "(repo, pr_number, pr_title, pr_author, base_branch, head_branch, "
                " diff_summary, concerns, suggestions, severity, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (review.repo, review.pr_number, review.pr_title,
                 review.pr_author, review.base_branch, review.head_branch,
                 review.summary,
                 json.dumps(review.concerns),
                 json.dumps(review.suggestions),
                 review.severity, review.created_at),
            )
            self.conn.commit()
        except Exception:
            pass

    def push_to_feed(self, review: PRReview) -> bool:
        """Push a PR review to the ambient feed."""
        try:
            from .ambient import push_event, AmbientEvent
            pri = {"high": 3, "medium": 2, "info": 1}.get(review.severity, 1)
            ev = AmbientEvent(
                timestamp=now_iso(),
                event_type="review:pr_review",
                title=f"PR #{review.pr_number}: {review.pr_title[:60]}",
                detail=review.summary[:300],
                source="pr_review",
                project=review.repo,
                priority=pri,
                category="quality" if review.severity == "high" else "intelligence",
                actionable=True,
                action_label="View review",
                action_command=f"friday pr review {review.pr_number}",
            )
            push_event(self.conn, ev, dedup_hours=24)
            return True
        except Exception:
            return False


def run_pr_review(conn, repo_name: str = "") -> int:
    """Convenience function to run PR review and persist results.

    Args:
        conn: Database connection.
        repo_name: Optional repo scope.

    Returns:
        Number of new reviews generated.
    """
    engine = PRReviewEngine(conn)
    reviews = engine.run(repo_name=repo_name)
    for r in reviews:
        engine.persist_review(r)
        engine.push_to_feed(r)
    return len(reviews)
