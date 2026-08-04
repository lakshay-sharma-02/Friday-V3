"""Permissions panel — pending tool asks + allow/deny buttons."""
from __future__ import annotations

from textual.widgets import Static


def render_permissions(pending: list[dict]) -> str:
    if not pending:
        return "no pending asks"
    return "\n".join(
        f"[{p['id']}] {p.get('summary', '')}  [allow] [deny]"
        for p in pending)


def _ask_from_file(path) -> dict:
    """Read one ``vault/permissions/pending/<id>.md`` ask into a dict.

    The vault is the source of truth: id is the file stem (the
    operator may be a different process, so in-memory registry state
    is not reliable here); summary is the first ```-fenced block.
    """
    try:
        body = path.read_text(encoding="utf-8")
    except OSError:
        return {"id": path.stem, "summary": ""}
    summary = body.split("```", 2)[1].strip() if "```" in body else ""
    return {"id": path.stem, "summary": summary}


class PermissionsPanel(Static):
    """Poll pending asks; buttons drive Engine.allow/deny."""

    def __init__(self, engine) -> None:
        super().__init__("")
        self._engine = engine

    def on_mount(self) -> None:
        self.set_interval(2.0, self._refresh)

    def _refresh(self) -> None:
        from ..permissions import VaultPermissions
        store = VaultPermissions(self._engine.vault.root)
        asks = [_ask_from_file(p) for p in store.pending_files()]
        self.update(render_permissions(asks))
