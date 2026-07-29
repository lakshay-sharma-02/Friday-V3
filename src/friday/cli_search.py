"""CLI for Semantic Code Search — ``friday search <query>``.

Uses the existing ``code_search.py`` module which:
1. LLM-expands a natural-language query into search terms
2. Runs ripgrep across all known repositories
3. LLM-reranks results for semantic relevance

Usage::

    friday search "find authentication code"
    friday search --no-rerank "database connection"
    friday search --no-expand "sqlalchemy session"
    friday search --json "handle errors"
"""

from __future__ import annotations

import argparse
import json

from .presentation.cli_format import header, green, yellow, red, gray


def cmd_search(args: argparse.Namespace) -> int:
    """Run a semantic code search across the workspace."""
    from .code_search import semantic_search
    from .db import connect

    query: str = args.query
    max_results: int = getattr(args, "max", 30)
    no_rerank: bool = getattr(args, "no_rerank", False)
    no_expand: bool = getattr(args, "no_expand", False)
    show_json: bool = getattr(args, "json", False)

    if not query.strip():
        print(red("  error: search query cannot be empty"))
        return 1

    conn = connect()
    try:
        result = semantic_search(
            conn,
            query=query,
            max_results=max_results,
            rerank=not no_rerank,
            expand_query=not no_expand,
        )
    finally:
        conn.close()

    if show_json:
        print(json.dumps(result.to_dict(), indent=2, default=str))
        return 0

    print(result.format())
    return 0


# ---------------------------------------------------------------------------
# Subparser registration
# ---------------------------------------------------------------------------


def add_subparser(sub) -> None:
    """Add the ``search`` subcommand parser."""
    p = sub.add_parser(
        "search",
        help="Semantic code search — search codebase by meaning, not just keywords.",
    )
    p.add_argument(
        "query",
        help="Natural-language search query (e.g. 'find authentication code').",
    )
    p.add_argument(
        "--max", type=int, default=30,
        help="Maximum results to show (default: 30).",
    )
    p.add_argument(
        "--no-rerank", action="store_true",
        help="Skip LLM re-ranking (faster but less relevant results).",
    )
    p.add_argument(
        "--no-expand", action="store_true",
        help="Skip LLM query expansion (search for exact words only).",
    )
    p.add_argument(
        "--json", action="store_true",
        help="Output JSON instead of formatted text.",
    )
    p.set_defaults(func=cmd_search)
