"""friday synthesize — cross-project structural overlap analysis.

Extension layer CLI (not frozen). Per the tasks spec: no auto-action, no code
generation, no PRs. Output is always confidence-labeled.
"""

from __future__ import annotations

import argparse
import sys

from .db import connect


def cmd_synthesize(args: argparse.Namespace) -> int:
    """Synthesize structural overlap analysis between two repos."""
    from .synthesis import synthesize

    conn = connect()
    result = synthesize(conn, args.repo_a, args.repo_b)
    conn.close()

    print(result.to_text())
    return 0


def add_subparser(sub) -> None:
    p = sub.add_parser(
        "synthesize",
        help="Analyse structural overlap between two repositories.",
    )
    p.add_argument("repo_a", help="First repository name.")
    p.add_argument("repo_b", help="Second repository name.")
    p.set_defaults(func=cmd_synthesize)
