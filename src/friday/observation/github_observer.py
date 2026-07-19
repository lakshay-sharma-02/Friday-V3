"""GitHubObserver (Milestone 7.5).

A NEW observer for the frozen Observation Engine. It observes engineering
activity happening on GitHub and emits deterministic engineering observations
that plug into the existing engine — no engine, context, or brain changes.

DESIGN (privacy-first, metadata-only):

  This observer is a PURE READER. It never clones, never syncs, never manages
  issues, never runs a webhook/daemon/poller. It reads *repository metadata*
  through one of three interchangeable providers and maps it to Observation
  facts:

    - GhCliProvider    — `gh` CLI (PREFERRED when available).
    - ApiTokenProvider — `GITHUB_TOKEN` via stdlib urllib (no extra deps).
    - FixtureProvider  — a JSON snapshot file / in-memory dicts (OFFLINE).

  Only the whitelisted metadata fields below are ever read or emitted. PR
  bodies, issue bodies, code diffs, review comments, and private messages are
  NEVER fetched and structurally cannot be emitted. The observer maps only
  counts, statuses, dates, and names to facts and ignores everything else.

Observations emitted per repository (subject = full_name):
  default_branch, stars, forks, open_issues, closed_issues, open_prs,
  merged_prs, archived, visibility, branch_protection, draft_prs,
  recent_commits, workflow_failures, workflow_success, review_requested,
  review_approved, review_changes_requested.

Entity-level current-state facts (the frozen engine's diff produces the
events in the spec):
  repo#PR        pr_state (open/merged/closed), pr_draft, review_state
  repo#ISSUE     issue_assigned
  repo@TAG       release (published)
  repo/WORKFLOW  ci_status (success/failure)

Engineering signals (evidence-backed, inferred where judgment is needed):
  repeated_ci_failures, repository_inactive, high_review_activity,
  release_cadence, merge_frequency, long_lived_pr, stale_issues.

Confidence follows the Observation Engine vocabulary (Observed/Derived/Inferred).
No LLM, no embeddings, no planner, no agents.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Protocol

from .interface import Health, Observer, ObserverHealth
from .model import Confidence, Observation

# --- Thresholds (frozen, evidence-backed) -----------------------------------

INACTIVE_DAYS = 30          # repo considered inactive after this idle span
LONG_LIVED_PR_DAYS = 14     # open PR older than this -> long_lived_pr
STALE_ISSUE_DAYS = 90       # open issue older than this -> stale_issue
REPEATED_CI_FAILURES = 2    # failing workflows >= this -> repeated_ci_failures
HIGH_REVIEW_THRESHOLD = 5   # total reviews >= this -> high_review_activity
CADENCE_WINDOW_DAYS = 30    # window for release_cadence / merge_frequency

# Config: repositories to observe (colon-separated "owner/name").
GITHUB_REPOS_ENV = "FRIDAY_GITHUB_REPOS"
# Config: offline snapshot path (JSON list or single repo object).
GITHUB_SNAPSHOT_ENV = "FRIDAY_GITHUB_SNAPSHOT"


# ---------------------------------------------------------------------------
# GitHub metadata model
# ---------------------------------------------------------------------------


class RepositorySnapshot:
    """Canonical, normalized GitHub repository metadata.

    A provider (gh CLI, token API, or fixture) produces a plain dict in this
    shape; the observer never sees the raw GitHub response directly. Only
    metadata is present — no PR/issue bodies, no diffs, no comments.
    """

    def __init__(
        self,
        full_name: str,
        default_branch: str = "main",
        stars: int = 0,
        forks: int = 0,
        open_issue_count: int = 0,
        closed_issue_count: int = 0,
        open_pr_count: int = 0,
        merged_pr_count: int = 0,
        archived: bool = False,
        private: bool = False,
        visibility: str = "public",
        branch_protection: bool = False,
        draft_pr_count: int = 0,
        recent_commits: Optional[list[dict]] = None,
        recent_releases: Optional[list[dict]] = None,
        workflows: Optional[list[dict]] = None,
        pull_requests: Optional[list[dict]] = None,
        issues: Optional[list[dict]] = None,
    ) -> None:
        self.full_name = full_name
        self.default_branch = default_branch
        self.stars = stars
        self.forks = forks
        self.open_issue_count = open_issue_count
        self.closed_issue_count = closed_issue_count
        self.open_pr_count = open_pr_count
        self.merged_pr_count = merged_pr_count
        self.archived = archived
        self.private = private
        self.visibility = visibility
        self.branch_protection = branch_protection
        self.draft_pr_count = draft_pr_count
        self.recent_commits = recent_commits or []
        self.recent_releases = recent_releases or []
        self.workflows = workflows or []
        self.pull_requests = pull_requests or []
        self.issues = issues or []

    @classmethod
    def from_dict(cls, d: dict) -> "RepositorySnapshot":
        return cls(
            full_name=d["full_name"],
            default_branch=d.get("default_branch", "main"),
            stars=int(d.get("stars", 0) or 0),
            forks=int(d.get("forks", 0) or 0),
            open_issue_count=int(d.get("open_issue_count", 0) or 0),
            closed_issue_count=int(d.get("closed_issue_count", 0) or 0),
            open_pr_count=int(d.get("open_pr_count", 0) or 0),
            merged_pr_count=int(d.get("merged_pr_count", 0) or 0),
            archived=bool(d.get("archived", False)),
            private=bool(d.get("private", False)),
            visibility=d.get("visibility", "public") or "public",
            branch_protection=bool(d.get("branch_protection", False)),
            draft_pr_count=int(d.get("draft_pr_count", 0) or 0),
            recent_commits=d.get("recent_commits") or [],
            recent_releases=d.get("recent_releases") or [],
            workflows=d.get("workflows") or [],
            pull_requests=d.get("pull_requests") or [],
            issues=d.get("issues") or [],
        )


# ---------------------------------------------------------------------------
# Provider protocol (one seam for gh CLI / token / fixture)
# ---------------------------------------------------------------------------


class GitHubProvider(Protocol):
    """Produces canonical RepositorySnapshot dicts for the configured repos."""

    def fetch(self) -> list[dict]:
        ...

    def describe(self) -> str:
        """Human-readable source description for health reporting."""
        ...


def _configured_repos() -> list[str]:
    raw = os.environ.get(GITHUB_REPOS_ENV, "").strip()
    if not raw:
        return []
    return [r.strip() for r in raw.split(":") if r.strip()]


class FixtureProvider:
    """Offline provider: returns pre-built canonical dicts or a JSON file.

    Used by every test and as the default offline mode (FRIDAY_GITHUB_SNAPSHOT).
    Never touches the network.
    """

    def __init__(self, snapshots: list[dict] | Path) -> None:
        self._source = snapshots

    def fetch(self) -> list[dict]:
        if isinstance(self._source, Path):
            return _load_snapshot_file(self._source)
        return list(self._source)

    def describe(self) -> str:
        if isinstance(self._source, Path):
            return f"fixture: {self._source}"
        return f"fixture: {len(self._source)} repo(s)"


class GhCliProvider:
    """Provider backed by the `gh` CLI (preferred when available)."""

    def __init__(self, repos: list[str]) -> None:
        self.repos = repos

    def fetch(self) -> list[dict]:
        out: list[dict] = []
        for slug in self.repos:
            raw = _gh_api(f"repos/{slug}")
            if not raw:
                continue
            out.append(_assemble_from_raw(slug, raw))
        return out

    def describe(self) -> str:
        return f"gh cli ({len(self.repos)} repo(s))"


class ApiTokenProvider:
    """Provider backed by the GitHub REST API using GITHUB_TOKEN (stdlib only)."""

    def __init__(self, token: str, repos: list[str]) -> None:
        self.token = token
        self.repos = repos

    def fetch(self) -> list[dict]:
        import urllib.request

        out: list[dict] = []
        for slug in self.repos:
            req = urllib.request.Request(
                f"https://api.github.com/repos/{slug}",
                headers={"Authorization": f"Bearer {self.token}",
                         "Accept": "application/vnd.github+json",
                         "X-GitHub-Api-Version": "2022-11-28"})
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    raw = json.loads(resp.read().decode("utf-8", "ignore"))
            except (OSError, ValueError, TypeError):
                continue
            out.append(_assemble_from_raw(slug, raw))
        return out

    def describe(self) -> str:
        return f"api token ({len(self.repos)} repo(s))"


def _gh_api(path: str) -> Optional[dict]:
    try:
        res = subprocess.run(
            ["gh", "api", path, "--jq", "."],
            capture_output=True, text=True, timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if res.returncode != 0 or not res.stdout.strip():
        return None
    try:
        return json.loads(res.stdout)
    except (ValueError, TypeError):
        return None


def _assemble_from_raw(slug: str, repo: dict) -> dict:
    """Map a GitHub REST `repo` object to the canonical snapshot dict.

    Pulls/issues/runs/releases/branches/commits are folded in from the repo
    object's summary counts plus the few list fields GitHub returns inline.
    Providers that fetched the richer list endpoints can override these keys;
    this gives a correct, network-tolerant baseline from a single `gh api
    repos/{slug}` call.
    """
    pulls = [p for p in (repo.get("pull_requests") or [])]
    issues = [i for i in (repo.get("issues") or [])]
    workflows = [w for w in (repo.get("workflows") or [])]
    return {
        "full_name": repo.get("full_name", slug),
        "default_branch": repo.get("default_branch", "main"),
        "stars": repo.get("stargazers_count", 0),
        "forks": repo.get("forks_count", 0),
        "open_issue_count": repo.get("open_issues_count", 0)
        - len([p for p in pulls if p.get("state") == "open"]),
        "closed_issue_count": repo.get("closed_issue_count", 0),
        "open_pr_count": len([p for p in pulls if p.get("state") == "open"]),
        "merged_pr_count": len([p for p in pulls
                                if p.get("state") == "merged"
                                or p.get("merged")]),
        "archived": bool(repo.get("archived", False)),
        "private": bool(repo.get("private", False)),
        "visibility": repo.get("visibility", "private" if repo.get("private")
                               else "public"),
        "branch_protection": bool(repo.get("branch_protection", False)),
        "draft_pr_count": len([p for p in pulls
                               if p.get("state") == "open" and p.get("draft")]),
        "recent_commits": repo.get("recent_commits") or [],
        "recent_releases": repo.get("recent_releases") or [],
        "workflows": workflows,
        "pull_requests": pulls,
        "issues": issues,
    }


def _load_snapshot_file(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, ValueError, TypeError):
        return []
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    return []


def default_provider(repos: Optional[list[str]] = None) -> GitHubProvider:
    """Choose a provider: fixture snapshot > gh CLI > token > empty."""
    snap = os.environ.get(GITHUB_SNAPSHOT_ENV)
    if snap:
        return FixtureProvider(Path(snap).expanduser())
    repos = repos if repos is not None else _configured_repos()
    if _gh_api("rate_limit") is not None:
        return GhCliProvider(repos)
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        return ApiTokenProvider(token, repos)
    return FixtureProvider([])  # healthy: nothing configured to observe


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_date(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _days_since(value: Optional[str]) -> Optional[int]:
    dt = _parse_date(value)
    if dt is None:
        return None
    return max(0, (datetime.now(timezone.utc) - dt).days)


def _is_failure(workflow: dict) -> bool:
    conclusion = (workflow.get("conclusion") or "").lower()
    status = (workflow.get("status") or "").lower()
    if conclusion in ("failure", "cancelled", "timed_out", "action_required"):
        return True
    if conclusion:
        return False
    return status == "failure"


# ---------------------------------------------------------------------------
# The observer
# ---------------------------------------------------------------------------


class GitHubObserver(Observer):
    name = "github"

    def __init__(self, provider: Optional[GitHubProvider] = None,
                 repos: Optional[list[str]] = None) -> None:
        # A provider is the ONLY input. Tests inject FixtureProvider.
        self.provider = provider or default_provider(repos)
        self._at = _now()

    # --- Observer interface --------------------------------------------------

    def health(self, conn) -> ObserverHealth:
        snaps = self._safe_fetch()
        method = self.provider.describe()
        if not snaps:
            # Nothing to observe is HEALTHY, not an error.
            return ObserverHealth(
                True, Health.HEALTHY, method,
                "no GitHub repositories configured to observe.")
        return ObserverHealth(True, Health.HEALTHY, method,
                              f"observing {len(snaps)} repository(ies).")

    def collect(self, conn) -> list[Observation]:
        snaps = self._safe_fetch()
        observations: list[Observation] = []
        # Stamp every fact in a run with the latest date seen in the snapshots
        # so repeated runs over identical fixtures produce identical, idempotent
        # ids (mirrors how TerminalObserver stamps each fact with its event ts).
        self._at = _now()
        best: Optional[str] = None
        for d in snaps:
            snap = RepositorySnapshot.from_dict(d)
            snap_at = self._snapshot_at(snap)
            if snap_at and (best is None or snap_at > best):
                best = snap_at
        if best:
            self._at = best
        for d in snaps:
            snap = RepositorySnapshot.from_dict(d)
            observations.extend(self._repo_facts(snap))
            observations.extend(self._pr_facts(snap))
            observations.extend(self._issue_facts(snap))
            observations.extend(self._release_facts(snap))
            observations.extend(self._ci_facts(snap))
            observations.extend(self._signals(snap))
        observations.append(self._ws(len(snaps)))
        return observations

    def summarize(self, conn) -> str:
        snaps = self._safe_fetch()
        repos = [RepositorySnapshot.from_dict(d) for d in snaps]
        n_repos = len(repos)
        open_prs = sum(r.open_pr_count for r in repos)
        merged_today = self._merged_today(repos)
        ci_failures = sum(
            sum(1 for w in r.workflows if _is_failure(w)) for r in repos)
        recent_releases = self._releases_in_window(repos)
        return (
            "GitHub Observer\n"
            "Healthy\n"
            f"Repositories\n{n_repos}\n"
            f"Open PRs\n{open_prs}\n"
            f"Merged today\n{merged_today}\n"
            f"CI failures\n{ci_failures}\n"
            f"Recent releases\n{recent_releases}"
        )

    # --- internals ----------------------------------------------------------

    def _safe_fetch(self) -> list[dict]:
        try:
            return self.provider.fetch()
        except Exception:
            return []

    def _obs(self, snap, aspect, value, conf, cause=None,
             subject=None) -> Observation:
        return Observation(
            source=self.name,
            subject=subject or snap.full_name,
            aspect=aspect, value=str(value),
            confidence=conf, observed_at=self._at,
            scope=snap.full_name, cause=cause,
        )

    def _snapshot_at(self, snap: RepositorySnapshot) -> Optional[str]:
        latest: Optional[datetime] = None
        for c in snap.recent_commits:
            dt = _parse_date(c.get("date"))
            if dt and (latest is None or dt > latest):
                latest = dt
        for r in snap.recent_releases:
            dt = _parse_date(r.get("published_at"))
            if dt and (latest is None or dt > latest):
                latest = dt
        for pr in snap.pull_requests:
            for key in ("created_at", "updated_at", "merged_at"):
                dt = _parse_date(pr.get(key))
                if dt and (latest is None or dt > latest):
                    latest = dt
        for iss in snap.issues:
            for key in ("created_at", "updated_at"):
                dt = _parse_date(iss.get(key))
                if dt and (latest is None or dt > latest):
                    latest = dt
        return latest.isoformat() if latest else None

    def _repo_facts(self, snap: RepositorySnapshot) -> list[Observation]:
        rows = [
            self._obs(snap, "default_branch", snap.default_branch,
                      Confidence.OBSERVED),
            self._obs(snap, "stars", snap.stars, Confidence.OBSERVED),
            self._obs(snap, "forks", snap.forks, Confidence.OBSERVED),
            self._obs(snap, "open_issues", snap.open_issue_count,
                      Confidence.OBSERVED),
            self._obs(snap, "closed_issues", snap.closed_issue_count,
                      Confidence.OBSERVED),
            self._obs(snap, "open_prs", snap.open_pr_count, Confidence.OBSERVED),
            self._obs(snap, "merged_prs", snap.merged_pr_count,
                      Confidence.OBSERVED),
            self._obs(snap, "archived", "true" if snap.archived else "false",
                      Confidence.OBSERVED),
            self._obs(snap, "visibility", snap.visibility, Confidence.OBSERVED),
            self._obs(snap, "branch_protection",
                      "true" if snap.branch_protection else "false",
                      Confidence.OBSERVED),
            self._obs(snap, "draft_prs", snap.draft_pr_count,
                      Confidence.OBSERVED),
            self._obs(snap, "recent_commits", len(snap.recent_commits),
                      Confidence.OBSERVED),
            self._obs(snap, "workflow_failures",
                      sum(1 for w in snap.workflows if _is_failure(w)),
                      Confidence.OBSERVED),
            self._obs(snap, "workflow_success",
                      sum(1 for w in snap.workflows if not _is_failure(w)),
                      Confidence.OBSERVED),
        ]
        # Review activity counts (Observed).
        req = appr = chg = 0
        for pr in snap.pull_requests:
            rs = (pr.get("review_state") or "none").lower()
            if rs == "requested":
                req += 1
            elif rs == "approved":
                appr += 1
            elif rs == "changes_requested":
                chg += 1
        rows.append(self._obs(snap, "review_requested", req,
                              Confidence.OBSERVED))
        rows.append(self._obs(snap, "review_approved", appr,
                              Confidence.OBSERVED))
        rows.append(self._obs(snap, "review_changes_requested", chg,
                              Confidence.OBSERVED))
        return rows

    def _pr_facts(self, snap: RepositorySnapshot) -> list[Observation]:
        rows: list[Observation] = []
        for pr in snap.pull_requests:
            num = pr.get("number")
            if not num:
                continue
            subj = f"{snap.full_name}#{num}"
            state = pr.get("state", "open")
            if pr.get("merged"):
                state = "merged"
            rows.append(self._obs(snap, "pr_state", state, Confidence.OBSERVED,
                                  subject=subj))
            rows.append(self._obs(snap, "pr_draft",
                                  "true" if pr.get("draft") else "false",
                                  Confidence.OBSERVED, subject=subj))
            rs = pr.get("review_state") or "none"
            rows.append(self._obs(snap, "review_state", rs, Confidence.OBSERVED,
                                  subject=subj))
        return rows

    def _issue_facts(self, snap: RepositorySnapshot) -> list[Observation]:
        rows: list[Observation] = []
        for iss in snap.issues:
            num = iss.get("number")
            if not num:
                continue
            subj = f"{snap.full_name}#{num}"
            assigned = bool(iss.get("assigned"))
            rows.append(self._obs(snap, "issue_assigned",
                                  "true" if assigned else "false",
                                  Confidence.OBSERVED, subject=subj))
        return rows

    def _release_facts(self, snap: RepositorySnapshot) -> list[Observation]:
        rows: list[Observation] = []
        for rel in snap.recent_releases:
            tag = rel.get("tag") or rel.get("name") or "unknown"
            subj = f"{snap.full_name}@{tag}"
            rows.append(self._obs(snap, "release", "published",
                                  Confidence.OBSERVED, subject=subj,
                                  cause=f"release {tag} published."))
        return rows

    def _ci_facts(self, snap: RepositorySnapshot) -> list[Observation]:
        rows: list[Observation] = []
        for wf in snap.workflows:
            name = wf.get("name") or "workflow"
            subj = f"{snap.full_name}/{name}"
            status = "failure" if _is_failure(wf) else "success"
            rows.append(self._obs(snap, "ci_status", status, Confidence.OBSERVED,
                                  subject=subj,
                                  cause=f"{name} is {status}."))
        return rows

    def _signals(self, snap: RepositorySnapshot) -> list[Observation]:
        rows: list[Observation] = []
        # Derived/Inferred engineering signals — evidence-backed only.

        # Recent commit date -> repository inactivity.
        last_commit = None
        for c in snap.recent_commits:
            dt = _parse_date(c.get("date"))
            if dt is not None and (last_commit is None or dt > last_commit):
                last_commit = dt
        idle = _days_since(last_commit.isoformat() if last_commit else None)
        if idle is not None and idle >= INACTIVE_DAYS:
            rows.append(self._obs(
                snap, "repository_inactive", "true", Confidence.INFERRED,
                cause=f"no commit for {idle} days (>= {INACTIVE_DAYS})."))

        # Repeated CI failures.
        failed = [w.get("name") for w in snap.workflows if _is_failure(w)]
        if len(failed) >= REPEATED_CI_FAILURES:
            rows.append(self._obs(
                snap, "repeated_ci_failures", "true", Confidence.INFERRED,
                cause=f"{len(failed)} workflow(s) failing: "
                      f"{', '.join(failed)}."))

        # Review activity.
        total_reviews = sum(
            1 for pr in snap.pull_requests
            if (pr.get("review_state") or "none") != "none")
        if total_reviews >= HIGH_REVIEW_THRESHOLD:
            rows.append(self._obs(
                snap, "high_review_activity", "true", Confidence.DERIVED,
                cause=f"{total_reviews} pull request(s) have review activity "
                      f"(>= {HIGH_REVIEW_THRESHOLD})."))

        # Release cadence (releases in window).
        cadence = self._releases_in_window([snap])
        if cadence > 0:
            rows.append(self._obs(
                snap, "release_cadence", cadence, Confidence.DERIVED,
                cause=f"{cadence} release(s) in last {CADENCE_WINDOW_DAYS} days."))

        # Merge frequency (merges in window).
        merges = 0
        for pr in snap.pull_requests:
            if pr.get("merged") or pr.get("state") == "merged":
                ma = pr.get("merged_at")
                if _days_since(ma) is not None and _days_since(ma) <= CADENCE_WINDOW_DAYS:
                    merges += 1
        if merges > 0:
            rows.append(self._obs(
                snap, "merge_frequency", merges, Confidence.DERIVED,
                cause=f"{merges} pull request(s) merged in last "
                      f"{CADENCE_WINDOW_DAYS} days."))

        # Long-lived PRs.
        for pr in snap.pull_requests:
            if pr.get("state") != "open":
                continue
            age = _days_since(pr.get("created_at"))
            if age is not None and age >= LONG_LIVED_PR_DAYS:
                rows.append(self._obs(
                    snap, "long_lived_pr", "true", Confidence.INFERRED,
                    subject=f"{snap.full_name}#{pr.get('number')}",
                    cause=f"PR #{pr.get('number')} open for {age} days "
                          f"(>= {LONG_LIVED_PR_DAYS})."))

        # Stale issues.
        for iss in snap.issues:
            if iss.get("state") != "open":
                continue
            age = _days_since(iss.get("created_at"))
            if age is not None and age >= STALE_ISSUE_DAYS:
                rows.append(self._obs(
                    snap, "stale_issue", "true", Confidence.INFERRED,
                    subject=f"{snap.full_name}#{iss.get('number')}",
                    cause=f"issue #{iss.get('number')} open for {age} days "
                          f"(>= {STALE_ISSUE_DAYS})."))

        return rows

    def _ws(self, n: int) -> Observation:
        return Observation(
            source=self.name, subject="github", aspect="repositories",
            value=str(n), confidence=Confidence.OBSERVED, observed_at=self._at,
            scope="", cause=None,
        )

    def _merged_today(self, repos: list[RepositorySnapshot]) -> int:
        count = 0
        for r in repos:
            for pr in r.pull_requests:
                if not (pr.get("merged") or pr.get("state") == "merged"):
                    continue
                ma = pr.get("merged_at")
                if _days_since(ma) is not None and _days_since(ma) <= 1:
                    count += 1
        return count

    def _releases_in_window(self, repos: list[RepositorySnapshot]) -> int:
        count = 0
        for r in repos:
            for rel in r.recent_releases:
                pa = rel.get("published_at")
                if _days_since(pa) is not None and _days_since(pa) <= CADENCE_WINDOW_DAYS:
                    count += 1
        return count
