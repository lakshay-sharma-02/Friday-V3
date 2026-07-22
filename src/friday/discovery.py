"""Discover Git repositories under one or more root folders.

A directory containing a `.git` entry is treated as a repository. Discovery
recurses into a repository's working tree so nested repositories are found (the
repo's own `.git` and other hidden/cache dirs are skipped, not analyzed).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# Directories that are never descended into.
IGNORED_DIRS = {
    "node_modules",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    "target",
    "dist",
    "build",
    ".cache",
    ".next",
    ".nuxt",
    ".idea",
    ".vscode",
    ".mypy_cache",
    ".pytest_cache",
    ".tox",
    ".git",
    ".hg",
    ".svn",
    ".claude",
    ".zcode",
    "dogfood_run",
    # site-packages is a pip-installed dependency directory (never source).
    # && is a shell-parsing artifact directory from a prior buggy install.
    "site-packages",
    "&&",
}


@dataclass
class Repo:
    path: Path


def is_repo(path: Path) -> bool:
    git = path / ".git"
    return git.is_dir() or (git.is_file())  # .git can be a file (submodule)


def discover(root: Path) -> list[Repo]:
    """Recursively find repositories under `root`.

    A repository is any directory containing a `.git` entry. Each directory is
    recorded exactly once (when it is the root of a discover call). We always
    recurse into a repo's working tree so nested repositories are found; the
    repo's own `.git` is skipped by the hidden-dir rule.
    """
    found: list[Repo] = []
    if is_repo(root):
        found.append(Repo(path=root))

    for entry in sorted(_walk(root)):
        if not entry.is_dir():
            continue
        if entry.name in IGNORED_DIRS or entry.name.startswith("."):
            continue
        found.extend(discover(entry))
    return found


def _walk(root: Path):
    """Yield immediate children of root; surfaces OSErrors without aborting."""
    try:
        yield from root.iterdir()
    except OSError:
        return


def discover_many(roots: list[Path]) -> list[Repo]:
    """Discover across multiple roots, deduplicating by resolved path."""
    seen: set[Path] = set()
    repos: list[Repo] = []
    for root in roots:
        for repo in discover(root):
            resolved = repo.path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            repos.append(Repo(path=resolved))
    return repos
