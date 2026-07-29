"""Ambient Event Feed — structured event log for proactive intelligence.

Every notable discovery in the daemon cycle produces a structured event here.
The feed is the source of truth for:
  - The terminal dashboard (``friday dashboard``)
  - Desktop notification decisions (what to alert on)
  - Historical querying ("what did Friday notice yesterday?")

Design:
  - Append-only insert; events are never mutated (only ``dismissed`` flips)
  - Priority 0-3: 0=info, 1=noteworthy, 2=important, 3=critical
  - Events have categories, types, actionability, and dedup support
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional

from .db import now_iso


# ---------------------------------------------------------------------------
# Event taxonomy
# ---------------------------------------------------------------------------


class EventType(str, Enum):
    """Every kind of event the ambient feed can hold."""

    # Discovery events
    REPO_CHANGED = "repo_changed"
    KNOWLEDGE_UPDATED = "knowledge_updated"
    UNDERSTANDING_DERIVED = "understanding_derived"

    # Intelligence events
    NEW_INITIATIVE = "new_initiative"
    NEW_INSIGHT = "new_insight"
    HIGH_SEVERITY_SUGGESTION = "high_severity_suggestion"
    NEW_PATTERN = "new_pattern"
    INTENT_LABELED = "intent_labeled"
    SKILL_FORMED = "skill_formed"
    CROSS_PROJECT_CORRELATION = "cross_project_correlation"

    # Status events
    CYCLE_COMPLETE = "cycle_complete"
    CYCLE_FAILED = "cycle_failed"
    KILL_SWITCH_ACTIVATED = "kill_switch_activated"
    KILL_SWITCH_DEACTIVATED = "kill_switch_deactivated"

    # Quality events
    SKILL_DRIFT_DETECTED = "skill_drift_detected"
    CAPABILITY_GAP_DETECTED = "capability_gap_detected"
    AUTO_DISPATCHED = "auto_dispatched"

    # Self-healing events
    WORKER_AUTO_APPROVED = "worker_auto_approved"
    SKILL_AUTO_REPAIRED = "skill_auto_repaired"

    # Execution events
    MISSION_STARTED = "mission_started"
    MISSION_COMPLETED = "mission_completed"
    TASK_FAILED = "task_failed"

    # Proactive events
    PROACTIVE_INSIGHT = "proactive_insight"

    # Review events
    REVIEW_DIRTY_REPO = "review:dirty_repo"
    REVIEW_PR = "review:pr_review"
    REVIEW_CI_FAILURE = "review:ci_failure"
    REVIEW_SKILL_DRIFT = "review:skill_drift"
    REVIEW_BLAST_RADIUS = "review:blast_radius"

    # Briefing event
    BRIEFING_AVAILABLE = "briefing_available"

    # Presence events
    PRESENCE_CHANGED = "presence_changed"
    PRESENCE_FOCUS_ON = "presence_focus_on"
    PRESENCE_FOCUS_OFF = "presence_focus_off"

    # System Intelligence events
    RESOURCE_ALERT = "resource_alert"
    PROCESS_ANOMALY = "process_anomaly"
    BUILD_STATUS_CHANGED = "build_status_changed"


class Category(str, Enum):
    """High-level grouping for the feed UI."""

    WORKSPACE = "workspace"
    INTELLIGENCE = "intelligence"
    QUALITY = "quality"
    EXECUTION = "execution"
    SYSTEM = "system"


# ---------------------------------------------------------------------------
# Event model
# ---------------------------------------------------------------------------


@dataclass
class AmbientEvent:
    """One structured event in the ambient feed.

    Every event is created with a title that fits in 60 chars and an optional
    detail of 1-3 sentences. The ``actionable`` / ``action_command`` pair lets
    the dashboard and notification engine offer a "do this" button.

    The canonical envelope across all sources::

        {source, project, timestamp, event_type, payload, confidence}

    ``source`` is the producer (daemon, observer, telegram, ...).
    ``project`` is the repo/project name the event relates to (or "" for global).
    ``payload`` holds structured JSON with the event's rich data.
    ``confidence`` is a 0.0–1.0 float expressing how sure we are.
    """

    timestamp: str
    event_type: str
    title: str
    priority: int = 0
    category: str = "system"
    detail: str = ""
    source: str = "daemon"
    project: str = ""         # which repo/project this event is about
    payload: str = ""         # structured JSON payload
    confidence: float = 1.0   # 0.0–1.0 confidence in this event
    dismissed: bool = False
    actionable: bool = False
    action_label: str = ""
    action_command: str = ""
    mission_id: str = ""
    graph_id: str = ""
    id: int = 0  # populated on insert / fetch

    @classmethod
    def from_observation(cls, obs) -> "AmbientEvent":
        """Create an AmbientEvent from an Observation (observation/model.py).

        Maps the canonical envelope::

            obs.source       → source
            obs.subject      → project (the repo/project the fact is about)
            obs.observed_at  → timestamp
            obs.aspect       → event_type (the kind of fact)
            obs.value        → title (human-readable summary)
            obs.confidence   → confidence (OBSERVED → 1.0, DERIVED → 0.7, INFERRED → 0.4)

        The ``payload`` is built as a JSON object with the original
        observation fields preserved for downstream consumers that need
        the raw data (dashboard drill-down, notification templates).
        """
        import json

        # Map observation confidence levels to numeric values.
        conf_str = obs.confidence.value if hasattr(obs.confidence, 'value') else str(obs.confidence)
        confidence_map = {
            "Observed": 1.0,
            "Derived": 0.7,
            "Inferred": 0.4,
        }
        confidence = confidence_map.get(conf_str, 0.5)

        # Build payload as JSON with all preserved fields.
        payload = json.dumps({
            "aspect": obs.aspect,
            "value": obs.value,
            "scope": obs.scope or "",
            "detail": obs.detail or "",
            "cause": obs.cause or "",
            "confidence_label": conf_str,
        })

        # Determine priority from confidence (observed=noteworthy, derived=info,
        # inferred=info). The event factories in this module handle more nuanced
        # priority — this is a sensible default for the bridge.
        priority = 0 if conf_str == "Observed" else 0

        # Use the observation's aspect as a human-readable title.
        title = f"{obs.aspect}: {obs.value[:80]}"

        # Determine category from source prefix.
        source_lower = (obs.source or "").lower()
        if source_lower in ("git", "workspace", "filesystem"):
            category = "workspace"
        elif source_lower in ("runtime", "mission", "execution"):
            category = "execution"
        elif source_lower in ("skill", "pattern", "intent"):
            category = "intelligence"
        elif source_lower in ("hyprland", "browser", "desktop"):
            category = "workspace"
        else:
            category = "system"

        return cls(
            timestamp=obs.observed_at or now_iso(),
            event_type=obs.aspect,
            title=title,
            priority=priority,
            category=category,
            detail=obs.detail or "",
            source=obs.source,
            project=obs.subject,
            payload=payload,
            confidence=confidence,
        )


# ---------------------------------------------------------------------------
# Event helpers — factory functions with sensible defaults
# ---------------------------------------------------------------------------


def _ts() -> str:
    return now_iso()


# ---------------------------------------------------------------------------
# SQL schema (idempotent)
# ---------------------------------------------------------------------------

AMBIENT_FEED_SCHEMA = """
CREATE TABLE IF NOT EXISTS ambient_feed (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    title           TEXT NOT NULL,
    detail          TEXT NOT NULL DEFAULT '',
    source          TEXT NOT NULL DEFAULT 'daemon',
    project         TEXT NOT NULL DEFAULT '',
    payload         TEXT NOT NULL DEFAULT '',
    confidence      REAL NOT NULL DEFAULT 1.0,
    priority        INTEGER NOT NULL DEFAULT 0,
    category        TEXT NOT NULL DEFAULT 'system',
    dismissed       INTEGER NOT NULL DEFAULT 0,
    actionable      INTEGER NOT NULL DEFAULT 0,
    action_label    TEXT NOT NULL DEFAULT '',
    action_command  TEXT NOT NULL DEFAULT '',
    mission_id      TEXT NOT NULL DEFAULT '',
    graph_id        TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_ambient_feed_timestamp ON ambient_feed(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_ambient_feed_dismissed ON ambient_feed(dismissed);
CREATE INDEX IF NOT EXISTS idx_ambient_feed_priority ON ambient_feed(priority);
CREATE INDEX IF NOT EXISTS idx_ambient_feed_category ON ambient_feed(category);
CREATE INDEX IF NOT EXISTS idx_ambient_feed_event_type ON ambient_feed(event_type);
CREATE INDEX IF NOT EXISTS idx_ambient_feed_project ON ambient_feed(project);
CREATE INDEX IF NOT EXISTS idx_ambient_feed_confidence ON ambient_feed(confidence);
"""


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def push_event(conn, event: AmbientEvent, dedup_hours: int | None = None) -> int:
    """Insert one event into the feed. Returns the new row id.

    If *dedup_hours* is set, checks whether an event with the same
    ``event_type`` **and** ``title`` already exists within that many hours.
    If a match is found the insert is skipped and 0 is returned — the
    caller can use this return value to decide whether to notify.
    """
    if dedup_hours is not None:
        from datetime import datetime, timezone, timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=dedup_hours)).isoformat()
        existing = conn.execute(
            "SELECT id FROM ambient_feed "
            "WHERE event_type = ? AND title = ? AND timestamp >= ? "
            "LIMIT 1",
            (event.event_type, event.title, cutoff),
        ).fetchone()
        if existing is not None:
            return 0  # duplicate, skip

    cur = conn.execute(
        """INSERT INTO ambient_feed
           (timestamp, event_type, title, detail, source, project, payload,
            confidence, priority, category, dismissed, actionable,
            action_label, action_command, mission_id, graph_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            event.timestamp or _ts(),
            event.event_type,
            event.title,
            event.detail,
            event.source,
            event.project,
            event.payload,
            event.confidence,
            event.priority,
            event.category,
            1 if event.dismissed else 0,
            1 if event.actionable else 0,
            event.action_label,
            event.action_command,
            event.mission_id,
            event.graph_id,
        ),
    )
    conn.commit()
    return cur.lastrowid or 0


def get_feed(
    conn,
    limit: int = 50,
    offset: int = 0,
    category: str | None = None,
    min_priority: int = 0,
    include_dismissed: bool = False,
) -> list[AmbientEvent]:
    """Query the feed with filters, newest first."""
    clauses: list[str] = []
    params: list = []

    if not include_dismissed:
        clauses.append("dismissed = 0")
    if category:
        clauses.append("category = ?")
        params.append(category)
    if min_priority > 0:
        clauses.append("priority >= ?")
        params.append(min_priority)

    where = " AND ".join(clauses) if clauses else "1"
    rows = conn.execute(
        f"SELECT * FROM ambient_feed WHERE {where} "
        "ORDER BY timestamp DESC, id DESC LIMIT ? OFFSET ?",
        params + [limit, offset],
    ).fetchall()

    return [_row_to_event(r) for r in rows]


def get_unread_count(conn) -> int:
    """Number of undismissed events."""
    row = conn.execute(
        "SELECT COUNT(*) AS cnt FROM ambient_feed WHERE dismissed = 0"
    ).fetchone()
    return row["cnt"] if row else 0


def get_unread_by_priority(conn) -> list[dict]:
    """Unread count grouped by priority level (for dashboard status panel)."""
    rows = conn.execute(
        "SELECT priority, COUNT(*) AS cnt FROM ambient_feed "
        "WHERE dismissed = 0 GROUP BY priority ORDER BY priority DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def dismiss_event(conn, event_id: int) -> None:
    """Mark one event as dismissed."""
    conn.execute("UPDATE ambient_feed SET dismissed = 1 WHERE id = ?", (event_id,))
    conn.commit()


def dismiss_all(conn, category: str | None = None, max_priority: int = 3) -> int:
    """Dismiss all undismissed events, optionally filtered by category and max priority.

    Returns the number of events dismissed.
    """
    if category:
        cur = conn.execute(
            "UPDATE ambient_feed SET dismissed = 1 "
            "WHERE dismissed = 0 AND category = ? AND priority <= ?",
            (category, max_priority),
        )
    else:
        cur = conn.execute(
            "UPDATE ambient_feed SET dismissed = 1 "
            "WHERE dismissed = 0 AND priority <= ?",
            (max_priority,),
        )
    conn.commit()
    return cur.rowcount


def get_latest_of_type(conn, event_type: str) -> AmbientEvent | None:
    """Get the most recent event of a given type (for dedup)."""
    row = conn.execute(
        "SELECT * FROM ambient_feed WHERE event_type = ? "
        "ORDER BY timestamp DESC, id DESC LIMIT 1",
        (event_type,),
    ).fetchone()
    return _row_to_event(row) if row else None


def summarize_recent(conn, hours: int = 24) -> dict:
    """Summarize recent activity by category + priority.

    Returns a dict like::

        {
            "total_events": 42,
            "by_category": {"workspace": 15, "intelligence": 20, ...},
            "high_priority": 3,
            "unread": 18,
            "latest_event": { ... },
        }
    """
    from datetime import datetime, timezone, timedelta

    cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=hours)
    ).isoformat()

    total = conn.execute(
        "SELECT COUNT(*) AS cnt FROM ambient_feed WHERE timestamp >= ?",
        (cutoff,),
    ).fetchone()["cnt"]

    by_cat = {}
    for r in conn.execute(
        "SELECT category, COUNT(*) AS cnt FROM ambient_feed "
        "WHERE timestamp >= ? GROUP BY category",
        (cutoff,),
    ).fetchall():
        by_cat[r["category"]] = r["cnt"]

    high_pri = conn.execute(
        "SELECT COUNT(*) AS cnt FROM ambient_feed "
        "WHERE timestamp >= ? AND priority >= 2",
        (cutoff,),
    ).fetchone()["cnt"]

    unread = get_unread_count(conn)

    latest = get_feed(conn, limit=1)
    latest_dict = None
    if latest:
        e = latest[0]
        latest_dict = {
            "id": e.id,
            "timestamp": e.timestamp,
            "event_type": e.event_type,
            "title": e.title,
            "priority": e.priority,
            "category": e.category,
        }

    return {
        "total_events": total,
        "by_category": by_cat,
        "high_priority": high_pri,
        "unread": unread,
        "latest_event": latest_dict,
    }


def prune_feed(
    conn,
    dismissed_retention_days: int = 7,
    low_pri_retention_days: int = 14,
) -> int:
    """Delete old events from the feed to prevent unbounded growth.

    Applies a tiered retention policy:
    - Dismissed events: deleted after *dismissed_retention_days* (default 7).
    - Undismissed priority-0 (info) events: deleted after
      *low_pri_retention_days* (default 14).
    - Priority 1-3 events: kept indefinitely (they're the useful signal).

    Returns the number of deleted rows.
    """
    from datetime import datetime, timezone, timedelta

    now = datetime.now(timezone.utc)
    total = 0

    # Delete dismissed events older than retention period.
    cutoff = (now - timedelta(days=dismissed_retention_days)).isoformat()
    cur = conn.execute(
        "DELETE FROM ambient_feed WHERE dismissed = 1 AND timestamp < ?",
        (cutoff,),
    )
    total += cur.rowcount

    # Delete undismissed priority-0 events older than low-pri retention.
    cutoff = (now - timedelta(days=low_pri_retention_days)).isoformat()
    cur = conn.execute(
        "DELETE FROM ambient_feed WHERE dismissed = 0 AND priority = 0 AND timestamp < ?",
        (cutoff,),
    )
    total += cur.rowcount

    if total:
        conn.commit()
    return total


def count_recent_of_type(conn, event_type: str, hours: int = 6) -> int:
    """Count how many events of a type occurred in the last N hours.

    Used by the notification engine for dedup — if we already notified for
    this event type recently, skip the notification.
    """
    from datetime import datetime, timezone, timedelta

    cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=hours)
    ).isoformat()
    row = conn.execute(
        "SELECT COUNT(*) AS cnt FROM ambient_feed "
        "WHERE event_type = ? AND timestamp >= ?",
        (event_type, cutoff),
    ).fetchone()
    return row["cnt"] if row else 0


# ---------------------------------------------------------------------------
# Event factory — helpers to build events from daemon cycle results
# ---------------------------------------------------------------------------


def repo_change_event(
    changed: int, scanned: int, names: list[str] | None = None
) -> AmbientEvent:
    """Build an event for repo changes detected in a cycle."""
    if changed == 0:
        return AmbientEvent(
            timestamp=_ts(),
            event_type=EventType.REPO_CHANGED,
            title="No repos changed",
            detail=f"All {scanned} repos are unchanged since last cycle.",
            priority=0,
            category=Category.WORKSPACE,
        )
    name_str = ", ".join(names[:3]) if names else ""
    if len(names or []) > 3:
        name_str += f" and {len(names) - 3} more"
    detail = f"{changed} of {scanned} repos have new activity: {name_str}."
    return AmbientEvent(
        timestamp=_ts(),
        event_type=EventType.REPO_CHANGED,
        title=f"{changed}/{scanned} repos changed",
        detail=detail,
        priority=1,
        category=Category.WORKSPACE,
    )


def knowledge_event(updated: int) -> AmbientEvent:
    """Build an event for knowledge updates."""
    if updated == 0:
        return AmbientEvent(
            timestamp=_ts(),
            event_type=EventType.KNOWLEDGE_UPDATED,
            title="Knowledge base unchanged",
            priority=0,
            category=Category.WORKSPACE,
        )
    return AmbientEvent(
        timestamp=_ts(),
        event_type=EventType.KNOWLEDGE_UPDATED,
        title=f"{updated} knowledge updates applied",
        detail=f"Knowledge base updated with {updated} new or changed entries.",
        priority=1,
        category=Category.WORKSPACE,
    )


def initiative_event(count: int) -> AmbientEvent:
    """Build an event for new pending initiatives."""
    if count == 0:
        return AmbientEvent(
            timestamp=_ts(),
            event_type=EventType.NEW_INITIATIVE,
            title="No new initiatives",
            priority=0,
            category=Category.INTELLIGENCE,
        )
    return AmbientEvent(
        timestamp=_ts(),
        event_type=EventType.NEW_INITIATIVE,
        title=f"{count} new initiative(s) emerged",
        detail=f"Review pending initiatives to see what Friday has identified.",
        priority=2,
        category=Category.INTELLIGENCE,
        actionable=True,
        action_label="Review initiatives",
        action_command="friday review pending",
    )


def suggestion_event(new_count: int, high_sev: int) -> AmbientEvent | None:
    """Build an event for cross-project suggestions. Returns None if none."""
    if new_count == 0:
        return None
    if high_sev:
        return AmbientEvent(
            timestamp=_ts(),
            event_type=EventType.HIGH_SEVERITY_SUGGESTION,
            title=f"{high_sev} high-severity integration suggestion(s)",
            detail=f"Friday found {high_sev} high-value integration opportunities worth reviewing.",
            priority=2,
            category=Category.INTELLIGENCE,
            actionable=True,
            action_label="View suggestions",
            action_command="friday suggest",
        )
    return AmbientEvent(
        timestamp=_ts(),
        event_type=EventType.HIGH_SEVERITY_SUGGESTION,
        title=f"{new_count} integration suggestion(s)",
        detail=f"Cross-project analysis found {new_count} integration opportunities.",
        priority=1,
        category=Category.INTELLIGENCE,
        actionable=True,
        action_label="View suggestions",
        action_command="friday suggest",
    )


def gap_event(new_gaps: int, open_gaps: int) -> AmbientEvent:
    """Build an event for capability gaps."""
    if new_gaps == 0 and open_gaps == 0:
        return AmbientEvent(
            timestamp=_ts(),
            event_type=EventType.CAPABILITY_GAP_DETECTED,
            title="No capability gaps",
            priority=0,
            category=Category.QUALITY,
        )
    title = f"{new_gaps} new capability gap(s)" if new_gaps else f"{open_gaps} open gap(s)"
    return AmbientEvent(
        timestamp=_ts(),
        event_type=EventType.CAPABILITY_GAP_DETECTED,
        title=title,
        detail=f"Friday detected capability gaps in its execution pipeline.",
        priority=2 if new_gaps else 1,
        category=Category.QUALITY,
        actionable=True,
        action_label="Analyze gaps",
        action_command="friday meta analyze",
    )


def pattern_event(new_patterns: int, top_patterns: int) -> AmbientEvent:
    """Build an event for mined patterns."""
    if new_patterns == 0:
        return AmbientEvent(
            timestamp=_ts(),
            event_type=EventType.NEW_PATTERN,
            title="No new action patterns",
            priority=0,
            category=Category.INTELLIGENCE,
        )
    return AmbientEvent(
        timestamp=_ts(),
        event_type=EventType.NEW_PATTERN,
        title=f"{top_patterns} frequent workflow pattern(s) detected",
        detail=f"Mined {new_patterns} action patterns ({top_patterns} high-frequency).",
        priority=1,
        category=Category.INTELLIGENCE,
        actionable=True,
        action_label="View patterns",
        action_command="friday patterns",
    )


def intent_event(new_intents: int, high_conf: int) -> AmbientEvent:
    """Build an event for labeled intents."""
    if new_intents == 0:
        return AmbientEvent(
            timestamp=_ts(),
            event_type=EventType.INTENT_LABELED,
            title="No new workflow intents",
            priority=0,
            category=Category.INTELLIGENCE,
        )
    return AmbientEvent(
        timestamp=_ts(),
        event_type=EventType.INTENT_LABELED,
        title=f"{high_conf} workflow(s) recognized",
        detail=f"Labeled {new_intents} workflow intents ({high_conf} high-confidence).",
        priority=1,
        category=Category.INTELLIGENCE,
    )


def skill_event(count: int) -> AmbientEvent:
    """Build an event for formed skills."""
    if count == 0:
        return AmbientEvent(
            timestamp=_ts(),
            event_type=EventType.SKILL_FORMED,
            title="No new skills formed",
            priority=0,
            category=Category.INTELLIGENCE,
        )
    return AmbientEvent(
        timestamp=_ts(),
        event_type=EventType.SKILL_FORMED,
        title=f"{count} new skill(s) formed",
        detail=f"Workflow patterns have been formed into replayable skills.",
        priority=2,
        category=Category.INTELLIGENCE,
        actionable=True,
        action_label="View skills",
        action_command="friday skills",
    )


def drift_event(count: int) -> AmbientEvent:
    """Build an event for drifted skills."""
    if count == 0:
        return AmbientEvent(
            timestamp=_ts(),
            event_type=EventType.SKILL_DRIFT_DETECTED,
            title="No skill drift detected",
            priority=0,
            category=Category.QUALITY,
        )
    return AmbientEvent(
        timestamp=_ts(),
        event_type=EventType.SKILL_DRIFT_DETECTED,
        title=f"{count} skill(s) are degrading",
        detail=f"Skill quality has dropped below threshold. They may need re-formation.",
        priority=2,
        category=Category.QUALITY,
        actionable=True,
        action_label="Check drift",
        action_command="friday skills drift",
    )


def correlation_event(count: int) -> AmbientEvent:
    """Build an event for cross-project correlations."""
    if count == 0:
        return AmbientEvent(
            timestamp=_ts(),
            event_type=EventType.CROSS_PROJECT_CORRELATION,
            title="No new correlations",
            priority=0,
            category=Category.INTELLIGENCE,
        )
    return AmbientEvent(
        timestamp=_ts(),
        event_type=EventType.CROSS_PROJECT_CORRELATION,
        title=f"{count} cross-project correlation(s) detected",
        detail=f"Structural and/or semantic overlap found between repositories.",
        priority=1,
        category=Category.INTELLIGENCE,
        actionable=True,
        action_label="View correlations",
        action_command="friday correlate",
    )


def dispatch_event(count: int, succeeded: int = 0) -> AmbientEvent:
    """Build an event for auto-dispatch results."""
    if count == 0:
        return AmbientEvent(
            timestamp=_ts(),
            event_type=EventType.AUTO_DISPATCHED,
            title="No skills auto-dispatched",
            priority=0,
            category=Category.QUALITY,
        )
    return AmbientEvent(
        timestamp=_ts(),
        event_type=EventType.AUTO_DISPATCHED,
        title=f"{count} skill(s) auto-dispatched ({succeeded} succeeded)",
        priority=1,
        category=Category.QUALITY,
    )


def cycle_complete_event() -> AmbientEvent:
    return AmbientEvent(
        timestamp=_ts(),
        event_type=EventType.CYCLE_COMPLETE,
        title="Observation cycle complete",
        priority=0,
        category=Category.SYSTEM,
    )


def cycle_failed_event(error: str) -> AmbientEvent:
    return AmbientEvent(
        timestamp=_ts(),
        event_type=EventType.CYCLE_FAILED,
        title="Observation cycle failed",
        detail=error[:300],
        priority=3,
        category=Category.SYSTEM,
    )


def code_review_event(note: dict) -> AmbientEvent:
    """Build an event for a spontaneous code review finding.

    Args:
        note: A dict with keys ``title``, ``severity``, ``category``,
            ``repo``, ``detail``, ``action_command``.
    """
    pri = {"high": 3, "medium": 2, "low": 1}.get(note.get("severity", "low"), 1)
    source = "spontaneous_review"
    if note.get("repo"):
        project = note["repo"]
    else:
        project = ""
    return AmbientEvent(
        timestamp=_ts(),
        event_type=f"review:{note.get('category', 'dirty_repo')}",
        title=note.get("title", "Code review finding"),
        detail=note.get("detail", "")[:300],
        source=source,
        project=project,
        priority=pri,
        category="quality" if note.get("severity") == "high" else "intelligence",
        actionable=bool(note.get("action_command")),
        action_command=note.get("action_command", ""),
        action_label="Review",
    )


def kill_switch_event(active: bool) -> AmbientEvent:
    return AmbientEvent(
        timestamp=_ts(),
        event_type=(
            EventType.KILL_SWITCH_ACTIVATED
            if active
            else EventType.KILL_SWITCH_DEACTIVATED
        ),
        title="Kill switch activated" if active else "Kill switch deactivated",
        detail="All execution is blocked until the kill switch is released."
        if active
        else "Normal execution has been resumed.",
        priority=3 if active else 2,
        category=Category.SYSTEM,
        actionable=True,
        action_label="Resume" if active else "Activate",
        action_command="friday autonomy resume" if active else "friday autonomy kill",
    )


def worker_approved_event(count: int) -> AmbientEvent:
    """Build an event for auto-approved worker proposals."""
    if count == 0:
        return AmbientEvent(
            timestamp=_ts(),
            event_type=EventType.WORKER_AUTO_APPROVED,
            title="No worker proposals auto-approved",
            priority=0,
            category=Category.QUALITY,
        )
    return AmbientEvent(
        timestamp=_ts(),
        event_type=EventType.WORKER_AUTO_APPROVED,
        title=f"{count} worker proposal(s) auto-approved",
        detail=f"Deterministic PATH-based proposals approved and registered "
               f"in WorkerRegistry. Run `friday capability list` to see them.",
        priority=1,
        category=Category.QUALITY,
        actionable=True,
        action_label="View workers",
        action_command="friday capability list",
    )


def skill_repaired_event(
    name: str, strategy: str, pre_health: str, post_health: str
) -> AmbientEvent:
    """Build an event for an auto-repaired skill."""
    pri = 2 if post_health in ("healthy", "deleted") else 3
    title = f"Skill auto-repaired: {name[:40]}" if post_health != "deleted" else f"Skill removed: {name[:40]}"
    detail = (
        f"'{name}' went from {pre_health} → {post_health} (strategy: {strategy})."
    )
    return AmbientEvent(
        timestamp=_ts(),
        event_type=EventType.SKILL_AUTO_REPAIRED,
        title=title,
        detail=detail,
        priority=pri,
        category=Category.QUALITY,
        actionable=True if post_health != "healthy" else False,
        action_label="Check skills" if post_health != "healthy" else "",
        action_command="friday skills" if post_health != "healthy" else "",
    )


def presence_changed_event(old_state: str, new_state: str) -> AmbientEvent:
    """Build an event for a presence state change."""
    import json
    return AmbientEvent(
        timestamp=_ts(),
        event_type=EventType.PRESENCE_CHANGED,
        title=f"Presence: {old_state} → {new_state}",
        detail=f"User presence changed from {old_state} to {new_state}.",
        source="presence",
        priority=2,
        category=Category.QUALITY,
        payload=json.dumps({"old_state": old_state, "new_state": new_state}),
    )


def focus_on_event(duration_minutes: int) -> AmbientEvent:
    """Build an event when focus mode is enabled."""
    return AmbientEvent(
        timestamp=_ts(),
        event_type=EventType.PRESENCE_FOCUS_ON,
        title=f"🔇 Focus mode ({duration_minutes}min)",
        detail=f"Focus mode enabled for {duration_minutes} minutes. Only urgent items will interrupt.",
        source="presence",
        priority=2,
        category=Category.SYSTEM,
    )


def focus_off_event() -> AmbientEvent:
    """Build an event when focus mode is disabled."""
    return AmbientEvent(
        timestamp=_ts(),
        event_type=EventType.PRESENCE_FOCUS_OFF,
        title="🔊 Focus mode disabled",
        detail="Normal interrupt behavior restored.",
        source="presence",
        priority=1,
        category=Category.SYSTEM,
    )


def briefing_event(summary: str = "") -> AmbientEvent:
    """Build an event indicating a new briefing is available after a cycle.

    The ``summary`` is the one-line summary from ``format_briefing_summary()``,
    shown in the feed as ``detail`` so the operator can see the key numbers
    without running ``friday briefing``.
    """
    return AmbientEvent(
        timestamp=_ts(),
        event_type=EventType.BRIEFING_AVAILABLE,
        title="Morning briefing available — `friday briefing`",
        detail=summary or "View the full briefing: `friday briefing`",
        priority=1,
        category=Category.SYSTEM,
        actionable=True,
        action_label="View briefing",
        action_command="friday briefing",
    )


# ---------------------------------------------------------------------------
# Observation bridge — converts observation engine results to feed events
# ---------------------------------------------------------------------------


def push_observations_to_feed(conn, engine_run) -> int:
    """Push observations from an ObservationEngine run to the ambient feed.

    Iterates all observers in the run, converts their observations to
    ``AmbientEvent`` instances via ``AmbientEvent.from_observation()``, and pushes
    them to the feed. Drops observations that are redundant or too noisy
    for the feed (e.g. file-level observations from WorkspaceObserver,
    which would flood the feed on every cycle).

    Args:
        conn: DB connection.
        engine_run: An ``ObservationRun`` from ``ObservationEngine.run()``.

    Returns:
        The number of events pushed to the feed.
    """
    pushed = 0

    for obs_result in engine_run.observers:
        if not obs_result.health.healthy:
            continue

        for obs in obs_result.observations:
            # Filter noise: skip file-level observations (too many per cycle).
            if obs.aspect and obs.aspect.startswith("file:"):
                continue
            # Skip lang: observations — they're per-file and duplicate info
            # already captured by language_count: observations.
            if obs.aspect and obs.aspect.startswith("lang:"):
                continue
            # Skip trivial count observations that change every cycle.
            if obs.aspect in ("file_count", "config_file_count",
                              "repository_count"):
                continue

            # Skip observations with zero/empty values (nothing to report).
            if not obs.value or obs.value.strip() in ("", "0", "[]"):
                continue

            try:
                event = AmbientEvent.from_observation(obs)
                push_event(conn, event, dedup_hours=6)
                pushed += 1
            except Exception:
                continue

    return pushed


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _row_to_event(row) -> AmbientEvent:
    return AmbientEvent(
        id=row["id"],
        timestamp=row["timestamp"],
        event_type=row["event_type"],
        title=row["title"],
        detail=row["detail"] or "",
        source=row["source"] or "daemon",
        project=row["project"] or "",
        payload=row["payload"] or "",
        confidence=float(row["confidence"] or 1.0),
        priority=row["priority"] or 0,
        category=row["category"] or "system",
        dismissed=bool(row["dismissed"]),
        actionable=bool(row["actionable"]),
        action_label=row["action_label"] or "",
        action_command=row["action_command"] or "",
        mission_id=row["mission_id"] or "",
        graph_id=row["graph_id"] or "",
    )
