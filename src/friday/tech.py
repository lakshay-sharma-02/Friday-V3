"""Deterministic technology detection from repository manifests.

Returns a list of (tech, evidence) pairs — never guessed, always evidenced.
Detection scans tracked files (via `git ls-files`) plus a few root-level
markers, so ignored build output is naturally excluded.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .discovery import Repo


@dataclass
class Detection:
    tech: str
    evidence: str


def _tracked_files(repo: Path) -> list[str]:
    """Relative file paths to inspect.

    Prefer `git ls-files` (respects .gitignore naturally). Fall back to a
    filesystem walk (skipping ignored dirs) when the path is not a git repo —
    this keeps detection usable on plain directories too.
    """
    try:
        res = subprocess.run(
            ["git", "-C", str(repo), "ls-files"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError):
        res = None
    if res is not None and res.returncode == 0:
        files = [l for l in res.stdout.splitlines() if l]
        if files:
            return files
    return _walk_files(repo)


def _walk_files(repo: Path) -> list[str]:
    out: list[str] = []
    try:
        for p in repo.rglob("*"):
            if not p.is_file():
                continue
            rel = p.relative_to(repo)
            parts = set(rel.parts)
            if parts & _IGNORED_WALK_DIRS:
                continue
            out.append(str(rel))
    except OSError:
        return out
    return out


_IGNORED_WALK_DIRS = {
    ".git",
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
    ".idea",
    ".vscode",
    ".mypy_cache",
    ".pytest_cache",
}


def _read(repo: Path, rel: str) -> Optional[str]:
    p = repo / rel
    try:
        return p.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None


def detect(repo: Repo) -> list[Detection]:
    root = repo.path
    files = _tracked_files(root)
    names = set(files)
    lower_names = {n.lower() for n in names}
    found: list[Detection] = []

    def add(tech: str, evidence: str) -> None:
        if not any(d.tech == tech for d in found):
            found.append(Detection(tech=tech, evidence=evidence))

    has = lambda name: name in names or name.lower() in lower_names

    # --- Language / build manifests ---
    if has("Cargo.toml"):
        add("Rust", "Cargo.toml")
        add("Cargo", "Cargo.toml")
    if has("Cargo.lock"):
        add("Cargo", "Cargo.lock")  # deduplicated if Cargo.toml already added it
    if has("go.mod"):
        add("Go", "go.mod")
    if has("pyproject.toml") or has("setup.py") or has("requirements.txt") or has("Pipfile"):
        add("Python", "Python manifest present")
    if has("pom.xml") or has("build.gradle") or has("build.gradle.kts"):
        add("Java", "Java build manifest")
    if has("CMakeLists.txt"):
        add("C++", "CMakeLists.txt")
    if has("package.json"):
        pkg = _read(root, "package.json") or "{}"
        try:
            data = json.loads(pkg)
        except json.JSONDecodeError:
            data = {}
        add("Node.js", "package.json")
        deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
        dep_names = set(deps)
        if deps.get("typescript") or has("tsconfig.json") or any(
            f.lower().endswith((".ts", ".tsx")) for f in lower_names
        ):
            add("TypeScript", "TypeScript in package.json / tsconfig / .ts files")
        if "next" in dep_names:
            add("Next.js", "next in package.json dependencies")
        if "react" in dep_names:
            add("React", "react in package.json dependencies")
        if has("package-lock.json"):
            add("npm", "package-lock.json")
        if has("pnpm-lock.yaml"):
            add("pnpm", "pnpm-lock.yaml")
        if has("yarn.lock"):
            add("yarn", "yarn.lock")
    else:
        dep_names = set()

    # Python frameworks from requirements / pyproject.
    py_text = ""
    for fn in ("requirements.txt", "pyproject.toml"):
        if has(fn):
            py_text += "\n" + (_read(root, fn) or "")
    py_lower = py_text.lower()
    if re.search(r"(?m)^\s*fastapi", py_text):
        add("FastAPI", "fastapi in Python deps")
    if "django" in py_lower:
        add("Django", "django in Python deps")
    if "flask" in py_lower:
        add("Flask", "flask in Python deps")
    if "torch" in py_lower:
        add("PyTorch", "torch in Python deps")
    if "tensorflow" in py_lower:
        add("TensorFlow", "tensorflow in Python deps")
    if "psycopg" in py_lower or "psycopg2" in py_lower:
        add("Postgres", "psycopg in Python deps")
    if "redis" in py_lower:
        add("Redis", "redis in Python deps")

    # Supabase
    if any("supabase" in d for d in dep_names) or has("supabase.toml") or (
        root / "supabase"
    ).is_dir():
        add("Supabase", "supabase dependency or config")

    # Docker
    if has("Dockerfile") or has("compose.yml") or has("compose.yaml") or has(
        "docker-compose.yml"
    ) or has("docker-compose.yaml"):
        add("Docker", "Dockerfile / compose manifest")
        # Detect DB services from compose files.
        for cf in ("compose.yml", "compose.yaml", "docker-compose.yml", "docker-compose.yaml"):
            txt = _read(root, cf) or ""
            if "postgres" in txt.lower():
                add("Postgres", f"{cf} declares postgres service")
            if "redis" in txt.lower():
                add("Redis", f"{cf} declares redis service")

    # SQLite: present as a data file, or as a dependency.
    if any(n.lower().endswith((".db", ".sqlite", ".sqlite3")) for n in lower_names):
        add("SQLite", "SQLite database file present")
    if "sqlite3" in py_lower or "sqlite" in py_lower:
        add("SQLite", "sqlite dependency present")

    return found
