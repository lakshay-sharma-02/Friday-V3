"""Activity panel — tail of today's raw log."""
from __future__ import annotations

import datetime

from textual.widgets import Static

from .parsers import tail_log


def render_activity(lines: list[str]) -> str:
    return "\n".join(lines) if lines else "(no activity yet today)"


class ActivityPanel(Static):
    """Poll the latest raw log every 2s."""

    def __init__(self, vault) -> None:
        super().__init__("")
        self._vault = vault

    def on_mount(self) -> None:
        self.set_interval(2.0, self._refresh)

    def _refresh(self) -> None:
        day = datetime.date.today().isoformat()
        lines = tail_log(self._vault.raw / f"{day}.log", n=6)
        self.update(render_activity(lines))
