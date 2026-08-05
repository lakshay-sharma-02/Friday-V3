"""CLI commands for the Wave 11 research/briefing surfaces.

Debug hatches (Law 1 — the NL paths are the product; these give
operators direct access):

    friday6 analyze <repo>                 # repo architecture analysis
    friday6 correlate <a> <b>              # cross-project integration cost
    friday6 briefing [morning|evening]     # briefing from real V4 state
    friday6 narrative [date]               # the day's story from the audit log
    friday6 report <title> <key=value...>  # deterministic cited report

Same conventions as every other `friday6` command: colors, exit codes,
JSON purity.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger("friday_v6.cli_research")

_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_CYAN = "\033[96m"
_GREEN = "\033[92m"
_RED = "\033[91m"
_YELLOW = "\033[93m"

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 3


def _print_logo(title: str = "Research"):
    print()
    print(f"  {_BOLD}{_CYAN}◆ FRIDAY{_RESET} {_DIM}V4 — {title}{_RESET}")
    print(f"  {_DIM}{'─' * 40}{_RESET}")


def _print_dim(text: str):
    print(f"  {_DIM}{text}{_RESET}")


def _print_friday(text: str):
    print(f"\n{_CYAN}  Friday:{_RESET} {text}")


def _print_error(text: str):
    print(f"  {_RED}✗ {text}{_RESET}")


def _resolve_db(args) -> Optional[object]:
    try:
        from . import db
        return db.connect(path=getattr(args, "db", None))
    except Exception as exc:
        logger.debug(f"research: db unavailable ({exc})")
        return None


def _close_db(conn) -> None:
    try:
        if conn is not None:
            conn.close()
    except Exception:
        pass


# ── commands ──────────────────────────────────────────────────────────


def cmd_analyze(args: argparse.Namespace) -> int:
    """`friday6 analyze <repo>` — evidence-cited repo analysis."""
    from .research import analyze
    profile = analyze(args.repo)
    if args.json:
        print(json.dumps(profile.to_dict(), indent=2, default=str))
        return EXIT_OK if profile.available else EXIT_FAILED
    _print_logo("Analyze")
    if not profile.available:
        _print_error(f"{args.repo} — not a readable directory")
        return EXIT_FAILED
    _print_friday(f"{profile.path}")
    for e in profile.evidence:
        _print_dim(f"  ↳ {e}")
    if not profile.evidence:
        _print_dim("  (nothing notable — no code signals found)")
    print()
    return EXIT_OK


def cmd_correlate(args: argparse.Namespace) -> int:
    """`friday6 correlate <a> <b>` — cross-project integration cost."""
    from .research import correlate
    est = correlate(args.a, args.b)
    if args.json:
        print(json.dumps(est.to_dict(), indent=2, default=str))
        return EXIT_OK if est.shared_languages or est.overlapping_files \
            else EXIT_FAILED
    _print_logo("Correlate")
    if not (est.shared_languages or est.overlapping_files):
        _print_error(f"{est.a} vs {est.b} — no shared signals found")
        return EXIT_FAILED
    _print_friday(f"{est.a} ⇄ {est.b}")
    for e in est.evidence:
        _print_dim(f"  ↳ {e}")
    _print_dim(f"  ⚖ overlap {est.overlap_score:.0%} · {est.days_range} "
               f"· confidence {est.confidence}")
    print()
    return EXIT_OK


def cmd_briefing(args: argparse.Namespace) -> int:
    """`friday6 briefing [morning|evening]` — briefing from real state."""
    conn = _resolve_db(args)
    try:
        from .briefing import build_briefing
        kind = args.kind or "morning"
        b = build_briefing(conn, kind=kind)
    except Exception as exc:
        _print_error(f"briefing failed: {exc}")
        return EXIT_FAILED
    finally:
        _close_db(conn)
    if args.json:
        print(json.dumps(b.to_dict(), indent=2, default=str))
        return EXIT_OK
    _print_logo(f"Briefing — {kind}")
    _print_friday(b.text)
    for s in b.sections:
        _print_dim(f"  ↳ {s}")
    _print_dim(f"  (tone: {b.tone}, depth {b.depth:.2f})")
    print()
    return EXIT_OK


def cmd_narrative(args: argparse.Namespace) -> int:
    """`friday6 narrative [date]` — the day's story from real state."""
    conn = _resolve_db(args)
    try:
        from .briefing import day_narrative
        n = day_narrative(conn, date=args.date or "")
    except Exception as exc:
        _print_error(f"narrative failed: {exc}")
        return EXIT_FAILED
    finally:
        _close_db(conn)
    if args.json:
        print(json.dumps(n.to_dict(), indent=2, default=str))
        return EXIT_OK
    _print_logo(f"Narrative — {n.date}")
    if not n.entries:
        _print_friday("Nothing recorded yet — the audit log is quiet.")
    else:
        for entry in n.entries:
            _print_dim(f"  {entry}")
    print()
    return EXIT_OK


def cmd_report(args: argparse.Namespace) -> int:
    """`friday6 report <title> <key=value...>` — deterministic cited report.

    Each ``key=value`` becomes a section with one finding (the value).
    ``--daily`` / ``--weekly`` build the report from *real V4 state*
    (missions, actions, security grade, memory, ambient events) — the
    Wave 11 ``synthesis/reports.py`` surface.
    """
    if args.daily or args.weekly:
        conn = _resolve_db(args)
        try:
            from .synthesis import (build_daily_report, build_weekly_report)
            report = build_weekly_report(conn) if args.weekly \
                else build_daily_report(conn)
        except Exception as exc:
            _print_error(f"report failed: {exc}")
            return EXIT_FAILED
        finally:
            _close_db(conn)
        if args.json:
            print(json.dumps(report, indent=2, default=str))
            return EXIT_OK
        from .synthesis import SynthesisReport
        sr = SynthesisReport(report["title"], report["sections"],
                             report["generated_at"])
        _print_logo("Report — " + ("weekly" if args.weekly else "daily"))
        print(sr.render())
        return EXIT_OK

    from .synthesis import synthesize
    sections: dict[str, list[str]] = {}
    for kv in args.items or []:
        if "=" in kv:
            k, v = kv.split("=", 1)
            sections.setdefault(k, []).append(v)
    if not sections:
        _print_error("report needs sections: friday6 report 'Security' "
                     "vulns='2 high' grade=A")
        return EXIT_USAGE
    report = synthesize(args.title, sections)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2, default=str))
        return EXIT_OK
    print(report.render())
    return EXIT_OK


# ── parsers ───────────────────────────────────────────────────────────


def build_research_parser(subparsers) -> None:
    """Register the ``research`` subcommand on the top-level parser.

    ``friday6 research analyze X`` — the top-level ``friday6`` parser
    passes its subparsers here; the research subcommands are built by
    :func:`build_research_commands` (shared with the standalone
    ``cli_research.main`` entry so ``friday6 research`` works too).
    """
    research_parser = subparsers.add_parser(
        "research", help="Research & reflection — analyze, correlate, briefing",
        description="Wave 11 surfaces: repo analysis, cross-project "
                    "correlation, briefings, narratives, reports.",
    )
    research_sub = research_parser.add_subparsers(dest="research_command")
    build_research_commands(research_sub)


def build_research_commands(subparsers) -> None:
    """Register the research subcommands (analyze/correlate/briefing/…)."""
    analyze_p = subparsers.add_parser("analyze",
                                      help="Analyze a repo's architecture")
    analyze_p.add_argument("repo", help="Path to the repository")
    analyze_p.add_argument("--json", action="store_true")
    analyze_p.set_defaults(func=cmd_analyze)

    correlate_p = subparsers.add_parser(
        "correlate", help="Estimate integration cost between two repos")
    correlate_p.add_argument("a", help="First repo path")
    correlate_p.add_argument("b", help="Second repo path")
    correlate_p.add_argument("--json", action="store_true")
    correlate_p.set_defaults(func=cmd_correlate)

    briefing_p = subparsers.add_parser(
        "briefing", help="Morning/evening briefing from real V4 state")
    briefing_p.add_argument("kind", nargs="?", choices=["morning", "evening"],
                            help="morning (default) or evening")
    briefing_p.add_argument("--db", type=Path, default=None)
    briefing_p.add_argument("--json", action="store_true")
    briefing_p.set_defaults(func=cmd_briefing)

    narrative_p = subparsers.add_parser(
        "narrative", help="The day's story from the audit log")
    narrative_p.add_argument("date", nargs="?", default="",
                             help="Date (YYYY-MM-DD); default today")
    narrative_p.add_argument("--db", type=Path, default=None)
    narrative_p.add_argument("--json", action="store_true")
    narrative_p.set_defaults(func=cmd_narrative)

    report_p = subparsers.add_parser(
        "report", help="Deterministic cited report (or daily/weekly from real state)")
    report_p.add_argument("title", help="Report title")
    report_p.add_argument("items", nargs="*",
                          help="key=value findings (one section each)")
    report_p.add_argument("--daily", action="store_true",
                          help="Build today's report from real V4 state")
    report_p.add_argument("--weekly", action="store_true",
                          help="Build this week's report from real V4 state")
    report_p.add_argument("--db", type=Path, default=None)
    report_p.add_argument("--json", action="store_true")
    report_p.set_defaults(func=cmd_report)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING)
    parser = argparse.ArgumentParser(prog="friday6 research")
    # Standalone entry (used by ``friday6 research`` in cli_nl): the
    # research subcommands sit directly under this parser — not nested
    # inside another "research" command. Regression: this used to pass
    # the parser itself to build_research_parser (which expects
    # subparsers) and crashed with AttributeError on every call.
    sub = parser.add_subparsers(dest="research_command")
    build_research_commands(sub)
    args = parser.parse_args(argv)
    if hasattr(args, "func"):
        return args.func(args) or 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
