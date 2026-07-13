"""Friday V3 command-line interface."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .ask import Exchange, ask
from .architecture import analyze_and_store
from .db import connect
from .knowledge import ingest_paths
from .observe import format_report, observe
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


def cmd_chat(args: argparse.Namespace) -> int:
    """Bounded conversational loop (M6.5D): remembers only the last exchange.

    Thin wrapper over ask(prev=...) — no new architecture, no persistence.
    """
    conn = connect()
    prev: Exchange | None = None
    print("Friday chat — type 'exit' to quit. I only remember the last thing we said.")
    while True:
        try:
            q = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not q:
            continue
        if q.lower() in ("exit", "quit"):
            break
        ans = ask(q, conn, prev=prev, verbose=args.verbose)
        prev = Exchange(q, ans)
        if args.verbose:
            print("[evidence]", "; ".join(ans.evidence.blocks) or "(none)")
        print(ans.text)
    conn.close()
    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    """Extract and persist architectural knowledge for one repository."""
    from .discovery import Repo

    path = Path(args.repository).expanduser().resolve()
    git = path / ".git"
    if not (git.is_dir() or git.is_file()):
        print(f"error: not a git repository: {path}", file=sys.stderr)
        return 2
    conn = connect()
    profile = analyze_and_store(conn, Repo(path=path))
    conn.close()
    print(f"Analyzed {profile.path}")
    print(f"  Architecture: {profile.architecture}")
    print(f"  Components:   {', '.join(c.name for c in profile.components) or '(none detected)'}")
    print(f"  Entry points: {', '.join(f'{e.kind} ({e.detail})' for e in profile.entry_points) or '(none detected)'}")
    if profile.circular:
        print(f"  Circular deps: {len(profile.circular)}")
    return 0


def cmd_observe(args: argparse.Namespace) -> int:
    """Record the workspace and report changes since the previous observation."""
    conn = connect()
    prev_time, changes = observe(conn)
    conn.close()
    print(format_report(prev_time, changes), end="")
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

    p_chat = sub.add_parser(
        "chat", help="Conversational loop that remembers only the last exchange."
    )
    p_chat.add_argument(
        "--verbose",
        action="store_true",
        help="Show the retrieved evidence block behind each answer.",
    )
    p_chat.set_defaults(func=cmd_chat)

    p_analyze = sub.add_parser(
        "analyze", help="Extract and persist repository architecture knowledge."
    )
    p_analyze.add_argument("repository", help="Path to a git repository.")
    p_analyze.set_defaults(func=cmd_analyze)

    p_observe = sub.add_parser(
        "observe", help="Record the workspace and report changes since last time."
    )
    p_observe.set_defaults(func=cmd_observe)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
