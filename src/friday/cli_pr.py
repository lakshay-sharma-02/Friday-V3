"""CLI for PR Review — ``friday pr``.

Usage::

    friday pr review --preview             # Show pending reviews
    friday pr review my/repo               # Run review for a specific repo
    friday pr review --auto                # Run and persist all
    friday pr list                         # List stored reviews
"""

from __future__ import annotations

import argparse
import json
from .presentation.cli_format import header, green, yellow, red, gray


def cmd_pr(args: argparse.Namespace) -> int:
    """Manage PR reviews."""
    from .db import connect
    action = getattr(args, "action", "list")

    conn = connect()
    try:
        if action == "review":
            preview = getattr(args, "preview", False)
            auto = getattr(args, "auto", False)
            repo = getattr(args, "repo", "")

            from .pr_review import PRReviewEngine

            engine = PRReviewEngine(conn)
            reviews = engine.run(repo_name=repo)

            if not reviews:
                print(gray("  No new PRs to review."))
                return 0

            for r in reviews:
                sev_color = {"high": red, "medium": yellow, "info": green}.get(r.severity, gray)
                print(header(f"PR #{r.pr_number}", r.repo))
                print(f"  {r.pr_title} by {r.pr_author}")
                print(f"  {r.base_branch} → {r.head_branch}")
                print(f"  Severity: {sev_color(r.severity.upper())}")
                print(f"  {r.summary}")
                if r.concerns:
                    print(f"  {'─' * 40}")
                    for c in r.concerns:
                        print(f"  ⚠ {c}")
                if r.suggestions:
                    for s in r.suggestions:
                        print(f"  💡 {s}")
                if r.test_gaps:
                    for g in r.test_gaps:
                        print(f"  🧪 {g}")
                print()

                if auto or preview:
                    engine.persist_review(r)
                    engine.push_to_feed(r)

            if auto:
                print(green(f"  Persisted {len(reviews)} review(s) to DB + feed."))
            elif not preview:
                print(gray(f"  Generated {len(reviews)} review(s). Use --preview or --auto."))
            return 0

        if action == "list":
            rows = conn.execute(
                "SELECT repo, pr_number, pr_title, severity, created_at "
                "FROM pr_reviews ORDER BY created_at DESC LIMIT 30"
            ).fetchall()
            if not rows:
                print(gray("  No PR reviews stored."))
                print(gray("  Run: friday pr review --auto"))
                return 0
            print(header("PR Reviews", f"{len(rows)} total"))
            print()
            for r in rows:
                sev_mark = {"high": red("●"), "medium": yellow("●"), "info": green("○")}.get(r["severity"], gray("○"))
                print(f"  {sev_mark} #{r['pr_number']:5d} {r['repo']:25s} {r['pr_title'][:50]}")
            return 0

        print(red(f"  Unknown action: {action}"))
        return 1
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Subparser registration
# ---------------------------------------------------------------------------


def add_subparser(sub) -> None:
    """Add the ``pr`` subcommand parser."""
    p = sub.add_parser("pr", help="PR review assistant — analyze and review pull requests.")
    subp = p.add_subparsers(dest="action", required=True)

    # review
    pr = subp.add_parser("review", help="Review pull requests.")
    pr.add_argument("--repo", "-r", default="", help="Repository name to scope review (optional).")
    pr.add_argument("--preview", action="store_true", help="Show review without persisting.")
    pr.add_argument("--auto", action="store_true", help="Persist all generated reviews automatically.")
    pr.set_defaults(func=cmd_pr)

    # list
    pl = subp.add_parser("list", help="List stored PR reviews.")
    pl.set_defaults(func=cmd_pr)
