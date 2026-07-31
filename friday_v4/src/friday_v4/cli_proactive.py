"""CLI commands for `friday proactive` — proactive intelligence & context awareness.

Usage:
    friday proactive status        # Show what FRIDAY knows about your context
    friday proactive suggest       # Get proactive suggestions now
    friday proactive learn         # Show learning stats and patterns
    friday proactive brief         # Get a morning/evening briefing
    friday proactive observe       # Manually trigger an observation
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime

logger = logging.getLogger("friday_v4.cli_proactive")

_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_GREEN = "\033[92m"
_YELLOW = "\033[93m"
_CYAN = "\033[96m"
_RED = "\033[91m"
_MAGENTA = "\033[95m"


def _print_logo():
    print()
    print(f"  {_BOLD}{_CYAN}◆ FRIDAY{_RESET} {_DIM}V4 — Proactive Intelligence{_RESET}")
    print(f"  {_DIM}{'─' * 40}{_RESET}")
    print()


# ---------------------------------------------------------------------------
# CLI Commands
# ---------------------------------------------------------------------------


def cmd_proactive_status(args: argparse.Namespace) -> int:
    """Show what FRIDAY knows about your current context."""
    from .proactive import AnticipationEngine

    _print_logo()

    engine = AnticipationEngine()
    try:
        # Get context summary
        context = engine.context_engine.get_context()
        print(f"  {_BOLD}Context Awareness{_RESET}")
        print(f"  {_DIM}{'─' * 30}{_RESET}")
        print(f"  App:          {_GREEN}{context.active_app or 'unknown'}{_RESET}")
        print(f"  Class:        {context.active_app_class or 'unknown'}")
        print(f"  Title:        {context.active_title[:60] or 'unknown'}")
        print(f"  Work mode:    {context.work_mode}")
        print(f"  Focus level:  {context.focus_level}")
        print(f"  Time:         {context.time_of_day} ({context.day_of_week})")
        print(f"  Session:      {context.session_minutes} min")

        if context.active_repo:
            print(f"\n  {_BOLD}Git Context{_RESET}")
            print(f"  {_DIM}{'─' * 30}{_RESET}")
            print(f"  Repo:         {context.active_repo}")
            print(f"  Branch:       {context.active_branch}")
            print(f"  Dirty:        {'yes' if context.dirty_repos else 'no'}")
            print(f"  Commits/wk:   {context.recent_commits_week}")

        # Learning stats
        stats = engine.get_learning_stats()
        print(f"\n  {_BOLD}Learning{_RESET}")
        print(f"  {_DIM}{'─' * 30}{_RESET}")
        patterns = stats.get("patterns", {})
        print(f"  Action pairs: {patterns.get('action_pairs_learned', 0)}")
        print(f"  App patterns: {patterns.get('app_transitions_learned', 0)}")
        print(f"  Sessions today: {stats.get('sessions_today', {}).get('session_count', 0)}")
        weekly = stats.get("sessions_this_week", {})
        print(f"  This week:    {weekly.get('total_hours', 0)}h across {weekly.get('total_sessions', 0)} sessions")

        # Active suggestions
        suggestions = engine.get_suggestions(force=True)
        if suggestions:
            print(f"\n  {_BOLD}Pending Suggestions{_RESET}")
            print(f"  {_DIM}{'─' * 30}{_RESET}")
            for s in suggestions[:3]:
                action = f"{_GREEN}SPEAK{_RESET}" if s.should_speak else (
                    f"{_YELLOW}QUEUE{_RESET}" if s.should_queue else (
                    f"{_DIM}SUPPRESS{_RESET}"
                ))
                priority_bar = "█" * (s.priority_score // 10) + "░" * (10 - s.priority_score // 10)
                print(f"  {action} [{priority_bar}] {s.text[:60]}...")

    finally:
        engine.cleanup()

    print()
    return 0


def cmd_proactive_suggest(args: argparse.Namespace) -> int:
    """Get proactive suggestions right now."""
    from .proactive import AnticipationEngine

    engine = AnticipationEngine()
    try:
        suggestions = engine.get_suggestions(force=True)

        _print_logo()

        if not suggestions:
            print(f"  {_DIM}No suggestions right now. FRIDAY is still learning your patterns.{_RESET}")
            print()
            return 0

        print(f"  {_BOLD}Suggestions{_RESET} ({len(suggestions)} found)")
        print(f"  {_DIM}{'─' * 40}{_RESET}\n")

        for i, s in enumerate(suggestions, 1):
            action_label = f"{_GREEN}SPEAK NOW{_RESET}" if s.should_speak else (
                f"{_YELLOW}QUEUED{_RESET}" if s.should_queue else (
                f"{_RED}SUPPRESSED{_RESET}"
            ))
            print(f"  {i}. {action_label} [{s.priority_score}/100]")
            print(f"     {s.text}")
            print(f"     {_DIM}from: {s.source} | urgency: {s.urgency}{_RESET}")
            print()

    finally:
        engine.cleanup()

    return 0


def cmd_proactive_learn(args: argparse.Namespace) -> int:
    """Show what FRIDAY has learned about you."""
    from .proactive import AnticipationEngine

    _print_logo()

    engine = AnticipationEngine()
    try:
        stats = engine.get_learning_stats()

        # Patterns
        patterns = stats.get("patterns", {})
        print(f"  {_BOLD}Learned Patterns{_RESET}")
        print(f"  {_DIM}{'─' * 30}{_RESET}")
        print(f"  Action pairs observed:    {patterns.get('total_actions_observed', 0)}")
        print(f"  Unique patterns learned:  {patterns.get('action_pairs_learned', 0)}")
        print(f"  App transitions tracked:  {patterns.get('app_transitions_learned', 0)}")
        print(f"  Timing patterns tracked:  {patterns.get('timing_patterns_tracked', 0)}")

        strongest_pair = patterns.get("strongest_action_pattern", "none")
        if strongest_pair != "none":
            print(f"\n  Strongest pattern: {_GREEN}{strongest_pair}{_RESET}")

        # Sessions
        today = stats.get("sessions_today", {})
        print(f"\n  {_BOLD}Today's Sessions{_RESET}")
        print(f"  {_DIM}{'─' * 30}{_RESET}")
        print(f"  Sessions:     {today.get('session_count', 0)}")
        print(f"  Total time:   {today.get('total_minutes', 0)} min")
        print(f"  Most used:    {today.get('most_used_app', 'none')}")
        print(f"  Active now:   {'yes' if today.get('active_now') else 'no'}")

        weekly = stats.get("sessions_this_week", {})
        print(f"\n  {_BOLD}This Week{_RESET}")
        print(f"  {_DIM}{'─' * 30}{_RESET}")
        print(f"  Total sessions: {weekly.get('total_sessions', 0)}")
        print(f"  Total time:     {weekly.get('total_hours', 0)} hours")
        print(f"  Avg/session:    {weekly.get('average_per_session', 0)} min")

        # Queue
        from .proactive import PriorityInference
        prio = PriorityInference()
        queue = prio.get_queue()
        if queue:
            print(f"\n  {_BOLD}Queued Items{_RESET} ({len(queue)})")
            print(f"  {_DIM}{'─' * 30}{_RESET}")
            for item in queue[:5]:
                print(f"  [{item.priority_score}] {item.text[:60]}...")

    finally:
        engine.cleanup()

    print()
    return 0


def cmd_proactive_brief(args: argparse.Namespace) -> int:
    """Get a briefing of what FRIDAY knows about your work context."""
    from .proactive import AnticipationEngine

    engine = AnticipationEngine()
    try:
        summary = engine.get_context_summary()
        suggestions = engine.get_suggestions(force=True)
        stats = engine.get_learning_stats()

        _print_logo()

        # Context summary
        print(f"  {_BOLD}Context{_RESET}")
        print(f"  {_DIM}{'─' * 30}{_RESET}")
        print(f"  {summary}")
        print()

        # Session stats
        weekly = stats.get("sessions_this_week", {})
        today = stats.get("sessions_today", {})
        print(f"  {_BOLD}Activity{_RESET}")
        print(f"  {_DIM}{'─' * 30}{_RESET}")
        print(f"  Today: {today.get('total_minutes', 0)} min ({today.get('session_count', 0)} sessions)")
        print(f"  Week:  {weekly.get('total_hours', 0)} hours ({weekly.get('total_sessions', 0)} sessions)")
        print()

        # Suggestions that passed priority filtering
        speak_now = [s for s in suggestions if s.should_speak]
        queued = [s for s in suggestions if s.should_queue]

        if speak_now:
            print(f"  {_BOLD}{_GREEN}Things worth mentioning{_RESET}")
            print(f"  {_DIM}{'─' * 30}{_RESET}")
            for s in speak_now:
                print(f"  • {s.text}")
            print()

        if queued:
            print(f"  {_BOLD}{_YELLOW}Things queued for later{_RESET}")
            print(f"  {_DIM}{'─' * 30}{_RESET}")
            for s in queued[:3]:
                print(f"  • {s.text}")
            print()

        if not speak_now and not queued:
            print(f"  {_DIM}No notable items to report. FRIDAY is watching.{_RESET}")
            print()

    finally:
        engine.cleanup()

    return 0


def cmd_proactive_observe(args: argparse.Namespace) -> int:
    """Manually trigger an observation to feed the pattern learner."""
    from .proactive import AnticipationEngine

    action = " ".join(args.action) if args.action else "manual_observation"
    context = {}
    if args.app:
        context["app"] = args.app
    if args.repo:
        context["repo"] = args.repo

    engine = AnticipationEngine()
    try:
        engine.observe_activity(action, context)
        print(f"  {_GREEN}✅ Observed: {action}{_RESET}")
        if context:
            print(f"     Context: {context}")
        print()
    finally:
        engine.cleanup()

    return 0


# ---------------------------------------------------------------------------
# Argument parsers
# ---------------------------------------------------------------------------


def build_proactive_parser(subparsers) -> None:
    """Build subparser for `friday proactive`."""
    parser = subparsers.add_parser(
        "proactive",
        help="Proactive intelligence & context awareness",
        description="Friday's proactive intelligence engine. "
                    "Understands your context, learns your patterns, "
                    "and anticipates your needs.",
    )
    proactive_sub = parser.add_subparsers(dest="proactive_command")

    # friday proactive status
    p = proactive_sub.add_parser("status", help="Show current context awareness")
    p.set_defaults(func=cmd_proactive_status)

    # friday proactive suggest
    p = proactive_sub.add_parser("suggest", help="Get proactive suggestions now")
    p.set_defaults(func=cmd_proactive_suggest)

    # friday proactive learn
    p = proactive_sub.add_parser("learn", help="Show learning stats and patterns")
    p.set_defaults(func=cmd_proactive_learn)

    # friday proactive brief
    p = proactive_sub.add_parser("brief", help="Get a context briefing")
    p.set_defaults(func=cmd_proactive_brief)

    # friday proactive observe
    p = proactive_sub.add_parser("observe", help="Feed an observation to the learner")
    p.add_argument("action", nargs="*", help="Action type and optional context")
    p.add_argument("--app", type=str, default="", help="App class context")
    p.add_argument("--repo", type=str, default="", help="Git repo context")
    p.set_defaults(func=cmd_proactive_observe)


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Standalone entry point for `friday proactive`."""
    logging.basicConfig(level=logging.WARNING)

    parser = argparse.ArgumentParser(prog="friday proactive")
    subparsers = parser.add_subparsers(dest="proactive_command")
    build_proactive_parser(subparsers)
    args = parser.parse_args(argv)

    if hasattr(args, "func"):
        return args.func(args) or 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
