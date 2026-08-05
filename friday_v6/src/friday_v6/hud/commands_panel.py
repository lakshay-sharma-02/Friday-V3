"""Commands panel — static hint of available keybindings."""
from __future__ import annotations

from textual.widgets import Static

from .parsers import render_commands


class CommandsPanel(Static):
    """Static command deck hint."""

    def __init__(self) -> None:
        # ``markup=False``: the deck is plain text ("[ask] ... [/find
        # term] ...") and Textual >= 8 parses ``[...]`` as Rich markup
        # — an unmatched closing tag (``[/find term]``) raises a fatal
        # MarkupError. Render the hint literally.
        super().__init__(render_commands(), markup=False)
