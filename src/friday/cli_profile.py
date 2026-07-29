"""CLI commands for Operator Identity.

`friday profile show`         — print the full OperatorProfile.
`friday profile set <k> <v>`  — write one explicit preference.
`friday profile unset <k>`    — delete one explicit preference.
`friday profile history`      — show preference change history.
`friday profile derive`       — force re-derive evidence-based preferences.
`friday profile stats`        — show behavioral statistics.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from .db import (
    connect,
    set_operator_preference,
    unset_operator_preference,
)
from .operator import build_operator_profile
from .operator.derivation import derive_preferences, get_operator_preference_history


def _render_profile(profile) -> str:
    """Render the full OperatorProfile as a human-readable string."""
    lines = ["Operator Profile", ""]

    # --- Evidence-derived ---
    lines.append("--- Evidence-derived ---")

    cap = profile.capability_approval_rate
    if cap:
        rate_pct = round(cap["rate"] * 100)
        lines.append(f"  capability_approval_rate:  {cap['approved']}/{cap['total']} "
                     f"approved ({rate_pct}%)")
        if cap["pending"]:
            lines.append(f"                            {cap['pending']} pending")
        if cap["rejected"]:
            lines.append(f"                            {cap['rejected']} rejected")
    else:
        lines.append("  capability_approval_rate:  (no proposals yet)")

    gr = profile.graph_review_pattern
    if gr:
        parts = []
        for status, count in sorted(gr.items()):
            parts.append(f"{count} {status}")
        lines.append(f"  graph_review_pattern:       {', '.join(parts)}")
    else:
        lines.append("  graph_review_pattern:       (no reviewed graphs yet)")

    ir = profile.initiative_review_pattern
    if ir:
        lines.append(f"  initiative_review:          {ir['reviewed']}/{ir['total']} reviewed, "
                     f"{ir['dismissed']} dismissed, {ir['actioned']} actioned")
        if ir["pending"]:
            lines.append(f"                            {ir['pending']} pending")
    else:
        lines.append("  initiative_review:          (no initiatives reviewed yet)")

    rr = profile.repair_approval_rate
    if rr:
        rate_pct = round(rr["rate"] * 100)
        lines.append(f"  repair_approval_rate:       {rr['approved']}/{rr['total']} "
                     f"approved ({rate_pct}%)")
    else:
        lines.append("  repair_approval_rate:       (no repair proposals yet)")

    active = profile.active_repos
    if active:
        lines.append(f"  active_repos:               {len(active)} repo(s)")
        for r in active:
            name = r.get("name", "?")
            commits = r.get("commit_count", 0)
            days = r.get("days_since_last_commit")
            days_str = f", {days}d ago" if days is not None else ""
            lines.append(f"    - {name} ({commits} commits{days_str})")
    else:
        lines.append("  active_repos:               (no repos ingested yet)")

    ws = profile.watch_stats
    if ws:
        rate_pct = round(ws["success_rate"] * 100)
        lines.append(f"  watch_cycles:               {ws['total']} cycles, "
                     f"{ws['succeeded']} ok, {ws['failed']} failed, "
                     f"{ws['skipped']} skipped ({rate_pct}% success)")
    else:
        lines.append("  watch_cycles:               (no watch cycles yet)")

    preferred = profile.preferred_initiative_types
    if preferred:
        lines.append(f"  preferred_initiative_types: {', '.join(preferred)}")

    lines.append("")
    lines.append("--- Explicit preferences ---")
    pref = profile.explicit_preferences
    if pref:
        for key, value in sorted(pref.items()):
            lines.append(f"  {key}: {value}")
    else:
        lines.append("  (none set — use `friday profile set <key> <value>` to add)")

    if not profile.has_profile:
        lines.append("")
        lines.append("Profile is empty. Evidence-derived fields will populate as you")
        lines.append("use Friday (approve/reject proposals, review initiatives, etc.).")
        lines.append('Add explicit preferences with:')
        lines.append('  friday profile set preferred_worker_types \'["worker:python","worker:shell"]\'')

    return "\n".join(lines)


def cmd_profile_show(args: argparse.Namespace) -> int:
    """Print the full OperatorProfile."""
    conn = connect()
    profile = build_operator_profile(conn)
    conn.close()
    print(_render_profile(profile))
    return 0


def cmd_profile_set(args: argparse.Namespace) -> int:
    """Set one explicit operator preference."""
    key = getattr(args, "key", None)
    value = getattr(args, "value", None)
    if not key or not value:
        print("error: both key and value are required: friday profile set <key> <value>",
              file=sys.stderr)
        return 2

    conn = connect()
    set_operator_preference(conn, key=key, value=value, source="explicit")
    conn.close()
    print(f"Set: {key} = {value}")
    return 0


def cmd_profile_unset(args: argparse.Namespace) -> int:
    """Delete one operator preference by key."""
    key = getattr(args, "key", None)
    if not key:
        print("error: key required: friday profile unset <key>",
              file=sys.stderr)
        return 2

    conn = connect()
    removed = unset_operator_preference(conn, key)
    conn.close()
    if removed:
        print(f"Unset: {key}")
    else:
        print(f"No preference found for '{key}'")
    return 0


def cmd_profile_history(args: argparse.Namespace) -> int:
    """Show preference change history."""
    conn = connect()
    rows = get_operator_preference_history(conn)
    conn.close()

    if not rows:
        print("No preference history yet.")
        return 0

    print("Operator Preference History\n")
    for r in rows:
        ts = r["changed_at"][:19] if r["changed_at"] else "?"
        key = r["key"]
        old = r.get("old_value") or "(none)"
        new = r.get("new_value") or "(none)"
        source = r.get("source", "")
        print(f"  [{ts}] {key}")
        print(f"         {old} → {new}  ({source})")
    return 0


def cmd_profile_derive(args: argparse.Namespace) -> int:
    """Force re-derive evidence-based preferences."""
    conn = connect()
    count = derive_preferences(conn)
    conn.close()
    print(f"Derived {count} preference(s) from evidence.")
    return 0


def cmd_profile_stats(args: argparse.Namespace) -> int:
    """Show behavioral statistics."""
    conn = connect()

    # Repo counts.
    repo_count = conn.execute("SELECT COUNT(*) AS c FROM repositories").fetchone()["c"]
    session_count = conn.execute("SELECT COUNT(*) AS c FROM sessions").fetchone()["c"]
    obs_count = conn.execute("SELECT COUNT(*) AS c FROM observations").fetchone()["c"]

    # Proposal counts.
    proposed = conn.execute(
        "SELECT status, COUNT(*) AS c FROM proposed_workers GROUP BY status"
    ).fetchall()
    proposal_summary = ", ".join(f"{r['status']}: {r['c']}" for r in proposed) if proposed else "none"

    # Execution counts.
    exec_count = conn.execute("SELECT COUNT(*) AS c FROM runtime_sessions").fetchone()["c"]
    repair_count = conn.execute("SELECT COUNT(*) AS c FROM repair_proposals").fetchone()["c"]

    # Knowledge counts.
    know_count = conn.execute("SELECT COUNT(*) AS c FROM knowledge").fetchone()["c"]
    init_count = conn.execute("SELECT COUNT(*) AS c FROM initiatives").fetchone()["c"]

    conn.close()

    print("Friday Usage Statistics\n")
    print(f"  Repositories:            {repo_count}")
    print(f"  Observations:            {obs_count}")
    print(f"  Engineering sessions:    {session_count}")
    print(f"  Knowledge entries:       {know_count}")
    print(f"  Initiatives:             {init_count}")
    print(f"  Execution sessions:      {exec_count}")
    print(f"  Repair proposals:        {repair_count}")
    print(f"  Worker proposals:        {proposal_summary}")
    return 0


def cmd_profile_depth(args: argparse.Namespace) -> int:
    """Show Friday's relationship depth with the operator."""
    conn = connect()
    try:
        from .operator.depth import compute_relationship_depth
        depth = compute_relationship_depth(conn)
        print(f"Relationship Depth: Level {depth.level} — {depth.label}")
        print(f"  {depth.description}")
        print(f"  Total conversations: {depth.total_conversations}")
        print(f"  Preferences known:   {depth.preferences_known}")
        print(f"  Name known:          {'Yes' if depth.name_known else 'No'}")
        print(f"  Positivity ratio:    {depth.positive_sentiment_ratio:.1%}")
    finally:
        conn.close()
    return 0


def cmd_profile_relationship(args: argparse.Namespace) -> int:
    """Show the long-term relationship graph summary."""
    conn = connect()
    try:
        # Read relationship metrics.
        rows = conn.execute(
            "SELECT metric_key, metric_value, computed_at, window_days "
            "FROM relationship_metrics ORDER BY computed_at DESC LIMIT 20"
        ).fetchall()

        from .operator.depth import compute_relationship_depth
        depth = compute_relationship_depth(conn)

        print(f"Relationship with Friday — Level {depth.level} ({depth.label})")
        print()

        if rows:
            print("Relationship Metrics:")
            for r in rows:
                key = r["metric_key"]
                val = r["metric_value"]
                days = r["window_days"]
                print(f"  {key}: {val} (window: {days}d)")
        else:
            print("No relationship metrics yet. These are computed during daemon cycles.")

        print()
        print("Conversation Overview:")
        conv_count = conn.execute(
            "SELECT COUNT(*) AS cnt FROM conversation_log"
        ).fetchone()["cnt"]
        print(f"  Total exchanges: {conv_count}")

        # Channel breakdown.
        channels = conn.execute(
            "SELECT channel, COUNT(*) AS cnt FROM conversation_log "
            "GROUP BY channel ORDER BY cnt DESC"
        ).fetchall()
        if channels:
            print("  By channel:")
            for c in channels:
                print(f"    {c['channel']}: {c['cnt']}")

        # Sentiment breakdown.
        try:
            sent_rows = conn.execute(
                "SELECT tone, COUNT(*) AS cnt FROM sentiment_observations "
                "GROUP BY tone ORDER BY cnt DESC"
            ).fetchall()
            if sent_rows:
                print("  Sentiment breakdown:")
                for s in sent_rows:
                    print(f"    {s['tone']}: {s['cnt']}")
        except Exception:
            pass

    finally:
        conn.close()
    return 0


def cmd_profile_sentiment(args: argparse.Namespace) -> int:
    """Show sentiment analysis summary for recent interactions."""
    conn = connect()
    try:
        from .sentiment import compute_trend_summary
        trend = compute_trend_summary(conn, lookback_hours=48)

        print("Sentiment Summary (last 48 hours)")
        print()
        print(f"  Total observations:  {trend['total_observations']}")
        print(f"  Most common tone:    {trend['most_common_tone']}")
        print(f"  Rolling tone:        {trend['rolling_tone']}")
        print(f"  Trend:               {trend['trend']}")

        if trend.get("tone_breakdown"):
            print()
            print("  Breakdown:")
            for tone, info in trend["tone_breakdown"].items():
                bar_len = max(1, int(info["percentage"] / 5))
                bar = "█" * bar_len
                print(f"    {tone:15s} {info['count']:4d} ({info['percentage']:5.1f}%) {bar}")

    finally:
        conn.close()
    return 0


def cmd_profile(args: argparse.Namespace) -> int:
    """Dispatch friday profile subcommands."""
    action = getattr(args, "action", "show")

    if action == "show":
        return cmd_profile_show(args)
    elif action == "set":
        return cmd_profile_set(args)
    elif action == "unset":
        return cmd_profile_unset(args)
    elif action == "history":
        return cmd_profile_history(args)
    elif action == "derive":
        return cmd_profile_derive(args)
    elif action == "stats":
        return cmd_profile_stats(args)
    elif action == "depth":
        return cmd_profile_depth(args)
    elif action == "relationship":
        return cmd_profile_relationship(args)
    elif action == "sentiment":
        return cmd_profile_sentiment(args)
    else:
        print(f"error: unknown action: {action}", file=sys.stderr)
        print("usage: friday profile <show|set|unset|history|derive|stats|depth|relationship|sentiment>",
              file=sys.stderr)
        return 2
