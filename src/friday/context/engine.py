"""Context engine (Milestone 7.2).

The bridge between the frozen Observation Engine and the Brain. It reads
persisted observations, derives EngineeringSessions, correlates them to
activities, and persists sessions append-only (referencing observation ids, never
duplicating them).

READ / WRITE are strictly separated:

  WRITE  — ContextEngine.build()  : reads observations, builds, persists.
  READ   — sessions / session / timeline / summary / is_stale : never write.

A read method must never mutate persistent state. `friday context build` is the
only WRITE entrypoint; `friday context` / `sessions` / `timeline` / `session`
are READ-ONLY.

It does NOT modify the Observation Engine, the observers, or the Brain.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

from ..db import (
    SessionRow,
    get_session,
    insert_sessions,
    latest_observation_time,
    latest_session_built_at,
    observations_all,
    sessions_all,
    sessions_on_day,
)
from ..observation.model import Observation, now_iso
from .correlate import build_correlated
from .models import Confidence, ContextSummary, EngineeringSession, SessionActivity
from .session import build_sessions
from .summarize import summarize_day
from .timeline import build_timeline


def _rows_to_obs(rows: List) -> List[Observation]:
    return [Observation.from_row(r) for r in rows]


@dataclass
class ContextBuildResult:
    """Outcome of a write (build) pass — for concise CLI reporting."""

    total: int
    created: int
    updated: int
    latest_observation: Optional[str]

    def to_text(self) -> str:
        lines = [
            "Engineering Context",
            "",
            f"Built {self.total} session(s)",
            f"Created {self.created} new session(s)",
            f"Updated {self.updated} session(s)",
        ]
        if self.latest_observation:
            lines.append(f"Latest observation: {self.latest_observation}")
        else:
            lines.append("Latest observation: (none)")
        lines.append("")
        lines.append("Done.")
        return "\n".join(lines) + "\n"


class ContextEngine:
    def __init__(self, conn) -> None:
        self.conn = conn

    # --- WRITE: derivation + persistence ------------------------------------

    def build(self, source: str = "git",
              as_of: Optional[str] = None) -> ContextBuildResult:
        """Derive sessions from observations of `source`, correlate, persist.

        WRITE operation. The only mutating entrypoint. `as_of` defaults to the
        latest observation timestamp, so re-running over the same data is
        idempotent (same window key → INSERT OR REPLACE) rather than appending
        duplicate sessions. Returns a ContextBuildResult (counts) and does NOT
        print.
        """
        obs = _rows_to_obs(observations_all(self.conn))
        obs = [o for o in obs if o.source == source]
        if as_of is None:
            # Anchor the build to the GLOBAL latest observation time, not the
            # per-source max. `friday observe` runs every observer in one pass,
            # and each observer stamps a slightly different timestamp; anchoring
            # to only `source`'s max would make the build look older than the
            # newest observation, so `is_stale()` would report the freshly built
            # context as immediately stale. The build represents the whole run.
            as_of = latest_observation_time(self.conn) or now_iso()

        prior_ids = {r["id"] for r in
                     self.conn.execute("SELECT id FROM sessions").fetchall()}
        prior_rows = {r["id"]: r for r in
                      self.conn.execute("SELECT * FROM sessions").fetchall()}

        sessions = build_sessions(obs)
        sessions = build_correlated(sessions)
        for s in sessions:
            s.built_at = as_of

        # Validate no session ID collisions before persisting
        session_ids = [s.id for s in sessions]
        if len(session_ids) != len(set(session_ids)):
            duplicates = [sid for sid in session_ids if session_ids.count(sid) > 1]
            raise ValueError(
                f"Session ID collision detected. Duplicate IDs: {list(set(duplicates))}. "
                f"This indicates sessions with identical (primary_repo, start_time). "
                f"This should not happen with the current observation model."
            )

        # Close any open implicit transaction from prior raw writes, then
        # run the session insert atomically.
        self.conn.commit()
        self.conn.execute("BEGIN TRANSACTION")
        try:
            insert_sessions(self.conn, [s.to_row() for s in sessions])
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

        created = 0
        updated = 0
        for s in sessions:
            if s.id not in prior_ids:
                created += 1
            elif _changed(prior_rows[s.id], s):
                updated += 1
        return ContextBuildResult(
            total=len(sessions),
            created=created,
            updated=updated,
            latest_observation=latest_observation_time(self.conn),
        )

    # --- READ: queries (never mutate) ---------------------------------------

    def sessions(self) -> List[EngineeringSession]:
        return [EngineeringSession.from_row(r) for r in sessions_all(self.conn)]

    def session(self, session_id: str) -> Optional[EngineeringSession]:
        row = get_session(self.conn, session_id)
        return EngineeringSession.from_row(row) if row else None

    def sessions_for_day(self, day: str) -> List[EngineeringSession]:
        return [EngineeringSession.from_row(r) for r in sessions_on_day(self.conn, day)]

    def timeline(self, sessions: Optional[List[EngineeringSession]] = None
                 ) -> List:
        if sessions is None:
            sessions = self.sessions()
        return build_timeline(sessions)

    def summary(self, day: Optional[str] = None) -> ContextSummary:
        if day:
            sessions = self.sessions_for_day(day)
        else:
            sessions = self.sessions()
            if sessions:
                day = _day_of(sessions[0].start_time)
        return summarize_day(sessions, day)

    def is_stale(self) -> bool:
        """READ-ONLY. True if observations exist newer than the last build."""
        last_obs = latest_observation_time(self.conn)
        last_build = latest_session_built_at(self.conn)
        if last_obs is None or last_build is None:
            return False
        return last_obs > last_build


def _changed(prior_row, session: EngineeringSession) -> bool:
    """Did this session's persisted content differ from the prior row?"""
    return (
        prior_row["activity"] != session.activity.value
        or prior_row["confidence"] != session.confidence.value
        or prior_row["end_time"] != session.end_time
        or prior_row["repositories"] != ",".join(session.repositories)
        or prior_row["observations"] != ",".join(session.observations)
        or prior_row["branch"] != session.branch
    )


def _day_of(ts: str) -> str:
    try:
        dt = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%Y-%m-%d")
