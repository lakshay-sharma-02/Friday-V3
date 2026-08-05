"""Commands panel — static hint of available keybindings."""
from __future__ import annotations

from textual.widgets import Static


def render_commands() -> str:
    return "[ask] type below   [end] session   [quit] q"


class CommandsPanel(Static):
    """Static command deck hint."""

    def __init__(self) -> None:
        super().__init__(render_commands())
