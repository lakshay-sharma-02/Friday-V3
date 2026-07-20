"""Mission Control live dashboard — composes widgets into a Rich Layout.

Usage (live):
    renderer = MissionRenderer()
    with Live(renderer.render(view), refresh_per_second=15) as live:
        while running:
            live.update(renderer.render(updated_view))

Usage (static):
    console.print(render_mission_view(view))
"""
from __future__ import annotations

from rich.layout import Layout

from ..models import MissionView
from ..widgets.header import HeaderWidget
from ..widgets.footer import FooterWidget
from ..widgets.progress import ProgressWidget
from ..widgets.workers import WorkersWidget
from ..widgets.timeline import TimelineWidget
from ..widgets.mission_graph import MissionGraphWidget


class MissionRenderer:
    """Composes all widgets into a full-screen Layout. Stateless — call render()."""

    def __init__(self):
        self.header = HeaderWidget()
        self.footer = FooterWidget()
        self.progress = ProgressWidget()
        self.workers = WorkersWidget()
        self.timeline = TimelineWidget()
        self.mission_graph = MissionGraphWidget()

    def render(self, view: MissionView) -> Layout:
        """Build a Layout from a MissionView snapshot."""
        layout = Layout()
        layout.split_column(
            Layout(self.header.render(view.id, view.elapsed_seconds), size=3),
            Layout(self.progress.render(view.progress, view.goal), size=3),
            Layout(self._middle_section(view), ratio=1),
            Layout(self.footer.render(self._status_line(view)), size=3),
        )
        return layout

    def _middle_section(self, view: MissionView) -> Layout:
        mid = Layout()
        mid.split_row(
            Layout(self._left_column(view), ratio=2),
            Layout(self.mission_graph.render(view.phase), ratio=1),
        )
        return mid

    def _left_column(self, view: MissionView) -> Layout:
        col = Layout()
        col.split_column(
            Layout(self.workers.render(view.workers), ratio=2),
            Layout(self.timeline.render(view.timeline), ratio=3),
        )
        return col

    @staticmethod
    def _status_line(view: MissionView) -> str:
        """Derive a single status sentence from the current view."""
        running = [w for w in view.workers if w.status.value == "running"]
        if running:
            w = running[0]
            prog = ""
            if w.progress and w.progress.total:
                prog = f" ({w.progress.current}/{w.progress.total})"
            return f"{w.name} is {w.current_task}{prog}"
        if all(w.status.value == "completed" for w in view.workers):
            return "Mission complete"
        return "Waiting for workers..."


def render_mission_view(view: MissionView) -> Layout:
    """Convenience: one-shot render (not live)."""
    return MissionRenderer().render(view)
