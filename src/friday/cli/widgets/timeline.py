"""Chronological event log — newest at bottom."""
from __future__ import annotations
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from ..models import TimelineEventView
from ..style import Color, Style


class TimelineWidget:
    """Displays events in chronological order."""

    def render(self, events: list[TimelineEventView]) -> Panel:
        table = Table.grid(padding=(0, 1))
        table.add_column("Time", style=Style.DIM, width=10)
        table.add_column("Event", width=60)

        for e in events:
            kind_style = {
                "phase": Style.MISSION_ID,
                "worker": Style.TEXT,
                "info": Style.DIM,
                "error": Style.ERROR,
            }.get(e.kind, Style.DIM)
            table.add_row(e.timestamp, Text(e.message, style=kind_style))

        return Panel(table, title="Timeline", style=Color.PANEL_BG,
                     border_style=Color.BORDER)
