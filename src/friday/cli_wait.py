"""CLI for Persistent Watchers — ``friday wait`` commands.

Usage::

    friday wait list
    friday wait create <name> --type shell_exit_code --command "pytest"
    friday wait create <name> --type http_status --url "http://localhost:8080"
    friday wait create <name> --type file_modified --path "README.md"
    friday wait create <name> --type process_running --process "chromium"
    friday wait show <name>
    friday wait check [--all]
    friday wait delete <name>
"""

from __future__ import annotations

import argparse
import json
import sys

from .presentation.cli_format import header, green, red, yellow, gray, cyan


def cmd_wait(args: argparse.Namespace) -> int:
    """Dispatch ``friday wait <action>``."""
    from .db import connect
    from .watcher import WatcherEngine

    action = args.action or "list"
    conn = connect()

    try:
        eng = WatcherEngine(conn)

        if action == "list":
            return _cmd_list(eng, args)
        elif action == "create":
            return _cmd_create(eng, args)
        elif action == "show":
            return _cmd_show(eng, args)
        elif action == "check":
            return _cmd_check(eng, args)
        elif action == "delete":
            return _cmd_delete(eng, args)
        elif action == "ack":
            return _cmd_ack(eng, args)
        elif action == "context":
            return _cmd_context(eng, args)
        else:
            print(f"  Unknown action: {action}")
            print(gray("  Available: list, create, show, check, delete, ack, context"))
            return 2
    finally:
        conn.close()


def _cmd_list(eng, args: argparse.Namespace) -> int:
    """List all persistent watchers."""
    from .watcher import format_watchers

    watchers = eng.list_all()
    print(header("Persistent Watchers", f"{len(watchers)} watcher(s)"))
    print()
    print(format_watchers(watchers))
    return 0


def _cmd_create(eng, args: argparse.Namespace) -> int:
    """Create a new persistent watcher."""
    from .watcher import Watcher

    name: str = args.name
    condition_type: str = args.type or "shell_exit_code"
    check_interval: int = args.interval or 300

    # Build params based on type.
    params: dict = {}
    if condition_type == "shell_exit_code":
        if not args.command:
            print(red("  error: --command is required for shell_exit_code"))
            return 1
        params["command"] = args.command
        if args.timeout:
            params["timeout"] = args.timeout
    elif condition_type == "http_status":
        if not args.url:
            print(red("  error: --url is required for http_status"))
            return 1
        params["url"] = args.url
        if args.timeout:
            params["timeout"] = args.timeout
    elif condition_type == "file_modified":
        if not args.path:
            print(red("  error: --path is required for file_modified"))
            return 1
        params["path"] = args.path
    elif condition_type == "process_running":
        if not args.process:
            print(red("  error: --process (or --name) is required for process_running"))
            return 1
        params["process"] = args.process
    else:
        print(red(f"  error: Unknown condition type '{condition_type}'"))
        print(gray("  Valid: shell_exit_code, http_status, file_modified, process_running"))
        return 1

    try:
        watcher = eng.create(
            name=name,
            condition_type=condition_type,
            condition_params=params,
            check_interval_seconds=check_interval,
            repeat=bool(getattr(args, "repeat", False)),
        )
        print(green(f"  ✓ Watcher '{name}' created"))
        print(gray(f"    Type: {condition_type}"))
        print(gray(f"    Interval: {check_interval}s"))
        if args.repeat:
            print(gray(f"    Repeat: auto-re-arm after firing"))
        print(gray(f"    Run `friday wait check` to test it"))
        return 0
    except ValueError as exc:
        print(red(f"  error: {exc}"))
        return 1


def _cmd_show(eng, args: argparse.Namespace) -> int:
    """Show a single watcher in detail."""
    from .watcher import format_watcher

    watcher = eng.get(args.name)
    if watcher is None:
        print(red(f"  error: Watcher '{args.name}' not found"))
        return 1
    print(format_watcher(watcher, verbose=True))
    return 0


def _cmd_context(eng, args: argparse.Namespace) -> int:
    """Show screen-context watchers grouped by source.

    Groups watchers into:
      - Screen context ([auto] watchers for apps, URLs, clipboard)
      - Traditional watchers (user-created shell/file/http/process)
    """
    from .db import connect

    # Check if --global or --no-global was passed.
    set_global = getattr(args, "global_flag", None)
    if set_global:
        from .watcher import is_global_mode, set_global_mode
        conn = connect()
        try:
            if set_global == "on":
                set_global_mode(conn, True)
                print(green("  ✓ Global mode enabled — every app will be auto-watched."))
                print(gray("    Use `friday wait context --no-global` to disable."))
            elif set_global == "off":
                set_global_mode(conn, False)
                print(green("  ✓ Global mode disabled — tuning rules and defaults apply."))
            current = "enabled" if is_global_mode(conn) else "disabled"
            print(gray(f"    Current state: {current}"))
        finally:
            conn.close()
        return 0

    # Check if --stats was passed.
    if getattr(args, "stats", False):
        return _cmd_stats(eng, args)

    # Check if --tune was passed (tuning mode).
    tune_action = getattr(args, "tune", None)
    if tune_action:
        return _cmd_tune(eng, args, tune_action)

    from .watcher import format_watchers

    watchers = eng.list_all()
    auto_watchers = [w for w in watchers if w.name.startswith("[auto] ")]
    user_watchers = [w for w in watchers if not w.name.startswith("[auto] ")]

    # Group auto-watchers by context category.
    app_watchers = [w for w in auto_watchers if w.condition_type == "active_app"]
    url_watchers = [w for w in auto_watchers if w.condition_type == "window_title"]
    clip_watchers = [w for w in auto_watchers if w.condition_type == "clipboard_content"]
    other_auto = [w for w in auto_watchers if w not in app_watchers + url_watchers + clip_watchers]

    print(header("Context Watcher Dashboard",
                 f"{len(watchers)} total: {len(auto_watchers)} screen-context, "
                 f"{len(user_watchers)} traditional"))
    print()

    if auto_watchers:
        print(cyan("  ── Screen Context Watchers (auto, expire ~30min) ──"))
        print()

        if app_watchers:
            print(gray("  Active App Watchers:"))
            for w in app_watchers:
                status = "🔥 triggered" if w.last_result else "⏳ watching"
                print(f"    • {w.name.replace('[auto] ', ''):<40} {status}")
            print()

        if url_watchers:
            print(gray("  Browser URL Watchers:"))
            for w in url_watchers:
                status = "🔥 triggered" if w.last_result else "⏳ watching"
                print(f"    • {w.name.replace('[auto] ', ''):<40} {status}")
            print()

        if clip_watchers:
            print(gray("  Clipboard Watchers:"))
            for w in clip_watchers:
                status = "🔥 triggered" if w.last_result else "⏳ watching"
                print(f"    • {w.name.replace('[auto] ', ''):<40} {status}")
            print()

        if other_auto:
            print(gray("  Other Auto-Watchers:"))
            for w in other_auto:
                status = "🔥 triggered" if w.last_result else "⏳ watching"
                print(f"    • {w.name.replace('[auto] ', ''):<40} [{w.condition_type}] {status}")
            print()

    if user_watchers:
        print(cyan(f"  ── Traditional Watchers ({len(user_watchers)}) ──"))
        print()
        print(format_watchers(user_watchers))
        print()

    if not watchers:
        print(gray("  No watchers defined."))
        print(gray("  Run `friday wait create` or switch apps to auto-create context watchers."))
        print(gray("  Use `friday wait context --tune list` to see tuning rules."))

    return 0


def _cmd_tune(eng, args: argparse.Namespace, tune_action: str) -> int:
    """Manage auto-watcher tuning rules.

    Lets you configure which apps create auto-watchers and which are ignored.
    """
    conn = None
    try:
        from .db import connect
        from .watcher import (
            get_tuning_rules,
            set_tuning_rule,
            remove_tuning_rule,
            reset_tuning_defaults,
            DEFAULT_BROWSER_KEYWORDS,
            DEFAULT_IDE_KEYWORDS,
            DEFAULT_TERMINAL_KEYWORDS,
        )
        conn = connect()

        if tune_action == "list":
            rules = get_tuning_rules(conn)

            from .watcher import is_global_mode
            global_on = is_global_mode(conn)

            print(header("Auto-Watcher Tuning Rules",
                         f"{len(rules)} rule(s) set  |  "
                         f"Global: {'ON' if global_on else 'OFF'}"))
            print()

            if global_on:
                print(green("  🌍 Global mode is ENABLED — every app will be auto-watched."))
                print(gray("     Tuning rules below are BYPASSED until global mode is disabled."))
                print()

            if rules:
                print(f"  {'App Pattern':<30} {'Action':<12} {'Source':<12} {'Set At':<20}")
                print(f"  {'-'*30} {'-'*12} {'-'*12} {'-'*20}")
                for r in rules:
                    action_display = green("watch") if r["action"] == "watch" else red("ignore")
                    print(f"  {r['app']:<30} {action_display:<12} {r['source']:<12} {r['set_at'][:19]}")
            else:
                print(gray("  No custom tuning rules. Default behavior applies:"))

            print()
            print(cyan("  ── Default Categories ──"))
            print(f"  Browser apps:    {', '.join(_DEFAULT_BROWSER_KEYWORDS)}")
            print(f"  IDE apps:        {', '.join(_DEFAULT_IDE_KEYWORDS)}")
            print(f"  Terminal apps:   {', '.join(_DEFAULT_TERMINAL_KEYWORDS)}")
            print()
            print(gray("  Usage:"))
            print(gray("    friday wait context --tune add --app brave --action ignore"))
            print(gray("    friday wait context --tune add --app slack --action watch"))
            print(gray("    friday wait context --tune remove --app brave"))
            print(gray("    friday wait context --tune defaults"))
            return 0

        elif tune_action == "add":
            app = getattr(args, "app", "") or ""
            action = getattr(args, "action", "watch") or "watch"

            if not app:
                print(red("  error: --app is required for add"))
                return 1

            try:
                set_tuning_rule(conn, app, action)
                action_word = "watched" if action == "watch" else "ignored"
                print(green(f"  ✓ '{app}' will be {action_word} for auto-watchers"))
                print(gray(f"    Run `friday wait context --tune list` to see all rules."))
                return 0
            except ValueError as exc:
                print(red(f"  error: {exc}"))
                return 1

        elif tune_action == "remove":
            app = getattr(args, "app", "") or ""
            if not app:
                print(red("  error: --app is required for remove"))
                return 1

            removed = remove_tuning_rule(conn, app)
            if removed:
                print(green(f"  ✓ Removed tuning rule for '{app}'"))
            else:
                print(yellow(f"  ○ No tuning rule found for '{app}'"))
            return 0

        elif tune_action == "defaults":
            count = reset_tuning_defaults(conn)
            print(green(f"  ✓ Reset {count} tuning rule(s) to defaults"))
            print(gray("    Default category detection will now apply to all apps."))
            return 0

        else:
            print(red(f"  error: Unknown tune action '{tune_action}'"))
            print(gray("  Available: list, add, remove, defaults"))
            return 2

    except Exception as exc:
        print(red(f"  error: {exc}"))
        return 1
    finally:
        if conn:
            conn.close()


def _cmd_check(eng, args: argparse.Namespace) -> int:
    """Check all watchers (or force a specific one)."""
    from .watcher import format_watcher

    if args.name:
        # Check a specific watcher.
        watcher = eng.get(args.name)
        if watcher is None:
            print(red(f"  error: Watcher '{args.name}' not found"))
            return 1
        print(gray(f"  Checking '{args.name}'..."))
        result = eng.check_one(watcher)
    else:
        # Check all due watchers.
        print(gray("  Checking all due watchers..."))
        results = eng.check_all()
        if not results:
            print(gray("  No watchers due for check."))
            return 0
        for r in results:
            if r.get("triggered"):
                print(f"  {green('✓')} {r['watcher_name']} — TRIGGERED")
            elif r.get("met"):
                print(f"  {green('✓')} {r['watcher_name']} — condition met")
            else:
                err = r.get("error") or "condition not met"
                print(f"  {yellow('○')} {r['watcher_name']} — {err[:80]}")
        return 0

    # Print single watcher result.
    if result.get("triggered"):
        print(green(f"  ✓ {result['watcher_name']} — TRIGGERED! Condition met."))
    elif result.get("met"):
        print(green(f"  ✓ {result['watcher_name']} — condition met"))
    else:
        err = result.get("error") or "condition not met"
        print(yellow(f"  ○ {result['watcher_name']} — {err}"))

    return 0


def _cmd_ack(eng, args: argparse.Namespace) -> int:
    """Acknowledge a fired watcher (dismiss the notification)."""
    from .watcher import Watcher

    if not args.name:
        print(red("  error: specify a watcher name to acknowledge"))
        return 1

    watcher = eng.get(args.name)
    if watcher is None:
        print(red(f"  error: Watcher '{args.name}' not found"))
        return 1

    eng.acknowledge(args.name)
    print(green(f"  ✓ Watcher '{args.name}' acknowledged"))
    return 0


def _cmd_delete(eng, args: argparse.Namespace) -> int:
    """Delete a persistent watcher."""
    if not args.yes:
        resp = input(f"  Delete watcher '{args.name}'? [y/N] ").strip().lower()
        if resp != "y":
            print(gray("  Cancelled."))
            return 0

    deleted = eng.delete(args.name)
    if deleted:
        print(green(f"  ✓ Watcher '{args.name}' deleted"))
        return 0
    print(red(f"  error: Watcher '{args.name}' not found"))
    return 1


def _cmd_stats(eng, args: argparse.Namespace) -> int:
    """Show auto-watcher usage statistics.

    Displays:
      - Summary: active auto-watchers, triggered count, traditional watchers
      - Top apps by trigger count
      - Ignored apps (tuning rules set to ignore)
      - Global mode: enabled/disabled, duration
    """
    from .db import connect
    from .watcher import get_auto_watcher_stats

    conn = connect()
    try:
        stats = get_auto_watcher_stats(conn)

        print(header("Auto-Watcher Statistics",
                     f"{stats['active_auto_watchers']} active, "
                     f"{stats['triggered_count']} triggered, "
                     f"{stats['total_traditional']} traditional"))
        print()

        # ── Summary line ──
        gm = stats["global_mode"]
        gm_str = green("ENABLED") if gm["enabled"] else gray("disabled")
        if gm["enabled"] and gm["duration_hours"]:
            gm_str += gray(f"  ({gm['duration_hours']}h active)")
        print(f"  {'Auto-watchers:':<20} {stats['active_auto_watchers']}")
        print(f"  {'Triggered:':<20} {stats['triggered_count']}")
        print(f"  {'Traditional:':<20} {stats['total_traditional']}")
        print(f"  {'Global mode:':<20} {gm_str}")
        if gm["enabled_since"]:
            print(gray(f"  {'Enabled since:':<20} {gm['enabled_since'][:19]}"))
        print()

        # ── Top apps by trigger count ──
        by_app = stats["by_app"]
        if by_app:
            print(cyan("  ── Apps by Trigger Count ──"))
            print()
            print(f"  {'App':<30} {'Watchers':<12} {'Triggers':<12} {'Last Seen':<20}")
            print(f"  {'-'*30} {'-'*12} {'-'*12} {'-'*20}")
            for entry in by_app:
                trigger_str = green(str(entry["triggered"])) if entry["triggered"] else gray(str(entry["triggered"]))
                last = entry["last_seen"][:19] if entry["last_seen"] else "?"
                print(f"  {entry['app']:<30} {entry['total']:<12} {trigger_str:<12} {last}")
            print()
        else:
            print(gray("  No auto-watchers have been created yet."))
            print(gray("  Switch between apps to trigger context-aware watchers."))
            print()

        # ── Ignored apps ──
        ignored = stats["ignored_apps"]
        if ignored:
            print(red("  ── Tuned OFF (Ignored) ──"))
            print()
            for app in ignored:
                print(f"    🔕 {app}")
            print()
            print(gray("  To re-enable: friday wait context --tune add --app <name> --action watch"))
            print()

        # ── Tips ──
        print(gray("  Tips:"))
        print(gray("    friday wait context --tune list    — see all tuning rules"))
        print(gray("    friday wait context --global        — watch EVERY app"))
        print(gray("    friday screen --tunable            — live toggle apps on/off"))

    finally:
        conn.close()
    return 0


# ---------------------------------------------------------------------------
# Subparser registration
# ---------------------------------------------------------------------------


def add_subparser(sub) -> None:
    """Add the ``wait`` subcommand parser."""
    p = sub.add_parser(
        "wait",
        help="Persistent watchers — monitor conditions and notify when met.",
    )
    p.add_argument(
        "action", nargs="?", default="list",
        choices=["list", "create", "show", "check", "delete", "ack", "context"],
        help="Action (default: list).",
    )
    p.add_argument(
        "name", nargs="?", default=None,
        help="Watcher name (create/show/check/delete).",
    )
    # Type-specific options (create action).
    p.add_argument(
        "--type", "-t", type=str, default=None,
        choices=["shell_exit_code", "http_status", "file_modified", "process_running"],
        help="Condition type (create action).",
    )
    p.add_argument(
        "--command", "-c", type=str, default=None,
        help="Shell command for shell_exit_code (e.g. 'pytest').",
    )
    p.add_argument(
        "--url", "-u", type=str, default=None,
        help="URL for http_status (e.g. 'http://localhost:8080').",
    )
    p.add_argument(
        "--path", "-p", type=str, default=None,
        help="File path for file_modified (e.g. 'README.md').",
    )
    p.add_argument(
        "--process", "--name", type=str, default=None,
        help="Process name for process_running (e.g. 'chromium').",
    )
    p.add_argument(
        "--interval", "-i", type=int, default=300,
        help="Check interval in seconds (default: 300).",
    )
    p.add_argument(
        "--timeout", type=int, default=None,
        help="Timeout per check in seconds (default: type-specific).",
    )
    p.add_argument(
        "--repeat", action="store_true",
        help="Auto-re-arm after firing (recurring watcher).",
    )
    p.add_argument(
        "--yes", "-y", action="store_true",
        help="Skip confirmation prompt (delete action).",
    )
    p.add_argument(
        "--global", "-g", dest="global_flag", nargs="?", const="on",
        choices=["on", "off"],
        help="Enable/disable global auto-watcher mode (bypasses all tuning). "
             "'on' watches every app, 'off' uses tuning rules.",
    )
    p.add_argument(
        "--no-global", "-G", dest="global_flag", action="store_const", const="off",
        help="Disable global auto-watcher mode.",
    )
    p.add_argument(
        "--tune", nargs="?", const="list", default=None,
        choices=["list", "add", "remove", "defaults"],
        help="Manage auto-watcher tuning rules (list/add/remove/defaults). "
             "Use with --app and --action.",
    )
    p.add_argument(
        "--app", type=str, default=None,
        help="App name/pattern to tune (e.g. 'brave', 'code', 'slack').",
    )
    p.add_argument(
        "--action", type=str, choices=["watch", "ignore"], default="watch",
        help="Action for tuning rule: 'watch' or 'ignore' (default: watch).",
    )
    p.add_argument(
        "--stats", "-s", action="store_true",
        help="Show auto-watcher usage statistics (trigger counts, ignored apps, global mode).",
    )
    p.set_defaults(func=cmd_wait)
