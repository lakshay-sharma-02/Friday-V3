"""Notices panel — latest proactive pings from the vault."""
from __future__ import annotations

from textual.widgets import Static

from .parsers import render_notices


class NoticesPanel(Static):
    """Poll vault notices every 2s."""

    def __init__(self, controller) -> None:
        # ``markup=False``: notice text is vault content and may contain
        # ``[...]`` — plain text, not Rich markup (Textual >= 8 would
        # swallow bracket content or raise on a bad closing tag).
        super().__init__("", markup=False)
        self._controller = controller

    def on_mount(self) -> None:
        self.set_interval(2.0, self._refresh)
        self._refresh()

    def _refresh(self) -> None:
        self.update(render_notices(self._controller.notices(5)))
