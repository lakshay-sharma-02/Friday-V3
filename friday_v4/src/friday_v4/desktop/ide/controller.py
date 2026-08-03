"""IDE control — open, reveal, run (Wave 6).

Friday *controls* the editor, not just reads from it. Every command is
adapted to the detected IDE's CLI dialect:

- **VS Code** — ``code -r <file>`` (reuse window) and ``code -g
  <file>:<line>`` (go-to line).
- **JetBrains** — ``<idea> <file>`` and ``<idea> --line <line> <file>``.
- **Neovim** — ``nvim <file>`` and ``nvim +<line> <file>``.
- **Sublime** — ``subl <file>`` and ``subl <file>:<line>``.
- **Emacs** — ``emacs <file>`` and ``emacs +<line> <file>``.

When no editor is detected, ``open_file`` falls back to the platform
opener (``xdg-open`` / ``open`` / ``start``) so files still reach the
user's desktop. ``run_command`` shells a command in a workspace with a
bounded timeout — the gated execution layer (``friday4 ide run``) is
the preferred path, but a raw runner is useful for surfaces that have
no execution pipeline.

Design laws: never raises (returns (ok, detail)); subprocesses are
argv-based (never ``shell=True``), timeout-bounded, and best-effort.
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from .detection import DetectedIDE

logger = logging.getLogger("friday_v4.desktop.ide.controller")

_TIMEOUT = 10.0
_RUN_TIMEOUT = 120.0


def _open_args(ide: DetectedIDE, path: Path) -> list[str]:
    """Open-file argv for the detected editor kind."""
    kind = ide.kind
    if kind == "vscode":
        return [ide.launcher, "-r", str(path)]
    if kind == "jetbrains":
        return [ide.launcher, str(path)]
    if kind == "neovim":
        return [ide.launcher, str(path)]
    if kind == "sublime":
        return [ide.launcher, str(path)]
    # emacs + fallback
    return [ide.launcher, str(path)]


def _reveal_args(ide: DetectedIDE, path: Path, line: int) -> list[str]:
    """Reveal-a-line argv for the detected editor kind."""
    kind = ide.kind
    if kind == "vscode":
        return [ide.launcher, "-r", "-g", f"{path}:{line}"]
    if kind == "jetbrains":
        return [ide.launcher, "--line", str(line), str(path)]
    if kind == "neovim":
        return [ide.launcher, f"+{line}", str(path)]
    if kind == "sublime":
        return [ide.launcher, f"{path}:{line}"]
    return [ide.launcher, f"+{line}", str(path)]


def _platform_open(path: Path) -> tuple[bool, str]:
    """Open with the OS opener when no editor is detected."""
    system = platform.system()
    if system == "Darwin":
        cmd = ["open", str(path)]
    elif system == "Windows":
        cmd = ["cmd", "/c", "start", "", str(path)]
    else:
        xdg = shutil.which("xdg-open")
        if not xdg:
            return False, "no editor detected and no xdg-open available"
        cmd = [xdg, str(path)]
    try:
        subprocess.run(cmd, timeout=_TIMEOUT, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True, f"opened {path} with the system opener"
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"open failed: {exc}"


def open_file(ide: Optional[DetectedIDE], path: str | Path) -> tuple[bool, str]:
    """Open a file in the editor (or the OS opener). Never raises."""
    p = Path(path).expanduser().resolve()
    if not p.exists():
        return False, f"no such file: {path}"
    if ide is None:
        return _platform_open(p)
    try:
        subprocess.run(_open_args(ide, p), timeout=_TIMEOUT, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True, f"opened {p.name} in {ide.name}"
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug(f"open in {ide.kind} failed: {exc}")
        return _platform_open(p)


def reveal(ide: Optional[DetectedIDE], path: str | Path,
           line: int) -> tuple[bool, str]:
    """Reveal a file at a line in the editor. Never raises."""
    p = Path(path).expanduser().resolve()
    line = max(int(line or 1), 1)
    if not p.exists():
        return False, f"no such file: {path}"
    if ide is None:
        return _platform_open(p)
    try:
        subprocess.run(_reveal_args(ide, p, line), timeout=_TIMEOUT,
                       check=False, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
        return True, f"revealed {p.name}:{line} in {ide.name}"
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug(f"reveal in {ide.kind} failed: {exc}")
        return _platform_open(p)


def run_command(command: str, cwd: Optional[str | Path] = None) -> tuple[bool, str]:
    """Run a shell command in a workspace (argv-based, bounded). Never raises.

    Prefer the gated execution layer (``friday4 ide run`` goes through
    it); this is the raw runner for surfaces without one.
    """
    if not (command or "").strip():
        return False, "empty command"
    try:
        import shlex
        args = shlex.split(command)
    except ValueError as exc:
        return False, f"unparseable command: {exc}"
    if not args:
        return False, "empty command"
    root = Path(cwd).resolve() if cwd else Path.cwd()
    try:
        proc = subprocess.run(args, cwd=str(root), timeout=_RUN_TIMEOUT,
                              check=False, capture_output=True, text=True)
    except FileNotFoundError as exc:
        return False, f"command not found: {exc.filename}"
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"run failed: {exc}"
    out = (proc.stdout or "").strip()
    if proc.returncode == 0:
        return True, out or "done"
    err = (proc.stderr or "").strip()
    return False, err or out or f"exit code {proc.returncode}"


__all__ = ["open_file", "reveal", "run_command"]
