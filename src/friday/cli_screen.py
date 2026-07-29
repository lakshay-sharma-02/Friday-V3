"""Screen/Workspace Awareness CLI — `friday screen` commands.

Shows what's on the user's screen, what apps they're running,
what they're working on — like MCU FRIDAY's workspace awareness.

Usage::

    friday screen            # One-shot workspace snapshot
    friday screen --watch    # Live-updating every 2s
    friday screen --json     # JSON output for scripting
    friday screen --ocr      # Include screenshot + OCR (requires tesseract)
    friday screen --clip     # Don't read clipboard (privacy)
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Optional


def cmd_screen(args: argparse.Namespace) -> int:
    """Dispatch `friday screen`."""
    watch = getattr(args, "watch", False)
    tunable = getattr(args, "tunable", False)
    json_mode = getattr(args, "json", False)
    history = getattr(args, "history", False)
    include_ocr = getattr(args, "ocr", False)
    include_clipboard = not getattr(args, "no_clipboard", False)
    count = getattr(args, "count", 5)
    interval = getattr(args, "interval", 2)
    limit = getattr(args, "limit", 20)

    if history:
        return _show_history(limit=limit)
    elif tunable:
        return _run_tunable_watch(
            include_ocr=include_ocr,
            include_clipboard=include_clipboard,
            interval=interval,
        )
    elif watch:
        return _run_watch(
            include_ocr=include_ocr,
            include_clipboard=include_clipboard,
            count=count,
            interval=interval,
        )
    elif json_mode:
        return _show_json(
            include_ocr=include_ocr,
            include_clipboard=include_clipboard,
        )
    else:
        return _show_snapshot(
            include_ocr=include_ocr,
            include_clipboard=include_clipboard,
        )


def _show_snapshot(
    include_ocr: bool = False,
    include_clipboard: bool = True,
) -> int:
    """Collect and display a single workspace snapshot."""
    try:
        from .screen import collect_screen_context

        ctx = collect_screen_context(
            include_ocr=include_ocr,
            include_clipboard=include_clipboard,
        )
        print(ctx.format_block())
        return 0
    except Exception as exc:
        print(f"error: screen context collection failed: {exc}", file=sys.stderr)
        return 1


def _show_json(
    include_ocr: bool = False,
    include_clipboard: bool = True,
) -> int:
    """Collect and display workspace snapshot as JSON."""
    try:
        from .screen import collect_screen_context
        import json

        ctx = collect_screen_context(
            include_ocr=include_ocr,
            include_clipboard=include_clipboard,
        )
        print(json.dumps(ctx.to_dict(), indent=2))
        return 0
    except Exception as exc:
        print(f"error: screen context collection failed: {exc}", file=sys.stderr)
        return 1


def _show_history(limit: int = 20) -> int:
    """Show screen context change history as a timeline."""
    try:
        from .db import connect
        from .memory import WorkingMemory
        from .presentation.cli_format import header, gray

        conn = connect()
        try:
            wm = WorkingMemory(conn)
            entries = wm.get_contexts_by_category("timeline", limit=limit)
        finally:
            conn.close()

        if not entries:
            print(gray("  No screen context history available yet."))
            print(gray("  History appears after the daemon detects app switches, URL changes,"))
            print(gray("  or clipboard changes across cycles."))
            return 0

        print(header("Screen Context Timeline", f"{len(entries)} event(s)"))
        print()

        for entry in reversed(entries):
            timestamp = entry.get("created_at", "")[:19]
            value = entry.get("value", "")
            key = entry.get("context_key", "")

            # Determine icon and color from context_key.
            if "app_switch" in key:
                icon = "🔄"
                prefix = "App"
            elif "url_change" in key:
                icon = "🌐"
                prefix = "URL"
            elif "clipboard_change" in key:
                icon = "📋"
                prefix = "Clipboard"
            elif "window_title" in key:
                icon = "🪟"
                prefix = "Window"
            else:
                icon = "•"
                prefix = ""

            time_str = timestamp[11:19] if len(timestamp) >= 19 else timestamp
            print(f"  {icon} {gray(time_str)} {value[:100]}")

        print()
        print(gray(f"  Run `friday screen` for current snapshot."))
        return 0

    except Exception as exc:
        print(f"error: history retrieval failed: {exc}", file=sys.stderr)
        return 1


def _run_watch(
    include_ocr: bool = False,
    include_clipboard: bool = True,
    count: int = 5,
    interval: int = 2,
) -> int:
    """Run a live-updating screen awareness watch."""
    try:
        from .screen import collect_screen_context
        from rich.live import Live
        from rich.panel import Panel
        from rich.table import Table
        from rich.console import Console
        from rich.layout import Layout
        from rich.text import Text
    except ImportError as exc:
        print(f"error: live mode requires rich: {exc}", file=sys.stderr)
        return 1

    console = Console()

    def _build_layout(ctx, iteration: int) -> Layout:
        layout = Layout()
        header = Text.assemble(
            ("╔═══ FRIDAY SCREEN AWARENESS ═══╗", "bold magenta"),
            Text(f"  snapshot #{iteration}", "dim"),
        )
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="body"),
        )
        layout["header"].update(Panel(header, style="magenta"))

        grid = Table.grid(padding=(0, 2))
        grid.add_column(style="bold")
        grid.add_column()

        grid.add_row("Desktop", ctx.desktop_environment or "?")
        grid.add_row("Active", ctx.active_window_process or "?")
        grid.add_row("Window", ctx.active_window_title[:80] or "?")
        if ctx.browser_url:
            grid.add_row("URL", f"{ctx.browser_name}: {ctx.browser_url[:70]}")
        if ctx.clipboard_text:
            clip = ctx.clipboard_text[:60].replace("\n", "\\n")
            grid.add_row("Clipboard", clip)
        if ctx.screen_text:
            st = ctx.screen_text[:80].replace("\n", " ")
            grid.add_row("Screen", st)
        grid.add_row("Processes", str(ctx.running_processes))

        layout["body"].update(Panel(
            grid,
            title="[bold]What You're Doing[/bold]",
            border_style="magenta",
        ))
        return layout

    try:
        with Live(auto_refresh=False, console=console, screen=False) as live:
            for i in range(count):
                ctx = collect_screen_context(
                    include_ocr=include_ocr,
                    include_clipboard=include_clipboard,
                )
                layout = _build_layout(ctx, i + 1)
                live.update(layout, refresh=True)
                if i < count - 1:
                    time.sleep(interval)
    except KeyboardInterrupt:
        pass

    return 0


def _run_tunable_watch(
    include_ocr: bool = False,
    include_clipboard: bool = True,
    interval: int = 2,
) -> int:
    """Run an interactive live screen watch with per-app tuning toggles.

    Shows the current screen context alongside a list of detected apps
    with their auto-watcher tuning state. Press number keys to toggle
    apps on/off. Press 'q' to quit.
    """
    try:
        from .screen import collect_screen_context
        from .watcher import (
            get_tuning_rules, set_tuning_rule, remove_tuning_rule,
            DEFAULT_BROWSER_KEYWORDS, DEFAULT_IDE_KEYWORDS,
            DEFAULT_TERMINAL_KEYWORDS,
        )
        from .db import connect
        from rich.live import Live
        from rich.panel import Panel
        from rich.table import Table
        from rich.console import Console
        from rich.layout import Layout
        from rich.text import Text
    except ImportError as exc:
        print(f"error: tunable mode requires rich: {exc}", file=sys.stderr)
        return 1

    import threading

    console = Console()
    running = threading.Event()
    running.set()
    key_pressed = []

    def _key_listener():
        """Background thread that reads single keypresses."""
        try:
            import termios
            import tty
            fd = sys.stdin.fileno()
            try:
                old = termios.tcgetattr(fd)
            except Exception:
                old = None
            try:
                if old is not None:
                    tty.setraw(fd)
                while running.is_set():
                    ch = sys.stdin.read(1)
                    key_pressed.append(ch)
                    if ch == "q":
                        break
            finally:
                if old is not None:
                    termios.tcsetattr(fd, termios.TCSADRAIN, old)
        except Exception:
            pass

    def _get_category(app_name: str) -> str:
        """Return the default category label for an app."""
        a = app_name.lower()
        if any(k in a for k in DEFAULT_BROWSER_KEYWORDS):
            return "browser"
        if any(k in a for k in DEFAULT_IDE_KEYWORDS):
            return "ide"
        if any(k in a for k in DEFAULT_TERMINAL_KEYWORDS):
            return "terminal"
        return "other"

    def _build_tunable_layout(ctx, rules_lookup: dict) -> Layout:
        """Build a layout with context + tuning panel."""
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="context", size=10),
            Layout(name="tuning"),
            Layout(name="footer", size=2),
        )

        # Header
        header = Text.assemble(
            ("╔═══ FRIDAY TUNABLE SCREEN ═══╗", "bold cyan"),
        )
        layout["header"].update(Panel(header, style="cyan"))

        # Context panel
        context_grid = Table.grid(padding=(0, 2))
        context_grid.add_column(style="bold")
        context_grid.add_column()
        context_grid.add_row("Desktop", ctx.desktop_environment or "?")
        context_grid.add_row("Active app", ctx.active_window_process or "?")
        context_grid.add_row("Window", ctx.active_window_title[:60] or "?")
        if ctx.browser_url:
            context_grid.add_row("URL", f"{ctx.browser_name}: {ctx.browser_url[:60]}")
        context_grid.add_row("Processes", str(ctx.running_processes))
        layout["context"].update(Panel(
            context_grid,
            title="[bold cyan]Current Context[/bold cyan]",
            border_style="cyan",
        ))

        # Tuning panel — show each detected app with toggle
        tuning_table = Table.grid(padding=(0, 2))
        tuning_table.add_column(style="bold cyan", width=5)  # Key
        tuning_table.add_column(style="bold", width=25)      # App name
        tuning_table.add_column(width=12)                    # Category
        tuning_table.add_column(width=14)                    # State
        tuning_table.add_column()                             # Action hint

        tuning_table.add_row("", "[underline]App</underline>", "Category", "Tuning", "")

        # Collect all detected unique apps from context.
        seen = set()
        app_items = []
        if ctx.active_window_process:
            app = ctx.active_window_process
            lower = app.lower()
            if lower not in seen:
                seen.add(lower)
                state = "watch" if rules_lookup.get(lower, "default") != "ignore" else "ignore"
                cat = _get_category(app)
                app_items.append((app, cat, state))

        if ctx.browser_name and ctx.browser_name.lower() not in seen:
            app = ctx.browser_name
            lower = app.lower()
            if lower not in seen:
                seen.add(lower)
                state = "watch" if rules_lookup.get(lower, "default") != "ignore" else "ignore"
                cat = _get_category(app)
                app_items.append((app, cat, state))

        # Add top processes as potential apps.
        for proc in ctx.top_processes[:5]:
            name = proc.get("name", "")
            if name:
                lower = name.lower()
                if lower not in seen and "kernel" not in lower and lower != "<idle>":
                    seen.add(lower)
                    state = "watch" if rules_lookup.get(lower, "default") != "ignore" else "ignore"
                    cat = _get_category(name)
                    app_items.append((name, cat, state))

        key_num = 1
        for name, cat, state in app_items:
            state_display = Text("🔔 WATCH", style="bold green") if state == "watch" else Text("🔕 IGNORE", style="dim red")
            key_str = f"[{key_num}]"
            toggle_hint = "[dim]toggle to ignore[/dim]" if state == "watch" else "[dim]toggle to watch[/dim]"
            tuning_table.add_row(key_str, name[:22], f"({cat})", state_display, toggle_hint)
            key_num += 1

        if not app_items:
            tuning_table.add_row("", "[dim]No apps detected[/dim]", "", "", "")

        layout["tuning"].update(Panel(
            tuning_table,
            title="[bold cyan]App Tuning (press key number to toggle)[/bold cyan]",
            border_style="cyan",
        ))

        # Footer with instructions
        footer = Text.assemble(
            ("[1-9]", "bold cyan"), " toggle app  ",
            ("[q]", "bold yellow"), " quit  ",
            ("[r]", "bold green"), " refresh  ",
            ("[d]", "bold red"), " reset to defaults",
        )
        layout["footer"].update(Panel(footer, style="dim"))

        return layout

    # Start keyboard listener thread.
    listener = threading.Thread(target=_key_listener, daemon=True)
    listener.start()

    conn = connect()
    try:
        with Live(auto_refresh=False, console=console, screen=False) as live:
            while running.is_set():
                # Process pending keypresses.
                while key_pressed:
                    ch = key_pressed.pop(0)
                    if ch == "q":
                        running.clear()
                        break
                    elif ch == "r":
                        pass  # Will refresh on next collect
                    elif ch == "d":
                        from .watcher import reset_tuning_defaults as _reset_tuning_defaults
                        _reset_tuning_defaults(conn)
                        console.log("[bold red]Reset all tuning to defaults![/bold red]")
                    elif ch.isdigit() and ch != "0":
                        # Digit pressed — figure out which app it maps to
                        # by re-collecting context and matching key number.
                        tmp_ctx = collect_screen_context(
                            include_ocr=False,
                            include_clipboard=False,
                        )
                        seen_num = set()
                        tmp_items = []
                        if tmp_ctx.active_window_process:
                            l = tmp_ctx.active_window_process.lower()
                            if l not in seen_num:
                                seen_num.add(l)
                                tmp_items.append(tmp_ctx.active_window_process)
                        if tmp_ctx.browser_name:
                            l = tmp_ctx.browser_name.lower()
                            if l not in seen_num:
                                seen_num.add(l)
                                tmp_items.append(tmp_ctx.browser_name)
                        for proc in tmp_ctx.top_processes[:5]:
                            name = proc.get("name", "")
                            if name:
                                l = name.lower()
                                if l not in seen_num and "kernel" not in l and l != "<idle>":
                                    seen_num.add(l)
                                    tmp_items.append(name)

                        idx = int(ch) - 1
                        if idx < len(tmp_items):
                            app_name = tmp_items[idx]
                            # Determine current state.
                            current_state = "ignored"
                            try:
                                rules = get_tuning_rules(conn)
                                for r in rules:
                                    if r["app"] == app_name.lower():
                                        current_state = r["action"]
                                        break
                            except Exception:
                                pass

                            if current_state == "ignore":
                                remove_tuning_rule(conn, app_name)
                                console.log(f"[green]✓ '{app_name}' → watching[/green]")
                            else:
                                set_tuning_rule(conn, app_name, "ignore")
                                console.log(f"[red]✕ '{app_name}' → ignored[/red]")

                if not running.is_set():
                    break

                # Collect context and build layout.
                ctx = collect_screen_context(
                    include_ocr=include_ocr,
                    include_clipboard=include_clipboard,
                )

                # Get current tuning rules.
                rules_lookup = {}
                try:
                    for r in get_tuning_rules(conn):
                        rules_lookup[r["app"]] = r["action"]
                except Exception:
                    pass

                layout = _build_tunable_layout(ctx, rules_lookup)
                live.update(layout, refresh=True)

                # Wait for interval, checking for quit.
                for _ in range(interval * 10):
                    if not running.is_set() or key_pressed:
                        break
                    time.sleep(0.1)

    except KeyboardInterrupt:
        pass
    finally:
        running.clear()
        conn.close()
        console.print("[dim]Tunable screen watch ended.[/dim]")

    return 0


def add_subparser(sub) -> None:
    """Add the `screen` subcommand parser."""
    p = sub.add_parser(
        "screen",
        help="Workspace screen awareness: active window, apps, clipboard, OCR.",
        description="See what's on your screen — the app you're using, browser URLs, clipboard, and more.",
    )
    p.add_argument(
        "--watch", "-w", action="store_true",
        help="Live-updating watch mode.",
    )
    p.add_argument(
        "--tunable", "-T", action="store_true",
        help="Interactive tunable watch mode — press number keys to toggle app auto-watchers.",
    )
    p.add_argument(
        "--json", "-j", action="store_true",
        help="Output as JSON.",
    )
    p.add_argument(
        "--ocr", "-o", action="store_true",
        help="Include screenshot + OCR (requires tesseract + ImageMagick).",
    )
    p.add_argument(
        "--no-clipboard", action="store_true",
        help="Skip clipboard reading (privacy).",
    )
    p.add_argument(
        "--count", "-n", type=int, default=5,
        help="Number of snapshots for watch mode (default: 5).",
    )
    p.add_argument(
        "--interval", "-i", type=int, default=2,
        help="Seconds between snapshots (default: 2).",
    )
    p.add_argument(
        "--history", "-H", action="store_true",
        help="Show screen context change history (app switches, URLs, clipboard).",
    )
    p.add_argument(
        "--limit", "-l", type=int, default=20,
        help="Number of history entries to show (default: 20).",
    )
    p.set_defaults(func=cmd_screen)
