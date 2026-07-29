"""CLI for `friday undo` — rollback and undo management.

Usage::

    friday undo                     # Undo the last mutating action
    friday undo <action_id>         # Undo a specific action by execution ID
    friday undo --list              # Show recent undoable actions
"""

from __future__ import annotations

import argparse


def cmd_undo(args: argparse.Namespace) -> int:
    """Dispatch `friday undo`."""
    list_mode = getattr(args, "list", False)
    action_id = getattr(args, "action_id", None)

    if list_mode:
        return _list_undoable()
    return _undo(action_id)


def _undo(action_id: str | None = None) -> int:
    """Undo the last mutating action, or a specific one by ID.

    Rollback is best-effort: snapshots are taken before mutating actions
    and restored here. If no rollback snapshot exists, the action cannot
    be undone.
    """
    from .db import connect, now_iso
    conn = connect()

    try:
        if action_id:
            row = conn.execute(
                "SELECT * FROM rollback_snapshots WHERE action_id = ? "
                "ORDER BY id DESC LIMIT 1",
                (action_id,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM rollback_snapshots WHERE restored_at IS NULL "
                "ORDER BY id DESC LIMIT 1",
            ).fetchone()

        if row is None:
            if action_id:
                print(f"  No rollback snapshot found for action '{action_id}'.")
            else:
                print("  No undoable actions found.")
            conn.close()
            return 1

        snap = dict(row)
        action_desc = snap.get("action_desc", "?")
        snap_path = snap.get("snapshot_path", "")
        snap_type = snap.get("snapshot_type", "file")
        reversible = bool(snap.get("reversible", 1))

        if not reversible:
            print(f"  Action '{action_desc}' is irreversible — no rollback available.")
            conn.close()
            return 1

        if not snap_path:
            print(f"  No snapshot path for action '{action_desc}'.")
            conn.close()
            return 1

        print(f"  Restoring snapshot for: {action_desc}")
        print(f"  Snapshot: {snap_path} ({snap_type})")

        # For file snapshots, the snapshot is stored in .friday/rollback/
        # Restoration would copy files back to their original locations.
        # For git snapshots, the HEAD SHA is recorded and can be restored.
        if snap_type == "git" and snap.get("head_sha"):
            import subprocess
            try:
                result = subprocess.run(
                    ["git", "reset", "--hard", snap["head_sha"]],
                    capture_output=True, text=True, timeout=30,
                )
                if result.returncode == 0:
                    print(f"  ✅ Git restored to {snap['head_sha'][:12]}.")
                else:
                    print(f"  ❌ Git restore failed: {result.stderr[:200]}")
            except Exception as exc:
                print(f"  ❌ Git restore error: {exc}")
                conn.close()
                return 1
        elif snap_type == "file":
            import shutil
            import os
            snap_dir = snap_path
            if os.path.isdir(snap_dir):
                # Snapshot directory contains copies of original files.
                # Copy them back (best-effort).
                restored = 0
                for root, dirs, files in os.walk(snap_dir):
                    for f in files:
                        src = os.path.join(root, f)
                        # Determine original location from relative path
                        rel = os.path.relpath(src, snap_dir)
                        dst = os.path.abspath(rel)
                        try:
                            os.makedirs(os.path.dirname(dst), exist_ok=True)
                            shutil.copy2(src, dst)
                            restored += 1
                        except Exception:
                            pass
                print(f"  ✅ Restored {restored} file(s) from snapshot.")
            else:
                print(f"  Snap path not found: {snap_dir}")
                conn.close()
                return 1
        else:
            print(f"  Unknown snapshot type: {snap_type}")

        # Mark as restored.
        conn.execute(
            "UPDATE rollback_snapshots SET restored_at = ? WHERE id = ?",
            (now_iso(), snap["id"]),
        )
        conn.commit()
        print(f"  ✅ Undo complete for: {action_desc}")
    except Exception as exc:
        print(f"  ❌ Undo failed: {exc}")
        conn.close()
        return 1
    finally:
        conn.close()

    return 0


def _list_undoable() -> int:
    """List recent undoable actions."""
    from .db import connect
    conn = connect()

    try:
        rows = conn.execute(
            "SELECT id, action_id, action_desc, action_type, snapshot_type, "
            "reversible, restored_at, created_at FROM rollback_snapshots "
            "ORDER BY id DESC LIMIT 20"
        ).fetchall()

        if not rows:
            print("  No rollback snapshots found.")
            conn.close()
            return 0

        print(f"  {'ID':<5} {'Action':<40} {'Type':<12} {'Reversible':<12} {'Restored':<10}")
        print(f"  {'-'*82}")
        for r in rows:
            rid = r["id"]
            desc = (r["action_desc"] or "?")[:38]
            atype = (r["action_type"] or "?")[:10]
            rev = "✅" if r["reversible"] else "❌"
            restored = "✅" if r["restored_at"] else "—"
            print(f"  {rid:<5} {desc:<40} {atype:<12} {rev:<12} {restored:<10}")
        conn.close()
        return 0
    except Exception as exc:
        print(f"  error: {exc}")
        conn.close()
        return 1


def add_subparser(sub) -> None:
    """Add the ``undo`` subcommand parser."""
    p = sub.add_parser(
        "undo",
        help="Undo the last mutating action (rollback).",
        description="Rollback the last mutating action or a specific one by execution ID.",
    )
    p.add_argument(
        "action_id", nargs="?", default=None,
        help="Action ID to undo (omit to undo the most recent).",
    )
    p.add_argument("--list", "-l", action="store_true",
                    help="Show recent undoable actions.")
    p.set_defaults(func=cmd_undo)
