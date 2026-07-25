"""Shared Hyprland IPC utilities.

Resolves the hyprctl binary path ONCE at module load so both the
HyprlandObserver and HyprlandExecutor use the SAME full path, even when
subprocess inherits a stripped PATH from the daemon fork or CLI dispatcher.

This is the fix for a bug found during Pillar A testing: the observer resolved
the binary path via ``shutil.which()`` while the executor hardcoded the bare
name ``"hyprctl"`` — in stripped-PATH subprocess contexts they disagreed,
causing the executor to silently fail while the observer appeared healthy.

Never change the path here without updating both consumers. The duplicate
warning in the docstring is intentional — see the meta-engine's shared
normalization helper for the same pattern.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import Optional

# Resolved once at module load. Falls back to bare name if which() fails
# (graceful degradation—the subprocess may still find it via inherited PATH).
HYPRCTL_PATH: str = shutil.which("hyprctl") or "hyprctl"

_DEFAULT_TIMEOUT = 10  # seconds; matches the observer's conservative timeout


def hyprctl(args: list[str]) -> Optional[str]:
    """Run hyprctl with *args. Returns stdout on success, None on failure.

    Uses the module-level ``HYPRCTL_PATH`` so every caller gets the same
    resolved binary path — never the bare ``"hyprctl"`` that would fail in a
    stripped-PATH subprocess context.
    """
    try:
        res = subprocess.run(
            [HYPRCTL_PATH, *args],
            capture_output=True, text=True, timeout=_DEFAULT_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if res.returncode != 0:
        return None
    return res.stdout
