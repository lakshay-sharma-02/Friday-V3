"""Session builder (Milestone 7.2).

Deterministically groups raw observations into EngineeringSessions. The builder
only groups — it does NOT label (that is correlate.py). Grouping is conservative
and evidence-only: observations are fused into one session only when they share
a repository, are close in time, and (where known) share a branch. If any of
those disagree, the conservative choice is to SPLIT, because a wrongly fused
session is far harder to untangle than two adjacent ones are to merge later.

Inputs are the persisted Observation rows (via the frozen Observation Engine),
ordered by observed_at. Because each engine run stamps one shared timestamp, a
run is treated as a single instantaneous observation event; gaps between runs
drive session boundaries.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

from ..observation.model import Confidence, Observation
from .models import EngineeringSession, SessionActivity

# Two observation events more than this many minutes apart start a new session.
SESSION_GAP_MIN = 90


def _parse(ts: str) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def build_sessions(observations: List[Observation]) -> List[EngineeringSession]:
    """Group ordered observations into per-repository, time/branch-bounded sessions.

    Algorithm (deterministic):
      1. Sort observations by observed_at.
      2. Reduce to a list of "observation events": one event per run timestamp,
         carrying every fact seen at that instant (so a single `friday observe`
         is one event, not N).
      3. Walk events in time order. Start a new session when:
           - the gap from the previous event exceeds SESSION_GAP_MIN, OR
           - the event's repository set overlaps the current session's but the
             branch is known and differs (branch switch = new context), OR
           - the event has no repository overlap with the current session
             (workspace-only facts do not extend a repo session).
      4. Assign primary_repo = the most frequently observed repo in the session.
    """
    events = _to_events(observations)
    if not events:
        return []

    sessions: List[EngineeringSession] = []
    cur: Optional[_OpenSession] = None

    for ev in events:
        repo = ev.primary_repo
        branch = ev.branch
        if cur is None or _should_split(cur, ev, repo, branch):
            if cur is not None:
                sessions.append(cur.close())
            cur = _OpenSession(start=ev.ts)
        cur.add(ev, repo, branch)

    if cur is not None:
        sessions.append(cur.close())

    # Workspace-only sessions (no repo at all) are dropped: they carry no
    # engineering work by themselves. A repo session absorbs any workspace fact
    # that falls inside its window (branch/dirty_count context).
    return [s for s in sessions if s.repositories]


def _to_events(observations: List[Observation]) -> List["_Event"]:
    by_time: dict[str, List[Observation]] = {}
    for o in observations:
        by_time.setdefault(o.observed_at, []).append(o)
    events: List[_Event] = []
    for ts in sorted(by_time):
        facts = by_time[ts]
        # Group facts by repo so each repo gets its own session.
        # A single `friday observe` run stamps all repos at once; without
        # splitting, every repo would collapse into one event with the
        # alphabetically-first repo as primary_repo — false sessions.
        by_subject: dict[str, List[Observation]] = {}
        for f in facts:
            by_subject.setdefault(f.subject, []).append(f)
        for subject in sorted(by_subject):
            events.append(_Event(ts=ts, facts=by_subject[subject]))
    return events


class _Event:
    """One engine-run instant and the facts it carried."""

    def __init__(self, ts: str, facts: List[Observation]) -> None:
        self.ts = ts
        self.facts = facts
        repos = {f.subject for f in facts
                 if f.subject != "workspace" and f.scope}
        self.repos = sorted(repos)
        # Branch is read from the "branch" aspect of the primary repo, if any.
        self.branch = self._branch(facts)
        self.primary_repo = self.repos[0] if self.repos else None

    @staticmethod
    def _branch(facts: List[Observation]) -> Optional[str]:
        for f in facts:
            if f.aspect == "branch" and f.subject != "workspace":
                return f.value or None
        return None


class _OpenSession:
    def __init__(self, start: str) -> None:
        self.start = start
        self.end = start
        self.repos: List[str] = []
        self.obs_ids: List[str] = []
        self.obs_objects: List[Observation] = []
        self.branch: Optional[str] = None
        self._repo_counts: dict[str, int] = {}

    def add(self, ev: _Event, repo: Optional[str], branch: Optional[str]) -> None:
        self.end = ev.ts
        self.obs_ids.extend(f.id for f in ev.facts)
        self.obs_objects.extend(ev.facts)
        if repo:
            self.repos.append(repo)
            self._repo_counts[repo] = self._repo_counts.get(repo, 0) + 1
        if branch:
            self.branch = branch

    def close(self) -> EngineeringSession:
        uniq_repos = list(dict.fromkeys(self.repos))  # stable order, no dups
        primary = max(self._repo_counts, key=self._repo_counts.get) \
            if self._repo_counts else None
        sess = EngineeringSession(
            start_time=self.start,
            end_time=self.end,
            repositories=uniq_repos,
            observations=list(dict.fromkeys(self.obs_ids)),
            activity=SessionActivity.UNKNOWN,
            confidence=Confidence.DERIVED,
            primary_repo=primary,
            branch=self.branch,
        )
        # Attach the source Observation objects so correlation can read facts
        # without re-querying. Not persisted (sessions store only ids).
        sess._obs_objects = self.obs_objects  # type: ignore[attr-defined]
        return sess


def _should_split(cur: _OpenSession, ev: _Event, repo: Optional[str],
                  branch: Optional[str]) -> bool:
    # Time gap.
    prev = _parse(cur.end)
    now = _parse(ev.ts)
    if prev is None or now is None:
        return True
    gap_min = (now - prev).total_seconds() / 60.0
    if gap_min > SESSION_GAP_MIN:
        return True
    # Branch switch within the same repo => new context (only when known).
    if (repo and repo in set(cur.repos) and cur.branch and branch
            and branch != cur.branch):
        return True
    # No repository overlap and this event has a repo => belongs elsewhere.
    if repo and repo not in set(cur.repos):
        return True
    return False
