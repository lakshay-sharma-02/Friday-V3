"""Stream panel — live engine output (user + assistant turns)."""
from __future__ import annotations

from textual.widgets import Static


def render_stream(lines: list[tuple[str, bool]]) -> str:
    """(text, is_final) pairs → last ~8 rendered lines."""
    if not lines:
        return "(idle)"
    return "\n".join(t for t, _ in lines[-8:])


class StreamPanel(Static):
    """Renders the engine's on_output feed (set externally)."""

    def __init__(self) -> None:
        super().__init__("(idle)")
        self._lines: list[tuple[str, bool]] = []

    def push(self, text: str, final: bool) -> None:
        """Append one output chunk (called from the engine thread)."""
        self._lines.append((text, final))
        self.update(render_stream(self._lines))
