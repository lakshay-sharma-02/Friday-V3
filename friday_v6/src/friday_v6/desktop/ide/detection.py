"""IDE detection — which editor is Friday living in (Wave 6).

Friday adapts to the editor that is *actually there*: VS Code, JetBrains
(IDEA / PyCharm / WebStorm / GoLand / CLion / RustRover), Neovim,
Sublime Text, or Emacs. Detection is layered by signal strength:

1. **Environment** — the strongest signal (the session that launched
   Friday): ``TERM_PROGRAM`` / ``VSCODE_*`` (VS Code), ``NVIM``
   (listening Neovim), ``GIO_LAUNCHED_DESKTOP_FILE`` (JetBrains /
   VS Code desktop entries).
2. **Running processes** — ``ps`` for editor binaries (POSIX only).
3. **Config directories** — ``~/.config/Code``, ``~/.config/JetBrains``,
   ``~/.config/nvim``, ``~/.config/sublime-text``, ``~/.emacs.d`` — an
   editor that is *installed* even if not running right now.

Each detected editor carries its launcher command, whether it is
LSP-capable, and whether Friday can control it (open/reveal files) —
the controller adapts its argv per editor kind.

Design laws: never crash (every signal is guarded), cheap (``ps`` only
on POSIX and bounded), testable (signals are monkeypatchable).
"""

from __future__ import annotations

import logging
import os
import platform
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger("friday_v6.desktop.ide.detection")


@dataclass
class DetectedIDE:
    """One discovered editor + how Friday can reach it."""

    name: str            # "VS Code" / "JetBrains IntelliJ IDEA" / "Neovim"
    kind: str            # vscode | jetbrains | neovim | sublime | emacs
    launcher: str        # the CLI command that opens/reveals files
    lsp_capable: bool    # can host an LSP server for deep analysis
    control_capable: bool  # can open / reveal files from the command line
    confidence: float    # 0..1 — signal strength
    source: str          # "env" | "process" | "config"

    def __str__(self) -> str:
        return f"{self.name} ({self.kind})"


#: kind → (display name, launcher candidates, lsp-capable)
_EDITORS: dict[str, tuple[str, tuple[str, ...], bool]] = {
    "vscode": ("VS Code", ("code", "code-insiders", "codium", "code-oss"), True),
    "jetbrains": ("JetBrains", ("idea", "pycharm", "webstorm", "goland",
                                "clion", "rustrover", "phpstorm", "rubymine",
                                "datagrip", "intellij-idea"), True),
    "neovim": ("Neovim", ("nvim",), True),
    "sublime": ("Sublime Text", ("subl", "sublime_text"), False),
    "emacs": ("Emacs", ("emacs",), True),
}

#: Process names that identify each editor kind (POSIX ps).
_PROCESS_NAMES: dict[str, tuple[str, ...]] = {
    "vscode": ("code", "codium", "code-oss", "code-insiders"),
    "jetbrains": ("idea", "pycharm", "webstorm", "goland", "clion",
                  "rustrover", "phpstorm", "rubymine", "datagrip"),
    "neovim": ("nvim",),
    "sublime": ("sublime_text",),
    "emacs": ("emacs",),
}

#: Config-dir fingerprints per editor kind.
_CONFIG_MARKERS: dict[str, tuple[str, ...]] = {
    "vscode": (".config/Code", ".config/Code - Insiders", ".vscode",
               ".config/VSCodium", "AppData/Roaming/Code"),
    "neovim": (".config/nvim", ".nvim"),
    "sublime": (".config/sublime-text",),
    "emacs": (".emacs.d", ".config/emacs"),
}


def _env_signals() -> list[tuple[str, float]]:
    """(kind, confidence) pairs from the environment (never raises)."""
    out: list[tuple[str, float]] = []
    term = os.environ.get("TERM_PROGRAM", "")
    if term.lower() == "vscode" or "VSCODE_" in " ".join(
            k for k in os.environ if k.startswith("VSCODE")):
        out.append(("vscode", 1.0))
    if os.environ.get("NVIM"):
        out.append(("neovim", 0.95))
    desktop = os.environ.get("GIO_LAUNCHED_DESKTOP_FILE", "") or ""
    desktop = os.path.basename(desktop).lower()
    if "jetbrains" in desktop:
        out.append(("jetbrains", 0.9))
    if "code" in desktop or "vscodium" in desktop:
        out.append(("vscode", 0.9))
    return out


def _running_processes() -> list[tuple[str, float]]:
    """(kind, confidence) pairs from live editor processes (POSIX only)."""
    if platform.system() not in ("Linux", "Darwin"):
        return []
    try:
        raw = os.popen("ps -eo comm= 2>/dev/null").read()  # noqa: S605
    except OSError as exc:  # pragma: no cover - defensive
        logger.debug(f"ps unavailable: {exc}")
        return []
    procs = {p.strip() for p in raw.splitlines() if p.strip()}
    found: list[tuple[str, float]] = []
    for kind, names in _PROCESS_NAMES.items():
        for proc in procs:
            base = os.path.basename(proc)
            if base in names or any(n in base for n in names if n in base):
                found.append((kind, 0.8))
                break
    return found


def _config_signals() -> list[tuple[str, float]]:
    """(kind, confidence) pairs from config dirs under the home dir."""
    home = Path.home()
    found: list[tuple[str, float]] = []
    for kind, markers in _CONFIG_MARKERS.items():
        for marker in markers:
            if (home / marker).exists():
                found.append((kind, 0.6))
                break
    # JetBrains config dirs are versioned (JetBrains/IntelliJIdea2024.1)
    try:
        jb = home / ".config" / "JetBrains"
        if jb.is_dir():
            for entry in jb.iterdir():
                if entry.is_dir():
                    found.append(("jetbrains", 0.7))
                    break
    except OSError as exc:  # pragma: no cover - defensive
        logger.debug(f"jetbrains config scan failed: {exc}")
    return found


def _resolve_launcher(kind: str) -> Optional[str]:
    """First launcher candidate for the kind that exists on PATH."""
    for candidate in _EDITORS[kind][1]:
        exe = shutil.which(candidate)
        if exe:
            return candidate
    return None


def detect_all() -> list[DetectedIDE]:
    """Every editor Friday can see, best first. Never raises.

    Signal strength: env > process > config. A single kind is emitted
    once, with its strongest confidence. Launchers are resolved against
    PATH so ``control_capable`` reflects what Friday can actually do.
    """
    signals: dict[str, float] = {}
    sources: dict[str, str] = {}
    for sig_kind, conf in _env_signals():
        signals[sig_kind] = max(signals.get(sig_kind, 0.0), conf)
        sources[sig_kind] = "env"
    for sig_kind, conf in _running_processes():
        if conf > signals.get(sig_kind, 0.0):
            signals[sig_kind] = conf
            sources[sig_kind] = "process"
    for sig_kind, conf in _config_signals():
        if conf > signals.get(sig_kind, 0.0):
            signals[sig_kind] = conf
            sources[sig_kind] = "config"

    result: list[DetectedIDE] = []
    for kind, conf in sorted(signals.items(), key=lambda kv: kv[1],
                             reverse=True):
        display, candidates, lsp = _EDITORS[kind]
        launcher = _resolve_launcher(kind)
        result.append(DetectedIDE(
            name=display, kind=kind, launcher=launcher or candidates[0],
            lsp_capable=lsp, control_capable=launcher is not None,
            confidence=conf, source=sources[kind]))
    return result


def detect() -> Optional[DetectedIDE]:
    """The best editor Friday detected, or None (never raises)."""
    found = detect_all()
    return found[0] if found else None


def is_available() -> bool:
    """Whether an IDE is detected (cheap gate for surfaces)."""
    return detect() is not None


__all__ = ["DetectedIDE", "detect", "detect_all", "is_available"]
