"""CLI commands for Operator Identity (Phase 1).

`friday profile show`      — print the full OperatorProfile.
`friday profile set <key> <value>` — write one explicit preference.
`friday profile unset <key>`       — delete one explicit preference.

Nothing here writes to decision points — this is model + population only.
"""

from __future__ import annotations

import argparse
import sys

from .db import connect, set_operator_preference, unset_operator_preference
from .operator import build_operator_profile


def cmd_profile_show(args: argparse.Namespace) -> int:
    """Print the full OperatorProfile.

    Evidence-derived fields and explicit preferences are clearly separated,
    matching the same rendering discipline as cli_identity.py.
    """
    conn = connect()
    profile = build_operator_profile(conn)
    conn.close()

    print("Operator Profile")
    print()
    print("--- Evidence-derived ---")

    cap = profile.capability_approval_rate
    if cap:
        rate_pct = round(cap["rate"] * 100)
        print(f"  capability_approval_rate:  {cap['approved']}/{cap['total']} "
              f"approved ({rate_pct}%)")
        if cap["pending"]:
            print(f"                            {cap['pending']} pending")
        if cap["rejected"]:
            print(f"                            {cap['rejected']} rejected")
    else:
        print("  capability_approval_rate:  (no proposals yet — not enough evidence)")

    gr = profile.graph_review_pattern
    if gr:
        parts = []
        approved = gr.get("approved", 0)
        rejected = gr.get("rejected", 0)
        proposals = gr.get("proposal", 0)
        if approved:
            parts.append(f"{approved} approved")
        if rejected:
            parts.append(f"{rejected} rejected")
        if proposals:
            parts.append(f"{proposals} pending review")
        print(f"  graph_review_pattern:       {', '.join(parts) if parts else '(none)'}")
    else:
        print("  graph_review_pattern:       (no reviewed graphs yet — not enough evidence)")

    print()
    print("--- Explicit preferences ---")
    pref = profile.explicit_preferences
    if pref:
        for key, value in sorted(pref.items()):
            print(f"  {key}: {value}")
    else:
        print("  (none set — use `friday profile set <key> <value>` to add)")

    if not profile.has_profile:
        print()
        print("Profile is empty. Evidence-derived fields will populate as you")
        print("approve/reject proposals. Add explicit preferences with:")
        print("  friday profile set prefers_additive_changes true")

    return 0


def cmd_profile_set(args: argparse.Namespace) -> int:
    """Set one explicit operator preference.

    Writes one row to operator_preferences with source='explicit'.
    Never writes to any decision table — this is model + population only.
    """
    key = getattr(args, "key", None)
    value = getattr(args, "value", None)
    if not key or not value:
        print("error: both key and value are required: friday profile set <key> <value>",
              file=sys.stderr)
        return 2

    conn = connect()
    set_operator_preference(conn, key=key, value=value, source="explicit")
    conn.close()
    print(f"Set: {key} = {value}")
    return 0


def cmd_profile_unset(args: argparse.Namespace) -> int:
    """Delete one operator preference by key.

    Removes one row from operator_preferences. Silent if the key didn't exist.
    """
    key = getattr(args, "key", None)
    if not key:
        print("error: key required: friday profile unset <key>",
              file=sys.stderr)
        return 2

    conn = connect()
    removed = unset_operator_preference(conn, key)
    conn.close()
    if removed:
        print(f"Unset: {key}")
    else:
        print(f"No preference found for '{key}'")
    return 0


def cmd_profile(args: argparse.Namespace) -> int:
    """Dispatch friday profile [show|set|unset]."""
    action = getattr(args, "action", "show")

    if action == "show":
        return cmd_profile_show(args)
    elif action == "set":
        return cmd_profile_set(args)
    elif action == "unset":
        return cmd_profile_unset(args)
    else:
        print(f"error: unknown action: {action}", file=sys.stderr)
        print("usage: friday profile <show|set|unset>", file=sys.stderr)
        return 2
