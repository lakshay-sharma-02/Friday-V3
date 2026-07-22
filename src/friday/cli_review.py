"""CLI commands for the Review subsystem (Milestone 9.6).

`friday review`              -> workspace engineering-health review.
`friday review <project>`    -> one project's quality review.
`friday review plan "<goal>"`-> review an existing plan.
`friday review graph <id>`   -> review a compiled task graph.
`friday review runtime <sid>`-> review an execution session.
`friday review portfolio`    -> portfolio-quality review.

Thin renderers over review.ReviewEngine. No review logic here.
"""

from __future__ import annotations

import argparse
import sys

from .db import connect
from .review import ReviewEngine, _resolve_graph_id, _resolve_session_id


def cmd_review(args: argparse.Namespace) -> int:
    """Dispatch friday review [<project>|plan|graph|runtime|portfolio|pending]."""
    token = getattr(args, "token", None)
    rest = getattr(args, "rest", None) or []

    if not token:
        return _cmd_review_workspace(args)
    if token == "pending":
        return _cmd_review_pending(args)
    if token == "portfolio":
        return _cmd_review_portfolio(args)
    if token == "plan":
        args.goal = rest
        return _cmd_review_plan(args)
    if token == "graph":
        args.ref = rest[0] if rest else None
        return _cmd_review_graph(args)
    if token == "runtime":
        args.ref = rest[0] if rest else None
        return _cmd_review_runtime(args)
    args.project = token
    return _cmd_review_project(args)


def _cmd_review_pending(args: argparse.Namespace) -> int:
    """Show pending initiatives discovered by watch loop.

    `friday review pending`         -> list all un-reviewed
    `friday review pending <id>`    -> show one with evidence
    `friday review pending dismiss <id>` -> dismiss one
    `friday review pending approve <id>` -> mark approved (manual placeholder)
    """
    conn = connect()
    rest = getattr(args, "rest", None) or []
    action = rest[0] if rest else None
    target_id = rest[1] if len(rest) > 1 else None

    if action == "dismiss" and target_id:
        conn.execute(
            "UPDATE pending_initiatives SET dismissed_at=? WHERE id=?",
            (conn.execute("SELECT datetime('now')").fetchone()[0], target_id))
        conn.commit()
        print(f"Dismissed: {target_id}")
        conn.close()
        return 0

    if action == "approve" and target_id:
        conn.execute(
            "UPDATE pending_initiatives SET reviewed=1, reviewed_at=? "
            "WHERE id=?",
            (conn.execute("SELECT datetime('now')").fetchone()[0], target_id))
        conn.commit()
        print(f"Approved: {target_id}")
        conn.close()
        return 0

    if action and action not in ("dismiss", "approve"):
        # Treat as an initiative ID to show detail.
        target_id = action

    if target_id:
        return _show_pending_detail(conn, target_id)

    # List all unreviewed, undismissed.
    rows = conn.execute(
        "SELECT id, title, statement, initiative_type, confidence, "
        "detected_at, reviewed, dismissed_at "
        "FROM pending_initiatives "
        "WHERE reviewed=0 AND dismissed_at IS NULL "
        "ORDER BY detected_at DESC"
    ).fetchall()
    conn.close()

    if not rows:
        print("No pending initiatives. The watch loop surfaces these\n"
              "when it discovers high-confidence work opportunities.\n")
        print("Run `friday watch --run-once` to trigger a cycle manually.")
        return 0

    count = len(rows)
    para = "initiative" if count == 1 else "initiatives"
    print(f"Pending review — {count} {para}\n")
    for r in rows:
        conf_mark = {"strong": "! ", "medium": "# "}.get(r["confidence"], "  ")
        print(f"  {conf_mark}{r['title']} ({r['confidence']})")
        print(f"      {r['statement'][:120]}")
        print(f"      detected: {r['detected_at'][:19]}, "
              f"type: {r['initiative_type']}")
        print(f"      -> friday review pending {r['id']} for details")
        print()

    print("Actions:")
    print("  friday review pending <id>          Show full detail + evidence")
    print("  friday review pending dismiss <id>  Dismiss (not interested)")
    print("  friday review pending approve <id>  Mark as reviewed")
    return 0


def _show_pending_detail(conn, iid: str) -> int:
    """Show one pending initiative with evidence."""
    row = conn.execute(
        "SELECT id, title, statement, initiative_type, confidence, "
        "understanding_ids, knowledge_ids, detected_at, "
        "reviewed, dismissed_at "
        "FROM pending_initiatives WHERE id=?",
        (iid,)).fetchone()
    if not row:
        # Maybe it's a new initiative not yet harvested; try initiatives table.
        row = conn.execute(
            "SELECT id, title, statement, initiative_type, confidence "
            "FROM initiatives WHERE id=?",
            (iid,)).fetchone()
        if row:
            conn.close()
            print(f"Initiative (not pending): {row['id']}\n")
            print(f"Title:       {row['title']}")
            print(f"Statement:   {row['statement']}")
            print(f"Type:        {row['initiative_type']}")
            print(f"Confidence:  {row['confidence']}")
            print("\nNot in pending queue. It may have been dismissed already.")
            return 0
        conn.close()
        print(f"error: initiative not found: {iid}", file=sys.stderr)
        return 2

    print(f"Pending initiative: {row['id']}\n")
    print(f"Title:       {row['title']}")
    print(f"Statement:   {row['statement']}")
    print(f"Type:        {row['initiative_type']}")
    print(f"Confidence:  {row['confidence']}")
    print(f"Detected:    {row['detected_at']}")
    print(f"Status:      {'reviewed' if row['reviewed'] else 'pending'}")
    if row['dismissed_at']:
        print(f"Dismissed:   {row['dismissed_at']}")

    # Fetch supporting evidence.
    conn.close()

    uids = (row["understanding_ids"] or "").strip().split(",")
    uids = [u for u in uids if u]
    if uids:
        print(f"\nSupporting understanding ({len(uids)}):")
        for uid in uids[:5]:
            print(f"  - {uid}")
        if len(uids) > 5:
            print(f"  ... and {len(uids) - 5} more")

    kids = (row["knowledge_ids"] or "").strip().split(",")
    kids = [k for k in kids if k]
    if kids:
        print(f"\nSupporting knowledge ({len(kids)}):")
        for kid in kids[:5]:
            print(f"  - {kid}")
        if len(kids) > 5:
            print(f"  ... and {len(kids) - 5} more")

    print()
    print("Actions:")
    print(f"  friday review pending approve {row['id']}")
    print(f"  friday review pending dismiss {row['id']}")

    return 0
    """Dispatch friday review [<project>|plan|graph|runtime|portfolio|pending]."""
    token = getattr(args, "token", None)
    rest = getattr(args, "rest", None) or []

    # `friday review` with no args -> workspace.
    if not token:
        return _cmd_review_workspace(args)
    # `friday review pending` -> pending initiatives.
    if token == "pending":
        return _cmd_review_pending(args)
    # `friday review portfolio`
    if token == "portfolio":
        return _cmd_review_portfolio(args)
    # `friday review plan "<goal>"`
    if token == "plan":
        args.goal = rest
        return _cmd_review_plan(args)
    # `friday review graph <id>`
    if token == "graph":
        args.ref = rest[0] if rest else None
        return _cmd_review_graph(args)
    # `friday review runtime <sid>`
    if token == "runtime":
        args.ref = rest[0] if rest else None
        return _cmd_review_runtime(args)
    # A bare token that isn't a subcommand is treated as a project name.
    args.project = token
    return _cmd_review_project(args)


def _cmd_review_workspace(args: argparse.Namespace) -> int:
    conn = connect()
    rep = ReviewEngine(conn).workspace()
    conn.close()
    sys.stdout.write(rep.to_text())
    return 0


def _cmd_review_portfolio(args: argparse.Namespace) -> int:
    conn = connect()
    rep = ReviewEngine(conn).portfolio()
    conn.close()
    sys.stdout.write(rep.to_text())
    return 0


def _cmd_review_project(args: argparse.Namespace) -> int:
    name = getattr(args, "project", None)
    if not name:
        print("error: project name required (friday review <project>)",
              file=sys.stderr)
        return 2
    conn = connect()
    rep = ReviewEngine(conn).project(name)
    conn.close()
    if rep is None:
        print(f"error: project not found: {name}", file=sys.stderr)
        return 2
    sys.stdout.write(rep.to_text())
    return 0


def _cmd_review_plan(args: argparse.Namespace) -> int:
    raw = getattr(args, "goal", None)
    goal = " ".join(raw) if isinstance(raw, (list, tuple)) else (raw or "")
    if not goal.strip():
        print('error: a goal is required: friday review plan "<goal>"',
              file=sys.stderr)
        return 2
    conn = connect()
    rep = ReviewEngine(conn).plan(goal)
    conn.close()
    if rep is None:
        print(f"error: no plan found for goal: {goal}", file=sys.stderr)
        return 2
    sys.stdout.write(rep.to_text())
    return 0


def _cmd_review_graph(args: argparse.Namespace) -> int:
    ref = getattr(args, "ref", None) or getattr(args, "graph_id", None)
    if not ref:
        print("error: graph ID required (friday review graph <id>)",
              file=sys.stderr)
        return 2
    conn = connect()
    eng = ReviewEngine(conn)
    from .planning import TaskGraphEngine
    geng = TaskGraphEngine(conn)
    resolved, err = _resolve_graph_id(ref, geng)
    if err is not None:
        count = len(geng.all_graphs())
        print(f"error: graph index {ref} out of range (1-{count} items)",
              file=sys.stderr)
        conn.close()
        return err
    rep = eng.graph(resolved)
    conn.close()
    if rep is None:
        print(f"error: graph not found: {ref}", file=sys.stderr)
        return 2
    sys.stdout.write(rep.to_text())
    return 0


def _cmd_review_runtime(args: argparse.Namespace) -> int:
    ref = getattr(args, "ref", None) or getattr(args, "session_id", None)
    if not ref:
        print("error: session ID required (friday review runtime <sid>)",
              file=sys.stderr)
        return 2
    conn = connect()
    resolved, err = _resolve_session_id(ref, conn)
    if err is not None:
        from .db import get_runtime_sessions
        count = len(get_runtime_sessions(conn))
        print(f"error: session index {ref} out of range (1-{count} items)",
              file=sys.stderr)
        conn.close()
        return err
    rep = ReviewEngine(conn).runtime(resolved)
    conn.close()
    if rep is None:
        print(f"error: session not found: {ref}", file=sys.stderr)
        return 2
    sys.stdout.write(rep.to_text())
    return 0
