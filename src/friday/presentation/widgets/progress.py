"""Goal + progress bar widget."""
from __future__ import annotations
from rich.panel import Panel
from rich.progress import BarColumn, Progress, TextColumn
from ..style import Color, Style


class ProgressWidget:
    """Shows the mission goal and a percentage progress bar."""

    def render(self, progress: float, goal: str) -> Panel:
        pct = int(progress * 100)
        bar = Progress(
            TextColumn("  {task.description}"),
            BarColumn(bar_width=None),
            TextColumn("{task.percentage:>3.0f}%"),
        )
        bar.add_task(goal, total=100, completed=pct)
        return Panel(bar, style=Color.PANEL_BG, border_style=Color.BORDER)
