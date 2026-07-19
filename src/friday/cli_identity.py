"""CLI commands for Project Identity (Milestone 3.5).

`friday identity`          -> list project identities (one line each).
`friday identity <project>` -> explain one project's identity in full.

Thin wrapper over existing identity.py. No new identity logic — only renderers.
"""

from __future__ import annotations

import argparse
import sys

from .db import connect
from .query import all_repositories, repo_by_name
from .identity import build_identity, explain_project_from_conn


def _resolve_repo(conn, name: str):
    if name.isdigit():
        for r in all_repositories(conn):
            if r.id == int(name):
                return r
        return None
    return repo_by_name(conn, name)


def cmd_identity_list(args: argparse.Namespace) -> int:
    """READ: list every ingested project's identity (one line each)."""
    conn = connect()
    repos = all_repositories(conn)
    if not repos:
        conn.close()
        print("No projects ingested yet.\n")
        print("Run:\n")
        print("  friday ingest <path>\n")
        return 0
    print(f"Project identities ({len(repos)}):\n")
    for r in repos:
        ident = build_identity(conn, r.id) if r.id is not None else None
        if ident is None:
            print(f"  {r.name}  (no identity stored)")
            continue
        purpose = ident.purpose or "(purpose unknown)"
        maturity = ident.maturity if ident.maturity != "Unknown" else "?"
        phase = f" — {ident.phase}" if ident.phase else ""
        conf = ident.purpose_confidence if ident.purpose_confidence != "None" else "?"
        print(f"  {r.name}")
        print(f"      {purpose}")
        print(f"      maturity: {maturity}{phase}  purpose confidence: {conf}")
    conn.close()
    return 0


def cmd_identity_explain(args: argparse.Namespace) -> int:
    """READ: explain one project identity in the user's terms."""
    name = getattr(args, "project", None)
    if not name:
        print("error: project name required (friday identity <project>)",
              file=sys.stderr)
        return 2
    conn = connect()
    repo = _resolve_repo(conn, name)
    if repo is None:
        conn.close()
        print(f"error: project not found: {name}", file=sys.stderr)
        print("run `friday identity` to list ingested projects", file=sys.stderr)
        return 2
    conn.close()
    conn = connect()
    print(explain_project_from_conn(conn, repo.id))
    conn.close()
    return 0


def cmd_identity(args: argparse.Namespace) -> int:
    """Dispatch friday identity [<project>]."""
    project = getattr(args, "project", None)
    if project:
        return cmd_identity_explain(args)
    return cmd_identity_list(args)
