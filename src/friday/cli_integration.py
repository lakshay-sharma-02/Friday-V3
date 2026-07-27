"""CLI command for `friday integrate` — cross-project integration via the pipeline.

Usage:
    friday integrate <repo-a> <repo-b> [<repo-c> ...]

Runs structural overlap analysis across ALL named repos, generates synthesis
artifacts (architecture comparison, shared patterns, integration plan, adapter
design), and creates a Task Graph that lands in ``friday graph review`` for
human approval. The graph never executes without review.

Minimum 2 repos, maximum 8.
"""

from __future__ import annotations

import argparse
import sys

from .db import connect


def cmd_integrate(args: argparse.Namespace) -> int:
    """Analyse multiple repos for integration and generate a Task Graph."""
    from .integration import IntegrationEngine

    repos = getattr(args, "repos", None) or []

    if len(repos) < 2:
        print(
            "error: at least two repository names are required:\n"
            "  friday integrate <repo-a> <repo-b> [<repo-c> ...]",
            file=sys.stderr,
        )
        return 2

    # Check for duplicates.
    if len(set(r.lower() for r in repos)) != len(repos):
        print(
            "error: duplicate repository names are not allowed",
            file=sys.stderr,
        )
        return 2

    conn = connect()
    engine = IntegrationEngine(conn)

    try:
        result = engine.integrate(*repos)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        conn.close()
        return 2
    except Exception as e:
        print(
            f"error: integration failed: {e}",
            file=sys.stderr,
        )
        conn.close()
        return 2

    conn.close()
    sys.stdout.write(result.to_text())
    return 0
