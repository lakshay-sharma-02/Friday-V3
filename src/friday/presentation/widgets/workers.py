"""Worker status cards — current task, progress, findings."""
from __future__ import annotations
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from ..models import WorkerView, WorkerStatus
from ..style import Color, Style, Icon


class WorkersWidget:
    """Table of active/completed workers with status and current task."""

    def render(self, workers: list[WorkerView]) -> Panel:
        table = Table.grid(padding=(0, 2))
        table.add_column("Status", style="bold", width=2)
        table.add_column("Name", style="bold", width=22)
        table.add_column("Task", width=40)
        table.add_column("Progress", width=10)

        for w in workers:
            if w.status == WorkerStatus.RUNNING:
                icon = Icon.RUNNING
                name_style = Style.TEXT
            elif w.status == WorkerStatus.COMPLETED:
                icon = Icon.COMPLETED
                name_style = Style.SUCCESS
            elif w.status == WorkerStatus.FAILED:
                icon = Icon.FAILED
                name_style = Style.ERROR
            elif w.status == WorkerStatus.WAITING:
                icon = Icon.WAITING
                name_style = Style.WARNING
            else:
                icon = Icon.PENDING
                name_style = Style.DIM

            prog = ""
            if w.progress:
                total_str = str(w.progress.total) if w.progress.total else "?"
                prog = f"{w.progress.current}/{total_str}"

            table.add_row(
                icon,
                Text(w.name, style=name_style),
                Text(w.current_task, style=Style.DIM),
                Text(prog, style=Style.DIM),
            )

        return Panel(table, title="Active Workers", style=Color.PANEL_BG,
                     border_style=Color.BORDER)
