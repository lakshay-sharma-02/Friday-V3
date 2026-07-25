"""Operator Profile — evidence derivation engine.

Reads behavioral evidence from existing DB tables and computes derived
preferences. Written to operator_preferences with source='derived'.

Follows the same discipline as the Knowledge/Understanding detectors:
deterministic, evidence-gated, no LLM.
"""

from __future__ import annotations

from typing import Optional

from ..db import (
    connect,
    get_all_operator_preferences,
    get_proposed_workers,
    now_iso,
    set_operator_preference,
)


# ---------------------------------------------------------------------------
# Evidence readers
# ---------------------------------------------------------------------------


def compute_capability_approval_rate(conn) -> Optional[dict[str, int]]:
    """Derive capability approval rate from proposed_workers table."""
    all_proposals = get_proposed_workers(conn)
    if not all_proposals:
        return None
    approved = sum(1 for p in all_proposals if p.status == "approved")
    rejected = sum(1 for p in all_proposals if p.status == "rejected")
    pending = sum(1 for p in all_proposals if p.status == "pending")
    total = approved + rejected + pending
    rate = round(approved / total, 2) if total > 0 else 0.0
    return {
        "approved": approved,
        "rejected": rejected,
        "pending": pending,
        "total": total,
        "rate": rate,
    }


def compute_graph_review_pattern(conn) -> Optional[dict[str, int]]:
    """Derive graph review pattern from task_graphs review status."""
    rows = conn.execute(
        "SELECT status, COUNT(*) AS c FROM task_graphs "
        "WHERE status IN ('proposal', 'approved', 'rejected') "
        "GROUP BY status"
    ).fetchall()
    if not rows:
        return None
    return {r["status"]: r["c"] for r in rows}


def compute_initiative_review_pattern(conn) -> Optional[dict[str, int]]:
    """Derive initiative review pattern from pending_initiatives."""
    rows = conn.execute(
        "SELECT reviewed, dismissed_at IS NOT NULL AS dismissed, "
        "       action_taken IS NOT NULL AND action_taken != '' AS actioned, "
        "       COUNT(*) AS c "
        "FROM pending_initiatives GROUP BY reviewed, dismissed, actioned"
    ).fetchall()
    if not rows:
        return None
    reviewed = sum(r["c"] for r in rows if r["reviewed"])
    dismissed = sum(r["c"] for r in rows if r["dismissed"])
    actioned = sum(r["c"] for r in rows if r["actioned"])
    total = sum(r["c"] for r in rows)
    return {
        "total": total,
        "reviewed": reviewed,
        "dismissed": dismissed,
        "actioned": actioned,
        "pending": total - reviewed,
    }


def compute_repair_approval_rate(conn) -> Optional[dict[str, int]]:
    """Derive repair approval rate from repair_proposals."""
    rows = conn.execute(
        "SELECT status, COUNT(*) AS c FROM repair_proposals GROUP BY status"
    ).fetchall()
    if not rows:
        return None
    result = {}
    for r in rows:
        result[r["status"]] = r["c"]
    approved = result.get("approved", 0)
    rejected = result.get("rejected", 0)
    total = approved + rejected
    rate = round(approved / total, 2) if total > 0 else 0.0
    return {
        "approved": approved,
        "rejected": rejected,
        "pending": result.get("pending", 0),
        "total": total + result.get("pending", 0),
        "rate": rate,
    }


def compute_active_repos(conn, limit: int = 5) -> Optional[list[dict]]:
    """Find the most actively used repos based on git activity and sessions.

    Returns the top N repos sorted by a composite activity score.
    None when no repos are ingested.
    """
    repos = conn.execute(
        "SELECT id, name, path, commit_count, last_commit_date "
        "FROM repositories ORDER BY COALESCE(commit_count, 0) DESC LIMIT ?",
        (limit,),
    ).fetchall()
    if not repos:
        return None

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    result: list[dict] = []
    for r in repos:
        # Compute days since last commit (if available).
        days_since = None
        if r["last_commit_date"]:
            try:
                last = datetime.fromisoformat(r["last_commit_date"])
                days_since = (now - last).days
            except (ValueError, TypeError):
                pass
        result.append({
            "id": r["id"],
            "name": r["name"],
            "path": r["path"],
            "commit_count": r["commit_count"] or 0,
            "days_since_last_commit": days_since,
        })
    return result


def compute_watch_stats(conn) -> Optional[dict[str, int | float]]:
    """Compute watch cycle statistics from watch_history."""
    row = conn.execute(
        "SELECT COUNT(*) AS total, "
        "       SUM(CASE WHEN outcome = 'succeeded' THEN 1 ELSE 0 END) AS succeeded, "
        "       SUM(CASE WHEN outcome = 'failed' THEN 1 ELSE 0 END) AS failed, "
        "       SUM(CASE WHEN outcome = 'skipped' THEN 1 ELSE 0 END) AS skipped "
        "FROM watch_history"
    ).fetchone()
    if row is None or row["total"] == 0:
        return None
    total = row["total"]
    succeeded = row["succeeded"] or 0
    failed = row["failed"] or 0
    skipped = row["skipped"] or 0
    success_rate = round(succeeded / total, 2) if total > 0 else 0.0
    return {
        "total": total,
        "succeeded": succeeded,
        "failed": failed,
        "skipped": skipped,
        "success_rate": success_rate,
    }


def compute_preferred_initiative_types(conn) -> Optional[list[str]]:
    """Derive preferred initiative types from initiative review patterns.

    Returns initiative types where the user has taken action (approved)
    more than they've dismissed, sorted by preference.
    """
    rows = conn.execute(
        "SELECT i.initiative_type, COUNT(*) AS total, "
        "  SUM(CASE WHEN pi.reviewed AND pi.action_taken IS NOT NULL "
        "           AND pi.action_taken != '' THEN 1 ELSE 0 END) AS actioned "
        "FROM initiatives i "
        "JOIN pending_initiatives pi ON i.id = pi.id "
        "WHERE pi.reviewed = 1 "
        "GROUP BY i.initiative_type "
        "ORDER BY actioned DESC"
    ).fetchall()
    if not rows:
        return None
    preferred = [r["initiative_type"] for r in rows if r["actioned"] > 0]
    return preferred if preferred else None


# ---------------------------------------------------------------------------
# Derivation runner
# ---------------------------------------------------------------------------


def derive_preferences(conn) -> int:
    """Run all preference derivation detectors and persist results.

    Writes derived preferences to operator_preferences with source='derived'.
    Skips dimensions where evidence is insufficient (None result).

    Returns count of preferences written.
    """
    detectors = [
        ("capability_approval_rate", compute_capability_approval_rate),
        ("graph_review_pattern", compute_graph_review_pattern),
        ("initiative_review_pattern", compute_initiative_review_pattern),
        ("repair_approval_rate", compute_repair_approval_rate),
        ("watch_stats", compute_watch_stats),
        ("preferred_initiative_types", compute_preferred_initiative_types),
    ]

    count = 0
    for key, detector in detectors:
        try:
            result = detector(conn)
        except Exception:
            continue
        if result is not None:
            import json
            value = json.dumps(result)
            set_operator_preference(conn, key=key, value=value, source="derived")
            count += 1

    return count


def get_operator_preference_history(conn, key=None, limit=50):
    """Get preference change history from profile_history table."""
    if key:
        rows = conn.execute(
            "SELECT * FROM profile_history WHERE key = ? "
            "ORDER BY id DESC LIMIT ?",
            (key, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM profile_history ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]

