"""Repo architecture analysis — the per-repo research primitive (Wave 11).

``analyze(repo)`` inspects a directory with pure stdlib (pathlib + a
little regex) and produces an evidence-cited :class:`RepoProfile`:
language heuristics, structure, test layout, git state. Cached by
(repo hash + mtime) so Friday "already did that" only when it actually
did. Never raises — a missing/unreadable repo yields a profile with
``available=False``, not an exception (daemon law).
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("friday_v4.research.architecture")

#: Extensions that count as "code" for language heuristics.
_LANG_EXT: dict[str, str] = {
    ".py": "Python", ".ts": "TypeScript", ".tsx": "TypeScript",
    ".js": "JavaScript", ".jsx": "JavaScript", ".rs": "Rust",
    ".go": "Go", ".java": "Java", ".c": "C", ".cpp": "C++",
    ".h": "C/C++", ".rb": "Ruby", ".php": "PHP", ".swift": "Swift",
    ".kt": "Kotlin", ".cs": "C#", ".sh": "Shell", ".json": "JSON",
    ".md": "Markdown", ".html": "HTML", ".css": "CSS", ".sql": "SQL",
}

#: Heuristic signals → claim templates (evidence keeps repo/file).
_FRAMEWORK_SIGNALS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("React", ("react", "react-dom", "next.js", "next/")),
    ("Vue", ("vue", "nuxt")),
    ("Svelte", ("svelte")),
    ("FastAPI", ("fastapi",)),
    ("Django", ("django",)),
    ("Flask", ("flask",)),
    ("Express", ("express",)),
    ("Supabase", ("supabase",)),
    ("Firebase", ("firebase",)),
    ("SQLAlchemy", ("sqlalchemy",)),
    ("pytest", ("pytest",)),
)

_TEST_MARKERS = ("test_", "_test.py", ".test.", "__tests__", "/tests/")

_CACHE_TTL = 3600 * 24  # 1 day


@dataclass
class RepoProfile:
    """Evidence-cited per-repo analysis (never fabricated)."""

    path: str
    available: bool = False
    languages: dict[str, int] = field(default_factory=dict)   # ext → files
    framework_signals: list[str] = field(default_factory=list)
    test_files: int = 0
    has_readme: bool = False
    has_ci: bool = False
    git_sha: str = ""
    evidence: list[str] = field(default_factory=list)  # cited claims

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "available": self.available,
            "languages": self.languages,
            "framework_signals": self.framework_signals,
            "test_files": self.test_files,
            "has_readme": self.has_readme,
            "has_ci": self.has_ci,
            "git_sha": self.git_sha,
            "evidence": self.evidence,
        }


def _cache_key(repo: Path) -> str:
    return hashlib.sha256(f"{repo}|{_dir_mtime(repo)}".encode()).hexdigest()[:16]


def _dir_mtime(repo: Path) -> float:
    """Newest file mtime in the repo — cache-invalidates on any change."""
    try:
        newest = 0.0
        for p in repo.rglob("*"):
            if p.is_file():
                newest = max(newest, p.stat().st_mtime)
        return newest
    except OSError:
        return 0.0


#: Simple process-global cache: key → (when, RepoProfile).
_CACHE: dict[str, tuple[float, RepoProfile]] = {}


def _cached(repo: Path) -> Optional[RepoProfile]:
    key = _cache_key(repo)
    hit = _CACHE.get(key)
    if hit and (time.time() - hit[0]) < _CACHE_TTL:
        return hit[1]
    return None


def _store(repo: Path, profile: RepoProfile) -> None:
    _CACHE[_cache_key(repo)] = (time.time(), profile)


def _count_under(repo: Path) -> tuple[dict[str, int], int, bool, bool]:
    """(languages, test_files, has_readme, has_ci) over the repo tree."""
    langs: dict[str, int] = {}
    tests = 0
    readme = False
    ci = False
    try:
        for p in repo.rglob("*"):
            if not p.is_file():
                continue
            if p.name.lower() in ("readme.md", "readme.rst", "readme.txt"):
                readme = True
            if p.name in (".github", ".gitlab-ci.yml") or ".github" in p.parts:
                ci = True
            ext = p.suffix.lower()
            if ext in _LANG_EXT:
                langs[_LANG_EXT[ext]] = langs.get(_LANG_EXT[ext], 0) + 1
            if any(m in p.name or m in str(p) for m in _TEST_MARKERS):
                tests += 1
    except OSError:
        pass
    return langs, tests, readme, ci


def _read_small(repo: Path, name: str, limit: int = 200_000) -> str:
    """Read a small repo file (setup.py/pyproject.toml/package.json…)."""
    try:
        for candidate in (repo / name, repo / name.upper()):
            if candidate.is_file():
                text = candidate.read_text(encoding="utf-8", errors="ignore")
                return text[:limit]
    except OSError:
        pass
    return ""


def _detect_frameworks(repo: Path) -> list[str]:
    """Framework heuristics from manifest files (evidence = file names)."""
    manifests = ["pyproject.toml", "package.json", "requirements.txt",
                 "Cargo.toml", "go.mod"]
    found: list[str] = []
    for name in manifests:
        text = _read_small(repo, name)
        if not text:
            continue
        lower = text.lower()
        for label, markers in _FRAMEWORK_SIGNALS:
            if label in found:
                continue
            if any(m in lower for m in markers):
                found.append(label)
    return found


def _git_sha(repo: Path) -> str:
    try:
        head = repo / ".git" / "HEAD"
        if head.is_file():
            ref = head.read_text(encoding="utf-8").strip()
            if ref.startswith("ref: "):
                target = repo / ".git" / ref[5:].strip()
                if target.is_file():
                    return target.read_text(encoding="utf-8").strip()[:12]
            return ref[:12]
    except OSError:
        pass
    return ""


def analyze(repo: str | Path) -> RepoProfile:
    """Analyze ``repo`` — cached, evidence-cited, never raises."""
    path = Path(repo).expanduser().resolve()
    if not path.is_dir():
        return RepoProfile(path=str(path), available=False)

    cached = _cached(path)
    if cached is not None:
        return cached

    langs, tests, readme, ci = _count_under(path)
    frameworks = _detect_frameworks(path)
    sha = _git_sha(path)

    evidence: list[str] = []
    if langs:
        top = ", ".join(sorted(langs, key=langs.get, reverse=True)[:3])
        evidence.append(f"{path.name} — languages: {top} "
                        f"({sum(langs.values())} files)")
    if tests:
        evidence.append(f"{path.name} — {tests} test file(s)")
    if frameworks:
        evidence.append(f"{path.name} — signals: {', '.join(frameworks)}")
    if sha:
        evidence.append(f"{path.name} — git {sha}")

    profile = RepoProfile(
        path=str(path), available=True, languages=langs,
        framework_signals=frameworks, test_files=tests,
        has_readme=readme, has_ci=ci, git_sha=sha, evidence=evidence)
    _store(path, profile)
    return profile


__all__ = ["RepoProfile", "analyze"]
