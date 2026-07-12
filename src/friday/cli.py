"""Friday V3 command-line interface."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .ask import ask
from .db import connect
from .knowledge import ingest_paths
from .summary import generate_summary


def cmd_ingest(args: argparse.Namespace) -> int:
    paths = [Path(p).expanduser() for p in args.paths]
    missing = [str(p) for p in paths if not p.exists()]
    if missing:
        print(f"error: path(s) not found: {', '.join(missing)}", file=sys.stderr)
        return 2
    conn = connect()
    report = ingest_paths(paths, conn)
    conn.close()
    print(
        f"Ingested {report.repos_stored} of {report.repos_found} repositories "
        f"({report.llm_summaries} with LLM README summaries)."
    )
    return 0


def cmd_summary(args: argparse.Namespace) -> int:
    conn = connect()
    text = generate_summary(conn)
    conn.close()
    print(text)
    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    conn = connect()
    answer = ask(args.question, conn, verbose=args.verbose)
    conn.close()
    if args.verbose:
        print("Question:")
        print(args.question)
        print("\nEvidence:")
        if answer.evidence.blocks:
            print("\n".join(f"- {b}" for b in answer.evidence.blocks))
        else:
            print("(no retrieved evidence)")
        print(f"\n[synthesized via LLM: {answer.used_llm}]\n")
    print(answer.text)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="friday",
        description="Friday V3 — workspace understanding operating partner.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_ingest = sub.add_parser("ingest", help="Scan directories and store knowledge.")
    p_ingest.add_argument("paths", nargs="+", help="One or more root folders.")
    p_ingest.set_defaults(func=cmd_ingest)

    p_summary = sub.add_parser("summary", help="Print the workspace knowledge summary.")
    p_summary.set_defaults(func=cmd_summary)

    p_ask = sub.add_parser("ask", help="Ask a question about your projects.")
    p_ask.add_argument("question", help="Natural-language question (quote it).")
    p_ask.add_argument(
        "--verbose",
        action="store_true",
        help="Show the retrieved evidence block behind the answer.",
    )
    p_ask.set_defaults(func=cmd_ask)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
