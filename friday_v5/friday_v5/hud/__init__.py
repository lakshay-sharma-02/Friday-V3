"""HUD — Friday's face: one Textual screen over the vault."""

from __future__ import annotations

from .app import HUD


def run_hud(engine=None, vault=None, notifier=None) -> int:
    """Launch the Textual HUD (blocking). Returns exit code."""
    try:
        from textual.app import App  # noqa: F401 - ensures dep present
    except Exception:
        print("HUD requires `textual` — run: pip install 'friday-v5[hud]'")
        return 1
    HUD(engine=engine, vault=vault, notifier=notifier).run()
    return 0
