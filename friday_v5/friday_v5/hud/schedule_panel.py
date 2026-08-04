"""Schedule panel — agenda from vault/wiki/schedule.md."""
from __future__ import annotations

from textual.widgets import Static

from .parsers import parse_schedule


def render_schedule(items: list[str]) -> str:
    if not items:
        return "nothing scheduled"
    return "\n".join(f"· {i}" for i in items)


class SchedulePanel(Static):
    """Poll wiki/schedule.md every 2s."""

    def __init__(self, vault) -> None:
        super().__init__("")
        self._vault = vault

    def on_mount(self) -> None:
        self.set_interval(2.0, self._refresh)

    def _refresh(self) -> None:
        items = parse_schedule(self._vault.wiki / "schedule.md")
        self.update(render_schedule(items))
