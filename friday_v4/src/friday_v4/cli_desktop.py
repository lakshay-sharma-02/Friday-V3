"""CLI commands for `friday4 desktop` — desktop awareness & control.

Usage:
    friday4 desktop status              # Show full desktop status
    friday4 desktop windows             # List open windows
    friday4 desktop switch <ws>         # Switch to workspace
    friday4 desktop focus <app>         # Focus an app (natural name or class)
    friday4 desktop launch <app>        # Launch an application
    friday4 desktop screenshot          # Take a screenshot
    friday4 desktop notify <msg>        # Send a desktop notification
    friday4 desktop platforms           # List supported platforms + current DE
    friday4 desktop tray                # Start the system tray icon
    friday4 desktop hotkeys             # Register global hotkeys
    friday4 desktop watch               # Ambient feed → desktop notifications
    friday4 desktop daemon              # Tray + hotkeys + notifier, together
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - import-time only
    from .desktop.wm_abstraction import WindowManager

logger = logging.getLogger("friday_v4.cli_desktop")

_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_GREEN = "\033[92m"
_YELLOW = "\033[93m"
_CYAN = "\033[96m"
_RED = "\033[91m"


def _print_logo():
    print()
    print(f"  {_BOLD}{_CYAN}◆ FRIDAY{_RESET} {_DIM}V4 — Desktop{_RESET}")
    print(f"  {_DIM}{'─' * 40}{_RESET}")
    print()


def _get_wm() -> "WindowManager":
    """Lazily construct a WindowManager facade."""
    from .desktop.wm_abstraction import WindowManager
    return WindowManager()


def _require_desktop(wm) -> bool:
    """Print an error and return False if the desktop is unavailable."""
    if not wm.is_available:
        print(f"  {_RED}✗ Desktop not available ({wm.desktop_environment}){_RESET}")
        print(f"  {_DIM}  Try: friday4 desktop platforms{_RESET}")
        return False
    return True


def _launch_voice_session() -> None:
    """Open an interactive voice session in a new terminal window.

    Used by the tray "🎙 Start voice" menu item and the push-to-talk
    hotkey. Spawns ``friday4 talk`` in the first available terminal
    emulator; falls back to a desktop notification if none is found.
    """
    import shutil
    import subprocess

    cmd = [sys.executable, "-m", "friday_v4.cli_talk", "talk", "--push-to-talk"]
    terminals = [
        ("kitty", ["kitty", "--", *cmd]),
        ("alacritty", ["alacritty", "-e", *cmd]),
        ("wezterm", ["wezterm", "start", "--", *cmd]),
        ("gnome-terminal", ["gnome-terminal", "--", *cmd]),
        ("x-terminal-emulator", ["x-terminal-emulator", "-e", *cmd]),
        ("xterm", ["xterm", "-e", *cmd]),
    ]
    for name, argv in terminals:
        if shutil.which(name):
            try:
                subprocess.Popen(
                    argv,
                    start_new_session=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                logger.info(f"Opened voice session via {name}")
                return
            except OSError as exc:
                logger.debug(f"Failed to launch voice via {name}: {exc}")

    print(f"  {_DIM}  🎙 Voice: run `friday4 talk` in a terminal{_RESET}")
    try:
        from .desktop.wm_abstraction import WindowManager
        WindowManager.notify(
            "Friday", "Voice: run `friday4 talk` in a terminal", "normal"
        )
    except Exception:
        pass


def _show_status() -> None:
    """Print a one-line desktop status and raise a notification.

    Shared by the tray menu and the status hotkey.
    """
    try:
        wm = _get_wm()
        status = wm.get_status()
        active = status.get("active_window")
        if active:
            title = (active.get("title") or "")[:40]
            line = f"Active: {active.get('app_class')} — {title}"
        else:
            line = (f"{wm.desktop_environment}: "
                    f"{len(status.get('windows', []))} windows")
        print(f"  {_CYAN}📊 {line}{_RESET}")
        try:
            from .desktop.wm_abstraction import WindowManager
            WindowManager.notify("Friday — Status", line, "normal")
        except Exception:
            pass
    except Exception:
        pass


# ---------------------------------------------------------------------------
# CLI Commands
# ---------------------------------------------------------------------------


def cmd_desktop_status(args: argparse.Namespace) -> int:
    """Show full desktop status."""
    wm = _get_wm()
    _print_logo()

    if not _require_desktop(wm):
        return 1

    status = wm.get_status()

    # Monitors
    for mon in status.get("monitors", []):
        print(f"  {_BOLD}Monitor:{_RESET} {mon['name']} "
              f"({mon['width']}x{mon['height']} @ {mon.get('refresh_rate', 60)}Hz)")
        if mon['is_active']:
            print(f"  {_DIM}  Active workspace: {mon.get('active_workspace', '?')}{_RESET}")

    print()

    # Workspaces
    workspaces = status.get("workspaces", [])
    print(f"  {_BOLD}Workspaces:{_RESET}  {len(workspaces)} total")
    for ws in workspaces:
        active_mark = "●" if ws.get("is_active") else "○"
        active_color = _GREEN if ws.get("is_active") else _DIM
        print(f"  {active_color}  {active_mark} Workspace {ws.get('id', '?')}"
              f" — {ws.get('window_count', 0)} windows{_RESET}")
        if ws.get("last_window_title"):
            print(f"  {_DIM}      Last: {ws['last_window_title'][:60]}{_RESET}")

    print()

    # Windows
    windows = status.get("windows", [])
    active_win = status.get("active_window")
    print(f"  {_BOLD}Windows:{_RESET}  {len(windows)} open")

    if active_win:
        print(f"\n  {_GREEN}  ◆ ACTIVE{_RESET}")
        print(f"    {active_win['app_class']} — "
              f"\"{active_win['title'][:60]}\"")
        print(f"    {_DIM}    {active_win['width']}x{active_win['height']}"
              f" @ ({active_win['x']},{active_win['y']})"
              f" | PID: {active_win['pid']}{_RESET}")

    # Group windows by workspace
    from collections import defaultdict
    by_ws: dict[int, list] = defaultdict(list)
    for w in windows:
        by_ws[w["workspace_id"]].append(w)

    for ws_id in sorted(by_ws.keys()):
        for w in by_ws[ws_id]:
            if w.get("is_active"):
                continue  # Already shown above
            print(f"\n  {_DIM}  ○ {w['app_class']} — "
                  f"\"{w['title'][:60]}\""
                  f" (WS {w['workspace_id']}){_RESET}")

    print()
    return 0


def cmd_desktop_windows(args: argparse.Namespace) -> int:
    """List all open windows organized by workspace."""
    from .desktop.wm_abstraction import SmartWindowResolver

    wm = _get_wm()
    _print_logo()

    if not _require_desktop(wm):
        return 1

    windows = wm.list_windows()
    workspaces = wm.list_workspaces()

    if not windows:
        print(f"  {_DIM}No windows found.{_RESET}")
        return 0

    # Group by workspace
    from collections import defaultdict
    by_ws: dict[int, list] = defaultdict(list)
    for w in windows:
        by_ws[w.workspace_id].append(w)

    for ws in sorted(workspaces, key=lambda x: x.id):
        ws_windows = by_ws.get(ws.id, [])
        if not ws_windows:
            continue

        active_mark = "●" if ws.is_active else "○"
        print(f"  {_GREEN if ws.is_active else _DIM}{active_mark} "
              f"Workspace {ws.id}{_RESET} ({len(ws_windows)} windows)")

        for w in ws_windows:
            marker = "◆" if w.is_active else "○"
            color = _GREEN if w.is_active else _DIM
            suggestions = SmartWindowResolver.suggest_for_window(w)
            hint = f" ({', '.join(suggestions)})" if suggestions else ""
            print(f"  {color}    {marker} {w.app_class} — "
                  f"\"{w.title[:50]}\"{hint}{_RESET}")

        print()

    return 0


def cmd_desktop_switch(args: argparse.Namespace) -> int:
    """Switch to a workspace."""
    wm = _get_wm()

    if not _require_desktop(wm):
        return 1

    target = args.workspace
    if wm.switch_workspace(target):
        print(f"  {_GREEN}✅ Switched to workspace {target}{_RESET}")
        return 0
    else:
        print(f"  {_RED}✗ Failed to switch to workspace {target}{_RESET}")
        return 1


def cmd_desktop_focus(args: argparse.Namespace) -> int:
    """Focus a window by natural name or class."""
    wm = _get_wm()

    if not _require_desktop(wm):
        return 1

    target = " ".join(args.app)
    resolved = wm.focus_smart(target)

    if resolved:
        print(f"  {_GREEN}✅ Focused {resolved}{_RESET}")
        return 0
    else:
        print(f"  {_YELLOW}⚠ Couldn't find '{target}'{_RESET}")
        print(f"  {_DIM}  Try: friday4 desktop windows{_RESET}")
        return 1


def cmd_desktop_launch(args: argparse.Namespace) -> int:
    """Launch an application."""
    wm = _get_wm()

    if not _require_desktop(wm):
        return 1

    app = " ".join(args.app)
    if wm.launch_app(app, args.path):
        print(f"  {_GREEN}✅ Launched {app}{_RESET}")
        return 0
    else:
        print(f"  {_RED}✗ Failed to launch {app}{_RESET}")
        return 1


def cmd_desktop_aliases(args: argparse.Namespace) -> int:
    """List the app aliases Friday has learned.

    ``--json`` prints ``{"todo app": "/usr/bin/obsidian", …}`` — the
    machine-readable contract the VS Code extension's sidebar parses
    (the human table is ANSI-colored, which is hostile to parsers).
    """
    import json as _json
    from .desktop.app_aliases import learned_aliases
    aliases = learned_aliases(args.store)
    if getattr(args, "json", False):
        print(_json.dumps(aliases, indent=2, sort_keys=True))
        return 0
    if not aliases:
        print(f"  {_DIM}No learned apps yet — say 'my <name> is <command>'"
              f" or run: friday4 desktop teach <name> <command>{_RESET}")
        return 0
    print(f"  {_GREEN}Learned apps{_RESET}")
    for name in sorted(aliases):
        print(f"  {_CYAN}  {name}{_RESET}  →  {aliases[name]}")
    return 0


def cmd_desktop_teach(args: argparse.Namespace) -> int:
    """Teach Friday a natural name for an app binary."""
    from .desktop.app_aliases import learn_alias
    name = " ".join(args.name)
    resolved = learn_alias(name, args.binary, args.store)
    if resolved:
        print(f"  {_GREEN}✅ Learned: '{name}' → {resolved}{_RESET}")
        return 0
    print(f"  {_RED}✗ Couldn't find '{args.binary}' on this machine —"
          f" nothing saved{_RESET}")
    return 1


def cmd_desktop_aliases_sync(args: argparse.Namespace) -> int:
    """Sync learned app aliases across Friday instances (collab bus).

    Pushes this machine's aliases as collab observations (keyed
    ``alias:<name>`` so CRDT last-writer-wins reconciles concurrent
    teaching), syncs with peers, then merges remote aliases into the
    local store — "my todo app" taught on the laptop works on the
    desktop. Never raises: collab absent/offline degrades to a message.
    """
    from .desktop.app_aliases import (
        aliases_as_observations, apply_collab_observations,
    )
    try:
        from .collab import Coordinator
    except Exception as exc:
        print(f"  {_RED}✗ Collab unavailable: {exc}{_RESET}")
        return 1

    coordinator = None
    try:
        coordinator = Coordinator()
        if not coordinator.start():
            print(f"  {_YELLOW}! Collab couldn't start — sync skipped{_RESET}")
            return 1
        pushed = 0
        for obs in aliases_as_observations(args.store):
            name = obs["subject"]
            if coordinator.add_observation(
                    obs, obs_id=f"alias:{name}"):
                pushed += 1
        stats = coordinator.sync_once()
        coordinator.stop()
        remote = coordinator.observations()
        coordinator = None
        applied = apply_collab_observations(remote, args.store)
        print(f"  {_GREEN}✅ Alias sync done{_RESET}")
        print(f"  {_DIM}  pushed {pushed} · synced with {stats.get('peers', 0)}"
              f" peer(s) · merged {stats.get('applied', 0)} · applied"
              f" {applied} remote alias(es){_RESET}")
        return 0
    except Exception as exc:
        logger.warning(f"alias sync failed: {exc}")
        print(f"  {_RED}✗ Alias sync failed: {exc}{_RESET}")
        return 1
    finally:
        if coordinator is not None:
            try:
                coordinator.stop()
            except Exception:
                pass


def cmd_desktop_forget(args: argparse.Namespace) -> int:
    """Forget a learned app alias."""
    from .desktop.app_aliases import forget_alias
    name = " ".join(args.name)
    if forget_alias(name, args.store):
        print(f"  {_GREEN}✅ Forgotten '{name}'{_RESET}")
        return 0
    print(f"  {_RED}✗ I didn't know '{name}' — nothing to forget{_RESET}")
    return 1


def cmd_desktop_screenshot(args: argparse.Namespace) -> int:
    """Take a screenshot."""
    wm = _get_wm()

    if not _require_desktop(wm):
        return 1

    path = args.output or None
    result = wm.take_screenshot(path)

    if result:
        print(f"  {_GREEN}📸 Screenshot saved{_RESET}")
        print(f"  {_DIM}  {result}{_RESET}")
        return 0
    else:
        print(f"  {_RED}✗ Screenshot failed{_RESET}")
        return 1


def cmd_desktop_notify(args: argparse.Namespace) -> int:
    """Send a desktop notification."""
    from .desktop.wm_abstraction import DesktopAbstraction

    if DesktopAbstraction.notify(
        args.title or "Friday",
        " ".join(args.message),
        args.urgency,
    ):
        print(f"  {_GREEN}✅ Notification sent{_RESET}")
        return 0
    else:
        print(f"  {_RED}✗ Notification failed{_RESET}")
        return 1


def cmd_desktop_platforms(args: argparse.Namespace) -> int:
    """List supported platforms and the current desktop environment."""
    from .desktop.wm_abstraction import (
        SUPPORTED_PLATFORMS,
        create_adapter,
        detect_desktop_environment,
    )

    _print_logo()
    de = detect_desktop_environment()
    print(f"  {_BOLD}Current desktop environment:{_RESET} {_GREEN}{de}{_RESET}")
    print()
    print(f"  {_BOLD}Supported platforms:{_RESET}")
    for platform in SUPPORTED_PLATFORMS:
        adapter = create_adapter(platform)
        marker = "✅" if adapter.is_available() else "○"
        print(f"  {_DIM}  {marker} {platform:<12}{_RESET} "
              f"{_DIM}({adapter.__class__.__name__}){_RESET}")
    print()

    # Show setup instructions for the current DE if unavailable
    adapter = create_adapter(de)
    setup = getattr(adapter, "setup_instructions", None)
    if setup and callable(setup) and not adapter.is_available():
        print(f"  {_YELLOW}⚠ Setup needed for {de}:{_RESET}")
        print(f"  {_DIM}  {setup()}{_RESET}")
        print()

    return 0


def cmd_desktop_tray(args: argparse.Namespace) -> int:
    """Start the system tray icon."""
    from .desktop.tray import SystemTray

    stop_event = threading.Event()

    print(f"  {_BOLD}Starting system tray...{_RESET}")
    tray = SystemTray(
        feed_count=0,
        daemon_state="running",
        on_voice=_launch_voice_session,
        on_status=_show_status,
        on_quit=lambda: stop_event.set(),
    )
    if not tray.available:
        print(f"  {_RED}✗ System tray unavailable.{_RESET}")
        print(f"  {_DIM}  Install: pip install friday-v4[desktop]{_RESET}")
        return 1

    tray.start(daemon=True)
    print(f"  {_GREEN}✅ System tray running (Ctrl+C to stop){_RESET}")
    try:
        while not stop_event.wait(0.5):
            pass
    except KeyboardInterrupt:
        pass
    print(f"\n  {_DIM}Stopping tray...{_RESET}")
    tray.stop()
    return 0


def cmd_desktop_hotkeys(args: argparse.Namespace) -> int:
    """Register global hotkeys."""
    from .desktop.hotkeys import GlobalHotkeys

    hotkeys = GlobalHotkeys(
        push_to_talk=args.push_to_talk,
        on_push_to_talk=_launch_voice_session,
        on_status=_show_status,
    )
    if not hotkeys.available:
        print(f"  {_RED}✗ Global hotkeys unavailable.{_RESET}")
        print(f"  {_DIM}  Install: pip install friday-v4[desktop]{_RESET}")
        return 1

    if not hotkeys.start():
        print(f"  {_RED}✗ Failed to register hotkeys{_RESET}")
        return 1

    print(f"  {_GREEN}✅ Hotkeys registered:{_RESET}")
    for hk in hotkeys.registered:
        print(f"  {_DIM}    {hk}{_RESET}")
    print(f"  {_DIM}  Press Ctrl+C to stop{_RESET}")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        hotkeys.stop()
    return 0


def cmd_desktop_watch(args: argparse.Namespace) -> int:
    """Watch the V3 ambient feed and raise desktop notifications."""
    from .desktop.notifier import DesktopNotificationChannel

    channel = DesktopNotificationChannel(
        min_priority=args.min_priority,
        poll_interval=args.poll,
    )
    print(f"  {_BOLD}Watching ambient feed (min priority ≥ {args.min_priority})...{_RESET}")
    print(f"  {_DIM}  Ctrl+C to stop{_RESET}")
    channel.start(daemon=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        channel.stop()
    return 0


def cmd_desktop_events(args: argparse.Namespace) -> int:
    """Watch the desktop and print window/app/workspace changes live.

    Implements the plan's desktop event-monitoring API
    (``on_window_change`` / ``on_workspace_change``) on the CLI — Friday
    reports what you switch to as it happens.
    """
    from .desktop.watcher import DesktopWatcher

    wm = _get_wm()
    _print_logo()

    if not _require_desktop(wm):
        return 1

    watcher = DesktopWatcher(
        wm=wm,
        poll_interval=max(args.interval, 0.2),
        on_window_change=lambda win: print(
            f"  {_CYAN}🪟 Window: {win.app_class} — "
            f"\"{(win.title or '')[:50]}\"{_RESET}"),
        on_app_change=lambda app: print(
            f"  {_GREEN}▶ App: {app}{_RESET}"),
        on_workspace_change=lambda ws: print(
            f"  {_YELLOW}⌗ Workspace: {ws.id}{_RESET}"),
    )
    watcher.start()
    print(f"  {_DIM}Watching for desktop changes"
          f" (every {max(args.interval, 0.2):g}s)... Ctrl+C to stop{_RESET}")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print(f"\n  {_DIM}Stopped watching.{_RESET}\n")
    finally:
        watcher.stop()
    return 0


def cmd_desktop_daemon(args: argparse.Namespace) -> int:
    """Run the full desktop presence daemon — tray + hotkeys + notifier.

    Combines everything into one process so Friday has a persistent
    desktop presence: a tray icon, global hotkeys, and the ambient
    feed → notification channel, all sharing the same callbacks.
    """
    from .desktop.hotkeys import GlobalHotkeys
    from .desktop.notifier import DesktopNotificationChannel
    from .desktop.tray import SystemTray

    _print_logo()
    print(f"  {_BOLD}Starting Friday desktop presence...{_RESET}")
    stop_event = threading.Event()

    # 1. System tray
    tray = SystemTray(
        feed_count=0,
        daemon_state="running",
        on_voice=_launch_voice_session,
        on_status=_show_status,
        on_quit=lambda: stop_event.set(),
    )
    tray_started = tray.available and tray.start(daemon=True)
    if tray_started:
        print(f"  {_GREEN}  ✓ Tray icon running{_RESET}")
    else:
        print(f"  {_YELLOW}  ○ Tray unavailable (pip install friday-v4[desktop]){_RESET}")

    # 2. Global hotkeys
    hotkeys = GlobalHotkeys(
        push_to_talk=args.push_to_talk,
        on_push_to_talk=_launch_voice_session,
        on_status=_show_status,
    )
    hotkeys_started = hotkeys.available and hotkeys.start()
    if hotkeys_started:
        print(f"  {_GREEN}  ✓ Hotkeys: {', '.join(hotkeys.registered)}{_RESET}")
    else:
        print(f"  {_YELLOW}  ○ Hotkeys unavailable (pip install friday-v4[desktop]){_RESET}")

    # 3. Ambient → desktop notification channel
    channel = DesktopNotificationChannel(
        min_priority=args.min_priority,
        poll_interval=args.poll,
    )
    channel.start(daemon=True)
    print(f"  {_GREEN}  ✓ Watching ambient feed (priority ≥ {args.min_priority}){_RESET}")

    # 4. Proactive pattern observer — feed real desktop activity (app
    #    switches + focus actions) into the PatternLearner so suggestions
    #    learn from what the user actually does.
    observer = None
    suggestion_channel = None
    try:
        from .proactive import AnticipationEngine
        observer = AnticipationEngine()
        # Event-driven: DesktopWatcher fires on app switches (~1s detection)
        # so patterns learn the moment you switch apps.
        observer.start_observer(interval_seconds=1.0)
        print(f"  {_GREEN}  ✓ Learning desktop patterns (proactive observer){_RESET}")

        # 5. Proactive suggestions → desktop notifications. Shares the same
        #    observer engine so notifications reflect freshly-learned patterns.
        from .desktop.notifier import ProactiveSuggestionChannel
        suggestion_channel = ProactiveSuggestionChannel(
            engine=observer, poll_interval=args.suggestion_poll)
        suggestion_channel.start(daemon=True)
        print(f"  {_GREEN}  ✓ Suggesting proactively (every {args.suggestion_poll:g}s){_RESET}")
    except Exception as exc:
        logger.debug(f"Proactive observer unavailable: {exc}")

    if not (tray_started or hotkeys_started):
        print(f"  {_YELLOW}  ○ No tray/hotkeys — ambient notifier still running.{_RESET}")
        print(f"  {_DIM}    Install: pip install friday-v4[desktop]{_RESET}")

    print(f"\n  {_DIM}  Press Ctrl+C to stop{_RESET}")
    try:
        while not stop_event.wait(0.5):
            pass
    except KeyboardInterrupt:
        pass
    print(f"\n  {_DIM}Stopping desktop presence...{_RESET}")
    if tray_started:
        tray.stop()
    if hotkeys_started:
        hotkeys.stop()
    channel.stop()
    if suggestion_channel:
        try:
            suggestion_channel.stop()
        except Exception:
            pass
    if observer:
        try:
            observer.stop_observer()
            observer.cleanup()
        except Exception:
            pass
    return 0


# ---------------------------------------------------------------------------
# Argument parsers
# ---------------------------------------------------------------------------


def build_desktop_parser(subparsers) -> None:
    """Build subparser for `friday4 desktop` (used by the integrated CLI).

    Registers a ``desktop`` subparser on the given subparsers and wires
    all the desktop subcommands onto it.
    """
    desktop_parser = subparsers.add_parser(
        "desktop",
        help="Desktop awareness & control",
        description="Control your desktop with Friday. "
                    "See what's open, switch workspaces, focus apps, "
                    "launch apps, take screenshots.",
    )
    desktop_sub = desktop_parser.add_subparsers(dest="desktop_command")
    _build_desktop_subcommands(desktop_sub)


def _build_desktop_subcommands(desktop_sub) -> None:
    """Register `friday4 desktop <cmd>` subcommands on a subparsers object.

    Shared by both the integrated `friday` CLI (via build_desktop_parser)
    and the standalone `python -m friday_v4.cli_desktop` entry point.
    """

    # friday4 desktop status
    p = desktop_sub.add_parser("status", help="Show full desktop status")
    p.set_defaults(func=cmd_desktop_status)

    # friday4 desktop windows
    p = desktop_sub.add_parser("windows", help="List open windows")
    p.set_defaults(func=cmd_desktop_windows)

    # friday4 desktop switch <workspace>
    p = desktop_sub.add_parser("switch", help="Switch to a workspace")
    p.add_argument("workspace", type=str,
                   help="Workspace ID (e.g., 1, 2) or name (e.g., main)")
    p.set_defaults(func=cmd_desktop_switch)

    # friday4 desktop focus <app>
    p = desktop_sub.add_parser("focus", help="Focus a window by name")
    p.add_argument("app", nargs="+",
                   help="App name (e.g., 'code editor', 'browser', 'kitty')")
    p.set_defaults(func=cmd_desktop_focus)

    # friday4 desktop launch <app>
    p = desktop_sub.add_parser("launch", help="Launch an application")
    p.add_argument("app", nargs="+", help="App name or command to launch")
    p.add_argument("--path", type=str, default=None,
                   help="Optional working directory / project to open")
    p.set_defaults(func=cmd_desktop_launch)

    # friday4 desktop aliases
    p = desktop_sub.add_parser(
        "aliases", help="List learned app aliases")
    p.add_argument("--store", type=str, default=None,
                   help="Alias store path (default ~/.friday/v4_desktop_aliases.json)")
    p.add_argument("--json", action="store_true",
                   help="Machine-readable output ({name: binary})")
    p.set_defaults(func=cmd_desktop_aliases)

    # friday4 desktop aliases sync
    p = desktop_sub.add_parser(
        "aliases-sync", help="Sync learned apps across Friday instances")
    p.add_argument("--store", type=str, default=None,
                   help="Alias store path (default ~/.friday/v4_desktop_aliases.json)")
    p.set_defaults(func=cmd_desktop_aliases_sync)

    # friday4 desktop teach <name> <binary>
    p = desktop_sub.add_parser(
        "teach", help="Teach Friday a natural name for an app")
    p.add_argument("name", nargs="+",
                   help="Natural name, e.g. 'todo app'")
    p.add_argument("binary", type=str,
                   help="Command or path, e.g. 'obsidian' or '/usr/bin/obsidian'")
    p.add_argument("--store", type=str, default=None,
                   help="Alias store path (default ~/.friday/v4_desktop_aliases.json)")
    p.set_defaults(func=cmd_desktop_teach)

    # friday4 desktop forget <name>
    p = desktop_sub.add_parser(
        "forget", help="Forget a learned app alias")
    p.add_argument("name", nargs="+",
                   help="Natural name to forget, e.g. 'todo app'")
    p.add_argument("--store", type=str, default=None,
                   help="Alias store path (default ~/.friday/v4_desktop_aliases.json)")
    p.set_defaults(func=cmd_desktop_forget)

    # friday4 desktop screenshot
    p = desktop_sub.add_parser("screenshot", help="Take a screenshot")
    p.add_argument("-o", "--output", type=str, default=None,
                   help="Output path for screenshot")
    p.set_defaults(func=cmd_desktop_screenshot)

    # friday4 desktop notify
    p = desktop_sub.add_parser("notify", help="Send a desktop notification")
    p.add_argument("--title", "-t", type=str, default="Friday",
                   help="Notification title (default: Friday)")
    p.add_argument("--urgency", "-u", type=str, default="normal",
                   choices=["low", "normal", "critical"],
                   help="Notification urgency (default: normal)")
    p.add_argument("message", nargs="+", help="Notification message")
    p.set_defaults(func=cmd_desktop_notify)

    # friday4 desktop platforms
    p = desktop_sub.add_parser("platforms",
                               help="List supported platforms + current DE")
    p.set_defaults(func=cmd_desktop_platforms)

    # friday4 desktop tray
    p = desktop_sub.add_parser("tray", help="Start the system tray icon")
    p.set_defaults(func=cmd_desktop_tray)

    # friday4 desktop hotkeys
    p = desktop_sub.add_parser("hotkeys", help="Register global hotkeys")
    p.add_argument("--push-to-talk", type=str, default="ctrl+shift+space",
                   help="Push-to-talk hotkey (default: ctrl+shift+space)")
    p.set_defaults(func=cmd_desktop_hotkeys)

    # friday4 desktop watch
    p = desktop_sub.add_parser(
        "watch",
        help="Watch ambient feed → desktop notifications",
    )
    p.add_argument("--min-priority", type=int, default=1, choices=[0, 1, 2, 3],
                   help="Only notify events with priority >= N (default: 1)")
    p.add_argument("--poll", type=float, default=10.0,
                   help="Poll interval in seconds (default: 10)")
    p.set_defaults(func=cmd_desktop_watch)

    # friday4 desktop events
    p = desktop_sub.add_parser(
        "events",
        help="Watch for window/app/workspace changes live",
    )
    p.add_argument("--interval", type=float, default=1.0,
                   help="Poll interval in seconds (default: 1)")
    p.set_defaults(func=cmd_desktop_events)

    # friday4 desktop daemon
    p = desktop_sub.add_parser(
        "daemon",
        help="Tray + hotkeys + ambient notifier + proactive suggestions, together",
    )
    p.add_argument("--push-to-talk", type=str, default="ctrl+shift+space",
                   help="Push-to-talk hotkey (default: ctrl+shift+space)")
    p.add_argument("--min-priority", type=int, default=1, choices=[0, 1, 2, 3],
                   help="Only notify events with priority >= N (default: 1)")
    p.add_argument("--poll", type=float, default=10.0,
                   help="Poll interval in seconds (default: 10)")
    p.add_argument("--suggestion-poll", type=float, default=120.0,
                   help="Proactive-suggestion poll interval in seconds (default: 120)")
    p.set_defaults(func=cmd_desktop_daemon)


# ---------------------------------------------------------------------------
# Main entry point for standalone testing
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Main entry point for `python -m friday_v4.cli_desktop`.

    Uses the same subcommand builder as the integrated CLI but exposes
    the commands directly (no nested ``desktop`` level), so
    ``python -m friday_v4.cli_desktop platforms`` works.
    """
    logging.basicConfig(level=logging.WARNING)

    parser = argparse.ArgumentParser(prog="friday4 desktop")
    subparsers = parser.add_subparsers(dest="desktop_command")
    _build_desktop_subcommands(subparsers)

    args = parser.parse_args(argv)

    if hasattr(args, "func"):
        return args.func(args) or 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
