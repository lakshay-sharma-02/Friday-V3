"""WorkspaceObserver (Gap #1 — Planner Context-Blindness fix).

Deterministically walks the filesystem of each ingested repository and emits
structural Observations describing existing files, directory roles, detected
languages/frameworks, and config file presence. These observations feed into
the Planning layer so task descriptions can reference real file paths instead
of the verbatim goal string.

Key design points:
- **Reality First (Law 1):** every fact is directly observed from the
  filesystem. No semantic analysis, no LLM, no guessing.
- **Observation Never Executes (Law 2):** read-only walk; never edits.
- **Size/depth limits from day one** to prevent noise from node_modules/,
  .venv/, build artifacts, etc.
- **Follows gitignore patterns** where possible (reuses the same exclusion
  conventions as GitObserver).

Confidence levels:
  OBSERVED  — file exists, directory exists (directly measurable)
  DERIVED   — directory role inferred from naming convention + build-file
              presence; language/framework detected from config files
"""

from __future__ import annotations

import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..db import get_repositories
from .interface import Health, Observer, ObserverHealth
from .model import Confidence, Observation


# ---------------------------------------------------------------------------
# Exclusion patterns — directories and files to skip during walk.
# Matches the same conventions git-aware observers use (node_modules/,
# .venv/, .git/, __pycache__/, build artifacts, etc.).
# ---------------------------------------------------------------------------

_EXCLUDED_DIRS: frozenset[str] = frozenset({
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".hypothesis",
    ".tox",
    ".eggs",
    "eggs",
    "bower_components",
    ".svn",
    ".hg",
    "target",          # Rust build output
    "build",           # C/CMake build output
    "dist",            # Python package build output
    ".next",           # Next.js build output
    ".nuxt",           # Nuxt build output
    ".output",         # Nuxt 3 build output
    "coverage",        # Coverage reports
    "htmlcov",
    ".coverage",
    ".serverless",     # Serverless framework
    ".terraform",      # Terraform
    ".docusaurus",     # Docusaurus build
    "site-packages",   # Python site-packages (shouldn't be in repo, but defensive)
})

_EXCLUDED_EXTENSIONS: frozenset[str] = frozenset({
    ".pyc", ".pyo", ".pyd",
    ".so", ".dll", ".dylib",
    ".o", ".obj", ".a", ".lib",
    ".class", ".jar",
    ".log", ".tmp", ".temp",
    ".swp", ".swo", ".swn",   # Vim swap files
    ".bak", ".orig",
    ".cache",
})

#: Max depth to recurse into a repository tree.
_MAX_DEPTH = 6

#: Max files to emit observations for per repository (safety cap).
_MAX_FILES_PER_REPO = 500

#: File extensions we recognize as source files. Observations for extensions
#: NOT in this set are still emitted but with language="other".
_SOURCE_EXTENSIONS: dict[str, str] = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".rs": "rust",
    ".go": "go",
    ".rb": "ruby",
    ".java": "java",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".cs": "csharp",
    ".swift": "swift",
    ".kt": "kotlin",
    ".scala": "scala",
    ".php": "php",
    ".r": "r",
    ".m": "matlab",
    ".sql": "sql",
    ".sh": "shell",
    ".bash": "shell",
    ".zsh": "shell",
    ".fish": "shell",
    ".ps1": "powershell",
    ".tf": "terraform",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".json": "json",
    ".toml": "toml",
    ".md": "markdown",
    ".rst": "rst",
    ".html": "html",
    ".css": "css",
    ".scss": "scss",
    ".less": "less",
    ".svelte": "svelte",
    ".vue": "vue",
    ".sol": "solidity",
    ".zig": "zig",
    ".ex": "elixir",
    ".exs": "elixir",
    ".cr": "crystal",
    ".nim": "nim",
    ".lua": "lua",
}

#: Config files that indicate a project's framework/technology.
_CONFIG_FILES: dict[str, str] = {
    "package.json": "node",
    "pyproject.toml": "python",
    "setup.py": "python",
    "setup.cfg": "python",
    "requirements.txt": "python",
    "Pipfile": "python",
    "Cargo.toml": "rust",
    "go.mod": "go",
    "Gemfile": "ruby",
    "Build.groovy": "groovy",
    "pom.xml": "java/maven",
    "build.gradle": "java/gradle",
    "composer.json": "php",
    "Dockerfile": "docker",
    "docker-compose.yml": "docker",
    "Makefile": "make",
    "justfile": "just",
    "tsconfig.json": "typescript",
    "next.config.js": "nextjs",
    "nuxt.config.js": "nuxtjs",
    "svelte.config.js": "svelte",
    "vite.config.ts": "vite",
    "vite.config.js": "vite",
    "webpack.config.js": "webpack",
    "rollup.config.js": "rollup",
    "eslint.config.js": "eslint",
}

#: Directory name patterns -> role classification.
_DIR_ROLE_PATTERNS: dict[str, str] = {
    "source": "source_dir",
    "src": "source_dir",
    "lib": "source_dir",
    "app": "source_dir",
    "test": "test_dir",
    "tests": "test_dir",
    "spec": "test_dir",
    "__tests__": "test_dir",
    "config": "config_dir",
    "conf": "config_dir",
    "docs": "docs_dir",
    "doc": "docs_dir",
    "scripts": "scripts_dir",
    "bin": "scripts_dir",
    "migrations": "migrations_dir",
    "migrate": "migrations_dir",
    "fixtures": "fixtures_dir",
    "fixture": "fixtures_dir",
    "seeds": "seeds_dir",
    "seed": "seeds_dir",
    "public": "public_dir",
    "static": "static_dir",
    "assets": "assets_dir",
    "components": "components_dir",
    "pages": "pages_dir",
    "routes": "routes_dir",
    "api": "api_dir",
    "middleware": "middleware_dir",
    "hooks": "hooks_dir",
    "utils": "utils_dir",
    "helpers": "utils_dir",
    "types": "types_dir",
    "typescript": "types_dir",
    "__generated__": "generated_dir",
}


class WorkspaceObserver(Observer):
    """Observes the filesystem structure of each ingested repository.

    Walks repository roots (from the DB's ``repositories`` table) and emits
    deterministic observations about file paths, languages, directory roles,
    and config file presence. Skips excluded directories and binary/build
    artifacts. Depth and file-count limits prevent observation store noise.

    The ``scope`` field of each Observation carries the repository path so
    downstream layers (Planning, Knowledge) can associate facts with repos.
    """

    name = "workspace"

    def __init__(self, max_depth: int = _MAX_DEPTH,
                 max_files_per_repo: int = _MAX_FILES_PER_REPO) -> None:
        self.max_depth = max_depth
        self.max_files_per_repo = max_files_per_repo

    # --- Observer interface ------------------------------------------------

    def collect(self, conn) -> list[Observation]:
        observed_at = datetime.now(timezone.utc).isoformat()
        rows: list[Observation] = []

        repos = get_repositories(conn)
        # Build a set of repo names for workspace-level observations.
        repo_count = len(repos)
        rows.append(self._obs(
            observed_at, "workspace", "repository_count", str(repo_count), "",
            Confidence.OBSERVED,
        ))

        for r in repos:
            path = r.path
            if not path:
                continue
            root = Path(path)
            if not root.exists() or not root.is_dir():
                continue

            name = r.name or root.name
            rows.extend(self._walk_repo(observed_at, name, root, path))

        return rows

    def summarize(self, conn) -> str:
        repos = get_repositories(conn)
        total_files = 0
        total_repos = 0
        languages: set[str] = set()

        for r in repos:
            path = r.path
            if not path:
                continue
            root = Path(path)
            if not root.exists() or not root.is_dir():
                continue
            total_repos += 1
            for fp in self._iter_files(root, depth=0):
                if fp.is_file():
                    total_files += 1
                    ext = fp.suffix.lower()
                    lang = _SOURCE_EXTENSIONS.get(ext)
                    if lang:
                        languages.add(lang)
            if total_files > _MAX_FILES_PER_REPO * 2:
                break  # cap summarization cost

        if total_repos == 0:
            return "workspace: no repositories indexed."
        lang_str = ", ".join(sorted(languages)[:8])
        t = "workspace"
        return (
            f"{t}: {total_files} files across {total_repos} repositor{'y' if total_repos == 1 else 'ies'}"
            + (f" ({lang_str})" if lang_str else "")
        )

    def health(self, conn) -> ObserverHealth:
        # The workspace observer is healthy as long as it can access the DB
        # and at least one repository path exists on disk.
        repos = get_repositories(conn)
        if not repos:
            return ObserverHealth(
                True, Health.HEALTHY, "no repos",
                "No repositories ingested yet — nothing to index.",
            )
        found = 0
        missing = 0
        for r in repos:
            p = r.path
            if p and Path(p).exists():
                found += 1
            else:
                missing += 1
        if found == 0:
            return ObserverHealth(
                False, Health.DOWN, "repos_missing",
                f"All {missing} ingested repository path(s) are missing from disk.",
            )
        if missing > 0:
            return ObserverHealth(
                True, Health.DEGRADED, "partial",
                f"{found} repo(s) found, {missing} repo path(s) missing from disk.",
            )
        return ObserverHealth(
            True, Health.HEALTHY, "walk_ok",
            f"{found} repository path(s) found on disk.",
        )

    # --- Internal helpers --------------------------------------------------

    def _obs(self, at: str, subject: str, aspect: str, value: str,
             scope: str, confidence: Confidence = Confidence.OBSERVED,
             cause: Optional[str] = None,
             detail: Optional[str] = None) -> Observation:
        return Observation(
            source=self.name, subject=subject, aspect=aspect, value=value,
            confidence=confidence, observed_at=at, scope=scope,
            cause=cause, detail=detail,
        )

    def _walk_repo(self, at: str, name: str, root: Path,
                   scope: str) -> list[Observation]:
        """Walk one repository root and emit structural observations."""
        rows: list[Observation] = []

        # Repo-level config file detection (DERIVED framework info).
        configs: dict[str, str] = {}
        for cfg_name, framework in _CONFIG_FILES.items():
            cfg_path = root / cfg_name
            if cfg_path.exists() and cfg_path.is_file():
                configs[cfg_name] = framework
                rows.append(self._obs(
                    at, name, "config_file", cfg_name, scope,
                    Confidence.OBSERVED,
                    cause=f"detected {framework}",
                    detail=f"detected {framework}",
                ))

        # If any config was detected, emit per-framework observations.
        seen_frameworks: set[str] = set()
        for fw in configs.values():
            if fw not in seen_frameworks:
                seen_frameworks.add(fw)
                rows.append(self._obs(
                    at, name, "framework", fw, scope,
                    Confidence.DERIVED,
                    cause=f"detected via config file(s): "
                          f"{', '.join(c for c, f in configs.items() if f == fw)}",
                    detail=f"{fw} framework detected",
                ))

        # Emit config_file_count for easy querying.
        if configs:
            rows.append(self._obs(
                at, name, "config_file_count", str(len(configs)), scope,
                Confidence.DERIVED,
            ))

        # Emit total files observation (counted during walk).
        file_count = 0
        lang_counts: dict[str, int] = {}

        for fp in self._iter_files(root, depth=0):
            if not fp.is_file():
                continue
            if file_count >= self.max_files_per_repo:
                break
            file_count += 1

            # File observation: path relative to repo root.
            try:
                rel = fp.relative_to(root)
            except ValueError:
                continue
            rel_str = str(rel.as_posix())

            # Language detection from extension.
            ext = fp.suffix.lower()
            lang = _SOURCE_EXTENSIONS.get(ext, "other")
            lang_counts[lang] = lang_counts.get(lang, 0) + 1

            size = fp.stat().st_size if fp.exists() else 0

            # Use a unique aspect per file so the observation ID
            # (observed_at:source:subject:aspect) doesn't collide across
            # multiple files in the same repo. File path is in `value`.
            rows.append(self._obs(
                at, name, f"file:{rel_str}", rel_str, scope,
                Confidence.OBSERVED,
                cause=f"language={lang}, size={size}",
                detail=f"language={lang}, size={size}",
            ))

            # Also emit per-language observations for queryability.
            rows.append(self._obs(
                at, name, f"lang:{lang}", rel_str, scope,
                Confidence.DERIVED if lang != "other" else Confidence.OBSERVED,
                cause=f"detected from extension {ext}",
                detail=f"detected from extension {ext}",
            ))

        # Emit language distribution observations.
        for lang, count in lang_counts.items():
            if lang == "other":
                continue
            rows.append(self._obs(
                at, name, f"language_count:{lang}", str(count), scope,
                Confidence.DERIVED,
                cause=f"{count} {lang} file(s) detected",
            ))

        if file_count > 0:
            rows.append(self._obs(
                at, name, "file_count", str(file_count), scope,
                Confidence.OBSERVED,
            ))

        # Directory role detection (DERIVED from naming conventions).
        # Walk shallow subdirectories only (depth 1) for role detection.
        seen_roles: set[str] = set()
        try:
            for entry in sorted(root.iterdir()):
                if not entry.is_dir():
                    continue
                dir_name = entry.name
                if dir_name in _EXCLUDED_DIRS:
                    continue
                role = _DIR_ROLE_PATTERNS.get(dir_name.lower())
                if role and role not in seen_roles:
                    seen_roles.add(role)
                    rows.append(self._obs(
                        at, name, "dir_role", role, scope,
                        Confidence.DERIVED,
                        cause=f"directory '{dir_name}' matches role pattern",
                        detail=f"dir '{dir_name}' -> {role}",
                    ))
        except PermissionError:
            pass

        return rows

    def _iter_files(self, root: Path, depth: int = 0):
        """Iterate files in a directory tree, respecting exclusion rules.

        Yields Path objects. Skips excluded directories and binary/build
        files. Respects depth limit.
        """
        if depth > self.max_depth:
            return
        try:
            for entry in sorted(root.iterdir()):
                if entry.name in _EXCLUDED_DIRS:
                    continue
                if entry.is_dir():
                    yield from self._iter_files(entry, depth + 1)
                elif entry.is_file():
                    ext = entry.suffix.lower()
                    if ext in _EXCLUDED_EXTENSIONS:
                        continue
                    yield entry
        except PermissionError:
            pass
