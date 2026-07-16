"""Friday V3 command-line interface."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _load_dotenv() -> None:
    """Load KEY=VALUE lines from a `.env` (cwd, else package root) into the
    environment without overriding already-set vars. No dependency — the spec
    forbids adding one. Silent on any error so the CLI never breaks on config."""
    for path in (Path.cwd() / ".env", Path(__file__).resolve().parents[2] / ".env"):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip().strip("'\""))
        return


from .ask import Exchange, ask
from .architecture import analyze_and_store
from .cli_knowledge import cmd_knowledge
from .cli_understanding import cmd_understanding
from .cli_initiative import cmd_initiatives
from .cli_insight import cmd_insights
from .context import ContextEngine, TimelineEntry, summarize_day
from .db import connect
from .ingest import ingest_paths
from .observe import format_report, observe, observe_via_engine
from .observation import default_registry, format_run
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
        cov = answer.evidence.raw.get("coverage_report")
        if cov:
            from .evidence_scope import format_coverage_report
            print("\n" + format_coverage_report(cov))
        audit = answer.evidence.raw.get("retrieval_audit")
        if audit:
            print("Retrieval audit:")
            print(f"  Objective: {audit['objective']}")
            print(f"  Providers requested: {', '.join(audit['providers_requested'])}")
            print(f"  Providers returned:  {', '.join(audit['providers_returned'])}")
            print(f"  Knowledge used:      {'yes' if audit['knowledge_used'] else 'no'}")
            print(f"  Confidence:          {audit['confidence']}")
            if answer.evidence.raw.get("widened"):
                print("  Coverage widened:    yes (adaptive expansion, once)")
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
    prev_time, changes = observe_via_engine(conn)
    conn.close()
    print(format_report(prev_time, changes), end="")
    return 0


def cmd_observers(args: argparse.Namespace) -> int:
    """List every registered observer and its health/summary."""
    conn = connect()
    reg = default_registry()
    print(f"Registered observers ({len(reg)}):\n")
    for obs in reg.all():
        h = obs.health(conn)
        state = h.status.value
        mark = "ok" if h.healthy else "!"
        print(f"  [{mark}] {obs.name}  ({state})")
        if h.detail and not h.healthy:
            print(f"       {h.detail}")
        try:
            print(f"       {obs.summarize(conn)}")
        except Exception as exc:
            print(f"       (summary unavailable: {exc})")
    conn.close()
    return 0


def _context_engine():
    conn = connect()
    return conn, ContextEngine(conn)


def cmd_context_build(args: argparse.Namespace) -> int:
    """WRITE: build engineering sessions from stored observations and persist."""
    conn, eng = _context_engine()
    result = eng.build()
    conn.close()
    print(result.to_text(), end="")
    return 0


def cmd_context(args: argparse.Namespace) -> int:
    """Dispatch friday context [build|today]."""
    if getattr(args, "action", None) == "build":
        return cmd_context_build(args)
    conn, eng = _context_engine()
    sessions = eng.sessions()
    if not sessions:
        conn.close()
        print("Engineering context has not been built.\n")
        print("Run:\n")
        print("  friday context build\n")
        return 0
    if eng.is_stale():
        print("Engineering context is out of date.")
        print("Latest observations are newer than the current context.\n")
        print("Run:\n")
        print("  friday context build\n")
        print()
    from datetime import datetime, timezone
    if getattr(args, "action", None) == "today":
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        summ = eng.summary(day)
    else:
        summ = eng.summary()
    conn.close()

    # Format context summary
    print(f"Engineering Context — {summ.day}\n")
    print(f"Sessions: {summ.session_count}")
    print(f"Active time: {summ.estimated_active_min:.1f} min")
    print(f"Context switches: {summ.context_switches}")
    if summ.most_active_repo:
        print(f"Most active: {summ.most_active_repo}")
    if summ.current_focus:
        print(f"Current focus: {summ.current_focus}")
    return 0


def cmd_sessions(args: argparse.Namespace) -> int:
    """READ-ONLY: list all engineering sessions (newest first)."""
    conn, eng = _context_engine()
    sessions = eng.sessions()
    conn.close()

    if not sessions:
        print("No sessions found.\n")
        print("Run:\n")
        print("  friday context build\n")
        return 0

    for s in sessions:
        print(f"{s.start_time[:16]} | {s.duration_min:>5.0f}m | {s.activity.value:20s} | {s.primary_repo or 'multiple'}")

    print(f"\nTotal: {len(sessions)} sessions")
    return 0


def cmd_timeline(args: argparse.Namespace) -> int:
    """READ-ONLY: show the chronological engineering timeline."""
    conn, eng = _context_engine()
    timeline = eng.timeline()
    conn.close()

    if not timeline:
        print("No timeline entries.\n")
        print("Run:\n")
        print("  friday context build\n")
        return 0

    for entry in timeline:
        if entry.kind == "session":
            print(f"[{entry.start_time[:16]}] {entry.duration_min:>5.0f}m | {entry.label} | {entry.detail or ''}")
        else:
            print(f"[{entry.start_time[:16]}] {entry.duration_min:>5.0f}m | {entry.label}")

    return 0


def cmd_observer(args: argparse.Namespace) -> int:
    """Show one observer's health, summary, and live facts."""
    conn = connect()
    reg = default_registry()
    if args.name not in reg:
        print(f"error: no such observer: {args.name}", file=sys.stderr)
        print(f"available: {', '.join(reg.names())}", file=sys.stderr)
        conn.close()
        return 2
    obs = reg.get(args.name)
    h = obs.health(conn)
    print(f"Observer: {obs.name}")
    print(f"Health:   {h.status.value}" + (f" — {h.detail}" if h.detail else "") + (f"  [{h.method}]" if h.method else ""))
    if h.healthy:
        print(f"Summary:  {obs.summarize(conn)}")
        if not args.summary_only:
            from .observation import ObservationEngine, ObserverRegistry
            reg_single = ObserverRegistry()
            reg_single.register(obs)
            run = ObservationEngine(reg_single, conn).run()
            print("\n" + format_run(run), end="")
    conn.close()
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    """Audit every repository for exactly why its evidence is weak (Part D)."""
    from .evidence_scope import audit_evidence_completeness, format_completeness_audit

    conn = connect()
    rows = audit_evidence_completeness(conn)
    conn.close()
    print(format_completeness_audit(rows))
    weak = sum(1 for r in rows if not r["complete"])
    print(f"\n{weak} of {len(rows)} repositories have weak evidence.")
    return 0


def main(argv: list[str] | None = None) -> int:
    _load_dotenv()
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

    p_audit = sub.add_parser(
        "audit", help="Show exactly why each repository contributes weak evidence."
    )
    p_audit.set_defaults(func=cmd_audit)

    p_observers = sub.add_parser(
        "observers", help="List all registered observers and their health."
    )
    p_observers.set_defaults(func=cmd_observers)

    p_context = sub.add_parser(
        "context", help="Show engineering context. Add 'build' to (re)build it."
    )
    p_context.add_argument(
        "action", nargs="?", default=None,
        choices=["build", "today"],
        help="'build' to derive+persist sessions (WRITE); 'today' for today only; omit to show current context.",
    )
    p_context.set_defaults(func=cmd_context)

    p_sessions = sub.add_parser(
        "sessions", help="List all engineering sessions (newest first)."
    )
    p_sessions.set_defaults(func=cmd_sessions)

    p_timeline = sub.add_parser(
        "timeline", help="Show the chronological engineering timeline."
    )
    p_timeline.set_defaults(func=cmd_timeline)

    p_observer = sub.add_parser(
        "observer", help="Show one observer's health, summary, and live facts."
    )
    p_observer.add_argument("name", help="Observer name (see `friday observers`).")
    p_observer.add_argument(
        "--summary-only", action="store_true",
        help="Print health + summary only; skip a fresh observation run.",
    )
    p_observer.set_defaults(func=cmd_observer)

    p_knowledge = sub.add_parser(
        "knowledge", help="Accumulated engineering knowledge (WRITE: 'build')."
    )
    p_knowledge.add_argument(
        "action", nargs="?", default=None,
        help="Action: 'build' (WRITE), 'list', 'explain', 'history', 'evolution', 'verify'; omit to list.",
    )
    p_knowledge.add_argument(
        "knowledge_id", nargs="?", default=None,
        help="Knowledge ID for 'explain' action (can also use --id)."
    )
    p_knowledge.add_argument(
        "--id", help="Knowledge ID for 'explain' action."
    )
    p_knowledge.add_argument(
        "--verbose", action="store_true",
        help="Show full evidence IDs when explaining."
    )
    p_knowledge.set_defaults(func=cmd_knowledge)

    p_understanding = sub.add_parser(
        "understanding", help="Derive and show engineering understanding (WRITE: 'build')."
    )
    p_understanding.add_argument(
        "action", nargs="?", default=None,
        choices=["build", "explain", "evolution"],
        help="'build' (WRITE), 'explain <id>', 'evolution'; omit to list.",
    )
    p_understanding.add_argument(
        "understanding_id", nargs="?", default=None,
        help="Understanding ID for 'explain' (can also use --id)."
    )
    p_understanding.add_argument(
        "--id", help="Understanding ID for 'explain' action."
    )
    p_understanding.set_defaults(func=cmd_understanding)

    p_initiatives = sub.add_parser(
        "initiatives", help="Derive and show engineering initiatives (WRITE: 'build')."
    )
    p_initiatives.add_argument(
        "action", nargs="?", default=None,
        choices=["build", "explain", "timeline"],
        help="'build' (WRITE), 'explain <id>', 'timeline'; omit to list.",
    )
    p_initiatives.add_argument(
        "initiative_id", nargs="?", default=None,
        help="Initiative ID for 'explain' (can also use --id)."
    )
    p_initiatives.add_argument(
        "--id", help="Initiative ID for 'explain' action."
    )
    p_initiatives.set_defaults(func=cmd_initiatives)

    p_insights = sub.add_parser(
        "insights", help="Derive and show engineering insights (WRITE: 'build')."
    )
    p_insights.add_argument(
        "action", nargs="?", default=None,
        choices=["build", "explain", "evolution"],
        help="'build' (WRITE), 'explain <id>', 'evolution'; omit to list.",
    )
    p_insights.add_argument(
        "insight_id", nargs="?", default=None,
        help="Insight ID for 'explain' (can also use --id)."
    )
    p_insights.add_argument(
        "--id", help="Insight ID for 'explain' action."
    )
    p_insights.set_defaults(func=cmd_insights)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
