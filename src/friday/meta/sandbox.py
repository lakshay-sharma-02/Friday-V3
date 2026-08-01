"""Sandbox execution — executes self-modifying plans in an isolated worktree.

Self-modifying code never touches the live Friday process or main branch
directly. Reuses the existing wave-based executor but points it at a sandbox
checkout of Friday's own repo.

Upgraded for multi-file capabilities (Self-Evolution Engine):
  - install_deps() — pip install packages inside the sandbox
  - read_file() — read a file from the sandbox checkout
  - file_exists() — check if a file exists in the sandbox
  - test_file() — run a specific test file
  - snapshot() — capture a git commit hash for rollback
  - rollback() — revert sandbox to a previous snapshot
  - dry_run() — simulate changes without modifying the sandbox
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
        self._base_commit: Optional[str] = None  # snapshot point for rollback

    @property
    def sandbox_path(self) -> Optional[str]:
        return self._sandbox_path

    @property
    def diff_path(self) -> Optional[str]:
        return self._diff_path

    @property
    def base_commit(self) -> Optional[str]:
        return self._base_commit

    # ── sandbox env ──────────────────────────────────────────────────────

    def sandbox_env(self) -> dict:
        """Return environment with PYTHONPATH pointing at the sandbox src.

        Ensures subprocesses (tests, code-generation tools) resolve imports
        from the sandbox copy of Friday, not the live installation.
        """
        env = os.environ.copy()
        if self._sandbox_path:
            sp_src = str(Path(self._sandbox_path) / "src")
            existing = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = (
                f"{sp_src}{os.pathsep}{existing}" if existing else sp_src
            )
        return env

    def create(self) -> str:
        """Create the sandbox. Returns the sandbox root path.

        Prefers a git worktree for diff-ability; falls back to a temp dir
        copy when the working tree is dirty (worktree won't accept dirty
        HEAD). Records the base commit for rollback.
        """
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

        # Record the base commit so we can snapshot/rollback.
        self._base_commit = self._git("rev-parse", "HEAD")

        # Install project runtime deps so the regression suite can run.
        # We do NOT `pip install -e .` — that would overwrite the live `friday`
        # CLI entry point (the sandbox version).  Instead we install only the
        # runtime dependencies (already satisfied globally since they are the
        # same project), then rely on PYTHONPATH to point at the sandbox source.
        # This way the live CLI stays intact.
        _deps_ok = False
        try:
            # Read pyproject.toml for runtime deps and install them.
            import tomllib
            pp = Path(sandbox_path) / "pyproject.toml"
            if pp.exists():
                pyproject = tomllib.loads(pp.read_text())
                deps = pyproject.get("project", {}).get("dependencies", [])
                if deps:
                    subprocess.run(
                        ["pip", "install", "--break-system-packages"] + deps,
                        capture_output=True, text=True, timeout=120,
                    )
                    _deps_ok = True
        except Exception:
            pass

        if not _deps_ok:
            # Fallback: install any deps that may be missing quietly.
            subprocess.run(
                ["pip", "install", "--break-system-packages",
                 "requests"],
                capture_output=True, text=True, timeout=60,
            )

        return sandbox_path

    def _git(self, *args: str) -> str:
        """Run a git command in the sandbox. Returns stdout."""
        if not self._sandbox_path:
            return ""
        result = subprocess.run(
            ["git"] + list(args),
            cwd=self._sandbox_path, capture_output=True, text=True, timeout=30,
        )
        return result.stdout.strip()

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
        env = self.sandbox_env()
        t0 = datetime.now(timezone.utc)
        try:
            result = subprocess.run(
                args, cwd=self._sandbox_path, env=env, capture_output=True,
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

    # ──────────────────────────────────────────────────────────────────────
    # Self-Evolution Engine upgrades
    # ──────────────────────────────────────────────────────────────────────

    def install_deps(self, deps: list[str]) -> dict:
        """Install pip packages inside the sandbox.

        Args:
            deps: List of package names (e.g. ["edge-tts", "faster-whisper"])

        Returns:
            {success, output, failed_packages}
        """
        if not deps:
            return {"success": True, "output": "No deps to install", "failed_packages": []}
        if not self._sandbox_path:
            return {"success": False, "output": "Sandbox not created", "failed_packages": deps}

        failed: list[str] = []
        output_parts: list[str] = []

        for dep in deps:
            try:
                result = subprocess.run(
                    ["pip", "install", "--break-system-packages", dep],
                    cwd=self._sandbox_path, capture_output=True, text=True, timeout=120,
                )
                if result.returncode == 0:
                    output_parts.append(f"  ✓ {dep}")
                else:
                    # Retry without --break-system-packages as fallback
                    result2 = subprocess.run(
                        ["pip", "install", dep],
                        cwd=self._sandbox_path, capture_output=True, text=True, timeout=120,
                    )
                    if result2.returncode == 0:
                        output_parts.append(f"  ✓ {dep}")
                    else:
                        failed.append(dep)
                        output_parts.append(f"  ✗ {dep}: {result.stderr[:100]}")
            except Exception as e:
                failed.append(dep)
                output_parts.append(f"  ✗ {dep}: {e}")

        success = len(failed) == 0
        return {
            "success": success,
            "output": "\n".join(output_parts),
            "failed_packages": failed,
        }

    def read_file(self, relative_path: str) -> str:
        """Read a file from the sandbox checkout.

        Args:
            relative_path: Path relative to sandbox root (e.g. "src/friday/cli.py")

        Returns:
            File contents as string, or empty string if file doesn't exist.
        """
        if not self._sandbox_path:
            return ""
        full = Path(self._sandbox_path) / relative_path
        try:
            return full.read_text(encoding="utf-8")
        except (OSError, IOError):
            return ""

    def file_exists(self, relative_path: str) -> bool:
        """Check if a file exists in the sandbox checkout."""
        if not self._sandbox_path:
            return False
        return (Path(self._sandbox_path) / relative_path).exists()

    def write_file(self, relative_path: str, content: str) -> bool:
        """Write a file to the sandbox checkout, creating directories.

        Args:
            relative_path: Path relative to sandbox root.
            content: File content to write.

        Returns:
            True on success.
        """
        if not self._sandbox_path:
            return False
        full = Path(self._sandbox_path) / relative_path
        try:
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(content, encoding="utf-8")
            return True
        except (OSError, IOError):
            return False

    def test_file(self, test_path: str) -> dict:
        """Run a specific test file and return {passed, output, duration_ms}.

        Args:
            test_path: Path relative to sandbox root (e.g. "tests/test_voice.py")

        Returns:
            {passed, output, duration_ms}
        """
        return self.run_tests([
            "python", "-m", "pytest", test_path, "-x", "--tb=short", "-v",
        ])

    def snapshot(self) -> Optional[str]:
        """Capture a git commit hash for rollback.

        Stages all changes and creates a snapshot commit. Returns the commit hash.
        """
        if not self._sandbox_path:
            return None
        self._git("add", "-A")
        # Check if there are changes to commit.
        status = self._git("status", "--porcelain")
        if status.strip():
            self._git("commit", "-m", f"snapshot before capability deploy ({now_iso()})")
        self._base_commit = self._git("rev-parse", "HEAD")
        return self._base_commit

    def rollback(self, commit_hash: Optional[str] = None) -> bool:
        """Revert sandbox to a previous snapshot.

        Args:
            commit_hash: Commit to reset to. If None, uses the base commit
                         (before any capability changes).

        Returns:
            True on success.
        """
        target = commit_hash or self._base_commit
        if not target or not self._sandbox_path:
            return False
        try:
            # Reset all tracked files to the target commit.
            subprocess.run(
                ["git", "reset", "--hard", target],
                cwd=self._sandbox_path, check=True, capture_output=True, text=True, timeout=30,
            )
            # Clean untracked files that the capability may have created.
            subprocess.run(
                ["git", "clean", "-fd"],
                cwd=self._sandbox_path, capture_output=True, text=True, timeout=15,
            )
            return True
        except subprocess.CalledProcessError:
            return False

    def dry_run(self, changes: list[dict]) -> dict:
        """Simulate changes without actually modifying the sandbox.

        Analyzes what WOULD change: files created, files modified, deps added.
        Useful for "what if I add voice support?" queries.

        Args:
            changes: List of change dicts, each with keys:
                - type: "new_file" | "modified_file" | "dependency" | "config_change"
                - path: file path (for files)
                - name: dependency name (for deps)
                - content_summary: brief description of content (optional)

        Returns:
            {created: [...], modified: [...], deps: [...], config_changes: [...]}
        """
        result: dict = {
            "created": [],
            "modified": [],
            "deps": [],
            "config_changes": [],
        }

        for c in changes:
            ctype = c.get("type", "")
            if ctype == "new_file":
                path = c.get("path", "?")
                summ = c.get("content_summary", "")
                result["created"].append(f"{path} ({summ})" if summ else path)
            elif ctype == "modified_file":
                path = c.get("path", "?")
                summ = c.get("content_summary", "")
                result["modified"].append(f"{path} ({summ})" if summ else path)
            elif ctype == "dependency":
                name = c.get("name", "?")
                result["deps"].append(name)
            elif ctype == "config_change":
                entry = c.get("name", c.get("path", "?"))
                result["config_changes"].append(entry)

        return result

    def run_claude_code(self, prompt: str, allowed_tools: list[str] | None = None,
                         model: str = "oc/deepseek-v4-flash-free") -> dict:
        """Run Claude Code inside the sandbox to implement a capability.

        Invokes ``claude --print --allowedTools ... -p "..."`` inside the sandbox
        directory. CC reads Friday's source code, creates/modifies files, runs
        tests, and iterates autonomously.

        Args:
            prompt: The capability request + context for CC.
            allowed_tools: Tools CC is allowed to use
                           (default: Write, Read, Bash, Edit).
            model: Model to use (default: oc/deepseek-v4-flash-free).

        Returns:
            {success, output, duration_ms, files_changed}
        """
        if not self._sandbox_path:
            return {
                "success": False,
                "output": "Sandbox not created",
                "duration_ms": 0,
                "files_changed": 0,
            }

        import shutil
        if not shutil.which("claude"):
            return {
                "success": False,
                "output": "Claude Code CLI not found on PATH",
                "duration_ms": 0,
                "files_changed": 0,
            }

        tools = allowed_tools or ["Write", "Read", "Bash", "Edit"]
        tools_arg = ",".join(tools)

        # Build the claude command.
        cmd = [
            "claude",
            "--print",  # headless mode, no interactive TUI
            "--model", model,
            "--allowedTools", tools_arg,
            "-p", prompt,
        ]

        t0 = datetime.now(timezone.utc)
        try:
            result = subprocess.run(
                cmd,
                cwd=self._sandbox_path,
                capture_output=True,
                text=True,
                timeout=300,  # 5 min default for CC
            )
        except subprocess.TimeoutExpired as e:
            dur = 300000
            return {
                "success": False,
                "output": f"TIMEOUT (300s)\n{e.output or ''}",
                "duration_ms": dur,
                "files_changed": 0,
            }
        except FileNotFoundError:
            return {
                "success": False,
                "output": "Claude Code CLI binary not found",
                "duration_ms": 0,
                "files_changed": 0,
            }

        dur = int((datetime.now(timezone.utc) - t0).total_seconds() * 1000)
        success = result.returncode == 0

        # Count files changed via git diff --stat.
        files_changed = 0
        diff_stat = subprocess.run(
            ["git", "diff", "--stat"],
            cwd=self._sandbox_path, capture_output=True, text=True, timeout=15,
        )
        # Count files in the diff stat (lines like "src/friday/foo.py | 10 ++++++++-")
        if diff_stat.returncode == 0:
            for line in diff_stat.stdout.splitlines():
                line = line.strip()
                if line and "|" in line and not line.startswith(" "):
                    files_changed += 1

        # Also count untracked/new files via git status.
        status_out = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=self._sandbox_path, capture_output=True, text=True, timeout=15,
        )
        if status_out.returncode == 0:
            for line in status_out.stdout.splitlines():
                line = line.strip()
                if line.startswith("??") or line.startswith("A "):
                    files_changed += 1

        output = (result.stdout or "") + "\n" + (result.stderr or "")
        return {
            "success": success,
            "output": output,
            "duration_ms": dur,
            "files_changed": files_changed,
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
            _empty_path = ""
            _diff_parts = line.split()
            if len(_diff_parts) >= 4 and _diff_parts[3].startswith("b/"):
                _empty_path = _diff_parts[3][2:]
            _peek_limit = min(i + 4, len(lines))
            for _pi in range(i, _peek_limit):
                if lines[_pi].startswith("--- /dev/null"):
                    _empty_path = ""
                    break
                if lines[_pi].startswith("diff --git "):
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
            if i + 1 < len(lines) and lines[i + 1].startswith("+++ b/"):
                current_path = lines[i + 1][6:]
                i += 2
                while i < len(lines):
                    if lines[i].startswith("@@"):
                        in_hunk = True
                        i += 1
                        break
                    if lines[i].startswith("diff --git "):
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
                i += 1
                continue
            if line.startswith("+") and not line.startswith("+++"):
                content_lines.append(line[1:])
            elif line.startswith("-"):
                pass
            elif line.startswith("\\ "):
                pass
            else:
                content_lines.append(line)
        i += 1

    if current_path:
        _write_new_file(root, current_path, content_lines)


def _write_new_file(root: Path, path: str, lines: list[str]) -> None:
    """Write content lines to a file under root, creating directories."""
    while lines and not lines[-1].strip():
        lines.pop()
    full = root / path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text("\n".join(lines) + "\n", encoding="utf-8")
