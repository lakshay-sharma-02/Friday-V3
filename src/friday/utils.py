"""Shared utilities for Friday V3.

Provides deduplicated helpers used across multiple modules:
- ``_strip_code_fences`` — remove markdown code fences from LLM output
- ``_extract_path`` — extract output directory from natural language
- ``_extract_filename`` — extract filename from natural language
- Shared constants (model lists, file type maps, project signatures)
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Markdown fence stripping
# ---------------------------------------------------------------------------


def _strip_code_fences(content: str) -> str:
    """Strip markdown code fences and trailing commentary from LLM output."""
    lines = content.splitlines()
    start = 0
    end = len(lines)
    for i, line in enumerate(lines):
        if line.strip().startswith("```"):
            start = i + 1
            break
    for i in range(end - 1, start - 1, -1):
        stripped = lines[i].strip()
        if stripped.startswith("```") or stripped.startswith("→"):
            end = i
        elif stripped == "":
            continue
        else:
            break
    clean = []
    for line in lines[start:end]:
        s = line.strip()
        if s.startswith("```"):
            continue
        clean.append(line)
    return "\n".join(clean).strip()


# ---------------------------------------------------------------------------
# Natural-language path / filename extraction
# ---------------------------------------------------------------------------

# Regex to extract an output directory from "in <path>" or "at <path>"
# Handles paths like ~/projects/myapp, /tmp/test, ./src, etc.
# The non-greedy +? + stop-word alternation prevents matching past the path.
_PATH_IN_AT_RE = re.compile(
    r"(?:in|at)\s+(~?/?[\w/._-]+?)(?:\s+(?:name it|named|and|with|the|a|an)|\s*$)",
    re.IGNORECASE,
)

# Regex to extract "name it <filename>" or "named <filename>"
_NAMED_FILE_RE = re.compile(
    r"(?:name it|named)\s+['\"]?(.+?[\.\w/]+)['\"]?(?:$|\s|,)",
    re.IGNORECASE,
)


def extract_output_path(text: str) -> Optional[str]:
    """Extract an output directory path from text like ``in /tmp/myapp``.

    Returns the path string (e.g. ``/tmp/myapp``) or ``None``.
    """
    m = _PATH_IN_AT_RE.search(text)
    if m:
        return m.group(1).strip()
    return None


def extract_filename(text: str) -> Optional[str]:
    """Extract a filename from text like ``named main.py``.

    Tries ``name it``/``named`` patterns first, then falls back to
    scanning for dotted words (e.g. ``calc.py``).
    """
    m = _NAMED_FILE_RE.search(text)
    if m:
        return m.group(1).strip().strip("'\".,;!")
    # Fallback: scan for dotted words.
    for w in reversed(text.split()):
        w = w.strip("'\".,;!")
        if "." in w or "/" in w:
            return w
    return None


# ---------------------------------------------------------------------------
# Shared LLM model fallback lists
# ---------------------------------------------------------------------------

# Free models tried when the primary model is unavailable or slow.
# Used by cli_nl.py for intent classification.
FREE_MODELS: list[str] = [
    "openrouter/google/gemma-4-26b-a4b-it:free",
    "openrouter/google/gemma-4-31b-it:free",
    "openrouter/nvidia/nemotron-3-super-120b-a12b:free",
    "openrouter/nvidia/nemotron-3-nano-30b-a3b:free",
]


def build_model_list(primary_model: str) -> list[str]:
    """Build an ordered, deduplicated list of models to try.

    ``primary_model`` comes from the environment (``FRIDAY_LLM_MODEL``).
    Free fallbacks are appended only if not already in the list.
    """
    models: list[str] = []
    if primary_model:
        models.append(primary_model)
    for m in FREE_MODELS:
        if m not in models:
            models.append(m)
    return models


# ---------------------------------------------------------------------------
# Project type detection signatures
# ---------------------------------------------------------------------------

PROJECT_SIGNATURES: dict[str, list[str]] = {
    "python": ["setup.py", "setup.cfg", "pyproject.toml", "requirements.txt", "Pipfile"],
    "node": ["package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml"],
    "react": ["package.json", "jsconfig.json", "tsconfig.json", "vite.config.ts", "next.config.js"],
    "flask": ["app.py", "wsgi.py", "flask_app.py", "requirements.txt"],
    "fastapi": ["main.py", "app.py"],
    "rust": ["Cargo.toml"],
    "go": ["go.mod"],
    "ruby": ["Gemfile", "Rakefile"],
}

FILE_TYPE_MAP: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".tsx": "react",
    ".jsx": "react",
    ".rs": "rust",
    ".go": "go",
    ".rb": "ruby",
    ".java": "java",
    ".md": "markdown",
    ".html": "html",
    ".css": "css",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".sh": "shell",
    ".sql": "sql",
}

# Directory names to skip when indexing existing project files.
SKIP_DIRS: set[str] = {
    ".git", "__pycache__", "node_modules", ".venv", "venv",
    ".tox", "dist", "build", ".egg-info", ".friday",
}


def detect_project_type(root: Path) -> Optional[str]:
    """Auto-detect project type from existing files in ``root``.

    Checks for signature files first, then falls back to extension frequency.
    """
    if not root.exists():
        return None
    for ptype, signatures in PROJECT_SIGNATURES.items():
        for sig in signatures:
            if (root / sig).exists():
                return ptype
    exts: dict[str, int] = {}
    for f in root.rglob("*"):
        if f.is_file() and f.suffix:
            lang = FILE_TYPE_MAP.get(f.suffix)
            if lang:
                exts[lang] = exts.get(lang, 0) + 1
    if exts:
        return max(exts, key=exts.get)
    return None
