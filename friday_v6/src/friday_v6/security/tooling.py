"""Tool discovery — locate optional scanner CLIs even when not on PATH.

The Wave 3 scanners (pip-audit, trufflehog, ruff, mypy, bandit) are
optional subprocess tools. They may be installed into the *current*
virtualenv (``sys.prefix/bin``) without that bin dir being exported on
``PATH`` — which is exactly the case for Friday V4's own venv. A bare
``shutil.which()`` would then wrongly report the tool as missing and the
scanner would silently degrade to its built-in checks.

``find_tool`` checks ``PATH`` first, then the active interpreter's
``bin`` / ``Scripts`` directories, so a venv-installed tool is found.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Optional


def find_tool(name: str) -> Optional[str]:
    """Return a path to an executable ``name``, or None.

    Resolution order:
      1. ``PATH`` via :func:`shutil.which`
      2. ``sys.prefix/bin/<name>`` (Unix venv layout)
      3. ``sys.prefix/Scripts/<name>`` (Windows venv layout)
    """
    found = shutil.which(name)
    if found:
        return found
    exe = name + (".exe" if os.name == "nt" else "")
    for bindir in (Path(sys.prefix) / "bin", Path(sys.prefix) / "Scripts"):
        candidate = bindir / exe
        # Match shutil.which's executable-only semantics: don't hand back a
        # non-executable stub that would fail at subprocess launch.
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def tool_available(name: str) -> bool:
    """Whether an optional scanner tool can be located at all."""
    return find_tool(name) is not None
