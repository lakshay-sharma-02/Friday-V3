"""CLI command for `friday integrate` — cross-project integration via the pipeline.

Usage:
    friday integrate <repo-a> <repo-b>

Runs structural overlap analysis (synthesis), generates a Task Graph, and lands
it in `friday graph review` for human approval. The graph never executes
without review — same trust model as every other pipeline path.
"""

from __future__ import annotations

import argparse
import sys

from .db import connect


def cmd_integrate(args: argparse.Namespace) -> int:
    """Analyse two repos for integration and generate a Task Graph."""
    from .integration import IntegrationEngine

    repo_a = getattr(args, "repo_a", None)
    repo_b = getattr(args, "repo_b", None)

    if not repo_a or not repo_b:
        print(
            "error: two repository names are required: friday integrate <repo-a> <repo-b>",
            file=sys.stderr,
        )
        return 2

    if repo_a == repo_b:
        print(
            "error: cannot integrate a repository with itself",
            file=sys.stderr,
        )
        return 2

    conn = connect()
    engine = IntegrationEngine(conn)

    try:
        result = engine.integrate(repo_a, repo_b)
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
