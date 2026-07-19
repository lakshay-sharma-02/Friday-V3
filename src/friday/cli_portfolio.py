"""CLI commands for Workspace Portfolio reasoning (Milestone 3.6).

`friday portfolio`                 -> workspace overview (synthesis + value + universe).
`friday portfolio themes`          -> recurring themes across projects.
`friday portfolio overlap`         -> meaningful overlap between projects.
`friday portfolio ranking`         -> project value ranking.
`friday portfolio recommendations` -> continue / pause / most-attention.
`friday portfolio integrations`    -> which projects should integrate with Friday.

Thin wrapper over existing portfolio.py. Every command calls one existing function.
"""

from __future__ import annotations

import argparse
import sys

from .db import connect
from .portfolio import (
    detect_themes,
    engineering_universe,
    meaningful_overlap,
    portfolio_synthesis,
    project_value_ranking,
    workspace_recommendations,
    integration_opportunities,
)


def cmd_portfolio(args: argparse.Namespace) -> int:
    """READ: workspace overview — synthesis, value ranking, universe."""
    conn = connect()
    blocks = portfolio_synthesis(conn)
    conn.close()
    conn = connect()
    print("Workspace overview\n")
    for line in blocks:
        print(line)
    conn.close()
    conn = connect()
    ranking = project_value_ranking(conn)
    conn.close()
    if ranking:
        print("\nProject value ranking:")
        for r in ranking:
            print(f"  [{r.confidence}] {r.repo}: {r.score:.1f}  ({'; '.join(r.signals)})")
    conn = connect()
    universe = engineering_universe(conn)
    conn.close()
    if universe:
        print("\nWorkspace observations:")
        for line in universe:
            print(f"  - {line}")
    return 0


def cmd_portfolio_themes(args: argparse.Namespace) -> int:
    """READ: recurring themes across projects."""
    conn = connect()
    themes = detect_themes(conn)
    conn.close()
    if not themes:
        print("No recurring themes detected yet.")
        return 0
    print("Recurring themes across your projects:\n")
    for t in themes:
        print(f"[{t.confidence}] {t.theme}")
        print(f"    projects: {', '.join(t.repos) or '(none)'}")
        for e in t.evidence:
            print(f"    - {e}")
    return 0


def cmd_portfolio_overlap(args: argparse.Namespace) -> int:
    """READ: meaningful overlap between projects (never syntax)."""
    conn = connect()
    overlaps = meaningful_overlap(conn)
    conn.close()
    if not overlaps:
        print("No meaningful overlap detected between projects.")
        return 0
    print("Meaningful overlap:\n")
    for o in overlaps:
        print(f"[{o.confidence}] {o.a} <-> {o.b}")
        for d in o.dimensions:
            print(f"    - {d}")
    return 0


def cmd_portfolio_ranking(args: argparse.Namespace) -> int:
    """READ: project value ranking."""
    conn = connect()
    ranking = project_value_ranking(conn)
    conn.close()
    if not ranking:
        print("No projects with enough evidence to rank yet.")
        return 0
    print("Project value ranking:\n")
    for r in ranking:
        print(f"[{r.confidence}] {r.repo}: {r.score:.1f}")
        for s in r.signals:
            print(f"    - {s}")
    return 0


def cmd_portfolio_recommendations(args: argparse.Namespace) -> int:
    """READ: continue / pause / most-attention recommendations."""
    conn = connect()
    rec = workspace_recommendations(conn)
    conn.close()
    print(f"Workspace recommendations ({rec.confidence} confidence)\n")
    print("Continue:")
    if rec.continue_projects:
        for name, why in rec.continue_projects:
            print(f"  - {name}: {why}")
    else:
        print("  (none)")
    print("\nMost attention:")
    print(f"  - {rec.attention[0]}: {rec.attention[1]}")
    print("\nPause / revisit:")
    if rec.pause_projects:
        for name, why in rec.pause_projects:
            print(f"  - {name}: {why}")
    else:
        print("  (none)")
    return 0


def cmd_portfolio_integrations(args: argparse.Namespace) -> int:
    """READ: which projects should integrate with Friday."""
    conn = connect()
    integ = integration_opportunities(conn)
    conn.close()
    if not integ:
        print("No integration candidates detected yet.")
        return 0
    print("Integration candidates (reasoned from project identity):\n")
    for i in integ:
        print(f"[{i.confidence}] {i.repo}")
        print(f"    {i.reason}")
    return 0


def cmd_portfolio_dispatch(args: argparse.Namespace) -> int:
    """Dispatch friday portfolio <subcommand>."""
    token = getattr(args, "token", None)
    if token == "themes":
        return cmd_portfolio_themes(args)
    if token == "overlap":
        return cmd_portfolio_overlap(args)
    if token == "ranking":
        return cmd_portfolio_ranking(args)
    if token == "recommendations":
        return cmd_portfolio_recommendations(args)
    if token == "integrations":
        return cmd_portfolio_integrations(args)
    return cmd_portfolio(args)
