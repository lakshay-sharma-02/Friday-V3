"""Mission header with title, ID, and elapsed time."""
from __future__ import annotations
from rich.panel import Panel
from rich.text import Text
from ..style import Color, Style, Icon


class HeaderWidget:
    """Top-of-mission bar showing FRIDAY Mission Control + ID + timer."""

    def render(self, mission_id: str, elapsed_seconds: int) -> Panel:
        mins, secs = divmod(elapsed_seconds, 60)
        hours, mins = divmod(mins, 60)
        if hours:
            elapsed = f"{hours:02d}:{mins:02d}:{secs:02d}"
        else:
            elapsed = f"{mins:02d}:{secs:02d}"

        title = Text.assemble(
            (f" {Icon.MISSION} ", Style.MISSION_ID),
            ("FRIDAY Mission Control", "bold white"),
        )
        right = Text.assemble(
            ("Mission #", Style.DIM),
            (mission_id, Style.MISSION_ID),
            ("  ", ""),
            (elapsed, Style.DIM),
        )
        return Panel(
            Text.assemble(title, " " * 4, right),
            style=Color.HEADER_BG,
            border_style=Color.BORDER,
        )
