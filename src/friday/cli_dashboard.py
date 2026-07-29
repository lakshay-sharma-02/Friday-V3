"""CLI command for the FRIDAY Command Center.

Usage::

    friday dashboard [--refresh SECONDS] [--legacy]

Launches the unified command center — a full-screen Rich terminal dashboard
with tabbed views for the ambient feed, system health, operator memory,
skills, and initiatives, plus an inline command bar for directly talking
to Friday.

Keyboard shortcuts while running:

    [Tab] / [→] [←]   Switch tabs
    [/]                Focus command bar
    [↑] [↓]            Scroll / select events
    [d]                Dismiss selected event (Feed tab)
    [Enter]            Execute action / send command
    [r]                Force refresh
    [q]                Quit
"""

from __future__ import annotations

import argparse


def cmd_dashboard(args: argparse.Namespace) -> int:
    """Launch the FRIDAY Command Center — unified entry point for everything.

    If ``--legacy`` is passed, launches the original ambient dashboard instead.
    """
    refresh_interval = getattr(args, "refresh", 3.0)
    legacy = getattr(args, "legacy", False)

    if legacy:
        try:
            from .presentation.ambient.dashboard import run_dashboard
            run_dashboard(refresh_interval=refresh_interval)
            return 0
        except ImportError as exc:
            print(f"error: legacy dashboard unavailable: {exc}")
            print("The dashboard requires the 'rich' library.")
            return 1
        except Exception as exc:
            print(f"error: legacy dashboard crashed: {exc}")
            return 1

    # Default: launch the unified command center
    try:
        from .presentation.command_center import run_command_center
        run_command_center(refresh_interval=refresh_interval)
    except ImportError as exc:
        print(f"error: command center unavailable: {exc}")
        print("The command center requires the 'rich' library.")
        return 1
    except Exception as exc:
        print(f"error: command center crashed: {exc}")
        return 1

    return 0
