"""HUD — Friday's face: one Textual screen over the vault (Wave 3).

Another surface of the same Friday: the input routes through the same
``TextCommandHandler``, the stream mirrors the durable ambient bus,
and the permission buttons resolve the same asks as phone/web/CLI.
Textual is an *optional* dependency: without it ``run_hud`` degrades
to a printed hint and the CLI stays green (never-crash law). The pure
parser/render/controller layer has no Textual import, so hermetic
tests never need it.
"""

from __future__ import annotations

from .controller import HudController
from .parsers import (  # noqa: F401 - re-exported for consumers/tests
    _find_terms,
    format_ambient_event,
    parse_notice_text,
    parse_schedule,
    render_activity,
    render_commands,
    render_notices,
    render_permissions,
    render_schedule,
    render_search,
    render_stream,
    tail_log,
)
from .vitals import _read, format_vitals  # noqa: F401

__all__ = [
    "HudController", "run_hud", "is_available",
    "format_vitals", "_read",
    "_find_terms", "parse_schedule", "parse_notice_text", "tail_log",
    "render_stream", "render_schedule", "render_notices",
    "render_activity", "render_permissions", "render_commands",
    "render_search", "format_ambient_event",
]


def is_available() -> bool:
    """True when Textual is installed (the HUD can run)."""
    try:
        import textual  # noqa: F401
        return True
    except Exception:
        return False


def run_hud(conn=None, vault_root=None, desktop_handler=None,
            llm=None) -> int:
    """Launch the Textual HUD (blocking). Returns exit code.

    Degrades honestly when Textual is missing — never crashes, never a
    dead-end (the CLI stays fully usable).
    """
    try:
        import textual  # noqa: F401 - ensures dep present
    except Exception:
        print("HUD requires `textual` — run: "
              "pip install 'friday-v6[hud]'")
        return 1
    from .app import HUD
    controller = HudController(conn=conn, vault_root=vault_root,
                               desktop_handler=desktop_handler, llm=llm)
    HUD(controller=controller).run()
    return 0
