"""Activity panel — tail of today's raw vault log."""
from __future__ import annotations

from textual.widgets import Static

from .parsers import render_activity


class ActivityPanel(Static):
    """Poll the latest raw log every 2s."""

    def __init__(self, controller) -> None:
        # ``markup=False``: raw log lines are free text and may contain
        # ``[...]`` — plain text, not Rich markup (Textual >= 8 would
        # swallow bracket content or raise on a bad closing tag).
        super().__init__("", markup=False)
        self._controller = controller

    def on_mount(self) -> None:
        self.set_interval(2.0, self._refresh)
        self._refresh()

    def _refresh(self) -> None:
        self.update(render_activity(self._controller.activity(6)))
