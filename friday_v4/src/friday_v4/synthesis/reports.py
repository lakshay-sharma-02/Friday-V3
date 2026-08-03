"""Daily/weekly reports from real V4 state (Wave 11 §3.2).

``build_daily_report`` / ``build_weekly_report`` compose deterministic,
evidence-cited reports from the V4 state DB — missions, actions,
security grade, memory facts, and durable ambient events. The WAVE_11
design's ``reports.py`` (daily/weekly report generation); the same
evidence in → the same report out (deterministic, testable).

Design laws:
- Synthesis is composition of evidence, never invention: every section
  is built from rows actually present in the V4 DB (or the persisted
  security scan state). Empty sections render as "nothing yet".
- Never raises: a missing DB / unreadable state yields a report with
  whatever real evidence exists (or an honest empty report).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from .. import db

logger = logging.getLogger("friday_v4.synthesis.reports")

_DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


# ──────────────────────────────────────────────────────────────────────────
# Section builders (each returns (heading, [evidence lines]) or None)
# ──────────────────────────────────────────────────────────────────────────


def _missions_section(conn) -> Optional[tuple[str, list[str]]]:
    missions = db.list_missions(conn, limit=20) or []
    active = [m for m in missions if m.get("status") not in ("done", "cancelled")]
    if not active:
        return None
    lines: list[str] = []
    for m in active[:5]:
        title = (m.get("title") or "?").strip()
        status = m.get("status") or "planned"
        steps = db.list_mission_steps(conn, m.get("id", "")) or []
        done = sum(1 for s in steps if s.get("status") == "completed")
        lines.append(f"{title} — {status} ({done}/{len(steps)} steps)")
    return ("missions", lines)


def _actions_section(conn, hours: int) -> Optional[tuple[str, list[str]]]:
    recent = db.recent_actions(conn, limit=100) or []
    recent = [a for a in recent if _within_hours(a, hours)]
    if not recent:
        return None
    ok = sum(1 for a in recent if a.get("status") == "succeeded")
    lines = [f"{len(recent)} action(s) in the last {hours}h, {ok} succeeded"]
    for a in recent[:5]:
        atype = a.get("action_type") or "?"
        what = (a.get("command") or a.get("goal") or "")[:80] or "(no detail)"
        status = a.get("status") or "?"
        lines.append(f"{atype} [{status}]: {what}")
    return ("actions", lines)


def _security_section(conn=None) -> Optional[tuple[str, list[str]]]:
    """Last scan state from the daemon's persisted status file (read-only).

    ``conn`` accepted for a uniform builder signature (ignored — the
    state file is the daemon's own, not a DB table).
    """
    try:
        import json
        from pathlib import Path
        state_file = Path.home() / ".friday" / "v4_security_last.json"
        if not state_file.exists():
            return None
        report = (json.loads(state_file.read_text()) or {}).get("report") or {}
        grade = report.get("grade") or "unknown"
        counts = report.get("counts_by_severity") or {}
        scanned = report.get("scanned_at") or ""
        lines = [f"grade {grade} — "
                 f"{int(counts.get('critical', 0) or 0)} critical, "
                 f"{int(counts.get('high', 0) or 0)} high, "
                 f"{int(counts.get('medium', 0) or 0)} medium"]
        if scanned:
            lines.append(f"last scanned {str(scanned)[:16]}")
        return ("security", lines)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug(f"security section failed: {exc}")
        return None


def _memory_section(conn) -> Optional[tuple[str, list[str]]]:
    memories = db.list_memories(conn, limit=10) or []
    if not memories:
        return None
    lines = [f"{m.get('mem_key')}: {str(m.get('value'))[:60]}"
             for m in memories[:5]]
    return ("memory", lines)


def _ambient_section(conn) -> Optional[tuple[str, list[str]]]:
    events = db.recent_ambient_events(conn, limit=10) or []
    if not events:
        return None
    lines = [f"{e.get('topic')}: {str(e.get('payload'))[:70]}"
             for e in events[:5]]
    return ("ambient events", lines)


def _by_day_section(conn, days: int) -> Optional[tuple[str, list[str]]]:
    """Action counts per day for weekly reports — evidence, not invention."""
    from datetime import datetime, timedelta, timezone
    recent = db.recent_actions(conn, limit=1000) or []
    counts: dict[str, int] = {}
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    for a in recent:
        when = a.get("created_at") or ""
        try:
            t = datetime.fromisoformat(when)
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if t < cutoff:
            continue
        day = _DAY_NAMES[t.weekday()]
        counts[day] = counts.get(day, 0) + 1
    if not counts:
        return None
    ordered = [(d, counts[d]) for d in _DAY_NAMES if d in counts]
    lines = [f"{d}: {n} action(s)" for d, n in ordered]
    return ("by day", lines)


# ──────────────────────────────────────────────────────────────────────────
# Report builders
# ──────────────────────────────────────────────────────────────────────────


def _within_hours(action: dict, hours: int) -> bool:
    when = action.get("created_at") or ""
    if not when:
        return False
    try:
        from datetime import datetime, timezone
        t = datetime.fromisoformat(when)
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        delta = (datetime.now(timezone.utc) - t).total_seconds()
        return delta <= hours * 3600
    except ValueError:
        return False


def build_daily_report(conn, kind: str = "daily", now: str = "") -> dict:
    """Compose today's evidence-cited report from real V4 state.

    Returns a serializable dict (title / generated_at / sections) so the
    CLI, briefing surfaces, and the web dashboard share one shape.
    Never raises — an empty DB yields an honest empty report.

    ``now`` pins the ``generated_at`` stamp (tests / deterministic
    callers); defaults to wall-clock ``db.now_iso()``.
    """
    from .synthesis import synthesize

    sections: dict[str, list[str]] = {}
    for builder in (_missions_section,
                    lambda c: _actions_section(c, 24),
                    _security_section, _memory_section, _ambient_section):
        try:
            result = builder(conn)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug(f"daily section failed: {exc}")
            continue
        if result:
            heading, lines = result
            sections[heading] = lines

    report = synthesize("Friday daily report", sections, generated_at=now)
    return report.to_dict()


def build_weekly_report(conn, days: int = 7, now: str = "") -> dict:
    """Compose this week's evidence-cited report (7-day window).

    Adds the per-day action histogram on top of the daily evidence set.
    Deterministic: same DB state → same report (``now`` pins the
    ``generated_at`` stamp; defaults to wall-clock ``db.now_iso()``).
    """
    from .synthesis import synthesize

    sections: dict[str, list[str]] = {}
    for builder in (_missions_section,
                    lambda c: _actions_section(c, days * 24),
                    lambda c: _by_day_section(c, days),
                    _security_section, _memory_section, _ambient_section):
        try:
            result = builder(conn)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug(f"weekly section failed: {exc}")
            continue
        if result:
            heading, lines = result
            sections[heading] = lines

    report = synthesize("Friday weekly report", sections, generated_at=now)
    return report.to_dict()


__all__ = ["build_daily_report", "build_weekly_report"]
