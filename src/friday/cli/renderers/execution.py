"""Static rendering of execution results (post-mission summary)."""
from __future__ import annotations

from rich.panel import Panel
from rich.text import Text

from ..models import MissionView, WorkerStatus
from ..style import Color, Style, Icon


def render_execution_summary(view: MissionView) -> Panel:
    """Post-mission summary panel."""
    duration = _format_duration(view.elapsed_seconds)
    n_workers = len(view.workers)
    n_completed = sum(1 for w in view.workers if w.status == WorkerStatus.COMPLETED)
    n_failed = sum(1 for w in view.workers if w.status == WorkerStatus.FAILED)

    parts: list[Text] = [
        Text.assemble(("Duration:   ", Style.DIM), (duration, Style.TEXT), ("\n", "")),
        Text.assemble(("Workers:    ", Style.DIM), (str(n_workers), Style.TEXT), ("\n", "")),
        Text.assemble(("Completed:  ", Style.DIM),
                       (str(n_completed), Style.SUCCESS), ("\n", "")),
    ]
    if n_failed:
        parts.append(Text.assemble(("Failed:     ", Style.DIM),
                                    (str(n_failed), Style.ERROR), ("\n", "")))

    if view.summary:
        s = view.summary
        parts.append(Text.assemble(("Modified:   ", Style.DIM),
                                   (str(s.files_modified), Style.TEXT), ("\n", "")))
        parts.append(Text.assemble(("Tests:      ", Style.DIM),
                                   (str(s.tests_passed), Style.SUCCESS), ("\n", "")))
        if s.warnings:
            parts.append(Text.assemble(("Warnings:   ", Style.DIM),
                                        (str(s.warnings), Style.WARNING), ("\n", "")))

    content = Text("")
    for p in parts:
        content += p

    return Panel(
        content,
        title=f"{Icon.MISSION} Mission Complete",
        border_style=Color.SUCCESS,
        style=Color.PANEL_BG,
    )


def _format_duration(seconds: int) -> str:
    mins, secs = divmod(seconds, 60)
    if mins >= 60:
        h, m = divmod(mins, 60)
        return f"{h}h {m}m {secs}s"
    return f"{mins}m {secs}s"
