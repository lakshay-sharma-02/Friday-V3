"""CLI commands for `friday6 skills` — learned workflows, shadow-first (Wave 10 §3.4).

The text surface for the skills layer: Friday learns a workflow from
your real executed actions (audit log), runs it in shadow mode (records
what it *would* do — never executes), and promotes it only after N
shadow matches + your explicit approval.

Usage:
    friday6 skills list                    # all skills + state
    friday6 skills learn                   # form shadow skills from patterns
    friday6 skills shadow                  # record shadow matches now
    friday6 skills promote <name>          # operator approval → promoted
    friday6 skills status                  # summary counts by state
    friday6 skills watch [name]            # Wave 14: watch me do this
    friday6 skills watch-stop [name]       # Wave 14: learn this → skill
    friday6 skills noticed                 # Wave 14: 'I noticed you do this every time'
    friday6 skills dispatch                # next-step suggestions on context match
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger("friday_v6.cli_skills")

_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_CYAN = "\033[96m"
_GREEN = "\033[92m"
_YELLOW = "\033[93m"
_RED = "\033[91m"

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 3

_STATE_ICONS = {
    "shadow": f"{_DIM}shadow{_RESET}",
    "verified": f"{_YELLOW}verified{_RESET}",
    "promoted": f"{_GREEN}promoted{_RESET}",
    "demoted": f"{_RED}demoted{_RESET}",
}


def _print_logo(title: str = "Skills"):
    print()
    print(f"  {_BOLD}{_CYAN}◆ FRIDAY{_RESET} {_DIM}V6 — {title}{_RESET}")
    print(f"  {_DIM}{'─' * 40}{_RESET}")


def _resolve_db(args) -> Optional[object]:
    try:
        from . import db
        return db.connect(path=getattr(args, "db", None))
    except Exception as exc:
        logger.debug(f"skills: db unavailable ({exc})")
        return None


def _close_db(conn) -> None:
    try:
        if conn is not None:
            conn.close()
    except Exception:
        pass


#: Active screen demo recorders by watch id (Wave 23). ``skills
#: watch`` starts one sampler alongside the WatchRecorder so a demo
#: captures the on-screen flow; ``skills watch-stop`` stops them all
#: before the skill forms. Never-crash: a stuck sampler is skipped.
_screen_recorders: dict = {}


def _start_screen_recorder(watch_id: str, conn) -> Optional[object]:
    """Best-effort screen sampler for a watch (never raises)."""
    try:
        from .screen.recorder import ScreenDemoRecorder
        recorder = ScreenDemoRecorder(watch_id, conn=conn)
        recorder.start()
        _screen_recorders[watch_id] = recorder
        return recorder
    except Exception as exc:
        logger.debug(f"screen recorder start failed: {exc}")
        return None


def _stop_screen_recorders() -> None:
    """Stop every active screen sampler (final observation captured)."""
    for wid, recorder in list(_screen_recorders.items()):
        try:
            recorder.stop()
        except Exception as exc:
            logger.debug(f"screen recorder stop failed: {exc}")
    _screen_recorders.clear()


def _skill_lines(skills) -> list[str]:
    lines = []
    for s in skills:
        state = _STATE_ICONS.get(s.verification_state, s.verification_state)
        lines.append(
            f"  {_CYAN}●{_RESET} {s.name} {_DIM}— {state} · conf "
            f"{s.confidence:.2f} · {len(s.steps)} step(s) · "
            f"{s.shadow_matches} shadow match(es)"
            f"{', ' + str(s.failure_count) + ' fail(s)' if s.failure_count else ''}"
            f"{_RESET}")
    return lines


def cmd_skills_list(args: argparse.Namespace) -> int:
    """`friday6 skills list` — all skills with verification state."""
    conn = _resolve_db(args)
    try:
        from .skills import SkillRegistry
        skills = SkillRegistry(conn).list(limit=100)
    except Exception as exc:
        print(f"  {_RED}✗ could not list skills: {exc}{_RESET}")
        return EXIT_FAILED
    finally:
        _close_db(conn)

    if args.json:
        print(json.dumps([{
            "name": s.name, "state": s.verification_state,
            "confidence": s.confidence, "steps": len(s.steps),
            "shadow_matches": s.shadow_matches,
            "failures": s.failure_count,
        } for s in skills], default=str))
        return EXIT_OK

    _print_logo()
    if not skills:
        print(f"  {_DIM}No skills yet — run 'friday6 skills learn' to form "
              f"one from your patterns.{_RESET}")
        print()
        return EXIT_OK
    print(f"  {_BOLD}{len(skills)} skill(s){_RESET}")
    print(f"  {_DIM}{'─' * 40}{_RESET}")
    for line in _skill_lines(skills):
        print(line)
    print()
    return EXIT_OK


def cmd_skills_learn(args: argparse.Namespace) -> int:
    """`friday6 skills learn` — form shadow skills from the audit log."""
    conn = _resolve_db(args)
    try:
        from .skills import ReplayExecutor
        formed = ReplayExecutor(conn).learn(prefix=args.prefix or "")
    except Exception as exc:
        print(f"  {_RED}✗ could not learn skills: {exc}{_RESET}")
        return EXIT_FAILED
    finally:
        _close_db(conn)

    if args.json:
        print(json.dumps([{
            "name": s.name, "state": s.verification_state,
            "steps": len(s.steps),
        } for s in formed], default=str))
        return EXIT_OK

    _print_logo("Skills · Learn")
    if not formed:
        print(f"  {_DIM}No repeated patterns found yet — keep working and "
              f"Friday will watch for them.{_RESET}")
        print()
        return EXIT_OK
    print(f"  {_GREEN}✓ Formed {len(formed)} shadow skill(s) from your "
          f"patterns:{_RESET}")
    for line in _skill_lines(formed):
        print(line)
    print(f"  {_DIM}  Skills start in shadow mode — they never execute until "
          f"verified + promoted.{_RESET}")
    print()
    return EXIT_OK


def cmd_skills_shadow(args: argparse.Namespace) -> int:
    """`friday6 skills shadow` — record shadow matches now (no execution)."""
    conn = _resolve_db(args)
    try:
        from .skills import ShadowExecutor
        matches = ShadowExecutor(conn).sweep()
    except Exception as exc:
        print(f"  {_RED}✗ shadow sweep failed: {exc}{_RESET}")
        return EXIT_FAILED
    finally:
        _close_db(conn)

    if args.json:
        print(json.dumps(matches, default=str))
        return EXIT_OK

    _print_logo("Skills · Shadow")
    if not matches:
        print(f"  {_DIM}No shadow matches right now — the skills observed "
              f"nothing matching.{_RESET}")
        print()
        return EXIT_OK
    print(f"  {_GREEN}✓ {len(matches)} shadow match(es) recorded{_RESET} "
          f"{_DIM}(nothing was executed){_RESET}")
    for m in matches:
        would = ", ".join(
            f"{s['action_type']}:{s['command']}" for s in m["would_do"]
        ) or "—"
        print(f"  {_CYAN}●{_RESET} {m['skill_name']} would do: {would}")
    print()
    return EXIT_OK


def cmd_skills_promote(args: argparse.Namespace) -> int:
    """`friday6 skills promote <name>` — the operator-approval step."""
    name = (args.name or "").strip()
    if not name:
        print(f"  {_RED}✗ give me a skill name, e.g. "
              f"'friday6 skills promote run-tests'.{_RESET}")
        return EXIT_USAGE
    conn = _resolve_db(args)
    try:
        from .skills import SkillRegistry
        reg = SkillRegistry(conn)
        skill = reg.get(name)
        if skill is None:
            print(f"  {_RED}✗ no skill named '{name}'.{_RESET}")
            return EXIT_FAILED
        if skill.verification_state != "verified":
            print(f"  {_YELLOW}⚠ '{name}' is {skill.verification_state} — only "
                  f"verified skills can be promoted. Run 'friday6 skills "
                  f"shadow' until it verifies.{_RESET}")
            return EXIT_FAILED
        ok = reg.promote(skill.id)
    except Exception as exc:
        print(f"  {_RED}✗ could not promote: {exc}{_RESET}")
        return EXIT_FAILED
    finally:
        _close_db(conn)

    if not ok:
        print(f"  {_RED}✗ promotion failed.{_RESET}")
        return EXIT_FAILED
    if args.json:
        print(json.dumps({"name": name, "state": "promoted"}, default=str))
        return EXIT_OK
    _print_logo("Skills · Promote")
    print(f"  {_GREEN}✓ '{name}' promoted — Friday may now suggest it when "
          f"your context matches (never without your approval).{_RESET}")
    print()
    return EXIT_OK


def cmd_skills_status(args: argparse.Namespace) -> int:
    """`friday6 skills status` — summary counts by state."""
    conn = _resolve_db(args)
    try:
        from .skills import SkillRegistry
        skills = SkillRegistry(conn).list(limit=100000)
    except Exception as exc:
        print(f"  {_RED}✗ could not read skills: {exc}{_RESET}")
        return EXIT_FAILED
    finally:
        _close_db(conn)

    counts = {}
    for s in skills:
        counts[s.verification_state] = counts.get(s.verification_state, 0) + 1
    payload = {
        "total": len(skills),
        "shadow": counts.get("shadow", 0),
        "verified": counts.get("verified", 0),
        "promoted": counts.get("promoted", 0),
        "demoted": counts.get("demoted", 0),
    }
    if args.json:
        print(json.dumps(payload, default=str))
        return EXIT_OK

    _print_logo("Skills · Status")
    print(f"  {_BOLD}Total{_RESET}   {payload['total']}")
    print(f"  {_DIM}shadow{_RESET}   {payload['shadow']}")
    print(f"  {_YELLOW}verified{_RESET} {payload['verified']}")
    print(f"  {_GREEN}promoted{_RESET} {payload['promoted']}")
    print(f"  {_RED}demoted{_RESET}  {payload['demoted']}")
    print()
    return EXIT_OK


def cmd_skills_watch(args: argparse.Namespace) -> int:
    """`friday6 skills watch` — start an explicit demonstration capture.

    Wave 23: alongside the WatchRecorder, Friday also samples the
    screen (screenshot + OCR) while you demonstrate, so the formed
    skill carries the on-screen flow — not just the audited commands.
    Best-effort: no screen tools → an honest note, never a crash.
    """
    conn = _resolve_db(args)
    try:
        from .skills import WatchRecorder
        watcher = WatchRecorder(conn)
        previous = watcher.active()
        wid = watcher.start(name=args.name or "", note=args.note or "")
    except Exception as exc:
        print(f"  {_RED}✗ could not start watching: {exc}{_RESET}")
        return EXIT_FAILED
    finally:
        _close_db(conn)

    if not wid:
        print(f"  {_RED}✗ could not start watching.{_RESET}")
        return EXIT_FAILED

    # Wave 23 — the screen side of the demonstration. Honest when the
    # tools are missing (``--no-screen`` opts out for hermetic runs).
    screen_started = False
    screen_note = ""
    if not getattr(args, "no_screen", False):
        recorder = _start_screen_recorder(wid, conn)
        if recorder is not None and recorder.available:
            screen_started = True
        else:
            screen_note = " (screen capture unavailable — I'll watch " \
                          "your commands only)"

    if args.json:
        payload = {"watch_id": wid, "name": args.name or "",
                   "started": True, "screen": screen_started}
        if previous:
            payload["replaced_watch"] = previous.get("id")
        print(json.dumps(payload, default=str))
        return EXIT_OK

    _print_logo("Skills · Watch")
    if previous:
        print(f"  {_YELLOW}⚠ closed the previous watch"
              f" ({previous.get('id', '')[:8]}).{_RESET}")
    name = f" as '{args.name}'" if args.name else ""
    print(f"  {_GREEN}● Watching{name}{screen_note} — go ahead. When you're "
          f"done, run 'friday6 skills watch-stop'.{_RESET}")
    print()
    return EXIT_OK


def cmd_skills_watch_stop(args: argparse.Namespace) -> int:
    """`friday6 skills watch-stop` — form a skill from what was watched.

    Wave 23: the screen sampler is stopped BEFORE the skill forms so
    the final observation is captured — the skill's screen-context
    steps end on what the demo ended with.
    """
    conn = _resolve_db(args)
    try:
        from .skills import WatchRecorder
        watcher = WatchRecorder(conn)
        active = watcher.active()
        # Stop the screen sampler first (captures the final state).
        _stop_screen_recorders()
        formed = watcher.stop(name=args.name or "")
    except Exception as exc:
        print(f"  {_RED}✗ could not finish watching: {exc}{_RESET}")
        return EXIT_FAILED
    finally:
        _close_db(conn)

    if formed is None:
        if args.json:
            print(json.dumps({"formed": False, "active": bool(active)},
                             default=str))
            return EXIT_OK
        if not active:
            print(f"  {_YELLOW}⚠ I wasn't watching anything — run "
                  f"'friday6 skills watch' first.{_RESET}")
        else:
            print(f"  {_YELLOW}⚠ I didn't catch any actions to learn from "
                  f"— the watch is closed.{_RESET}")
        print()
        return EXIT_OK

    skill = formed["skill"]
    count = formed.get("actions", 0)
    screen_steps = [s for s in skill.steps
                    if s.get("action_type") == "screen"]
    if args.json:
        print(json.dumps({
            "formed": True,
            "name": skill.name,
            "state": skill.verification_state,
            "steps": len(skill.steps),
            "actions_watched": count,
            "screen_observations": len(screen_steps),
        }, default=str))
        return EXIT_OK

    _print_logo("Skills · Watch")
    print(f"  {_GREEN}✓ Learned from {count} watched action(s):{_RESET}")
    for line in _skill_lines([skill]):
        print(line)
    if screen_steps:
        print(f"  {_DIM}  …with {len(screen_steps)} screen observation(s) — "
              f"what the screen showed during the demo.{_RESET}")
    print(f"  {_DIM}  Shadow mode — never executes until verified + "
          f"promoted.{_RESET}")
    print()
    return EXIT_OK


def cmd_skills_noticed(args: argparse.Namespace) -> int:
    """`friday6 skills noticed` — 'I noticed you do this every time'."""
    conn = _resolve_db(args)
    try:
        from .skills import RepetitionNoticer
        offers = RepetitionNoticer(conn).notice()
    except Exception as exc:
        print(f"  {_RED}✗ could not scan for patterns: {exc}{_RESET}")
        return EXIT_FAILED
    finally:
        _close_db(conn)

    if args.json:
        print(json.dumps(offers, default=str))
        return EXIT_OK

    _print_logo("Skills · Noticed")
    if not offers:
        print(f"  {_DIM}Nothing new to notice — keep working and I'll watch "
              f"for patterns you haven't taught me yet.{_RESET}")
        print()
        return EXIT_OK
    print(f"  {_CYAN}◉ I noticed you do this every time:{_RESET}")
    for offer in offers:
        print(f"  {_CYAN}●{_RESET} {offer.get('offer', '')}")
        print(f"    {_DIM}→ 'friday6 skills watch' then do it, or say "
              f"'learn this'.{_RESET}")
    print()
    return EXIT_OK


def cmd_skills_dispatch(args: argparse.Namespace) -> int:
    """`friday6 skills dispatch` — next-step suggestions on context match.

    Promoted skills may *suggest* their next step when the operator's
    current activity matches the skill's trigger. Read-only: nothing is
    executed here — the confirm gate stays in the execution layer.
    """
    conn = _resolve_db(args)
    try:
        from .skills import SkillDispatcher
        suggestions = SkillDispatcher(conn).suggest()
    except Exception as exc:
        print(f"  {_RED}✗ could not check dispatch: {exc}{_RESET}")
        return EXIT_FAILED
    finally:
        _close_db(conn)

    if args.json:
        print(json.dumps(suggestions, default=str))
        return EXIT_OK

    _print_logo("Skills · Dispatch")
    if not suggestions:
        print(f"  {_DIM}No promoted skill matches your current context "
              f"right now.{_RESET}")
        print()
        return EXIT_OK
    for s in suggestions:
        name = s.get("skill_name", "?")
        nexts = s.get("next_steps") or []
        chain = " → ".join(
            f"{step.get('action_type', '?')}:{step.get('command', '')}"
            for step in nexts) or "—"
        print(f"  {_GREEN}◉{_RESET} {name} — next: {chain}"
              f" {_DIM}(pending approval){_RESET}")
    print()
    return EXIT_OK


def cmd_skills_md_list(args: argparse.Namespace) -> int:
    """`friday6 skills md list` — bundled/operator SKILL.md files."""
    try:
        from .skills.markdown import MarkdownSkillLibrary
        skills = MarkdownSkillLibrary().list()
    except Exception as exc:
        print(f"  {_RED}✗ could not list markdown skills: {exc}{_RESET}")
        return EXIT_FAILED

    if args.json:
        print(json.dumps([s.to_dict() for s in skills], default=str))
        return EXIT_OK

    _print_logo("Skills · Markdown")
    if not skills:
        print(f"  {_DIM}No markdown skills found — drop a SKILL.md into "
              f"~/.friday/v6_skills/<name>/.{_RESET}")
        print()
        return EXIT_OK
    print(f"  {_BOLD}{len(skills)} markdown skill(s){_RESET}")
    print(f"  {_DIM}{'─' * 40}{_RESET}")
    for s in skills:
        source = f"{_CYAN}●{_RESET}" if s.source == "bundled" else \
            f"{_GREEN}●{_RESET} [operator]"
        print(f"  {source} {_BOLD}{s.name}{_RESET} {_DIM}— {s.description}{_RESET}")
    print(f"\n  {_DIM}Say 'use the <name> skill …' to run one (via Claude "
          f"Code when the bridge is available).{_RESET}")
    print()
    return EXIT_OK


def cmd_skills_md_show(args: argparse.Namespace) -> int:
    """`friday6 skills md show <name>` — the full SKILL.md body."""
    name = (args.name or "").strip()
    try:
        from .skills.markdown import MarkdownSkillLibrary
        skill = MarkdownSkillLibrary().get(name)
    except Exception as exc:
        print(f"  {_RED}✗ could not read skill: {exc}{_RESET}")
        return EXIT_FAILED
    if skill is None:
        print(f"  {_RED}✗ no markdown skill named '{name}'.{_RESET}")
        return EXIT_USAGE
    if args.json:
        print(json.dumps({**skill.to_dict(), "body": skill.body},
                         default=str))
        return EXIT_OK
    _print_logo(f"Skills · {skill.name}")
    print(f"  {_BOLD}{skill.name}{_RESET} {_DIM}— {skill.description}{_RESET}")
    print(f"  {_DIM}{'─' * 40}{_RESET}")
    for line in skill.body.splitlines():
        print(f"  {line}")
    print()
    return EXIT_OK


def cmd_skills_md_match(args: argparse.Namespace) -> int:
    """`friday6 skills md match <text>` — which skill would route.

    The deterministic floor's answer for an utterance: the best
    description-token match, or nothing. Lets the operator see exactly
    how Friday routes without the Claude bridge.
    """
    text = " ".join(args.text or []).strip()
    if not text:
        print(f"  {_RED}✗ give me some text, e.g. "
              f"'friday6 skills md match add standup to my agenda'.{_RESET}")
        return EXIT_USAGE
    try:
        from .skills.markdown import MarkdownSkillLibrary
        lib = MarkdownSkillLibrary()
        explicit = lib.explicit_name(text)
        matched = explicit or lib.match(text)
    except Exception as exc:
        print(f"  {_RED}✗ could not match: {exc}{_RESET}")
        return EXIT_FAILED
    if args.json:
        print(json.dumps({
            "text": text,
            "match": matched.to_dict() if matched else None,
            "explicit": explicit.to_dict() if explicit else None,
        }, default=str))
        return EXIT_OK
    _print_logo("Skills · Match")
    if matched is None:
        print(f"  {_DIM}No markdown skill matched that text.{_RESET}")
        print()
        return EXIT_OK
    via = "explicit" if explicit else "description"
    print(f"  {_GREEN}✓ {_BOLD}{matched.name}{_RESET} {_DIM}(via {via}){_RESET}")
    print(f"  {_DIM}  {matched.description}{_RESET}")
    print()
    return EXIT_OK


def _add_skills_commands(subparsers) -> None:
    p = subparsers.add_parser("list", help="List all skills")
    p.add_argument("--db", type=Path, default=None)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_skills_list)

    p = subparsers.add_parser("learn", help="Form shadow skills from patterns")
    p.add_argument("--prefix", type=str, default="",
                   help="Optional name prefix, e.g. --prefix run-tests")
    p.add_argument("--db", type=Path, default=None)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_skills_learn)

    p = subparsers.add_parser("shadow", help="Record shadow matches now")
    p.add_argument("--db", type=Path, default=None)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_skills_shadow)

    p = subparsers.add_parser("promote",
                              help="Approve a verified skill for dispatch")
    p.add_argument("name", help="Skill name to promote")
    p.add_argument("--db", type=Path, default=None)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_skills_promote)

    p = subparsers.add_parser("status", help="Summary by verification state")
    p.add_argument("--db", type=Path, default=None)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_skills_status)

    p = subparsers.add_parser("watch",
                              help="Watch me do this (demonstration capture)")
    p.add_argument("name", nargs="?", default="",
                   help="Optional skill-name hint, e.g. 'deploy routine'")
    p.add_argument("--note", type=str, default="", help="Optional note")
    p.add_argument("--no-screen", action="store_true",
                   help="Skip the screen sampler (hermetic runs)")
    p.add_argument("--db", type=Path, default=None)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_skills_watch)

    p = subparsers.add_parser("watch-stop",
                              help="Learn this — form a skill from the watch")
    p.add_argument("name", nargs="?", default="",
                   help="Optional skill-name override")
    p.add_argument("--db", type=Path, default=None)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_skills_watch_stop)

    p = subparsers.add_parser("noticed",
                              help="'I noticed you do this every time' offers")
    p.add_argument("--db", type=Path, default=None)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_skills_noticed)

    p = subparsers.add_parser(
        "dispatch", help="Suggest next step on context match (read-only)")
    p.add_argument("--db", type=Path, default=None)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_skills_dispatch)

    # Wave 2 — V5's markdown skills: SKILL.md files (bundled + operator
    # ~/.friday/v6_skills/) with frontmatter + body. `md list` discovers,
    # `md show` prints the how-to, `md match` shows the deterministic
    # routing floor for an utterance.
    md_parser = subparsers.add_parser(
        "md", help="Markdown skills (SKILL.md files, Wave 2)",
        description="The V5 markdown skills: discoverable SKILL.md "
                    "files (bundled + ~/.friday/v6_skills/) with "
                    "frontmatter + how-to body.",
    )
    md_sub = md_parser.add_subparsers(dest="md_command")

    p = md_sub.add_parser("list", help="List markdown skills")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_skills_md_list)

    p = md_sub.add_parser("show", help="Show a skill's SKILL.md body")
    p.add_argument("name", help="Skill name")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_skills_md_show)

    p = md_sub.add_parser("match",
                          help="Which skill routes for some text")
    p.add_argument("text", nargs="+", help="The utterance to route")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_skills_md_match)


def build_skills_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "skills", help="Learned workflows (shadow-first)",
        description="Skills formed from your real patterns — shadow mode "
                    "first, promotion only after verification + approval.",
    )
    skills_sub = parser.add_subparsers(dest="skills_command")
    _add_skills_commands(skills_sub)


def main(argv: list[str] | None = None) -> int:
    """Standalone entry point for `python -m friday_v6.cli_skills`."""
    logging.basicConfig(level=logging.WARNING)
    parser = argparse.ArgumentParser(prog="friday6 skills")
    sub = parser.add_subparsers(dest="command")
    _add_skills_commands(sub)

    args = parser.parse_args(argv)
    if hasattr(args, "func"):
        return args.func(args) or 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
