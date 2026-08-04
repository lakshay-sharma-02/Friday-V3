"""HUD — Friday's face: one Textual screen over the vault.

Layout (no tabs):

    ┌──────────┬────────────────────────────────┐
    │ VITALS   │  STREAM (engine output, live)  │
    │ COMMANDS │  SCHEDULE (wiki/schedule.md)   │
    │ INPUT    │  NOTICES (vault/notices/)      │
    │          │  ACTIVITY (raw tail)           │
    └──────────┴────────────────────────────────┘

The vault is the single source of truth; panels poll it (~2s). The
input box routes to the same Engine as voice; permission asks render
as allow/deny buttons driving ``Engine.allow()/deny()``.
"""
from __future__ import annotations

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header

from ..engine import Engine
from ..vault import Vault
from .activity_panel import ActivityPanel
from .notices_panel import NoticesPanel
from .permissions_panel import PermissionsPanel
from .prompt import PromptPanel
from .schedule_panel import SchedulePanel
from .vitals import Vitals


class HUD(App):
    """One-screen Friday HUD."""

    TITLE = "FRIDAY V5"

    def __init__(self, engine: Engine | None = None,
                 vault: Vault | None = None) -> None:
        super().__init__()
        self.engine = engine or Engine(vault=vault or Vault())
        self.vault = vault or self.engine.vault

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            with Vertical(id="left"):
                yield Vitals()
                yield PermissionsPanel(self.engine)
                yield PromptPanel(self.engine)
            with Vertical(id="right"):
                yield SchedulePanel(self.vault)
                yield NoticesPanel(self.vault)
                yield ActivityPanel(self.vault)
        yield Footer()

    def on_mount(self) -> None:
        self.set_interval(2.0, self._poll_vault)

    def _poll_vault(self) -> None:
        # Panels are passive Static widgets; they re-render on their
        # own timers from vault reads. This hook is for engine-side
        # checks (permissions pending) — see PermissionsPanel.
        pass
