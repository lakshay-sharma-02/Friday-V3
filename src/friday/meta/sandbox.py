"""Sandbox execution — executes self-modifying plans in an isolated worktree.

Self-modifying code never touches the live Friday process or main branch
directly. Reuses the existing wave-based executor but points it at a sandbox
checkout of Friday's own repo.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..db import now_iso


class Sandbox:
    """Isolated worktree for self-improvement execution.

    Creates a git worktree (or plain copy) of Friday's repo, executes the
    TaskGraph inside it, and reports results without touching the live
    checkout.
    """

    def __init__(self, repo_path: Optional[str] = None, label: str = "meta"):
        self._friday_root = repo_path or _find_friday_root()
        self._label = label
        self._sandbox_path: Optional[str] = None
        self._diff_path: Optional[str] = None

    @property
    def sandbox_path(self) -> Optional[str]:
        return self._sandbox_path

    @property
    def diff_path(self) -> Optional[str]:
        return self._diff_path

    def create(self) -> str:
        """Create the sandbox. Returns the sandbox root path.

        Prefers a git worktree for diff-ability; falls back to a temp dir
        copy when the working tree is dirty (worktree won't accept dirty
        HEAD)."""
        self._cleanup()

        root = Path(self._friday_root).resolve()
        if not (root / ".git").exists():
            raise RuntimeError(f"Not a git repository: {root}")

        # Try git worktree first (produces clean diffs).
        sandbox = tempfile.mkdtemp(prefix=f"friday_sandbox_{self._label}_")
        sandbox_path = str(Path(sandbox) / "friday")

        # Check if working tree is clean enough for a worktree.
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=root, capture_output=True, text=True, timeout=10,
        )
        is_clean = not status.stdout.strip()

        if is_clean:
            try:
                subprocess.run(
                    ["git", "worktree", "add", sandbox_path, "HEAD"],
                    cwd=root, check=True, capture_output=True, text=True, timeout=30,
                )
            except subprocess.CalledProcessError:
                is_clean = False  # fall through to copy

        if not is_clean:
            # Copy instead of worktree.
            shutil.copytree(root, sandbox_path,
                            ignore=shutil.ignore_patterns(
                                "__pycache__", ".git", "*.pyc", ".pytest_cache",
                                "node_modules", ".venv", "venv",
                            ))
            # Re-init git so operations work inside the copy.
            subprocess.run(
                ["git", "init"],
                cwd=sandbox_path, capture_output=True, text=True, timeout=15,
            )
            subprocess.run(
                ["git", "add", "-A"],
                cwd=sandbox_path, capture_output=True, text=True, timeout=30,
            )
            subprocess.run(
                ["git", "commit", "-m", "sandbox base"],
                cwd=sandbox_path, capture_output=True, text=True, timeout=30,
            )

        self._sandbox_path = sandbox_path

        # Install project deps so the regression suite can run.
        try:
            subprocess.run(
                ["pip", "install", "-e", "."],
                cwd=sandbox_path, capture_output=True, text=True, timeout=120,
            )
        except subprocess.CalledProcessError as e:
            print(f"  warning: pip install failed in sandbox: {e.stderr[:200]}")
        except Exception as e:
            print(f"  warning: pip install error: {e}")

        return sandbox_path

    def apply_patch(self, patch_content: str) -> None:
        """Apply a patch to the sandbox."""
        if not self._sandbox_path:
            raise RuntimeError("Sandbox not created yet")
        with tempfile.NamedTemporaryFile(
                mode="w", suffix=".patch", delete=False
        ) as f:
            f.write(patch_content)
            patch_file = f.name
        try:
            subprocess.run(
                ["git", "am", patch_file],
                cwd=self._sandbox_path, check=True, capture_output=True,
                text=True, timeout=30,
            )
        except subprocess.CalledProcessError:
            # Try plain apply instead.
            subprocess.run(
                ["git", "apply", patch_file],
                cwd=self._sandbox_path, check=True, capture_output=True,
                text=True, timeout=30,
            )
            subprocess.run(
                ["git", "add", "-A"],
                cwd=self._sandbox_path, capture_output=True, text=True, timeout=15,
            )
            subprocess.run(
                ["git", "commit", "-m", "self-improvement patch"],
                cwd=self._sandbox_path, capture_output=True, text=True, timeout=15,
            )
        finally:
            os.unlink(patch_file)

    def run_tests(self, test_args: list[str] | None = None) -> dict:
        """Run the test suite inside the sandbox. Returns {passed, output, duration_ms}."""
        if not self._sandbox_path:
            raise RuntimeError("Sandbox not created yet")
        args = test_args or ["python", "-m", "pytest", "tests/", "-x", "--tb=short"]
        t0 = datetime.now(timezone.utc)
        try:
            result = subprocess.run(
                args, cwd=self._sandbox_path, capture_output=True,
                text=True, timeout=120,
            )
        except subprocess.TimeoutExpired as e:
            return {
                "passed": False,
                "output": f"TIMEOUT (120s)\n{e.output or ''}",
                "duration_ms": 120000,
            }
        dur = int((datetime.now(timezone.utc) - t0).total_seconds() * 1000)
        passed = result.returncode == 0
        return {
            "passed": passed,
            "output": (result.stdout or "") + "\n" + (result.stderr or ""),
            "duration_ms": dur,
        }

    def capture_diff(self) -> str:
        """Capture the diff between the sandbox and base, return it as text.

        Stages new/untracked files first so ``git diff HEAD`` captures both
        modified and newly-added files (LLM-generated modules etc.).
        """
        if not self._sandbox_path:
            return ""
        # Stage so new files appear in the diff.
        subprocess.run(
            ["git", "add", "-A"],
            cwd=self._sandbox_path, capture_output=True, text=True, timeout=15,
        )
        result = subprocess.run(
            ["git", "diff", "HEAD"],
            cwd=self._sandbox_path, capture_output=True, text=True, timeout=15,
        )
        diff = result.stdout.strip()
        if diff:
            # Write diff to a temp file for permanent reference.
            diff_dir = Path(self._sandbox_path).parent
            diff_file = diff_dir / "changes.diff"
            diff_file.write_text(diff)
            self._diff_path = str(diff_file)
        return diff

    def cleanup(self) -> None:
        """Remove the sandbox and its worktree registration."""
        self._cleanup()

    def _cleanup(self) -> None:
        if not self._sandbox_path:
            return
        sp = Path(self._sandbox_path)
        if not sp.exists():
            self._sandbox_path = None
            return
        # Check if it's a git worktree (has .git file pointing elsewhere).
        git_file = sp / ".git"
        if git_file.is_file():
            # Remove the worktree from the parent repo first.
            root = Path(self._friday_root)
            try:
                subprocess.run(
                    ["git", "worktree", "remove", str(sp)],
                    cwd=root, capture_output=True, text=True, timeout=30,
                )
            except Exception:
                pass
        # Force-remove whatever remains.
        shutil.rmtree(str(sp), ignore_errors=True)
        self._sandbox_path = None


def _find_friday_root() -> str:
    """Find the root of Friday's own repository."""
    cwd = Path(__file__).resolve()
    # Walk up from src/friday/meta/__init__.py.
    for parent in cwd.parents:
        if (parent / ".git").exists() and (parent / "pyproject.toml").exists():
            return str(parent)
    # Fallback to cwd.
    return str(Path.cwd())


def _apply_diff_files(sandbox_root: str, diff_content: str) -> None:
    """Write new files from a git diff into the sandbox.

    Parses unified-diff ``--- /dev/null`` / ``+++ b/path`` headers for
    newly-added files and writes their content (everything after ``@@``
    and the leading ``+`` markers). Existing-file diffs are skipped —
    this is meant to bring generated worker modules into a fresh sandbox.
    """
    import re
    root = Path(sandbox_root)
    lines = diff_content.splitlines()
    i = 0
    current_path: str | None = None
    content_lines: list[str] = []
    in_hunk = False

    while i < len(lines):
        line = lines[i]

        # ``diff --git`` starts a new file entry — flush previous.
        if line.startswith("diff --git "):
            if current_path and content_lines:
                _write_new_file(root, current_path, content_lines)
            current_path = None
            content_lines = []
            in_hunk = False

            # Peek: detect empty new files (no ``--- /dev/null``/``+++ b/``
            # section follows — git omits them for 0-byte files).
            # Format: diff --git, new file mode, index, [next diff --git]
            _empty_path = ""
            # Extract the ``b/path`` from ``diff --git a/path b/path`` as
            # fallback when ``+++ b/`` is absent (empty files).
            _diff_parts = line.split()
            if len(_diff_parts) >= 4 and _diff_parts[3].startswith("b/"):
                _empty_path = _diff_parts[3][2:]
            _peek_limit = min(i + 4, len(lines))
            for _pi in range(i, _peek_limit):
                if lines[_pi].startswith("--- /dev/null"):
                    # Normal new file — clear the fallback path since the
                    # normal ---/+++ handler will process it.
                    _empty_path = ""
                    break
                if lines[_pi].startswith("diff --git "):
                    # New section started without --- /dev/null — empty file.
                    if _empty_path:
                        _write_new_file(root, _empty_path, [])
                    break
                if lines[_pi].startswith("+++ b/"):
                    _empty_path = lines[_pi][6:]

            i += 1
            continue

        # Detect new file: "--- /dev/null" followed by "+++ b/path"
        if line.startswith("--- /dev/null"):
            if current_path and content_lines:
                _write_new_file(root, current_path, content_lines)
                content_lines = []
            current_path = None
            in_hunk = False
            # Next line should be "+++ b/<path>"
            if i + 1 < len(lines) and lines[i + 1].startswith("+++ b/"):
                current_path = lines[i + 1][6:]  # strip "+++ b/"
                i += 2
                # Skip to the @@ hunk header.
                # If we hit a ``diff --git`` before ``@@``, the file is
                # empty (no hunk). Write it and re-process the next
                # diff --git line on the next outer loop iteration.
                while i < len(lines):
                    if lines[i].startswith("@@"):
                        in_hunk = True
                        i += 1
                        break
                    if lines[i].startswith("diff --git "):
                        # Empty file — write it now.
                        if current_path:
                            _write_new_file(root, current_path, content_lines)
                        current_path = None
                        content_lines = []
                        in_hunk = False
                        break
                    i += 1
                continue
        if in_hunk and current_path:
            if line.startswith("@@"):
                # New hunk in same file — flush previous, continue.
                i += 1
                continue
            if line.startswith("+") and not line.startswith("+++"):
                content_lines.append(line[1:])  # strip leading +
            elif line.startswith("-"):
                pass  # skip removed lines
            elif line.startswith("\\ "):
                # Git diff meta-line like "\ No newline at end of file".
                pass
            else:
                # Context line — part of the diff but not added content.
                # For new files this shouldn't happen, but include it.
                content_lines.append(line)
        i += 1

    # Flush last file (even if empty — trailing __init__.py, etc.).
    if current_path:
        _write_new_file(root, current_path, content_lines)


def _write_new_file(root: Path, path: str, lines: list[str]) -> None:
    """Write content lines to a file under root, creating directories."""
    # Strip trailing empty lines.
    while lines and not lines[-1].strip():
        lines.pop()
    full = root / path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text("\n".join(lines) + "\n", encoding="utf-8")
