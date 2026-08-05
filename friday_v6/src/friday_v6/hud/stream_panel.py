"""Stream panel — live ambient feed + local turns (Wave 3)."""
from __future__ import annotations

from textual.widgets import Static

from .parsers import render_stream


class StreamPanel(Static):
    """Polls the controller's stream every 2s."""

    def __init__(self, controller) -> None:
        # ``markup=False``: stream lines carry ambient topic tags like
        # ``⚠ [security] …`` — plain text, not Rich markup. Textual >= 8
        # swallows ``[tag]`` content with default markup (the topic label
        # vanished from the live feed). Render lines literally.
        super().__init__("(idle)", markup=False)
        self._controller = controller

    def on_mount(self) -> None:
        self.set_interval(2.0, self._refresh)
        self._refresh()

    def _refresh(self) -> None:
        self.update(render_stream(self._controller.stream_lines(8)))
