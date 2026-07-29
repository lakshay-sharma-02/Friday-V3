"""CLI for Standup Reports — ``friday standup`` and ``friday yesterday``.

Usage::

    friday standup            # 5-7 line standup summary (last 24h)
    friday yesterday          # same content, "yesterday" label
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .presentation.cli_format import header, gray


def cmd_standup(args: argparse.Namespace) -> int:
    """Produce a 5-7 line standup summary formatted for daily standups.

    No LLM needed — purely template-driven from DB state.
    Covers: what I worked on, blockers, next steps.
    """
    from .db import connect

    conn = connect()
    try:
        print(build_standup_report(conn))
    finally:
        conn.close()
    return 0


def cmd_yesterday(args: argparse.Namespace) -> int:
    """Show what happened yesterday — commits, events, blockers.

    Same data as standup but formatted as a "yesterday" summary.
    """
    from .db import connect

    conn = connect()
    try:
        print(build_yesterday_summary(conn))
    finally:
        conn.close()
    return 0


def build_standup_report(conn) -> str:
    """Build a 5-7 line standup report from the last 24h of data.

    Sections:
      1. What I worked on — projects, commits, sessions
      2. Blockers — failed builds, CI failures, errors
      3. Next — active branches, open PRs, pending reviews
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=24)
    cutoff_str = cutoff.isoformat()
    today_str = now.strftime("%A, %B %d")

    lines: list[str] = []
    lines.append(header("Standup Report", today_str))
    lines.append("")

    # ── What I worked on ─────────────────────────────────────────────────
    try:
        from .db import get_repositories
        repos = get_repositories(conn)
        active_repos: list[str] = []
        total_commits = 0
        for repo in repos:
            rname = repo.name if hasattr(repo, "name") else repo.get("name", "")
            rpath = repo.path if hasattr(repo, "path") else repo.get("path", "")
            if not rpath:
                continue
            import subprocess
            from pathlib import Path
            if not Path(rpath).exists():
                continue
            try:
                out = subprocess.run(
                    ["git", "-C", rpath, "log", f"--after={cutoff_str}",
                     "--oneline", "--format=%s"],
                    capture_output=True, text=True, timeout=10,
                )
                if out.stdout.strip():
                    commit_lines = [l.strip() for l in out.stdout.splitlines() if l.strip()]
                    total_commits += len(commit_lines)
                    active_repos.append(rname)
            except Exception:
                continue

        if active_repos:
            repo_str = ", ".join(active_repos[:5])
            if len(active_repos) > 5:
                repo_str += f" and {len(active_repos) - 5} more"
            lines.append(f"  What I worked on:")
            lines.append(f"    {total_commits} commits across {len(active_repos)} repo(s): {repo_str}")
        else:
            lines.append(f"  What I worked on:")
            lines.append(f"    No commits detected in the last 24h")
    except Exception:
        lines.append(f"  What I worked on:")
        lines.append(f"    (unable to scan repositories)")

    # ── Blockers ─────────────────────────────────────────────────────────
    blockers: list[str] = []

    try:
        ci_rows = conn.execute(
            "SELECT COUNT(*) AS cnt FROM ambient_feed "
            "WHERE event_type LIKE 'review:ci_failure%' AND timestamp >= ? "
            "AND dismissed = 0", (cutoff_str,)
        ).fetchone()
        ci_count = ci_rows["cnt"] if ci_rows else 0
        if ci_count:
            blockers.append(f"{ci_count} CI failure(s) unresolved")
    except Exception:
        pass

    try:
        err_rows = conn.execute(
            "SELECT COUNT(*) AS cnt FROM ambient_feed "
            "WHERE event_type = 'cycle_error' AND timestamp >= ? "
            "AND dismissed = 0", (cutoff_str,)
        ).fetchone()
        err_count = err_rows["cnt"] if err_rows else 0
        if err_count:
            blockers.append(f"{err_count} daemon error(s)")
    except Exception:
        pass

    try:
        review_rows = conn.execute(
            "SELECT COUNT(*) AS cnt FROM pending_initiatives "
            "WHERE initiative_type LIKE 'spontaneous_review:%' "
            "AND reviewed = 0 AND dismissed_at IS NULL"
        ).fetchone()
        review_count = review_rows["cnt"] if review_rows else 0
        if review_count:
            blockers.append(f"{review_count} pending review(s)")
    except Exception:
        pass

    lines.append("")
    if blockers:
        lines.append(f"  Blockers:")
        for b in blockers:
            lines.append(f"    ⚠ {b}")
    else:
        lines.append(f"  Blockers: None")

    # ── Next ─────────────────────────────────────────────────────────────
    next_items: list[str] = []
    try:
        branch_rows = conn.execute(
            "SELECT DISTINCT branch FROM sessions WHERE built_at >= ? AND branch IS NOT NULL AND branch != ''",
            (cutoff_str,),
        ).fetchall()
        branches = list(set(r["branch"] for r in branch_rows if r["branch"]))
        if branches:
            next_items.append(f"Active branches: {', '.join(branches[:3])}")
    except Exception:
        pass

    try:
        from .observation.github_observer import _load_cache
        cache = _load_cache()
        open_pr_count = 0
        for snap in cache.get("snapshots", []):
            for pr in snap.get("pull_requests", []):
                state = (pr.get("state") or "").lower()
                if state not in ("merged", "closed"):
                    open_pr_count += 1
        if open_pr_count:
            next_items.append(f"{open_pr_count} open PR(s) pending review")
    except Exception:
        pass

    lines.append("")
    if next_items:
        lines.append(f"  Next:")
        for n in next_items:
            lines.append(f"    → {n}")
    else:
        lines.append(f"  Next: No pending items")

    # ── Working on ───────────────────────────────────────────────────────
    try:
        from .memory import WorkingMemory
        wm = WorkingMemory(conn)
        ctx = wm.get_contexts_by_category("workspace", limit=5)
        if ctx:
            active_app = None
            for c in ctx:
                if c["context_key"] == "active_app":
                    active_app = c["value"]
                    break
            if active_app:
                lines.append("")
                lines.append(f"  Currently: {active_app}")
    except Exception:
        pass

    lines.append("")
    lines.append(f"  Generated {now.strftime('%H:%M UTC')}")

    return "\n".join(lines)


def build_yesterday_summary(conn) -> str:
    """Build a 'yesterday' summary — what happened in the last 24h.

    Similar to standup but more focused on events/activity.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=24)
    cutoff_str = cutoff.isoformat()
    today_str = now.strftime("%B %d, %Y")

    lines: list[str] = []
    lines.append(header("Yesterday", today_str))
    lines.append("")

    # ── Repo activity ──────────────────────────────────────────────────
    try:
        from .db import get_repositories
        repos = get_repositories(conn)
        active = 0
        total_commits = 0
        dirty = 0
        for repo in repos:
            rpath = repo.path if hasattr(repo, "path") else repo.get("path", "")
            if not rpath:
                continue
            import subprocess
            from pathlib import Path
            if not Path(rpath).exists():
                continue
            try:
                out = subprocess.run(
                    ["git", "-C", rpath, "log", f"--after={cutoff_str}",
                     "--oneline"],
                    capture_output=True, text=True, timeout=10,
                )
                if out.stdout.strip():
                    active += 1
                    total_commits += len([l for l in out.stdout.splitlines() if l.strip()])
            except Exception:
                pass
            try:
                dirty_out = subprocess.run(
                    ["git", "-C", rpath, "status", "--porcelain"],
                    capture_output=True, text=True, timeout=5,
                )
                if dirty_out.stdout.strip():
                    dirty += 1
            except Exception:
                pass
        lines.append(f"  Repository Activity:")
        lines.append(f"    {active} repo(s) active, {total_commits} commit(s)")
        if dirty:
            lines.append(f"    ⚠ {dirty} repo(s) have uncommitted changes")
    except Exception:
        pass

    # ── Events ─────────────────────────────────────────────────────────
    try:
        rows = conn.execute(
            "SELECT event_type, COUNT(*) AS cnt FROM ambient_feed "
            "WHERE timestamp >= ? AND dismissed = 0 "
            "GROUP BY event_type ORDER BY cnt DESC LIMIT 5",
            (cutoff_str,),
        ).fetchall()
        if rows:
            lines.append("")
            lines.append(f"  Events:")
            for r in rows:
                lines.append(f"    {r['event_type']}: {r['cnt']}")
    except Exception:
        pass

    # ── System health ──────────────────────────────────────────────────
    health_items: list[str] = []
    try:
        err_row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM ambient_feed "
            "WHERE event_type = 'cycle_error' AND timestamp >= ?",
            (cutoff_str,),
        ).fetchone()
        if err_row and err_row["cnt"]:
            health_items.append(f"{err_row['cnt']} cycle error(s)")
    except Exception:
        pass
    try:
        skill_row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM skills "
            "WHERE health IN ('unhealthy', 'degrading')"
        ).fetchone()
        if skill_row and skill_row["cnt"]:
            health_items.append(f"{skill_row['cnt']} degraded skill(s)")
    except Exception:
        pass
    try:
        init_row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM pending_initiatives WHERE reviewed = 0"
        ).fetchone()
        if init_row and init_row["cnt"]:
            health_items.append(f"{init_row['cnt']} pending initiative(s)")
    except Exception:
        pass

    if health_items:
        lines.append("")
        lines.append(f"  System:")
        for h in health_items:
            lines.append(f"    · {h}")

    lines.append("")
    lines.append(gray(f"  Generated {now.strftime('%H:%M UTC')}"))

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Subparser registration
# ---------------------------------------------------------------------------


def add_standup_subparser(sub) -> None:
    """Add the ``standup`` subcommand parser."""
    p = sub.add_parser(
        "standup",
        help="Standup report — 5-7 line summary for daily standup meetings.",
    )
    p.set_defaults(func=cmd_standup)


def add_yesterday_subparser(sub) -> None:
    """Add the ``yesterday`` subcommand parser."""
    p = sub.add_parser(
        "yesterday",
        help="Show yesterday's activity — commits, events, blockers.",
    )
    p.set_defaults(func=cmd_yesterday)
