"""CLI commands for `friday6 abort` — the kill switch (Wave 5).

``friday6 abort`` stops a runaway agent mid-session: it arms a durable
kill switch that the Claude bridge's tool hook checks FIRST — every
tool call is denied immediately (no ask recorded) and new ``CLAUDE:``
prompts are refused until cleared. ``friday6 abort --clear`` disarms;
``friday6 abort --status`` shows the current state.

Same conventions as the other V6 CLIs: exit codes, ``--json`` purity,
``--flag`` for the flag-file path (tests stay hermetic — never the
real ``~/.friday`` state).

Safety law: the flag file is the *source of truth* — it is read by
every process (bridge hook, CLI, daemon), so the operator's override
survives restarts and is visible on every surface.
"""

from __future__ import annotations

import argparse
import json
import logging

logger = logging.getLogger("friday_v6.cli_abort")

_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_CYAN = "\033[96m"
_GREEN = "\033[92m"
_RED = "\033[91m"

EXIT_OK = 0
EXIT_FAILED = 1


def _switch(args):
    from .abort import KillSwitch
    return KillSwitch(path=getattr(args, "flag", None),
                      db_path=getattr(args, "db", None))


def cmd_abort(args: argparse.Namespace) -> int:
    """`friday6 abort [reason...]` — arm the kill switch + stop the bridge."""
    reason = " ".join(getattr(args, "reason", []) or [])
    switch = _switch(args)
    if args.clear:
        was = switch.clear()
        if args.json:
            print(json.dumps({"aborted": False, "cleared": True,
                              "was_armed": was}))
        else:
            if was:
                print(f"  {_GREEN}✓{_RESET} Kill switch cleared — Friday may "
                      f"resume agent work.")
            else:
                print(f"  {_DIM}· Kill switch was not armed.{_RESET}")
        return EXIT_OK
    if args.status:
        st = switch.status()
        if args.json:
            print(json.dumps(st))
        else:
            if st["armed"]:
                reason = f" — {st['reason']}" if st.get("reason") else ""
                since = f"  {_DIM}(since {st['at']}){_RESET}" if st.get("at") \
                    else ""
                print(f"  {_RED}⛔ ARMED{_RESET}{reason}{since}")
            else:
                print(f"  {_GREEN}○ not armed{_RESET} — the agent bridge may "
                      f"run tool calls normally.")
        return EXIT_OK
    # Arm — this is the operator's explicit override.
    try:
        from .abort import abort_now
        newly = abort_now(reason, path=getattr(args, "flag", None),
                          db_path=getattr(args, "db", None))
    except Exception as exc:
        logger.warning(f"abort failed: {exc}")
        newly = _switch(args).arm(reason)
    st = _switch(args).status()
    if args.json:
        print(json.dumps(st))
        return EXIT_OK
    if newly:
        detail = f" ({reason})" if reason else ""
        print(f"  {_RED}⛔ ABORT{_RESET} — kill switch armed. Friday stops "
              f"the agent session and denies every tool call{detail}.")
        print(f"  {_DIM}Clear with: friday6 abort --clear{_RESET}")
    else:
        detail = f" — {st.get('reason', '')}" if st.get("reason") else ""
        print(f"  {_RED}⛔ Already armed{_RESET}{detail}.")
    return EXIT_OK


def build_abort_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "abort", help="Kill switch — stop a runaway agent mid-session",
        description="Arm Friday's durable kill switch: the Claude bridge "
                    "denies every tool call and refuses new prompts until "
                    "cleared. The operator's override — one command, every "
                    "surface.",
    )
    parser.add_argument("reason", nargs="*", default=[],
                        help="Optional reason, shown in status (e.g. "
                             "'stop the deploy')")
    parser.add_argument("--clear", action="store_true",
                        help="Disarm the kill switch")
    parser.add_argument("--status", action="store_true",
                        help="Show whether the switch is armed")
    parser.add_argument("--flag", type=str, default=None,
                        help="Kill-switch flag file (default "
                             "~/.friday/v6_abort.json)")
    parser.add_argument("--db", type=str, default=None,
                        help="SQLite DB for the ambient event (default "
                             "~/.friday/v4.db; tests pass a tmp path)")
    parser.add_argument("--json", action="store_true",
                        help="Machine-readable output")
    parser.set_defaults(func=cmd_abort)


def main(argv: list[str] | None = None) -> int:
    """Standalone entry point for `python -m friday_v6.cli_abort`."""
    logging.basicConfig(level=logging.WARNING)
    parser = argparse.ArgumentParser(prog="friday6 abort")
    build_abort_parser(parser)
    args = parser.parse_args(argv)
    if hasattr(args, "func"):
        return args.func(args) or 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
