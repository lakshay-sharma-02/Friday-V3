"""Single-line status update — the first place users look."""
from __future__ import annotations
from rich.text import Text
from rich.panel import Panel
from ..style import Color, Style, Icon


class FooterWidget:
    """Constantly-updating status line (like Claude Code's status)."""

    def render(self, status: str) -> Panel:
        text = Text.assemble(
            (f" {Icon.RUNNING} ", Style.MISSION_ID),
            (status, Style.TEXT),
        )
        return Panel(text, style=Color.PANEL_BG, border_style=Color.BORDER)
