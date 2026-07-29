"""CLI for What-If Sandbox — ``friday sandbox <action>``.

Simulates an action in an isolated temp directory and shows what WOULD
happen without any side effects on the real workspace.

Usage::

    friday sandbox "rm -rf node_modules"
    friday sandbox --file '{"op":"write","path":"README.md","content":"# Hi"}'
    friday sandbox --git "reset --hard HEAD~1"
    friday sandbox --json "ls -la"
    friday sandbox --diff-only "touch new_file.txt"
"""

from __future__ import annotations

import argparse
import json
import os

from .presentation.cli_format import header, green, yellow, red, gray


def cmd_sandbox(args: argparse.Namespace) -> int:
    """Run a what-if sandbox simulation."""
    from .db import connect
    from .sandbox import SandboxEngine

    action: str = args.action
    file_mode: bool = getattr(args, "file", False)
    git_mode: bool = getattr(args, "git", False)
    show_json: bool = getattr(args, "json", False)
    diff_only: bool = getattr(args, "diff_only", False)
    keep: bool = getattr(args, "keep", False)

    if not action.strip():
        print(red("  error: action cannot be empty"))
        print(gray("  Usage: friday sandbox \"command to simulate\""))
        return 1

    # Determine the repo path from CWD or first known repo.
    repo_path = _resolve_repo_path()

    engine = SandboxEngine(keep_sandbox=keep)

    if file_mode or (action.strip().startswith("{") and not git_mode):
        try:
            file_spec = json.loads(action)
        except json.JSONDecodeError as e:
            print(red(f"  error: --file requires valid JSON: {e}"))
            return 1
        result = engine.simulate_file(file_spec, repo_path=repo_path)
    else:
        result = engine.simulate(action, repo_path=repo_path)

    if show_json:
        print(json.dumps(result.to_dict(), indent=2, default=str))
        return 0

    if diff_only:
        if result.diff and result.diff != "(no diff — clean sandbox)":
            print(result.diff)
        else:
            print("(no diff — nothing changed)")
        return 0

    print(result.format())
    return 0


def _resolve_repo_path() -> str | None:
    """Walk up from CWD to find a git repo root, or None."""
    try:
        import subprocess
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass

    # Try finding a repo in the DB.
    try:
        from .db import connect
        conn = connect()
        row = conn.execute(
            "SELECT path FROM repositories WHERE path IS NOT NULL LIMIT 1"
        ).fetchone()
        conn.close()
        if row and row["path"]:
            return row["path"]
    except Exception:
        pass

    return None


# ---------------------------------------------------------------------------
# Subparser registration
# ---------------------------------------------------------------------------


def add_subparser(sub) -> None:
    """Add the ``sandbox`` subcommand parser."""
    p = sub.add_parser(
        "sandbox",
        help="What-If Sandbox — simulate an action without side effects.",
    )
    p.add_argument(
        "action",
        help="Action to simulate (shell command, or JSON for --file).",
    )
    p.add_argument(
        "--file", action="store_true",
        help="Treat action as a file-operation JSON",
    )
    p.add_argument(
        "--git", action="store_true",
        help="Force git-mode (action is a git subcommand)",
    )
    p.add_argument(
        "--json", action="store_true",
        help="Output raw JSON instead of formatted text.",
    )
    p.add_argument(
        "--diff-only", action="store_true",
        help="Show only the git diff, no header or file list.",
    )
    p.add_argument(
        "--keep", action="store_true",
        help="Keep the sandbox directory after simulation (for inspection).",
    )
    p.set_defaults(func=cmd_sandbox)
