"""Reusable Panel builders for static info commands."""
from __future__ import annotations
from rich.panel import Panel
from rich.text import Text
from ..style import Color, Style


def info_panel(title: str, content: str, style: str = Style.TEXT) -> Panel:
    """Standard info panel with title and body text."""
    return Panel(
        Text(content, style=style),
        title=title,
        border_style=Color.BORDER,
        style=Color.PANEL_BG,
    )


def error_panel(title: str, content: str) -> Panel:
    """Error panel with red accent."""
    return Panel(
        Text(content, style=Style.ERROR),
        title=title,
        border_style=Color.ERROR,
        style=Color.PANEL_BG,
    )
