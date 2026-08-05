"""Schedule panel — agenda from vault/wiki/schedule.md."""
from __future__ import annotations

from textual.widgets import Static

from .parsers import render_schedule


class SchedulePanel(Static):
    """Poll vault wiki/schedule.md every 2s."""

    def __init__(self, controller) -> None:
        # ``markup=False``: schedule items are vault text and may contain
        # ``[...]`` — plain text, not Rich markup (Textual >= 8 would
        # swallow bracket content or raise on a bad closing tag).
        super().__init__("", markup=False)
        self._controller = controller

    def on_mount(self) -> None:
        self.set_interval(2.0, self._refresh)
        self._refresh()

    def _refresh(self) -> None:
        self.update(render_schedule(self._controller.schedule()))
