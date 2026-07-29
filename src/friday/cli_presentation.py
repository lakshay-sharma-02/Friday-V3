"""CLI commands for Presentation & Interface: hud, viz, web, report."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def cmd_hud(args: argparse.Namespace) -> int:
    """Terminal HUD — heads-up display with status bar and popups.

    ``friday hud on``       Enable HUD (full mode)
    ``friday hud off``      Disable HUD
    ``friday hud compact``  Compact mode (minimal status line)
    ``friday hud full``     Full mode (status bar + popups)
    """
    from .presentation.cli_format import header, green, gray, yellow

    action = (args.action or "status").lower()

    if action == "on" or action == "full":
        try:
            from .presentation.hud import hud_start
            hud_start("full")
            print(green("  HUD enabled (full mode)."))
            print(gray("  Status bar showing at bottom of terminal."))
        except Exception as exc:
            print(f"  Error starting HUD: {exc}")
            return 1
        return 0

    if action == "compact":
        try:
            from .presentation.hud import hud_start
            hud_start("compact")
            print(green("  HUD enabled (compact mode)."))
        except Exception as exc:
            print(f"  Error starting HUD: {exc}")
            return 1
        return 0

    if action == "off":
        try:
            from .presentation.hud import hud_stop
            hud_stop()
            print(green("  HUD disabled."))
        except Exception as exc:
            print(f"  Error stopping HUD: {exc}")
            return 1
        return 0

    # status
    from .presentation.hud import get_hud
    hud = get_hud()
    mode = hud.mode.value if hasattr(hud, "mode") else "off"
    print(header("HUD", mode))
    print(f"  Status: {green('active') if mode != 'off' else yellow('inactive')}")
    print(f"  Mode:   {mode}")
    return 0


def cmd_viz(args: argparse.Namespace) -> int:
    """Architecture visualization.

    ``friday viz arch [--format tree|mermaid|image] [--output FILE]``
    ``friday viz deps [--format tree|mermaid]``
    ``friday viz timeline [--format tree|mermaid]``
    ``friday viz impact <symbol> [--format tree|mermaid]``
    """
    from .presentation.cli_format import header, green, gray, yellow, error as perror

    kind = (args.kind or "arch").lower()
    fmt = (args.format or "tree").lower()
    output = getattr(args, "output", None)
    target = getattr(args, "target", None)

    if kind == "impact" and not target:
        print(perror("Specify a symbol: friday viz impact <symbol>"))
        return 1

    print(header("Viz", f"{kind} ({fmt})"))

    from .presentation.arch_viz import visualize
    result = visualize(kind, target, fmt=fmt, output=Path(output) if output else None)
    print()
    print(result)
    return 0


def cmd_web(args: argparse.Namespace) -> int:
    """Web interface — start the Friday dashboard web server.

    ``friday web``             Start on default port 8321
    ``friday web --port 8888`` Start on custom port
    ``friday web --open``      Start and open browser
    """
    from .presentation.cli_format import header, green, gray

    port = getattr(args, "port", 8321)
    open_browser = getattr(args, "open_browser", False)

    print(header("Web", f"port {port}"))
    print()

    try:
        from .presentation.web_server import start_server
        start_server(port=port, open_browser=open_browser)
    except KeyboardInterrupt:
        print(gray("  Server stopped."))
    except Exception as exc:
        print(f"  Error: {exc}")
        return 1

    return 0


def cmd_report(args: argparse.Namespace) -> int:
    """Generate rich reports.

    ``friday report daily [--format markdown|html] [--output FILE]``
    ``friday report weekly [--format markdown|html] [--output FILE]``
    ``friday report impact <symbol> [--format markdown|html] [--output FILE]``
    """
    from .presentation.cli_format import header, green, gray

    kind = (args.kind or "daily").lower()
    fmt = (args.format or "markdown").lower()
    output = getattr(args, "output", None)
    target = getattr(args, "target", None)

    if kind == "impact" and not target:
        print("Specify a symbol: friday report impact <symbol>")
        return 1

    print(header("Report", f"{kind} ({fmt})"))
    print()

    from .presentation.reports import generate_report
    result = generate_report(kind, fmt=fmt, output=Path(output) if output else None, symbol=target)
    print(result)

    if output:
        print(gray(f"  Saved to: {output}"))
    return 0
