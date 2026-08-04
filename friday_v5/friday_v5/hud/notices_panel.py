"""Notices panel — latest proactive pings from vault/notices/."""
from __future__ import annotations

from textual.widgets import Static


def render_notices(notices: list[dict]) -> str:
    if not notices:
        return "no notices"
    return "\n".join(f"· {n['text']}" for n in notices)


class NoticesPanel(Static):
    """Poll vault/notices every 2s."""

    def __init__(self, vault) -> None:
        super().__init__("")
        self._vault = vault

    def on_mount(self) -> None:
        self.set_interval(2.0, self._refresh)

    def _refresh(self) -> None:
        self.update(render_notices(self._vault.latest_notices(5)))
