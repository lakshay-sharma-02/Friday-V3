"""Permissions panel — pending tool asks + allow/deny buttons (Wave 3).

The asks are the SAME durable permission requests the autonomy loop
raises for every surface (phone, web, CLI). The HUD resolves them
through ``HudController.allow/deny`` — one ask, every surface.
"""
from __future__ import annotations

from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Static


def _ask_lines(pending: list[dict]) -> str:
    """One line per pending ask: ``[id] description``."""
    if not pending:
        return "no pending asks"
    return "\n".join(
        f"[{p.get('id', '?')}] "
        f"{p.get('description') or p.get('command') or p.get('action_type') or ''}"
        for p in pending)


class PermissionsPanel(Vertical):
    """Summary of pending asks + real allow/deny buttons per ask."""

    def __init__(self, controller) -> None:
        super().__init__()
        self._controller = controller
        self._asks: list[dict] = []
        # ``markup=False``: the summary line embeds request ids as
        # ``[<id>] description`` — plain text, not Rich markup. Textual
        # >= 8 parses ``[...]`` as markup tags, so a hex id that happens
        # to start with a letter (a valid tag name) would be swallowed
        # from the display (flaky, id-dependent). Render it literally.
        self._summary = Static("", markup=False)
        #: Button id → (action, request id) — request ids may contain
        #: arbitrary characters, so they never go into CSS ids; the map
        #: carries them instead.
        self._button_map: dict[str, tuple[str, str]] = {}
        self._rows: list[Horizontal] = []

    def compose(self):
        yield self._summary

    def on_mount(self) -> None:
        self.set_interval(2.0, self._refresh)
        self._refresh()

    def _refresh(self) -> None:
        self._asks = self._controller.pending_asks() or []
        self._summary.update(_ask_lines(self._asks))
        # Rebuild the action buttons: remove the previous rows, then
        # mount one row per ask (parent already in the tree, so the
        # buttons can mount into it directly).
        for row in self._rows:
            row.remove()
        self._rows = []
        self._button_map = {}
        for i, ask in enumerate(self._asks):
            rid = ask.get("id")
            if not rid:
                continue
            # Textual >= 8 refuses ``row.mount(child)`` on a row that is
            # not itself mounted yet ("Can't mount widget(s) before
            # Horizontal() is mounted"). Passing the buttons as children
            # to the row constructor works on every supported Textual
            # (0.80 through 8.x): the row mounts already-composed, then
            # ``self.mount(row)`` attaches the whole subtree at once.
            allow = Button("allow", id=f"allow-{i}")
            deny = Button("deny", id=f"deny-{i}", variant="error")
            self._button_map[allow.id] = ("allow", rid)
            self._button_map[deny.id] = ("deny", rid)
            row = Horizontal(allow, deny)
            self.mount(row)
            self._rows.append(row)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        action, rid = self._button_map.get(event.button.id or "",
                                           ("", ""))
        if not rid:
            return
        if action == "allow":
            self._controller.allow(rid)
        elif action == "deny":
            self._controller.deny(rid)
        self._refresh()
