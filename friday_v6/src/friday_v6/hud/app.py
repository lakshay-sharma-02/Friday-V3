"""HUD — Friday's face: one Textual screen over the vault (Wave 3).

Layout (no tabs):

    ┌──────────┬──────────────────────────────┐
    │ VITALS   │  SCHEDULE (vault/schedule.md) │
    │ STREAM   │  NOTICES (vault/notices/)     │
    │ (live)   │  PERMISSIONS (allow/deny)     │
    │ INPUT    │  ACTIVITY (raw tail)          │
    └──────────┴──────────────────────────────┘

The vault is the source of truth for schedule/notices/activity; the
stream mirrors the durable ambient bus; the input box routes through
the SAME :class:`~friday_v6.nl_router.TextCommandHandler` as voice,
CLI, web, and phone — one presence, one brain. Permission asks are
the same durable asks the autonomy loop raises, resolved here with
real buttons. Panels poll (~2s); vitals refresh each second.
"""
from __future__ import annotations

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header, Static

from .activity_panel import ActivityPanel
from .commands_panel import CommandsPanel
from .notices_panel import NoticesPanel
from .permissions_panel import PermissionsPanel
from .prompt import PromptPanel
from .schedule_panel import SchedulePanel
from .stream_panel import StreamPanel
from .vitals import _read, format_vitals


class VitalsWidget(Static):
    """Vitals panel — refreshes every second (psutil optional)."""

    def on_mount(self) -> None:
        self.set_interval(1.0, self._refresh)
        self._refresh()

    def _refresh(self) -> None:
        self.update(format_vitals(*_read()))


class HUD(App):
    """One-screen Friday HUD."""

    TITLE = "FRIDAY V6"
    BINDINGS = [("q", "quit", "quit"), ("c", "clear_stream", "clear")]

    def __init__(self, controller) -> None:
        super().__init__()
        self._controller = controller

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            with Vertical(id="left"):
                yield VitalsWidget()
                yield CommandsPanel()
                yield StreamPanel(self._controller)
                yield PromptPanel(self._controller)
            with Vertical(id="right"):
                yield SchedulePanel(self._controller)
                yield NoticesPanel(self._controller)
                yield PermissionsPanel(self._controller)
                yield ActivityPanel(self._controller)
        yield Footer()

    def action_clear_stream(self) -> None:
        try:
            self._controller.push("── stream cleared ──")
        except Exception:
            pass


__all__ = ["HUD"]
