"""CLI command for Pillar B Stage 1 — Action Log read surface.

``friday actions``              — show most recent actions (50 by default).
``friday actions recent [n]``   — show last N actions.
``friday actions --source <s>`` — filter by source (friday, hyprland, browser).
"""

from __future__ import annotations

import argparse
import json

from .action_log import get_recent_actions
from .db import connect


def cmd_actions(args: argparse.Namespace) -> int:
    """Show recent action events from the actions table."""
    conn = connect()
    try:
        n = getattr(args, "n", 50)
        source = getattr(args, "source", None)
        rows = get_recent_actions(conn, limit=n, source=source)
    finally:
        conn.close()

    if not rows:
        print("No actions logged yet.")
        print()
        print("Friday logs actions when executors (HyprlandExecutor,")
        print("BrowserExecutor) execute successfully. Actions are also")
        print("derived from observation diffs by the daemon.")
        return 0

    # Column widths.
    src_w = max(len(r.get("source", "") or "") for r in rows)
    typ_w = max(len(r.get("action_type", "") or "") for r in rows)
    src_w = max(src_w, 8)
    typ_w = max(typ_w, 12)
    tgt_w = 50

    hdr = (
        f"{'observed_at':<28} {'source':<{src_w}} {'type':<{typ_w}} "
        f"{'target':<{tgt_w}} confidence"
    )
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        print(
            f"{(r.get('observed_at') or '-'):<28} "
            f"{(r.get('source') or ''):<{src_w}} "
            f"{(r.get('action_type') or ''):<{typ_w}} "
            f"{(r.get('target') or '')[:tgt_w]:<{tgt_w}} "
            f"{r.get('confidence') or ''}"
        )

    print()
    print(f"Total shown: {len(rows)}")
    return 0
