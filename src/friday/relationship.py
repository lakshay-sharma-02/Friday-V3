"""Relationship Metrics — long-term relationship graph computation.

Computes and stores relationship metrics that track how the operator and
Friday interact over time. Metrics are computed via daemon post-cycle hooks
and stored in the ``relationship_metrics`` table.

Metrics computed:
- interaction_frequency: exchanges per day/week
- top_topics: most discussed subjects (extracted from routing)
- preferred_interaction_times: time-of-day clusters
- preferred_answer_length: average response length (verbose vs terse)
- ignored_topics: topics the operator consistently dismisses (not yet implemented)
- acted_on_topics: topics the operator acts on (not yet implemented)
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from typing import Optional

from .db import now_iso


# ---------------------------------------------------------------------------
# Table setup (created via migration 027)
# ---------------------------------------------------------------------------

_REL_METRICS_TABLE = """
CREATE TABLE IF NOT EXISTS relationship_metrics (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    metric_key      TEXT NOT NULL,
    metric_value    TEXT NOT NULL,
    computed_at     TEXT NOT NULL,
    window_days     INTEGER NOT NULL DEFAULT 7
);
CREATE INDEX IF NOT EXISTS idx_rel_metrics_key
    ON relationship_metrics(metric_key);
CREATE INDEX IF NOT EXISTS idx_rel_metrics_computed
    ON relationship_metrics(computed_at DESC);
"""


def _ensure_table(conn) -> None:
    """Create relationship_metrics table if it doesn't exist."""
    try:
        conn.executescript(_REL_METRICS_TABLE)
        conn.commit()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Metric storage helpers
# ---------------------------------------------------------------------------


def _store_metric(conn, key: str, value: str, window_days: int = 7) -> None:
    """Store a single relationship metric (upsert on key)."""
    try:
        existing = conn.execute(
            "SELECT id FROM relationship_metrics WHERE metric_key = ?",
            (key,),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE relationship_metrics SET metric_value = ?, "
                "computed_at = ?, window_days = ? WHERE id = ?",
                (value, now_iso(), window_days, existing["id"]),
            )
        else:
            conn.execute(
                "INSERT INTO relationship_metrics "
                "(metric_key, metric_value, computed_at, window_days) "
                "VALUES (?, ?, ?, ?)",
                (key, value, now_iso(), window_days),
            )
        conn.commit()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Compute functions
# ---------------------------------------------------------------------------


def compute_interaction_frequency(conn, window_days: int = 7) -> dict:
    """Compute interaction frequency (exchanges per day and week).

    Args:
        conn: DB connection.
        window_days: How many days back to look.

    Returns:
        Dict with ``exchanges_per_day``, ``exchanges_per_week`` (extrapolated),
        and ``total_exchanges``.
    """
    _ensure_table(conn)
    try:
        from datetime import datetime, timedelta, timezone
        since = datetime.now(timezone.utc) - timedelta(days=window_days)
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM conversation_log WHERE conversation_at >= ?",
            (since.isoformat(),),
        ).fetchone()
        total = row["cnt"] if row else 0
        per_day = round(total / max(window_days, 1), 1)
        per_week = round(per_day * 7, 1)

        result = {
            "total_exchanges": total,
            "exchanges_per_day": per_day,
            "exchanges_per_week": per_week,
            "window_days": window_days,
        }

        _store_metric(conn, "interaction_frequency", json.dumps(result), window_days)
        return result
    except Exception:
        return {"total_exchanges": 0, "exchanges_per_day": 0, "exchanges_per_week": 0, "window_days": window_days}


def compute_top_topics(conn, window_days: int = 7, limit: int = 10) -> list[dict]:
    """Extract most discussed topics from conversation routing and content.

    Args:
        conn: DB connection.
        window_days: How many days back to look.
        limit: Maximum topics to return.

    Returns:
        List of dicts with ``topic`` and ``count``, sorted descending.
    """
    _ensure_table(conn)
    try:
        from datetime import datetime, timedelta, timezone
        since = datetime.now(timezone.utc) - timedelta(days=window_days)
        rows = conn.execute(
            "SELECT routing, user_message FROM conversation_log "
            "WHERE conversation_at >= ?",
            (since.isoformat(),),
        ).fetchall()
        if not rows:
            return []

        # Count routing types.
        routing_counts: Counter = Counter()
        for r in rows:
            routing = r["routing"] or "unknown"
            routing_counts[routing] += 1

        # Extract nouns from user messages as rough topic indicators.
        word_counts: Counter = Counter()
        stop_words = {
            "the", "a", "an", "this", "that", "these", "those", "i", "you",
            "it", "we", "they", "he", "she", "is", "are", "was", "were",
            "be", "been", "have", "has", "had", "do", "does", "did",
            "can", "could", "will", "would", "shall", "should", "may",
            "might", "must", "to", "of", "in", "on", "at", "for",
            "with", "by", "about", "from", "as", "into", "through",
            "during", "before", "after", "above", "below", "between",
            "and", "or", "but", "not", "so", "if", "because", "then",
            "than", "too", "very", "just", "like", "what", "which",
            "who", "whom", "where", "when", "why", "how", "all",
            "each", "every", "both", "few", "more", "most", "some",
            "any", "no", "only", "own", "same", "here", "there",
            "please", "help", "tell", "show", "make", "get", "let",
            "know", "think", "want", "need", "see", "try", "going",
            "thanks", "thank", "hi", "hello", "hey", "yes", "no",
            "ok", "okay", "sure", "fine", "right", "well", "actually",
            "really", "also", "even", "still", "already", "yet",
        }

        for r in rows:
            msg = r["user_message"] or ""
            for word in msg.lower().split():
                word = word.strip(".,!?;:'\"()[]{}")
                if len(word) > 3 and word not in stop_words and word.isalpha():
                    word_counts[word] += 1

        topics = []
        # Top routing topics.
        for routing, count in routing_counts.most_common(5):
            topics.append({"topic": f"routing:{routing}", "count": count})

        # Top word topics (filter out obvious conversation starters).
        for word, count in word_counts.most_common(limit):
            if count >= 2:  # Need at least 2 mentions.
                topics.append({"topic": word, "count": count})

        topics = topics[:limit] if len(topics) > limit else topics

        _store_metric(conn, "top_topics", json.dumps(topics), window_days)
        return topics
    except Exception:
        return []


def compute_preferred_times(conn, window_days: int = 7) -> dict:
    """Compute preferred interaction times (hour-of-day clusters).

    Args:
        conn: DB connection.
        window_days: How many days back to look.

    Returns:
        Dict with ``peak_hour``, ``hours_distribution``, and ``period_summary``.
    """
    _ensure_table(conn)
    try:
        from datetime import datetime, timedelta, timezone
        since = datetime.now(timezone.utc) - timedelta(days=window_days)
        rows = conn.execute(
            "SELECT conversation_at FROM conversation_log "
            "WHERE conversation_at >= ?",
            (since.isoformat(),),
        ).fetchall()
        if not rows:
            return {"peak_hour": -1, "hours_distribution": {}, "period_summary": "insufficient_data"}

        hour_counts: Counter = Counter()
        for r in rows:
            ts = r["conversation_at"]
            try:
                hour = datetime.fromisoformat(ts).hour
                hour_counts[hour] += 1
            except Exception:
                pass

        if not hour_counts:
            return {"peak_hour": -1, "hours_distribution": {}, "period_summary": "insufficient_data"}

        # Period summaries.
        morning = sum(hour_counts.get(h, 0) for h in range(6, 12))
        afternoon = sum(hour_counts.get(h, 0) for h in range(12, 18))
        evening = sum(hour_counts.get(h, 0) for h in range(18, 23))
        night = sum(hour_counts.get(h, 0) for h in range(0, 6))

        total = morning + afternoon + evening + night
        period_summary = {}
        if total > 0:
            period_summary = {
                "morning (6-12)": round(morning / total * 100, 1),
                "afternoon (12-18)": round(afternoon / total * 100, 1),
                "evening (18-23)": round(evening / total * 100, 1),
                "night (0-6)": round(night / total * 100, 1),
            }

        peak_hour = hour_counts.most_common(1)[0][0]
        hours_dist = {str(h): hour_counts[h] for h in sorted(hour_counts.keys())}

        result = {
            "peak_hour": peak_hour,
            "hours_distribution": hours_dist,
            "period_summary": period_summary,
        }

        _store_metric(conn, "preferred_times", json.dumps(result), window_days)
        return result
    except Exception:
        return {"peak_hour": -1, "hours_distribution": {}, "period_summary": "insufficient_data"}


def compute_average_response_length(conn, window_days: int = 7) -> dict:
    """Compute average Friday response length (word count).

    Args:
        conn: DB connection.
        window_days: How many days back to look.

    Returns:
        Dict with ``avg_words``, ``min_words``, ``max_words``, and ``total_responses``.
    """
    _ensure_table(conn)
    try:
        from datetime import datetime, timedelta, timezone
        since = datetime.now(timezone.utc) - timedelta(days=window_days)
        rows = conn.execute(
            "SELECT friday_reply FROM conversation_log "
            "WHERE conversation_at >= ? AND friday_reply != ''",
            (since.isoformat(),),
        ).fetchall()
        if not rows:
            return {"avg_words": 0, "count": 0}

        word_counts_list = [
            len(r["friday_reply"].split()) for r in rows
        ]
        total_responses = len(word_counts_list)
        avg_words = round(sum(word_counts_list) / total_responses, 1) if total_responses > 0 else 0
        min_words = min(word_counts_list) if word_counts_list else 0
        max_words = max(word_counts_list) if word_counts_list else 0

        result = {
            "avg_words": avg_words,
            "min_words": min_words,
            "max_words": max_words,
            "total_responses": total_responses,
        }

        _store_metric(conn, "avg_response_length", json.dumps(result), window_days)
        return result
    except Exception:
        return {"avg_words": 0, "count": 0}


# ---------------------------------------------------------------------------
# Full computation entry point (called by daemon)
# ---------------------------------------------------------------------------


def compute_all_metrics(conn, window_days: int = 7) -> dict:
    """Compute all relationship metrics and store them.

    Called by the daemon's post-cycle hook.

    Args:
        conn: DB connection.
        window_days: How many days back to look.

    Returns:
        Dict summarizing what was computed: keys are metric names,
        values are the result dicts.
    """
    _ensure_table(conn)
    return {
        "interaction_frequency": compute_interaction_frequency(conn, window_days),
        "top_topics": {"topics": compute_top_topics(conn, window_days)},
        "preferred_times": compute_preferred_times(conn, window_days),
        "avg_response_length": compute_average_response_length(conn, window_days),
    }
