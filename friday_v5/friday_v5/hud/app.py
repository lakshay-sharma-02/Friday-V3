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
from .commands_panel import CommandsPanel
from .notices_panel import NoticesPanel
from .permissions_panel import PermissionsPanel
from .prompt import PromptPanel
from .schedule_panel import SchedulePanel
from .stream_panel import StreamPanel
from .vitals import Vitals


class HUD(App):
    """One-screen Friday HUD."""

    TITLE = "FRIDAY V5"

    def __init__(self, engine: Engine | None = None,
                 vault: Vault | None = None,
                 notifier=None) -> None:
        super().__init__()
        self.engine = engine or Engine(vault=vault or Vault())
        self.vault = vault or self.engine.vault
        self.notifier = notifier  # VoiceNotifier (optional; W6 wiring)
        self.stream_panel = StreamPanel()

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            with Vertical(id="left"):
                yield Vitals()
                yield CommandsPanel()
                yield PermissionsPanel(self.engine)
                yield self.stream_panel
                yield PromptPanel(self.engine)
            with Vertical(id="right"):
                yield SchedulePanel(self.vault)
                yield NoticesPanel(self.vault)
                yield ActivityPanel(self.vault)
        yield Footer()

    def on_mount(self) -> None:
        self.set_interval(2.0, self._poll_vault)

    def _poll_vault(self) -> None:
        # Live stream: engine output → stream panel (+ notifier for
        # proactive pings). The vault polls are the panels' own timers.
        if self.engine.on_output is None:
            def _fwd(text: str, final: bool) -> None:
                self.stream_panel.push(text, final)
                if final and self.notifier is not None:
                    try:
                        self.notifier.notify(text)
                    except Exception:
                        pass
            self.engine.on_output = _fwd
        # Proactive watcher: surface new notices in the notifier's
        # stream (HUD's NoticesPanel already polls latest_notices).
        # Guarded so the 2s timer never rebuilds the watcher (which
        # would re-fire every existing notice).
        if not hasattr(self, "_proactive"):
            from ..proactive import Proactive
            self._proactive = Proactive(vault_root=self.vault.root,
                                        interval=2.0)
            self._proactive.on_notice = lambda n: self.stream_panel.push(
                f"notice: {n['text']}", final=False)
            self._proactive.start()
