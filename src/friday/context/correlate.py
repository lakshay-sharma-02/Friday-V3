"""Context correlation (Milestone 7.2).

Assigns each EngineeringSession a single evidence-backed activity label. Rules
are deliberately conservative: a label is applied ONLY when the signal is
unambiguous. When two interpretations are both plausible, we keep UNKNOWN so the
session can be merged or re-labeled later. Never invent a narrative.

Evidence vocabulary (from GitObserver facts, Milestone 7):
  commit_count         — commits gained this run (Observed delta)
  dirty                — working tree had uncommitted changes (Observed)
  branch / branch_switch
  merge_events         — merge commits in recent history (Derived)
  revert_events        — revert mentions in recent history (Derived)
  repeated_reverts     — >=2 reverts (Inferred)
  README               — not emitted by GitObserver; doc work is inferred from
                          a commit while the README is the only changed path.

Because observers are added over time, correlation reads ONLY the facts present
and reasons at the session level, never assuming a specific observer exists.
"""

from __future__ import annotations

from typing import List, Optional

from ..observation.model import Confidence
from .models import EngineeringSession, SessionActivity


def correlate(session: EngineeringSession,
              facts: Optional[dict] = None,
              conn=None) -> EngineeringSession:
    """Label a session in place (returns the same object).

    `facts` is an optional precomputed {aspect: value} for the session's primary
    repo, used by tests; when omitted, facts are reconstructed from the
    observations referenced elsewhere. We only use session-local signals.

    When `conn` is provided, commit_count delta is computed against the
    immediately prior observation row in the DB — necessary for single-observation
    sessions where within-session delta is zero.
    """
    activity, confidence = _classify(session, facts, conn)
    session.activity = activity
    session.confidence = confidence
    return session


def _classify(session: EngineeringSession, facts, conn=None) -> tuple[SessionActivity, Confidence]:
    # Read the session's own fact signals from its observations (we stored them
    # as Observation objects upstream; here we accept either Observations or a
    # precomputed dict). Keep it branch-free of any specific observer.
    signals = _signals(session, facts, conn)

    commits = signals.get("commit_count", 0)
    has_dirty = signals.get("dirty", False)
    merge = signals.get("merge_events", 0)
    reverts = signals.get("revert_events", 0)
    repeated = signals.get("repeated_reverts", False)
    branch_switch = signals.get("branch_switch", False)
    readme_changed = signals.get("readme_changed", False)

    # Repeated reverts => debugging is the strongest, clearest signal.
    if repeated and commits >= 0:
        return SessionActivity.DEBUGGING, Confidence.INFERRED

    # Many revert mentions without a clean story => debugging (Inferred).
    if reverts >= 2:
        return SessionActivity.DEBUGGING, Confidence.INFERRED

    # Branch switch + new commits => feature implementation (Derived).
    if (branch_switch or merge) and commits > 0:
        return SessionActivity.FEATURE_WORK, Confidence.DERIVED

    # Commits only (no branch switch, no doc signal) => committing work.
    if commits > 0 and not readme_changed:
        if merge:
            return SessionActivity.FEATURE_WORK, Confidence.DERIVED
        return SessionActivity.COMMITTING, Confidence.OBSERVED

    # README changed (doc signal) and nothing else => documentation.
    if readme_changed and commits == 0 and not has_dirty:
        return SessionActivity.DOCUMENTATION, Confidence.DERIVED

    # Dirty tree, no commits, no doc signal => testing / in-progress work.
    if has_dirty and commits == 0:
        return SessionActivity.TESTING, Confidence.DERIVED

    # Nothing definitive => stay neutral. Conservative by design.
    return SessionActivity.UNKNOWN, Confidence.DERIVED


def _signals(session: EngineeringSession, facts, conn=None) -> dict:
    if facts is not None:
        return dict(facts)
    out: dict = {
        "commit_count": 0, "dirty": False, "merge_events": 0,
        "revert_events": 0, "repeated_reverts": False,
        "branch_switch": False, "readme_changed": False,
    }
    obs_list = sorted(getattr(session, "_obs_objects", []),
                      key=lambda o: (o.observed_at, o.aspect))
    for o in obs_list:
        _fold(out, o)

    # commit_count: use DELTA, not absolute value. Absolute is always >0 on
    # any repo with history — using it raw labels every observed repo as
    # "committing".
    # Strategy: take the delta between first and last commit_count obs
    # within this session (two observations = multiple observe ticks while
    # commits were happening). Single obs: compare against prior DB row, or
    # return 0 (baseline).
    commit_obs = [o for o in obs_list if o.aspect == "commit_count"]
    if len(commit_obs) >= 2:
        # Within-session delta tells the real story
        try:
            first_val = int(commit_obs[0].value) if commit_obs[0].value else 0
            last_val = int(commit_obs[-1].value) if commit_obs[-1].value else 0
            out["commit_count"] = max(0, last_val - first_val)
        except (TypeError, ValueError):
            out["commit_count"] = 0
    elif len(commit_obs) == 1 and conn is not None:
        # Compare against prior DB row
        last_obs = commit_obs[0]
        try:
            cur_count = int(last_obs.value) if last_obs.value else 0
        except (TypeError, ValueError):
            cur_count = 0
        prior = conn.execute(
            "SELECT value FROM observations WHERE subject = ? AND aspect = 'commit_count' "
            "AND observed_at < ? AND source = ? "
            "ORDER BY observed_at DESC LIMIT 1",
            (last_obs.subject, last_obs.observed_at, last_obs.source)
        ).fetchone()
        if prior and prior["value"]:
            prior_count = int(prior["value"])
            out["commit_count"] = max(0, cur_count - prior_count)
        else:
            out["commit_count"] = 0  # baseline — no change
    else:
        out["commit_count"] = 0
    return out


def _fold(out: dict, o) -> None:
    a = o.aspect
    v = o.value
    if a == "commit_count":
        try:
            out["commit_count"] = int(v)
        except (TypeError, ValueError):
            out["commit_count"] = 0
    elif a == "dirty":
        out["dirty"] = (v == "true")
    elif a == "merge_events":
        out["merge_events"] = _int(v)
    elif a == "revert_events":
        out["revert_events"] = _int(v)
    elif a == "repeated_reverts":
        out["repeated_reverts"] = (v == "true")
    elif a == "branch_switch":
        out["branch_switch"] = bool(v)
    elif a == "readme_changed":
        out["readme_changed"] = (v == "true")


def _int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def build_correlated(sessions: List[EngineeringSession],
                     facts_by_session: Optional[dict] = None,
                     conn=None) -> List[EngineeringSession]:
    """Correlate many sessions.

    Each session reads its own facts from the Observation objects attached
    during build (`_obs_objects`); `facts_by_session` is accepted for callers
    that precompute a {session_id: fact-dict} override but is otherwise unused.
    """
    out = []
    for s in sessions:
        f = (facts_by_session or {}).get(s.id) if facts_by_session else None
        out.append(correlate(s, f, conn=conn))
    return out
