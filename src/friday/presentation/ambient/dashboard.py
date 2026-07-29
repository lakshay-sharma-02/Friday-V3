"""Ambient dashboard — live Rich terminal UI for the ambient event feed.

Composits status panel + feed widget + footer into a full-screen Rich Layout
with live auto-refresh and keyboard interaction.

Usage::

    from .dashboard import run_dashboard
    run_dashboard()
"""
from __future__ import annotations

import sys
from typing import Optional

from rich.align import Align
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from ...db import connect

from ..style import Color, Icon, Style as TStyle
from .feed_widget import FeedWidget
from .models import DashboardView, FeedEventView, FooterView, StatusView
from .status_panel import StatusPanel


# ---------------------------------------------------------------------------
# Data fetching (pure functions — no widget state)
# ---------------------------------------------------------------------------


def _fetch_view(scroll_offset: int = 0, selected_index: int = -1) -> DashboardView:
    """Build a DashboardView snapshot from the current DB state."""
    conn = connect()

    try:
        # --- Feed events ---
        from ...ambient import get_feed, get_unread_count, get_unread_by_priority

        raw_events = get_feed(conn, limit=100, include_dismissed=False)
        events = [
            FeedEventView(
                id=e.id,
                timestamp=e.timestamp,
                event_type=e.event_type,
                title=e.title,
                detail=e.detail,
                priority=e.priority,
                category=e.category,
                project=e.project,
                payload=e.payload,
                confidence=e.confidence,
                salience=e.salience,
                actionable=e.actionable,
                action_label=e.action_label,
                action_command=e.action_command,
                dismissed=e.dismissed,
            )
            for e in raw_events
        ]

        unread = get_unread_count(conn)
        by_pri = get_unread_by_priority(conn)
        unread_by_priority: dict[int, int] = {}
        high_pri = 0
        for r in by_pri:
            p = r["priority"]
            c = r["cnt"]
            unread_by_priority[p] = c
            if p >= 2:
                high_pri += c

        # --- Daemon status ---
        from ...daemon import get_status

        st = get_status()
        dstate = st.get("state", "stopped")

        # Read Phase A fields from daemon status.
        pending_initiatives = st.get("new_pending_initiatives", 0) or st.get(
            "initiatives_changed", 0
        )
        open_gaps = st.get("open_gaps", 0) or st.get("new_gaps", 0)
        active_skills = st.get("new_skills", 0) or 0
        drifted_skills = st.get("drifted_skills", 0) or 0
        new_suggestions = st.get("new_suggestions", 0) or st.get(
            "high_severity_suggestions", 0
        )

        status = StatusView(
            daemon_state=dstate,
            last_cycle_at=st.get("last_cycle_at", ""),
            last_cycle_outcome=st.get("last_cycle_outcome", ""),
            cycle_count=st.get("cycle_count", 0),
            repos_scanned=st.get("watched_repos", 0),
            unread_events=unread,
            unread_by_priority=unread_by_priority,
            high_priority_unread=high_pri,
            pending_initiatives=pending_initiatives,
            open_gaps=open_gaps,
            active_skills=active_skills,
            drifted_skills=drifted_skills,
            new_suggestions=new_suggestions,
            kill_switch_active=st.get("kill_switch_active", False),
        )

        footer = FooterView(
            text=_footer_text(dstate, len(events), status),
            event_count=len(events),
            unread_count=unread,
        )

        return DashboardView(
            events=events,
            status=status,
            footer=footer,
            scroll_offset=scroll_offset,
            selected_index=selected_index,
        )
    finally:
        conn.close()


def _footer_text(
    dstate: str, event_count: int, status: StatusView
) -> str:
    """Derive a one-line status sentence from the current state."""
    parts: list[str] = []
    if dstate == "running":
        parts.append("Daemon is active")
    elif dstate == "stopped":
        parts.append("Daemon is stopped — run `friday daemon start`")
    elif dstate == "crashed":
        parts.append("Daemon crashed — check `friday daemon logs`")

    if status.last_cycle_outcome == "failed":
        parts.append("⚠ Last cycle failed")
    if status.kill_switch_active:
        parts.append("🛑 Kill switch ACTIVE")

    return " · ".join(parts) if parts else "Friday is idle."


# ---------------------------------------------------------------------------
# Layout builder
# ---------------------------------------------------------------------------


def _build_layout(view: DashboardView) -> Layout:
    """Compose all widgets into a full-screen Layout."""
    feed_widget = FeedWidget(max_visible=20)
    status_panel = StatusPanel()

    layout = Layout()
    layout.split_column(
        Layout(_render_header(view), size=3),
        Layout(
            _middle_section(feed_widget, status_panel, view), ratio=1
        ),
        Layout(_render_footer(view), size=3),
    )
    return layout


def _render_header(view: DashboardView) -> Panel:
    """Top bar: title + quick status."""
    daemon_indicator = (
        f"[{Color.SUCCESS}]● Running[/]"
        if view.status.daemon_state == "running"
        else f"[{Color.DIM}]○ Stopped[/]"
        if view.status.daemon_state == "stopped"
        else f"[{Color.ERROR}]✗ Crashed[/]"
    )

    right = f"  {view.footer.event_count} events  {view.footer.unread_count} unread  {daemon_indicator}"

    title = Text.assemble(
        (f" ◆ FRIDAY Dashboard", f"bold {Color.PRIMARY}"),
    )
    right_text = Text.from_markup(right)

    return Panel(
        Text.assemble(title, Text(" " * 4), right_text),
        style=Color.HEADER_BG,
        border_style=Color.BORDER,
    )


def _middle_section(
    feed_widget: FeedWidget, status_panel: StatusPanel, view: DashboardView
) -> Layout:
    """Left = feed, right = status panel, with empty-state guides."""
    mid = Layout()

    # Empty state: daemon stopped with no events → show a welcome/CTA guide.
    if view.status.daemon_state == "stopped" and not view.events:
        guide = Text.assemble(
            ("\n\n  ◆ Welcome to Friday\n\n", f"bold {Color.PRIMARY}"),
            ("  Start the daemon to begin ambient observation:\n\n", TStyle.TEXT),
            ("    friday daemon start\n\n", TStyle.BOLD),
            ("  Or explore the CLI:\n\n", TStyle.TEXT),
            ("    friday feed       Show ambient event feed\n", TStyle.DIM),
            ("    friday review     Review pending initiatives\n", TStyle.DIM),
            ("    friday suggest    Cross-project suggestions\n", TStyle.DIM),
            ("    friday status     Daemon status\n\n", TStyle.DIM),
            ("  Press  [q]  to quit this dashboard.\n", TStyle.DIM),
        )
        welcome_panel = Panel(
            guide,
            border_style=Color.BORDER,
            style=Color.PANEL_BG,
            padding=(1, 2),
        )
        mid.split_row(
            Layout(welcome_panel, ratio=2),
            Layout(status_panel.render(view.status), ratio=1),
        )
        return mid

    # Empty state: daemon crashed with no events.
    if view.status.daemon_state == "crashed" and not view.events:
        guide = Text.assemble(
            ("\n\n  ✗ Daemon Crashed\n\n", f"bold {Color.ERROR}"),
            ("  The daemon process exited unexpectedly.\n\n", TStyle.TEXT),
            ("  Check the logs for details:\n\n", TStyle.DIM),
            ("    friday daemon logs\n\n", TStyle.BOLD),
            ("  Then restart:\n\n", TStyle.DIM),
            ("    friday daemon start\n\n", TStyle.BOLD),
            ("  Press  [q]  to quit this dashboard.\n", TStyle.DIM),
        )
        crashed_panel = Panel(
            guide,
            border_style=Color.BORDER,
            style=Color.PANEL_BG,
            padding=(1, 2),
        )
        mid.split_row(
            Layout(crashed_panel, ratio=2),
            Layout(status_panel.render(view.status), ratio=1),
        )
        return mid

    # Empty state: daemon active but no events yet.
    if not view.events:
        guide = Text.assemble(
            ("\n\n  📡 No events yet\n\n", f"bold {Color.PRIMARY}"),
            ("  The daemon will populate the feed as it observes\n", TStyle.TEXT),
            ("  workspace activity across cycles.\n\n", TStyle.TEXT),
            ("  Try running a manual cycle:\n\n", TStyle.DIM),
            ("    friday daemon cycle\n\n", TStyle.DIM),
            ("  Press  [r]  to refresh this view.\n", TStyle.DIM),
        )
        waiting_panel = Panel(
            guide,
            border_style=Color.BORDER,
            style=Color.PANEL_BG,
            padding=(1, 2),
        )
        mid.split_row(
            Layout(waiting_panel, ratio=2),
            Layout(status_panel.render(view.status), ratio=1),
        )
        return mid

    # Normal state: show feed + status.
    mid.split_row(
        Layout(feed_widget.render(view.events, view.scroll_offset, view.selected_index), ratio=2),
        Layout(status_panel.render(view.status), ratio=1),
    )
    return mid


def _render_footer(view: DashboardView) -> Panel:
    """Bottom bar: keyboard hints + status text."""
    hints = Text.assemble(
        (f"  {Icon.RUNNING} ", f"{Color.DIM}"),
        ("[↑↓] scroll  ", TStyle.DIM),
        ("[d] dismiss  ", TStyle.DIM),
        ("[enter] act  ", TStyle.DIM),
        ("[r] refresh  ", TStyle.DIM),
        ("[q] quit", TStyle.DIM),
    )

    status_line = Text(view.footer.text, style=TStyle.DIM)

    return Panel(
        Text.assemble(hints, Text("  "), status_line),
        style=Color.HEADER_BG,
        border_style=Color.BORDER,
    )


# ---------------------------------------------------------------------------
# Action handlers (keyboard interaction callbacks)
# ---------------------------------------------------------------------------


def _handle_dismiss(view: DashboardView) -> DashboardView:
    """Dismiss the selected event and return an updated view."""
    if view.selected_index < 0 or view.selected_index >= len(view.events):
        return view
    ev = view.events[view.selected_index]
    conn = connect()
    try:
        from ...ambient import dismiss_event

        dismiss_event(conn, ev.id)
    finally:
        conn.close()
    # Re-fetch with the same scroll offset.
    return _fetch_view(scroll_offset=view.scroll_offset)


def _handle_action(view: DashboardView) -> DashboardView:
    """Execute the action command of the selected event."""
    if view.selected_index < 0 or view.selected_index >= len(view.events):
        return view
    ev = view.events[view.selected_index]
    if ev.actionable and ev.action_command:
        import subprocess

        try:
            subprocess.Popen(
                ev.action_command,
                shell=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass
    return view


# ---------------------------------------------------------------------------
# Keyboard input thread
# ---------------------------------------------------------------------------

import threading
import queue


class _InputReader(threading.Thread):
    """Background thread that reads keystrokes and puts them on a queue.

    Handles multi-byte escape sequences (arrow keys) by buffering: when
    ``\x1b`` is received it reads the next byte(s) within a 50ms window
    and reassembles the full sequence before pushing to the queue.
    """

    def __init__(self, input_queue: queue.Queue, daemon: bool = True):
        super().__init__(daemon=daemon)
        self.input_queue = input_queue

    def run(self):
        import select

        while True:
            try:
                ch = sys.stdin.read(1)
                if not ch:
                    continue

                # If this is the start of an escape sequence, buffer the
                # next bytes so arrow keys (\x1b[A) are pushed as a single
                # token rather than three separate characters.
                if ch == "\x1b":
                    seq = ch
                    # Read up to 2 more bytes with a short timeout.
                    for _ in range(2):
                        r, _, _ = select.select([sys.stdin], [], [], 0.05)
                        if r:
                            b = sys.stdin.read(1)
                            if b:
                                seq += b
                            else:
                                break
                        else:
                            break
                    self.input_queue.put(seq)
                else:
                    self.input_queue.put(ch)
            except (EOFError, OSError):
                break


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_dashboard(refresh_interval: float = 2.0) -> None:
    """Run the live dashboard until the user presses 'q'.

    Args:
        refresh_interval: Seconds between automatic feed refreshes.
    """
    input_queue: queue.Queue = queue.Queue()
    reader = _InputReader(input_queue)
    reader.start()

    view = _fetch_view()

    with Live(
        _build_layout(view),
        refresh_per_second=4,
        screen=True,
    ) as live:
        last_refresh = 0.0
        import time

        while True:
            now = time.monotonic()

            # Process keyboard input.
            try:
                while True:
                    ch = input_queue.get_nowait()
                    if ch == "q" or ch == "\x1b":  # q or ESC
                        return
                    elif ch == "\x1b[A":  # Up arrow
                        if view.selected_index < 0:
                            view = DashboardView(
                                events=view.events,
                                status=view.status,
                                footer=view.footer,
                                scroll_offset=view.scroll_offset,
                                selected_index=0,
                            )
                        elif view.selected_index > 0:
                            new_sel = view.selected_index - 1
                            new_offset = view.scroll_offset
                            if new_sel < new_offset:
                                new_offset = max(0, new_offset - 1)
                            view = DashboardView(
                                events=view.events,
                                status=view.status,
                                footer=view.footer,
                                scroll_offset=new_offset,
                                selected_index=new_sel,
                            )
                        live.update(_build_layout(view))
                    elif ch == "\x1b[B":  # Down arrow
                        max_index = len(view.events) - 1
                        if view.selected_index < max_index:
                            new_sel = view.selected_index + 1 if view.selected_index >= 0 else 0
                            new_offset = view.scroll_offset
                            if new_sel >= new_offset + 20:
                                new_offset += 1
                            view = DashboardView(
                                events=view.events,
                                status=view.status,
                                footer=view.footer,
                                scroll_offset=new_offset,
                                selected_index=new_sel,
                            )
                        elif view.selected_index < 0 and view.events:
                            view = DashboardView(
                                events=view.events,
                                status=view.status,
                                footer=view.footer,
                                scroll_offset=view.scroll_offset,
                                selected_index=0,
                            )
                        live.update(_build_layout(view))
                    elif ch == "d":
                        view = _handle_dismiss(view)
                        live.update(_build_layout(view))
                    elif ch == "\n" or ch == "\r":  # Enter
                        _handle_action(view)
                    elif ch == "r":
                        view = _fetch_view(
                            scroll_offset=view.scroll_offset,
                            selected_index=view.selected_index,
                        )
                        live.update(_build_layout(view))
            except queue.Empty:
                pass

            # Auto-refresh.
            if now - last_refresh >= refresh_interval:
                view = _fetch_view(
                    scroll_offset=view.scroll_offset,
                    selected_index=view.selected_index,
                )
                live.update(_build_layout(view))
                last_refresh = now

            time.sleep(0.05)  # 50ms sleep to prevent busy-waiting
