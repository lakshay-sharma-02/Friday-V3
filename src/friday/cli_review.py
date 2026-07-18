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
    """Dispatch friday review [<project>|plan|graph|runtime|portfolio]."""
    token = getattr(args, "token", None)
    rest = getattr(args, "rest", None) or []

    # `friday review` with no args -> workspace.
    if not token:
        return _cmd_review_workspace(args)
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
