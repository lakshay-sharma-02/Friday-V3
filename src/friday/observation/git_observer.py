"""GitObserver (Milestone 7).

Deterministically observes the git environment via the `git` CLI (no GitPython).
Every field is read live so change is detected without a full re-ingest. The
observer emits:

  Observed  — branch, dirty/clean, commit count, remote url, default branch,
              last commit date, author count.
  Derived   — commit-count delta vs. a reference, days since last activity,
              repository activity classification.
  Inferred  — dormant repository (Derived: days idle beyond threshold),
              repeated reverts (Derived: revert commits in recent history),
              merge events (Derived: merge commits in recent history),
              branch switch (Derived across runs: current branch != prior).

Confidence is assigned per fact. Inferred facts always carry a cause.

No LLM, no planner, no daemon. `collect` is pure: it reads git + the prior run
and returns Observation rows; the engine persists and diffs.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..db import get_repositories, observation_state_as_of
from ..discovery import Repo
from ..gitmeta import collect as git_collect
from .interface import Health, Observer, ObserverHealth
from .model import Confidence, Observation

# A repository is Inferred dormant after this many idle days.
DORMANT_DAYS = 30
# Look back this many commits when classifying merge / revert events.
_EVENT_LOOKBACK = 50


def _run(repo: Path, args: list[str]) -> Optional[str]:
    try:
        res = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if res.returncode != 0:
        return None
    return res.stdout.strip()


def _days_since(iso: Optional[str]) -> Optional[int]:
    if not iso:
        return None
    try:
        when = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0, (datetime.now(timezone.utc) - when).days)


class GitObserver(Observer):
    name = "git"

    def __init__(self, roots: Optional[list[Path]] = None):
        # Roots are informational; the observer reads whatever repos are stored
        # in the DB (same universe `observe` already watches).
        self.roots = roots or []

    # --- Observer interface --------------------------------------------------

    def collect(self, conn) -> list[Observation]:
        observed_at = datetime.now(timezone.utc).isoformat()
        rows: list[Observation] = []
        prior = observation_state_as_of(conn, self.name, observed_at)
        prior_by = {(o.subject, o.aspect): o for o in prior}

        repos = get_repositories(conn)
        names = {r.path: r.name for r in repos}

        for r in repos:
            path = r.path
            repo = Repo(path=Path(path))
            meta = git_collect(repo)
            friendly = r.name or (Path(path).name)
            rows.extend(self._facts(observed_at, path, friendly, meta, prior_by))

        # Workspace-level: count of repositories and how many are dirty.
        rows.extend(self._workspace(observed_at, repos, prior_by))
        return rows

    def summarize(self, conn) -> str:
        repos = get_repositories(conn)
        dirty = 0
        n = 0
        dormant = 0
        for r in repos:
            n += 1
            meta = git_collect(Repo(path=Path(r.path)))
            if meta.is_dirty:
                dirty += 1
            idle = _days_since(meta.last_commit_date)
            if idle is not None and idle >= DORMANT_DAYS:
                dormant += 1
        if n == 0:
            return "git: no repositories observed."
        return (
            f"git: watching {n} repositor{'y' if n == 1 else 'ies'}; "
            f"{dirty} dirty, {dormant} dormant."
        )

    def health(self, conn) -> ObserverHealth:
        git = _run(Path("."), ["--version"])
        if git is None:
            return ObserverHealth(False, Health.DOWN, "git --version",
                                  "git executable not found on PATH.")
        return ObserverHealth(True, Health.HEALTHY, "git --version", git)

    # --- Fact builders -------------------------------------------------------

    def _obs(self, at, subject, aspect, value, scope, conf, cause=None):
        return Observation(
            source=self.name, subject=subject, aspect=aspect, value=value,
            confidence=conf, observed_at=at, scope=scope, cause=cause,
        )

    def _facts(self, at, path, friendly, meta, prior_by):
        rows: list[Observation] = []
        rows.append(self._obs(at, friendly, "branch", meta.default_branch or "",
                              path, Confidence.OBSERVED))
        rows.append(self._obs(at, friendly, "dirty",
                              "true" if meta.is_dirty else "false", path,
                              Confidence.OBSERVED))
        rows.append(self._obs(at, friendly, "commit_count",
                              str(meta.commit_count or 0), path, Confidence.OBSERVED))
        rows.append(self._obs(at, friendly, "remote_url", meta.remote_url or "",
                              path, Confidence.OBSERVED))
        rows.append(self._obs(at, friendly, "last_commit_date",
                              meta.last_commit_date or "", path, Confidence.OBSERVED))

        # Derived: idle days + activity classification.
        idle = _days_since(meta.last_commit_date)
        if idle is not None:
            rows.append(self._obs(at, friendly, "idle_days", str(idle), path,
                                  Confidence.DERIVED,
                                  cause=f"{idle} days since last commit."))
            activity = "dormant" if idle >= DORMANT_DAYS else "active"
            cause = (f"no commit for {idle} days (>= {DORMANT_DAYS} = dormant)."
                     if activity == "dormant"
                     else f"{idle} days since last commit.")
            rows.append(self._obs(at, friendly, "activity", activity, path,
                                  Confidence.DERIVED, cause=cause))

        # Inferred: dormant repository.
        if idle is not None and idle >= DORMANT_DAYS:
            rows.append(self._obs(
                at, friendly, "dormant", "true", path, Confidence.INFERRED,
                cause=f"repository idle for {idle} days (last commit "
                      f"{meta.last_commit_date})."))

        # Derived: merge events + repeated reverts from recent history.
        rows.extend(self._events(at, path, friendly))

        # Derived across runs: branch switch.
        prev = prior_by.get((friendly, "branch"))
        if prev is not None and prev.value and meta.default_branch:
            if prev.value != meta.default_branch:
                rows.append(self._obs(
                    at, friendly, "branch_switch",
                    f"{prev.value} -> {meta.default_branch}", path, sc.DERIVED,
                    cause=f"checked-out branch moved from {prev.value} to "
                          f"{meta.default_branch}."))
        return rows

    def _events(self, at, path, friendly) -> list[Observation]:
        rows: list[Observation] = []
        log = _run(Path(path), [
            "log", f"-{_EVENT_LOOKBACK}", "--format=%s", "HEAD",
        ])
        if not log:
            return rows
        subjects = log.splitlines()
        merges = sum(1 for s in subjects if s.lower().startswith("merge"))
        reverts = sum(1 for s in subjects if "revert" in s.lower())
        rows.append(self._obs(at, friendly, "merge_events",
                              str(merges), path, Confidence.DERIVED,
                              cause=f"{merges} merge commit(s) in last "
                                    f"{_EVENT_LOOKBACK}."))
        rows.append(self._obs(at, friendly, "revert_events",
                              str(reverts), path, Confidence.DERIVED,
                              cause=f"{reverts} commit message(s) mention revert "
                                    f"in last {_EVENT_LOOKBACK}."))
        if reverts >= 2:
            rows.append(self._obs(
                at, friendly, "repeated_reverts", "true", path,
                Confidence.INFERRED,
                cause=f"{reverts} revert commits in the last {_EVENT_LOOKBACK} "
                      f"commits — instability in this branch."))
        return rows

    def _workspace(self, at, repos, prior_by):
        rows: list[Observation] = []
        n = len(repos)
        dirty = sum(1 for r in repos
                    if git_collect(Repo(path=Path(r.path))).is_dirty)
        rows.append(self._obs(at, "workspace", "repository_count", str(n), "",
                              Confidence.OBSERVED))
        rows.append(self._obs(at, "workspace", "dirty_count", str(dirty), "",
                              Confidence.DERIVED,
                              cause=f"{dirty} of {n} repositories have "
                                    f"uncommitted changes."))
        return rows
