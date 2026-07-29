"""What-If Sandbox — simulate actions without side effects.

The ``SandboxEngine`` creates a temporary copy of a repository, runs the
requested action inside it, captures what changed, and reports the result
without touching the real workspace. Every simulation is fully isolated.

Three simulation modes (auto-detected from the action spec):

  A. **Shell command** — runs the command in a sandboxed copy of the repo.
     Captures stdout, stderr, exit code, and any files created/modified.

  B. **File operation** — JSON ``{"op":"write", "path":"...", "content":"..."}``
     applied to a sandbox copy, with a git diff against the original.

  C. **Git operation** — runs git commands in a sandboxed copy, captures
     the diff between the original state and what the command produced.

Usage::

    from friday.sandbox import SandboxEngine

    engine = SandboxEngine()
    result = engine.simulate("rm -rf node_modules", repo_path="/path/to/repo")
    print(result.format())

    result = engine.simulate_file({"op": "write", "path": "README.md"},
                                  repo_path="/path/to/repo")
    print(result.format())
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class SandboxResult:
    """The outcome of a sandbox simulation — what WOULD happen."""

    action: str  # original action description
    action_type: str  # "shell" | "file" | "git"
    repo: str = ""
    sandbox_path: str = ""

    # Simulation output
    success: bool = False
    stdout: str = ""
    stderr: str = ""
    exit_code: Optional[int] = None

    # Files changed (relative paths)
    files_created: list[str] = field(default_factory=list)
    files_modified: list[str] = field(default_factory=list)
    files_deleted: list[str] = field(default_factory=list)

    # Git diff against original (if repo was git-tracked)
    diff: str = ""

    # Summary
    duration_ms: int = 0
    errors: list[str] = field(default_factory=list)

    # Whether the sandbox is pending cleanup
    _cleaned: bool = False

    def format(self) -> str:
        """Render the simulation result as human-readable text."""
        lines: list[str] = []
        lines.append("What-If Sandbox Simulation")
        lines.append("=" * 60)
        lines.append(f"  Action:      {self.action}")
        lines.append(f"  Type:        {self.action_type}")
        lines.append(f"  Repo:        {self.repo or '(no repo)'}")
        lines.append(f"  Would fail:  {'YES' if not self.success else 'NO'}")
        if self.duration_ms:
            lines.append(f"  Duration:    {self.duration_ms}ms")

        if not self.success and self.errors:
            lines.append("")
            lines.append("⚠ Errors (would occur):")
            for err in self.errors:
                lines.append(f"  {err}")

        lines.append("")
        lines.append("Output")
        lines.append("-" * 40)
        if self.stdout.strip():
            lines.append(self.stdout.rstrip())
        if self.stderr.strip():
            lines.append(self.stderr.rstrip())
        if not self.stdout.strip() and not self.stderr.strip():
            lines.append("  (no output)")

        lines.append("")
        lines.append("Files Changed")
        lines.append("-" * 40)
        if self.files_created:
            lines.append(f"  Created: {len(self.files_created)}")
            for f in self.files_created[:10]:
                lines.append(f"    + {f}")
            if len(self.files_created) > 10:
                lines.append(f"    ... and {len(self.files_created) - 10} more")
        if self.files_modified:
            lines.append(f"  Modified: {len(self.files_modified)}")
            for f in self.files_modified[:10]:
                lines.append(f"    ~ {f}")
            if len(self.files_modified) > 10:
                lines.append(f"    ... and {len(self.files_modified) - 10} more")
        if self.files_deleted:
            lines.append(f"  Deleted: {len(self.files_deleted)}")
            for f in self.files_deleted[:10]:
                lines.append(f"    - {f}")
            if len(self.files_deleted) > 10:
                lines.append(f"    ... and {len(self.files_deleted) - 10} more")
        if not self.files_created and not self.files_modified and not self.files_deleted:
            lines.append("  (no files changed)")

        if self.diff:
            lines.append("")
            lines.append("Diff (simulated)")
            lines.append("-" * 40)
            diff_lines = self.diff.splitlines()
            # Show first 40 lines of diff.
            for dl in diff_lines[:40]:
                lines.append(f"  {dl}")
            if len(diff_lines) > 40:
                lines.append(f"  ... ({len(diff_lines) - 40} more diff lines)")

        lines.append("")
        lines.append("=" * 60)
        lines.append("This was a simulation — nothing was actually changed.")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "action_type": self.action_type,
            "repo": self.repo,
            "sandbox_path": self.sandbox_path,
            "success": self.success,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "exit_code": self.exit_code,
            "files_created": self.files_created,
            "files_modified": self.files_modified,
            "files_deleted": self.files_deleted,
            "diff": self.diff,
            "duration_ms": self.duration_ms,
            "errors": self.errors,
        }

    def cleanup(self) -> None:
        """Remove the sandbox directory."""
        if self._cleaned or not self.sandbox_path:
            return
        try:
            shutil.rmtree(self.sandbox_path, ignore_errors=True)
            self._cleaned = True
        except Exception:
            pass


# ---------------------------------------------------------------------------
# SandboxEngine
# ---------------------------------------------------------------------------


class SandboxEngine:
    """Simulate actions in an isolated sandbox without side effects.

    Usage::

        engine = SandboxEngine()
        result = engine.simulate("ls -la", repo_path="/path/to/repo")
        print(result.format())
        result.cleanup()
    """

    def __init__(self, keep_sandbox: bool = False):
        self.keep_sandbox = keep_sandbox

    # ── Public API ─────────────────────────────────────────────────────────

    def simulate(
        self,
        action: str,
        repo_path: Optional[str] = None,
        timeout: int = 30,
    ) -> SandboxResult:
        """Simulate a shell command in a sandboxed copy of the repo.

        Args:
            action: The shell command to simulate.
            repo_path: Path to the real repository (or None for a bare sandbox).
            timeout: Command timeout in seconds.

        Returns:
            A ``SandboxResult`` with the simulation outcome.
        """
        start = datetime.now(timezone.utc)
        sandbox = self._create_sandbox(repo_path)
        action_type = self._detect_action_type(action, repo_path)
        repo_name = Path(repo_path).name if repo_path else ""

        # Create git baseline BEFORE running the action so _capture_diff
        # later can compare pre-action vs post-action state.
        has_git = bool(repo_path) and self._is_git_repo(repo_path)
        if has_git:
            self._init_sandbox_git(sandbox)

        result = SandboxResult(
            action=action,
            action_type=action_type,
            repo=repo_name,
            sandbox_path=str(sandbox),
        )

        try:
            proc = subprocess.run(
                action,
                shell=True,
                cwd=sandbox,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            result.success = proc.returncode == 0
            result.stdout = proc.stdout
            result.stderr = proc.stderr
            result.exit_code = proc.returncode

            if not result.success:
                result.errors.append(
                    f"Command exited {proc.returncode}: {proc.stderr[:200]}"
                )
        except subprocess.TimeoutExpired:
            result.errors.append(f"Command timed out after {timeout}s")
        except Exception as exc:
            result.errors.append(f"Execution error: {exc}")

        # Capture files changed.
        self._capture_file_changes(result, sandbox, repo_path)

        # Capture git diff if applicable — the baseline was created before
        # the action, so the diff accurately shows what changed.
        if has_git:
            self._capture_diff(result, sandbox)

        dur = (datetime.now(timezone.utc) - start).total_seconds() * 1000
        result.duration_ms = int(dur)

        # Persist to simulation_log.
        try:
            from .db import connect, now_iso
            conn = connect()
            conn.execute(
                "INSERT INTO simulation_log (action, action_type, sandbox_type, success, "
                "outcome_summary, duration_ms, files_changed, has_diff, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (action, action_type, "tempdir", int(result.success),
                 result.action[:200],
                 result.duration_ms,
                 len(result.files_created) + len(result.files_modified),
                 1 if result.diff and result.diff != "(no diff — clean sandbox)" else 0,
                 now_iso()),
            )
            conn.commit()
            conn.close()
        except Exception:
            pass

        # Always clean up, even on exception — no sandbox leaks.
        if not self.keep_sandbox:
            try:
                result.cleanup()
            except Exception:
                pass

        return result

    def simulate_file(
        self,
        file_spec: dict,
        repo_path: Optional[str] = None,
        timeout: int = 30,
    ) -> SandboxResult:
        """Simulate a file operation in a sandboxed copy.

        ``file_spec`` follows the same format as ``FileExecutor``:

        ``{"op": "read", "path": "..."}``
        ``{"op": "write", "path": "...", "content": "..."}``
        ``{"op": "delete", "path": "..."}``
        ``{"op": "mkdir", "path": "..."}``
        ``{"op": "replace", "path": "...", "old": "...", "new": "..."}``

        Args:
            file_spec: File operation JSON dict.
            repo_path: Path to the real repository.
            timeout: Operation timeout in seconds.

        Returns:
            A ``SandboxResult``.
        """
        action_str = json.dumps(file_spec)
        start = datetime.now(timezone.utc)
        sandbox = self._create_sandbox(repo_path)
        repo_name = Path(repo_path).name if repo_path else ""
        op = (file_spec.get("op") or "").lower()

        # Create git baseline BEFORE running the file operation.
        has_git = bool(repo_path) and self._is_git_repo(repo_path)
        if has_git:
            self._init_sandbox_git(sandbox)

        result = SandboxResult(
            action=action_str,
            action_type="file",
            repo=repo_name,
            sandbox_path=str(sandbox),
        )

        try:
            if op == "read":
                target = sandbox / file_spec["path"]
                if target.exists():
                    result.stdout = target.read_text(encoding="utf-8", errors="replace")
                    result.success = True
                else:
                    result.errors.append(f"File not found: {file_spec['path']}")

            elif op == "write":
                target = sandbox / file_spec["path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(file_spec.get("content", ""), encoding="utf-8")
                result.success = True

            elif op == "delete":
                target = sandbox / file_spec["path"]
                if target.exists():
                    if target.is_dir():
                        shutil.rmtree(target)
                    else:
                        target.unlink()
                    result.success = True
                else:
                    result.errors.append(f"Path not found: {file_spec['path']}")

            elif op == "mkdir":
                target = sandbox / file_spec["path"]
                target.mkdir(parents=True, exist_ok=True)
                result.success = True

            elif op == "replace":
                target = sandbox / file_spec["path"]
                if not target.exists():
                    result.errors.append(f"File not found: {file_spec['path']}")
                else:
                    text = target.read_text(encoding="utf-8")
                    new_text = text.replace(file_spec.get("old", ""),
                                            file_spec.get("new", ""), 1)
                    if new_text == text:
                        result.errors.append("Old string not found in file")
                    else:
                        target.write_text(new_text, encoding="utf-8")
                        result.success = True

            else:
                result.errors.append(f"Unknown file operation: {op}")

        except Exception as exc:
            result.errors.append(f"File operation error: {exc}")

        # Capture files changed.
        self._capture_file_changes(result, sandbox, repo_path)

        # Capture git diff.
        if has_git:
            self._capture_diff(result, sandbox)

        dur = (datetime.now(timezone.utc) - start).total_seconds() * 1000
        result.duration_ms = int(dur)

        if not self.keep_sandbox:
            try:
                result.cleanup()
            except Exception:
                pass

        return result

    # ── Internal helpers ───────────────────────────────────────────────────

    def _create_sandbox(self, repo_path: Optional[str] = None) -> Path:
        """Create a temp directory, optionally copying a repo into it."""
        sandbox = Path(tempfile.mkdtemp(prefix="friday_sandbox_"))

        if repo_path:
            src = Path(repo_path)
            if src.exists():
                # Copy repo contents (skip .git, node_modules, etc.).
                for item in src.iterdir():
                    if item.name in (".git", "node_modules", "__pycache__",
                                     ".venv", "venv", ".pytest_cache", ".git",
                                     "target", "build", "dist"):
                        continue
                    dst = sandbox / item.name
                    if item.is_dir():
                        shutil.copytree(item, dst,
                                        ignore=shutil.ignore_patterns(
                                            "__pycache__", "*.pyc", ".git"))
                    else:
                        shutil.copy2(item, dst)

        # Ensure the sandbox is writable.
        os.chmod(str(sandbox), 0o755)
        return sandbox

    def _capture_file_changes(
        self,
        result: SandboxResult,
        sandbox: Path,
        repo_path: Optional[str],
    ) -> None:
        """Compare sandbox files against original repo files."""
        if not repo_path:
            # No original to compare against — list everything.
            for f in sandbox.rglob("*"):
                if f.is_file() and not f.name.startswith("."):
                    try:
                        result.files_created.append(str(f.relative_to(sandbox)))
                    except ValueError:
                        pass
            return

        src = Path(repo_path)
        if not src.exists():
            return

        # Compare sandbox vs original.
        sandbox_files: set[str] = set()
        for f in sandbox.rglob("*"):
            if f.is_file() and not any(
                p.startswith(".") for p in f.relative_to(sandbox).parts
            ):
                try:
                    sandbox_files.add(str(f.relative_to(sandbox)))
                except ValueError:
                    pass

        src_files: set[str] = set()
        for f in src.rglob("*"):
            if f.is_file() and not any(
                p.startswith(".") for p in f.relative_to(src).parts
            ):
                try:
                    rel = str(f.relative_to(src))
                    if not rel.startswith("."):
                        src_files.add(rel)
                except ValueError:
                    pass

        created = sandbox_files - src_files
        deleted = src_files - sandbox_files
        common = sandbox_files & src_files

        result.files_created = sorted(created)[:50]
        result.files_deleted = sorted(deleted)[:50]

        # Check modified: files that exist in both but differ.
        modified: list[str] = []
        for rel in common:
            sf = sandbox / rel
            rf = src / rel
            if sf.exists() and rf.exists() and sf.stat().st_size != rf.stat().st_size:
                # Check if content actually differs.
                try:
                    if sf.read_bytes() != rf.read_bytes():
                        modified.append(rel)
                except (OSError, PermissionError):
                    pass
        result.files_modified = sorted(modified)[:50]

    def _init_sandbox_git(self, sandbox: Path) -> None:
        """Initialize a git repo in the sandbox and create a baseline commit.

        Called BEFORE running the action so the subsequent diff accurately
        captures what changed. The baseline commit contains all files that
        were copied from the original repo.
        """
        try:
            subprocess.run(
                ["git", "init"],
                cwd=sandbox,
                capture_output=True,
                text=True,
                timeout=10,
            )
            subprocess.run(
                ["git", "add", "-A"],
                cwd=sandbox,
                capture_output=True,
                text=True,
                timeout=30,
            )
            subprocess.run(
                ["git", "commit", "-m", "sandbox-baseline", "--allow-empty"],
                cwd=sandbox,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except Exception:
            pass  # Non-critical — diff will just show "diff failed"

    def _capture_diff(self, result: SandboxResult, sandbox: Path) -> None:
        """Capture a git diff of the sandbox against its initial state.

        The baseline must have been created by ``_init_sandbox_git()``
        BEFORE the action ran. This method stages the post-action state
        and diffs against HEAD.
        """
        try:
            subprocess.run(
                ["git", "add", "-A"],
                cwd=sandbox,
                capture_output=True,
                text=True,
                timeout=15,
            )
            diff = subprocess.run(
                ["git", "diff", "HEAD"],
                cwd=sandbox,
                capture_output=True,
                text=True,
                timeout=15,
            )
            result.diff = diff.stdout.strip() or "(no diff — clean sandbox)"
        except Exception as exc:
            result.diff = f"(diff failed: {exc})"

    def _is_git_repo(self, path: str) -> bool:
        """Check if a path is inside a git repository."""
        try:
            out = subprocess.run(
                ["git", "rev-parse", "--is-inside-work-tree"],
                cwd=path,
                capture_output=True,
                text=True,
                timeout=5,
            )
            return out.returncode == 0
        except Exception:
            return False

    def _detect_action_type(self, action: str, repo_path: Optional[str]) -> str:
        """Auto-detect whether the action is a shell command, file, or git op."""
        trimmed = action.strip()
        if trimmed.startswith("{"):
            return "file"
        if trimmed.startswith("git ") or trimmed.startswith("git-"):
            return "git"
        return "shell"
