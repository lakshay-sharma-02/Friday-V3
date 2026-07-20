"""Knowledge rendering — tables, panels for knowledge commands."""
from __future__ import annotations

from rich.panel import Panel
from rich.table import Table

from ..style import Color, Style, Icon


def render_knowledge_table(rows: list[dict]) -> Panel:
    """Render a list of knowledge entries as a Rich Table."""
    table = Table(border_style=Color.BORDER, style=Color.PANEL_BG)
    table.add_column("Type", style=Style.DIM)
    table.add_column("Subject", style=Style.TEXT)
    table.add_column("Confidence", style=Style.MISSION_ID)
    table.add_column("Status", style=Style.SUCCESS)

    for r in rows:
        table.add_row(
            r.get("type", ""),
            r.get("subject", "")[:50],
            r.get("confidence", ""),
            r.get("status", ""),
        )

    return Panel(table, title=f"{Icon.MISSION} Knowledge", border_style=Color.BORDER)
