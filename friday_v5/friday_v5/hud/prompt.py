"""Prompt panel — typed input routed to the same Engine as voice."""
from __future__ import annotations

from textual.containers import Vertical
from textual.widgets import Input, Static


class PromptPanel(Vertical):
    """Input box → Engine.ask; stream output to a sibling Static."""

    def __init__(self, engine) -> None:
        super().__init__()
        self._engine = engine
        self._output = Static("")

    def compose(self):
        yield self._output
        yield Input(placeholder="ask Friday…")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        event.input.value = ""
        self._output.update(f"you: {text}")
        self._engine.ask(text)
