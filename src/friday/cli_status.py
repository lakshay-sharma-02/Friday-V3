"""CLI commands for presence status and focus mode."""

from __future__ import annotations

import argparse
import datetime as dt
from typing import Any


def cmd_status(args: argparse.Namespace) -> int:
    """Show current presence state and focus mode status."""
    from .db import connect
    from .presence import (
        PresenceDetector, get_current_state, format_state,
        get_pending_interrupts_count, AttentionLevel, attention_for_state,
    )

    conn = connect()
    try:
        state, focus_active = get_current_state(conn)
        state_label = format_state(state)

        # Get attention level
        attention = attention_for_state(state)
        attention_labels = {
            0: "🔴 None — nothing passes",
            1: "🟠 Minimal — urgent only",
            2: "🟡 Low — urgent + queued for later",
            3: "🟢 Moderate — urgent + important",
            4: "🟢 High — everything passes",
        }
        attention_label = attention_labels.get(attention, "Unknown")

        # Get pending interrupts
        pending = get_pending_interrupts_count(conn)

        # Get focus info
        focus_info = ""
        if focus_active:
            expires_row = conn.execute(
                "SELECT value FROM operator_preferences WHERE key = 'focus_expires_at'"
            ).fetchone()
            if expires_row:
                try:
                    expires_dt = dt.datetime.fromisoformat(expires_row["value"])
                    remaining = (expires_dt - dt.datetime.now(dt.timezone.utc)).total_seconds()
                    if remaining > 0:
                        mins = int(remaining / 60)
                        focus_info = f" (Focus active — {mins}min remaining)"
                    else:
                        focus_info = " (Focus expired — use `friday focus off` to clear)"
                except Exception:
                    focus_info = " (Focus active)"

        presence_text = f"🧑 {state_label}{focus_info}"
        attention_text = f"📡 Attention: {attention_label}"
        pending_text = f"📨 Pending interrupts: {pending}"

        print("── Presence Status ──────────────────────────────")
        print(f"  {presence_text}")
        print(f"  {attention_text}")
        print(f"  {pending_text}")
        print("─────────────────────────────────────────────────")

        # Show recent signal breakdown if available
        try:
            detector = PresenceDetector()
            signals = detector.collect_signals(conn)
            parts = []
            if signals.idle_seconds is not None:
                parts.append(f"idle: {signals.idle_seconds}s")
            if signals.current_event_title:
                parts.append(f"event: {signals.current_event_title[:40]}")
            if signals.git_activity_minutes_ago is not None:
                parts.append(f"git: {signals.git_activity_minutes_ago}min ago")
            if signals.learned_focus_windows:
                windows = ", ".join(f"{s}-{e}h" for s, e in signals.learned_focus_windows)
                parts.append(f"focus windows: {windows}")
            if parts:
                print(f"  Signals: {' · '.join(parts)}")
                print("─────────────────────────────────────────────────")
        except Exception:
            pass

        print()
        print("Commands:")
        print("  `friday focus on <minutes>` — Enable focus mode")
        print("  `friday focus off`          — Disable focus mode")
    finally:
        conn.close()

    return 0


def cmd_focus(args: argparse.Namespace) -> int:
    """Manage focus mode — auto DND for deep work."""
    from .db import connect
    from .presence import set_focus_mode, disable_focus_mode, is_focus_mode
    from .ambient import push_event, focus_on_event, focus_off_event

    conn = connect()
    try:
        if args.action == "on":
            duration = getattr(args, "minutes", 90)
            msg = set_focus_mode(conn, duration)
            print(msg)
            # Push ambient event
            push_event(conn, focus_on_event(duration), dedup_hours=24)
        elif args.action == "off":
            if not is_focus_mode(conn):
                print("Focus mode is not currently active.")
            else:
                msg = disable_focus_mode(conn)
                print(msg)
                push_event(conn, focus_off_event(), dedup_hours=24)
        else:
            # Show current status
            if is_focus_mode(conn):
                expires_row = conn.execute(
                    "SELECT value FROM operator_preferences WHERE key = 'focus_expires_at'"
                ).fetchone()
                if expires_row:
                    try:
                        expires_dt = dt.datetime.fromisoformat(expires_row["value"])
                        remaining = (expires_dt - dt.datetime.now(dt.timezone.utc)).total_seconds()
                        if remaining > 0:
                            mins = int(remaining / 60)
                            print(f"🔇 Focus mode active — {mins} minutes remaining.")
                        else:
                            print("🔇 Focus mode expired. Use `friday focus off` to clear.")
                    except Exception:
                        print("🔇 Focus mode is active.")
                else:
                    print("🔇 Focus mode is active.")
            else:
                print("🔊 Focus mode is not active. Use `friday focus on <minutes>` to enable.")
    finally:
        conn.close()

    return 0


def add_status_parser(subparsers) -> None:
    """Add the `friday status` and `friday focus` subparsers."""
    p_status = subparsers.add_parser(
        "status",
        help="Show current presence state and attention level",
        description="Shows what Friday knows about your current state — at desk, in a meeting, deep focus, etc.",
    )
    p_status.set_defaults(func=cmd_status)

    p_focus = subparsers.add_parser(
        "focus",
        help="Manage focus mode (auto DND for deep work)",
        description="Enable or disable focus mode. During focus, only urgent items interrupt.",
    )
    p_focus.add_argument(
        "action",
        nargs="?",
        choices=["on", "off"],
        default="status",
        help="'on' to enable focus (optionally with --minutes), 'off' to disable, or omit to check status",
    )
    p_focus.add_argument(
        "-m", "--minutes",
        type=int,
        default=90,
        help="Duration of focus mode in minutes (default: 90)",
    )
    p_focus.set_defaults(func=cmd_focus)
