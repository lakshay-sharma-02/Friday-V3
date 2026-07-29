"""Operator Profile engine — assemble profile and expose integration helpers."""

from __future__ import annotations

import json
from typing import Optional

from ..db import connect, get_all_operator_preferences
from .derivation import (
    compute_active_repos,
    compute_capability_approval_rate,
    compute_graph_review_pattern,
    compute_initiative_review_pattern,
    compute_preferred_initiative_types,
    compute_repair_approval_rate,
    compute_watch_stats,
    derive_preferences,
)
from .models import OperatorProfile


def build_operator_profile(conn=None) -> OperatorProfile:
    """Assemble an OperatorProfile from persisted evidence + explicit prefs.

    Runs all derivation detectors to ensure derived preferences are current,
    then loads both explicit and derived preferences.

    Returns a fully populated profile (never None), with None fields for
    dimensions that lack evidence.
    """
    own_conn = conn is None
    if own_conn:
        conn = connect()

    try:
        # Ensure derived preferences are up to date.
        derive_preferences(conn)

        # Build profile from evidence readers + explicit preferences.
        cap_rate = compute_capability_approval_rate(conn)
        graph_review = compute_graph_review_pattern(conn)
        init_review = compute_initiative_review_pattern(conn)
        repair_rate = compute_repair_approval_rate(conn)
        active = compute_active_repos(conn)
        watch = compute_watch_stats(conn)
        preferred_types = compute_preferred_initiative_types(conn)

        # Load preferences (both explicit and derived).
        explicit = {
            r.key: r.value
            for r in get_all_operator_preferences(conn, source="explicit")
        }

        return OperatorProfile(
            capability_approval_rate=cap_rate,
            graph_review_pattern=graph_review,
            initiative_review_pattern=init_review,
            repair_approval_rate=repair_rate,
            active_repos=active,
            watch_stats=watch,
            preferred_initiative_types=preferred_types,
            explicit_preferences=explicit,
        )
    finally:
        if own_conn:
            conn.close()


# ---------------------------------------------------------------------------
# Integration helpers
# ---------------------------------------------------------------------------


def get_active_repos(conn=None, limit: int = 5) -> list[str]:
    """Return names of the most actively used repos.

    Used by the daemon to prioritize which repos to observe more frequently.
    Returns repo names sorted by activity.
    """
    own_conn = conn is None
    if own_conn:
        conn = connect()
    try:
        repos = compute_active_repos(conn, limit=limit)
        if repos is None:
            return []
        return [r["name"] for r in repos if r.get("name")]
    finally:
        if own_conn:
            conn.close()


def get_preferred_worker_types(conn=None) -> list[str]:
    """Return preferred worker types from explicit profile preferences.

    Used by the resolver to weight worker selection.
    Returns empty list if no preference is set.
    """
    own_conn = conn is None
    if own_conn:
        conn = connect()
    try:
        prefs = get_all_operator_preferences(conn, source="explicit")
        for p in prefs:
            if p.key == "preferred_worker_types":
                try:
                    return json.loads(p.value)
                except (json.JSONDecodeError, TypeError):
                    return [p.value]
        return []
    finally:
        if own_conn:
            conn.close()


def get_preferred_capabilities(conn=None) -> list[str]:
    """Return preferred capabilities from explicit profile preferences."""
    own_conn = conn is None
    if own_conn:
        conn = connect()
    try:
        prefs = get_all_operator_preferences(conn, source="explicit")
        for p in prefs:
            if p.key == "preferred_capabilities":
                try:
                    return json.loads(p.value)
                except (json.JSONDecodeError, TypeError):
                    return [p.value]
        return []
    finally:
        if own_conn:
            conn.close()


def should_notify(conn=None) -> bool:
    """Check if the operator has opted into desktop notifications.

    Checks BOTH explicit and derived preferences so that LLM-learned
    preferences from conversation (Phase B) are respected.

    Default: True (opt-out via 'friday profile set no_notifications true'
    or saying "I don't want notifications" on Telegram/Slack).
    """
    own_conn = conn is None
    if own_conn:
        conn = connect()
    try:
        # Check both explicit and derived — the conversation learner stores
        # learned preferences as 'derived' source.
        for source in ("explicit", "derived"):
            prefs = get_all_operator_preferences(conn, source=source)
            for p in prefs:
                if p.key == "no_notifications" and p.value.lower() in ("true", "1", "yes"):
                    return False
        return True
    finally:
        if own_conn:
            conn.close()


def get_preferred_channel(conn=None) -> str | None:
    """Return the operator's preferred notification channel from preferences.

    Checks both explicit and derived preferences so that LLM-learned
    preferences from conversation (Phase B) are respected.

    Returns:
        Channel name ("telegram", "slack", "email", "desktop") or None
        if no preference is set.
    """
    own_conn = conn is None
    if own_conn:
        conn = connect()
    try:
        # Check explicit first (takes priority), then derived.
        for source in ("explicit", "derived"):
            prefs = get_all_operator_preferences(conn, source=source)
            for p in prefs:
                if p.key == "preferred_channel" and p.value.strip():
                    return p.value.strip().lower()
        return None
    finally:
        if own_conn:
            conn.close()
