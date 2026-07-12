"""Collect repository metadata via the `git` CLI (no GitPython dependency)."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .discovery import Repo

# Extension -> language classification (used for file counts via `git ls-files`).
LANG_EXTENSIONS: dict[str, str] = {
    ".py": "Python",
    ".rs": "Rust",
    ".go": "Go",
    ".cpp": "C++",
    ".cc": "C++",
    ".cxx": "C++",
    ".hpp": "C++",
    ".h": "C/C++",
    ".c": "C",
    ".java": "Java",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".mjs": "JavaScript",
    ".cjs": "JavaScript",
    ".rb": "Ruby",
    ".php": "PHP",
    ".swift": "Swift",
    ".kt": "Kotlin",
    ".scala": "Scala",
    ".sh": "Shell",
    ".html": "HTML",
    ".css": "CSS",
    ".scss": "CSS",
    ".sql": "SQL",
    ".md": "Markdown",
    ".rst": "reStructuredText",
}


@dataclass
class Metadata:
    name: str
    path: str
    default_branch: Optional[str]
    languages: dict[str, int] = field(default_factory=dict)
    is_dirty: bool = False
    first_commit_date: Optional[str] = None
    last_commit_date: Optional[str] = None
    remote_url: Optional[str] = None
    commit_count: Optional[int] = None
    primary_author: Optional[str] = None
    license: Optional[str] = None


def _run(repo: Path, args: list[str]) -> Optional[str]:
    try:
        res = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if res.returncode != 0:
        return None
    return res.stdout.strip()


def default_branch(repo: Path) -> Optional[str]:
    out = _run(repo, ["symbolic-ref", "refs/remotes/origin/HEAD"])
    if out:
        return out.rsplit("/", 1)[-1]
    # Fall back to current branch when no remote HEAD exists.
    return _run(repo, ["rev-parse", "--abbrev-ref", "HEAD"])


def languages(repo: Path) -> dict[str, int]:
    out = _run(repo, ["ls-files"])
    if out is None:
        return {}
    counts: dict[str, int] = {}
    for line in out.splitlines():
        suffix = Path(line).suffix.lower()
        lang = LANG_EXTENSIONS.get(suffix)
        if lang:
            counts[lang] = counts.get(lang, 0) + 1
    return counts


def is_dirty(repo: Path) -> bool:
    out = _run(repo, ["status", "--porcelain"])
    return bool(out)


def last_commit_date(repo: Path) -> Optional[str]:
    return _run(repo, ["log", "-1", "--format=%cI"])


def first_commit_date(repo: Path) -> Optional[str]:
    out = _run(repo, ["log", "--reverse", "--format=%cI", "HEAD"])
    if not out:
        return None
    return out.splitlines()[0]


def remote_url(repo: Path) -> Optional[str]:
    url = _run(repo, ["remote", "get-url", "origin"])
    if url:
        return url
    # No origin: take the first remote.
    name = _run(repo, ["remote"])
    if name:
        return _run(repo, ["remote", "get-url", name.splitlines()[0]])
    return None


def commit_count(repo: Path) -> Optional[int]:
    out = _run(repo, ["rev-list", "--count", "HEAD"])
    if out is None:
        return None
    try:
        return int(out)
    except ValueError:
        return None


def primary_author(repo: Path) -> Optional[str]:
    return _run(repo, ["log", "-1", "--format=%ae"])


_LICENSE_NAMES = ("LICENSE", "LICENCE", "COPYING", "LICENSE.md", "LICENSE.txt")


def license_name(repo: Path) -> Optional[str]:
    for child in repo.iterdir():
        if not child.is_file():
            continue
        upper = child.name.upper()
        if upper in {n.upper() for n in _LICENSE_NAMES} or upper.startswith("LICENSE"):
            return child.name
    return None


def collect(repo: Repo) -> Metadata:
    path = repo.path
    meta = Metadata(
        name=path.name,
        path=str(path),
        default_branch=default_branch(path),
        languages=languages(path),
        is_dirty=is_dirty(path),
        first_commit_date=first_commit_date(path),
        last_commit_date=last_commit_date(path),
        remote_url=remote_url(path),
        commit_count=commit_count(path),
        primary_author=primary_author(path),
        license=license_name(path),
    )
    return meta
