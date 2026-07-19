"""Repository architecture intelligence (Milestone 3).

Everything here is deterministic and evidence-backed: structure is read from the
filesystem, dependency graphs from AST / import scanning, and architecture
patterns + components from filenames, manifests, and code structure. No LLM is
used and no repository is modified.

The public entry points are:

  analyze(path)            -> ArchitectureProfile  (extraction only)
  analyze_and_store(conn, repo) -> ArchitectureProfile  (extraction + persist)

Cross-repository similarity is computed downstream (in `query`/`ask`) from the
persisted component / entry-point / architecture rows.
"""

from __future__ import annotations

import ast
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .discovery import Repo
from . import judgment

# Directories that are never descended into (mirrors tech.py).
IGNORED_DIRS = {
    "node_modules", ".venv", "venv", "env", "__pycache__", "target", "dist",
    "build", ".cache", ".next", ".nuxt", ".idea", ".vscode", ".mypy_cache",
    ".pytest_cache", ".tox", ".git", ".hg", ".svn",
}

# File extensions we treat as source for structure / import scanning.
_PY_EXT = {".py", ".pyi"}
_JS_EXT = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}
# Config-file extensions additionally collected so config loading is detectable.
_CONFIG_EXT = {".toml", ".yaml", ".yml", ".ini", ".cfg", ".conf", ".env"}
_CONFIG_NAMES = {".env", ".env.example", ".env.local"}


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class EntryPoint:
    kind: str       # "main()", "CLI", "FastAPI app", "Flask app",
                    # "Next.js app", "Cargo binary", "Executable script"
    detail: str     # file path or command name
    evidence: str


@dataclass
class Component:
    name: str
    evidence: str
    strength: str = "Medium"  # Weak/Medium/Strong (judgment.COMPONENT_STRENGTH)


@dataclass
class ArchitectureProfile:
    path: str
    # 1. Structure
    top_level: list[str] = field(default_factory=list)
    packages: list[str] = field(default_factory=list)
    apps: list[str] = field(default_factory=list)
    libraries: list[str] = field(default_factory=list)
    modules: list[str] = field(default_factory=list)
    tests: list[str] = field(default_factory=list)
    config_files: list[str] = field(default_factory=list)
    scripts: list[str] = field(default_factory=list)
    # 2. Dependency graph
    internal_imports: list[tuple[str, str]] = field(default_factory=list)
    package_boundaries: list[str] = field(default_factory=list)
    external_dependencies: list[str] = field(default_factory=list)
    important_libraries: list[str] = field(default_factory=list)
    layering: list[str] = field(default_factory=list)
    circular: list[str] = field(default_factory=list)
    # 3. Architecture
    architecture: str = "Unknown"
    architecture_evidence: list[str] = field(default_factory=list)
    architecture_confidence: str = "Unknown"  # Verified / Likely / Unknown
    patterns: list[tuple[str, str]] = field(default_factory=list)
    # 4. Components
    components: list[Component] = field(default_factory=list)
    # 5. Entry points
    entry_points: list[EntryPoint] = field(default_factory=list)
    # 6. Summary
    data_flow: list[str] = field(default_factory=list)
    known_patterns: list[str] = field(default_factory=list)
    complexity: str = "Unknown"


# ---------------------------------------------------------------------------
# Filesystem walk
# ---------------------------------------------------------------------------


def _rel_text(repo: Path, rel: str) -> Optional[str]:
    try:
        return (repo / rel).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None


def _source_files(repo: Path) -> list[str]:
    """Relative paths of source + config + manifest files (excluding ignored dirs)."""
    out: list[str] = []
    try:
        for root, dirs, files in os.walk(repo):
            dirs[:] = [d for d in dirs if d not in IGNORED_DIRS]
            for f in files:
                rel = os.path.relpath(os.path.join(root, f), repo)
                ext = os.path.splitext(f)[1].lower()
                if ext in _PY_EXT or ext in _JS_EXT or f in _MANIFEST_NAMES:
                    out.append(rel)
                elif ext in _CONFIG_EXT or f in _CONFIG_NAMES:
                    out.append(rel)
                elif f.endswith(".sh") or rel.split("/")[0] in ("bin", "scripts", "tools"):
                    out.append(rel)
    except OSError:
        return out
    return sorted(out)


_MANIFEST_NAMES = {
    "package.json", "Cargo.toml", "go.mod", "pyproject.toml", "setup.py",
    "requirements.txt", "pom.xml", "build.gradle", "CMakeLists.txt",
    "composer.json", "Gemfile",
}


# ---------------------------------------------------------------------------
# 1. Structure
# ---------------------------------------------------------------------------


def _structure(repo: Path, files: list[str]) -> dict:
    top_level = sorted(
        p for p in os.listdir(repo)
        if not p.startswith(".") and (repo / p).is_dir()
    )
    packages: list[str] = []
    libraries: list[str] = []
    modules: list[str] = []
    tests: list[str] = []
    config_files: list[str] = []
    scripts: list[str] = []
    apps: list[str] = []

    for rel in files:
        low = rel.lower()
        parts = rel.split("/")
        name = parts[-1]
        ext = os.path.splitext(name)[1].lower()
        # Package: any dir containing __init__.py.
        if name == "__init__.py":
            pkg = "/".join(parts[:-1])
            if pkg and pkg not in packages:
                packages.append(pkg)
        # Tests
        if re.search(r"(^|/)tests?/", rel) or re.match(r"test_|_test\.|spec\.", name) or name.endswith(".test.ts") or name.endswith(".test.tsx"):
            tests.append(rel)
        # Config files
        if _is_config_file(rel):
            config_files.append(rel)
        # Scripts
        if parts[0] in ("bin", "scripts") or name.endswith(".sh"):
            scripts.append(rel)
        # Top-level application / library / module directories.
        if len(parts) == 1 and ext in _PY_EXT:
            modules.append(rel)
        if len(parts) == 1 and ext in _JS_EXT:
            modules.append(rel)

    # Apps: conventional application roots.
    for cand in ("app", "src/app", "pages", "cmd", "services", "apps"):
        if (repo / cand).is_dir():
            apps.append(cand)

    # Libraries: importable package dirs that are not tests/apps.
    for pkg in packages:
        if pkg.split("/")[0] in ("tests", "test"):
            continue
        libraries.append(pkg)

    return {
        "top_level": top_level,
        "packages": sorted(set(packages)),
        "apps": sorted(set(apps)),
        "libraries": sorted(set(libraries)),
        "modules": sorted(set(modules)),
        "tests": sorted(set(tests)),
        "config_files": sorted(set(config_files)),
        "scripts": sorted(set(scripts)),
    }


# Build manifests are tracked separately (see tech/gitmeta); config files are
# files that *load* configuration. We exclude build manifests here on purpose.
_BUILD_MANIFESTS = {
    "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "pipfile",
    "package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
    "cargo.toml", "go.mod", "go.sum", "pom.xml", "build.gradle",
    "build.gradle.kts", "cmakelists.txt", "composer.json", "gemfile",
    "gemfile.lock",
}

_CONFIG_RE = [
    re.compile(r"^(config|settings|conf)(\.[a-z0-9]+)?$"),
    re.compile(r"\.env(\.example)?$"),
    re.compile(r"^config/"),
    re.compile(r"appsettings\.[a-z0-9]+$"),
    re.compile(r"[a-z_]+config\.[a-z0-9]+$"),
]


def _is_config_file(rel: str) -> bool:
    low = rel.lower()
    if low in _BUILD_MANIFESTS:
        return False
    # Only genuine config files: root-level named config, config/ dir, or nested
    # *config* / *settings* modules (not arbitrary json/toml build artifacts).
    if "/" in low and not low.startswith("config/"):
        if not re.search(r"/config[a-z_]*\.(py|ts|js|toml|json|yaml|yml)$", low):
            return False
    for pat in _CONFIG_RE:
        if pat.search(low):
            return True
    return False


# ---------------------------------------------------------------------------
# 2. Dependency graph
# ---------------------------------------------------------------------------


def _first_party_roots(repo: Path, files: list[str]) -> set[str]:
    """Top-level importable package roots (e.g. `friday` from src/friday)."""
    roots: set[str] = set()
    for rel in files:
        parts = rel.split("/")
        if parts[-1] == "__init__.py":
            # src/friday/__init__.py -> 'friday'; friday/__init__.py -> 'friday'
            pkg_parts = parts[:-1]
            if pkg_parts and pkg_parts[0] == "src":
                pkg_parts = pkg_parts[1:]
            if pkg_parts:
                roots.add(pkg_parts[0])
    # Single-file top-level modules (cli.py -> 'cli').
    for rel in files:
        parts = rel.split("/")
        if len(parts) == 1 and os.path.splitext(parts[0])[1] in _PY_EXT:
            roots.add(os.path.splitext(parts[0])[0])
    return roots


_STDLIB = set(getattr(sys, "stdlib_module_names", set()))


def _import_roots_of(text: str) -> list[str]:
    """Full dotted import targets from a Python source string.

    Returns e.g. 'pkg.b' for `import pkg.b` and 'pkg.b' for `from pkg.b import c`
    (not just the top-level component), so dependency-graph resolution can map a
    target to the exact module file.
    """
    roots: list[str] = []
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return roots
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                roots.append(a.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                roots.append(node.module)
    return roots


_JS_IMPORT_RE = re.compile(
    r"""import\s+(?:[^'"]*?\s+from\s+)?['"]([^'"]+)['"]"""
    r"""|require\(\s*['"]([^'"]+)['"]\s*\)"""
)


def _js_import_roots_of(text: str) -> list[str]:
    roots: list[str] = []
    for m in _JS_IMPORT_RE.finditer(text):
        spec = m.group(1) or m.group(2)
        if not spec:
            continue
        if spec.startswith((".", "/")):
            continue  # internal / relative
        # npm scope: '@scope/name' -> '@scope/name'
        parts = spec.split("/")
        if spec.startswith("@") and len(parts) >= 2:
            roots.append("/".join(parts[:2]))
        else:
            roots.append(parts[0])
    return roots


def _dependency_graph(repo: Path, files: list[str], roots: set[str]) -> dict:
    internal: list[tuple[str, str]] = []
    ext_counter: dict[str, int] = {}
    seen_edges: set[tuple[str, str]] = set()
    module_map = _build_module_map(files)

    for rel in files:
        ext = os.path.splitext(rel)[1].lower()
        text = _rel_text(repo, rel)
        if text is None:
            continue
        importers: list[str] = []
        if ext in _PY_EXT:
            importers = _import_roots_of(text)
        elif ext in _JS_EXT:
            importers = _js_import_roots_of(text)
        for name in importers:
            top = name.split(".")[0]
            if top in roots:
                # Store the full dotted target (pkg.b) — cycle detection resolves
                # it to the concrete source file; package-level reporting derives
                # the boundary from first_party_roots at the caller.
                edge = (rel, name)
                if edge not in seen_edges:
                    seen_edges.add(edge)
                    internal.append(edge)
            elif top not in _STDLIB and top:
                ext_counter[top] = ext_counter.get(top, 0) + 1

    external = sorted(ext_counter)
    # Important libraries: frequently used (>=3) or a recognized framework.
    _IMPORTANT = {
        "fastapi", "flask", "django", "sqlalchemy", "pydantic", "starlette",
        "react", "next", "express", "axios", "vue", "tailwindcss", "prisma",
        "mongoose", "typeorm", "tokio", "serde", "clap", "rocket", "actix",
    }
    important = sorted(
        r for r, c in ext_counter.items() if c >= 3 or r in _IMPORTANT
    )
    circular = _find_cycles(internal, files)

    return {
        "internal_imports": internal,
        "external_dependencies": external,
        "important_libraries": important,
        "circular": circular,
    }


def _module_of(rel: str) -> str:
    """Dotted module name for a Python file, relative to the repo root.
    `src/pkg/b.py` -> `pkg.b`; `src/pkg/__init__.py` -> `pkg`; `cli.py` -> `cli`."""
    parts = rel.split("/")
    if parts[0] == "src":
        parts = parts[1:]
    if not parts:
        return ""
    stem = parts[-1][:-3] if parts[-1].endswith(".py") else parts[-1]
    if stem == "__init__":
        parts = parts[:-1]
    else:
        parts = parts[:-1] + [stem]
    return ".".join(parts)


def _build_module_map(files: list[str]) -> dict[str, str]:
    """module name -> relative file path, for the first-party modules present."""
    out: dict[str, str] = {}
    for rel in files:
        if not rel.lower().endswith(".py"):
            continue
        mod = _module_of(rel)
        if mod:
            out[mod] = rel
    return out


def _resolve_internal_target(name: str, module_map: dict[str, str]) -> Optional[str]:
    """Map an imported dotted name to a present first-party file, if any."""
    parts = name.split(".")
    for i in range(len(parts), 0, -1):
        head = ".".join(parts[:i])
        if head in module_map:
            return module_map[head]
    return None


def _find_cycles(internal: list[tuple[str, str]], files: list[str]) -> list[str]:
    """Module-level circular imports (Python, first-party only).

    We resolve each imported name to the actual source file when possible, so a
    real cycle such as a.py -> pkg.b and b.py -> pkg.a is detected even when both
    live inside the same package.
    """
    module_map = _build_module_map(files)
    graph: dict[str, set[str]] = {}
    for src, dep in internal:
        tgt = _resolve_internal_target(dep, module_map)
        if tgt is None or tgt == src:
            continue
        graph.setdefault(src, set()).add(tgt)

    cycles: list[list[str]] = []
    visited: set[str] = set()
    path: list[str] = []
    on_stack: set[str] = set()

    def dfs(node: str) -> None:
        visited.add(node)
        path.append(node)
        on_stack.add(node)
        for nxt in sorted(graph.get(node, ())):
            if nxt in on_stack:
                idx = path.index(nxt)
                cycles.append(path[idx:] + [nxt])
            elif nxt not in visited:
                dfs(nxt)
        path.pop()
        on_stack.discard(node)

    for start in sorted(graph):
        if start not in visited:
            dfs(start)

    seen: set[tuple[str, ...]] = set()
    out: list[str] = []
    for cyc in cycles:
        sig = tuple(sorted(set(cyc)))
        if sig in seen or len(sig) < 2:
            continue
        seen.add(sig)
        out.append(" <-> ".join(cyc[:-1]))
    return out


# ---------------------------------------------------------------------------
# 3. Architecture pattern detection
# ---------------------------------------------------------------------------


def _read_manifest(repo: Path, name: str) -> Optional[str]:
    p = repo / name
    if p.is_file():
        return _rel_text(repo, name)
    return None


def _ast_call_names(text: str) -> dict[str, int]:
    """Name of every called function/class (Name.id or Attribute.attr)."""
    out: dict[str, int] = {}
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return out
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            name = None
            if isinstance(f, ast.Name):
                name = f.id
            elif isinstance(f, ast.Attribute):
                name = f.attr
            if name:
                out[name] = out.get(name, 0) + 1
    return out


_ROUTE_METHODS = {"get", "post", "put", "delete", "patch", "head", "options"}


def _route_decorators(text: str) -> list[str]:
    """Real route decorators via AST, ignoring string literals.

    Returns decorator spellings like `@app.get`, `@app.route`, `@bp.get`.
    Used by both FastAPI (`.get/.post/...`) and Flask (`.route`) detection.
    """
    out: list[str] = []
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return out
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            func = dec.func if isinstance(dec, ast.Call) else dec
            if isinstance(func, ast.Attribute):
                if func.attr in _ROUTE_METHODS or func.attr == "route":
                    obj = func.value.id if isinstance(func.value, ast.Name) else "app"
                    out.append(f"@{obj}.{func.attr}")
    return out


def _detect_fastapi(repo: Path, files: list[str], ctx: dict) -> Optional[list[str]]:
    ev: list[str] = []
    if "fastapi" in ctx["ext"]:
        ev.append("fastapi imported")
    if "uvicorn" in ctx["ext"]:
        ev.append("uvicorn startup")
    for rel in files:
        if rel.lower().endswith(".py"):
            t = _rel_text(repo, rel) or ""
            calls = _ast_call_names(t)
            if "FastAPI" in calls:
                ev.append(f"FastAPI() instantiated in {rel}")
            if any(d.endswith((".get", ".post", ".put", ".delete", ".patch")) for d in _route_decorators(t)):
                ev.append(f"route decorator in {rel}")
    if any(re.search(r"(^|/)routers?/", f) for f in files):
        ev.append("routers/ directory present")
    return ev or None


def _detect_flask(repo: Path, files: list[str], ctx: dict) -> Optional[list[str]]:
    ev: list[str] = []
    if "flask" in ctx["ext"]:
        ev.append("flask imported")
    for rel in files:
        if rel.lower().endswith(".py"):
            t = _rel_text(repo, rel) or ""
            calls = _ast_call_names(t)
            if "Flask" in calls:
                ev.append(f"Flask app created in {rel}")
            if any(d.endswith(".route") for d in _route_decorators(t)):
                ev.append(f"@app.route decorator in {rel}")
    return ev or None


def _detect_django(repo: Path, files: list[str], ctx: dict) -> Optional[list[str]]:
    if "django" in ctx["ext"]:
        ev = ["django imported"]
        if any(f.endswith("settings.py") for f in files):
            ev.append("Django settings.py present")
        if any(f.endswith("urls.py") for f in files):
            ev.append("Django urls.py present")
        if any(f.endswith("wsgi.py") for f in files):
            ev.append("Django wsgi.py present")
        return ev
    return None


def _detect_next_app_router(repo: Path, files: list[str], ctx: dict) -> Optional[list[str]]:
    if "next" not in ctx["ext"]:
        return None
    if (repo / "app").is_dir():
        # App Router: app/ containing layout/page/route files (any nesting depth).
        has_route = any(
            re.search(r"(^|/)app/(([^/]+/)*(page|layout|route))\.(tsx|jsx|ts|js)$", f)
            for f in files
        )
        if has_route:
            return ["next dependency", "app/ directory with page/layout/route files"]
    return None


def _detect_next_pages_router(repo: Path, files: list[str], ctx: dict) -> Optional[list[str]]:
    if "next" not in ctx["ext"]:
        return None
    if (repo / "pages").is_dir():
        return ["next dependency", "pages/ directory present (Pages Router)"]
    return None


def _detect_next_unknown_router(repo: Path, files: list[str], ctx: dict) -> Optional[list[str]]:
    """Next.js present but neither app/ nor pages/ route dir found.

    We must NOT claim a specific router type. Confidence is Likely and the label
    explicitly states the router type is unknown.
    """
    if "next" not in ctx["ext"]:
        return None
    if (repo / "app").is_dir() or (repo / "pages").is_dir():
        return None
    return ["next dependency", "no app/ or pages/ route directory found"]


def _detect_react_spa(repo: Path, files: list[str], ctx: dict) -> Optional[list[str]]:
    if "react" not in ctx["ext"]:
        return None
    # A bare react import is NOT enough (audit §6): require at least one SPA
    # signal (Vite bundler, public/index.html entry, or main.tsx/main.jsx).
    # Without it we must NOT claim "React SPA" — fall through to Unknown/Library.
    ev = ["react dependency"]
    spa_signal = False
    if (repo / "vite.config.ts").is_file() or (repo / "vite.config.js").is_file():
        ev.append("Vite config (SPA bundler)")
        spa_signal = True
    if (repo / "public" / "index.html").is_file():
        ev.append("public/index.html entry")
        spa_signal = True
    if any(f.endswith("main.tsx") or f.endswith("main.jsx") for f in files):
        ev.append("main.tsx/main.jsx entry")
        spa_signal = True
    if not spa_signal:
        return None
    return ev


def _detect_cargo_workspace(repo: Path, files: list[str], ctx: dict) -> Optional[list[str]]:
    txt = _read_manifest(repo, "Cargo.toml")
    if txt is None:
        return None
    ev: list[str] = []
    if re.search(r"^\s*\[workspace\]", txt, re.MULTILINE):
        ev.append("Cargo.toml [workspace] section")
    n_crates = sum(
        1 for f in files
        if f.endswith("Cargo.toml") and f != "Cargo.toml"
    )
    if n_crates >= 1:
        ev.append(f"{n_crates + 1} Cargo.toml manifests (multi-crate)")
    return ev or None


def _detect_cargo_binary(repo: Path, files: list[str], ctx: dict) -> Optional[list[str]]:
    txt = _read_manifest(repo, "Cargo.toml")
    if txt is None:
        return None
    ev: list[str] = []
    if re.search(r"^\s*\[\[bin\]\]", txt, re.MULTILINE):
        ev.append("Cargo.toml [[bin]] section")
    if (repo / "src" / "main.rs").is_file():
        ev.append("src/main.rs with fn main")
    return ev or None


def _detect_cli(repo: Path, files: list[str], ctx: dict) -> Optional[list[str]]:
    ev: list[str] = []
    cli_libs = {"argparse", "click", "typer", "commander", "cobra", "clap", "optparse"}
    hits = cli_libs & set(ctx["ext"])
    if hits:
        ev.append("CLI library imported: " + ", ".join(sorted(hits)))
    # Manifest [project.scripts] / console_scripts.
    pyproj = _read_manifest(repo, "pyproject.toml") or ""
    setup = _read_manifest(repo, "setup.py") or ""
    if re.search(r"\[project\.scripts\]|console_scripts", pyproj + setup):
        ev.append("entry-point script declared in manifest")
    if any(re.search(r"(^|/)bin/", f) for f in files):
        ev.append("bin/ directory with executables")
    return ev or None


def _detect_pytest(repo: Path, files: list[str], ctx: dict) -> Optional[list[str]]:
    if "pytest" in ctx["ext"] or any(f for f in files if re.search(r"(^|/)tests?/", f)):
        ev = []
        if "pytest" in ctx["ext"]:
            ev.append("pytest dependency")
        if any(re.search(r"test_.*\.py$|_test\.py$", f) for f in files):
            ev.append("test_*.py / *_test.py files present")
        return ev or None
    return None


def _detect_library(repo: Path, files: list[str], ctx: dict) -> Optional[list[str]]:
    if ctx["packages"]:
        return [f"importable package(s): {', '.join(ctx['packages'][:5])}"]
    pyproj = _read_manifest(repo, "pyproject.toml") or ""
    if "[project]" in pyproj and not re.search(r"\[project\.scripts\]", pyproj):
        py_src = [f for f in files if f.endswith(".py") and not re.search(r"(^|/)tests?/", f)]
        if py_src:
            return ["pyproject [project] with source modules and no CLI script"]
    if (repo / "Cargo.toml").is_file() and re.search(
        r"^\s*\[lib\]", _read_manifest(repo, "Cargo.toml") or "", re.MULTILINE
    ):
        return ["Cargo.toml [lib] section"]
    return None


def _detect_generic_web(repo: Path, files: list[str], ctx: dict) -> Optional[list[str]]:
    if any(re.search(r"(^|/)public/", f) or f == "index.html" for f in files):
        return ["static/public web assets present"]
    if "express" in ctx["ext"] or "koa" in ctx["ext"] or "gin" in ctx["ext"]:
        return ["web server framework dependency"]
    return None


# (label, detector, priority) — higher priority wins for the primary label.
# NOTE: Pytest is intentionally NOT a primary architecture. A library/CLI with
# tests is still a Library/CLI; testing tooling never defines the project.
_ARCH_DETECTORS: list[tuple[str, Callable, int, str]] = [
    ("FastAPI REST API", _detect_fastapi, 100, "Verified"),
    ("Flask web app", _detect_flask, 95, "Verified"),
    ("Django web app", _detect_django, 95, "Verified"),
    ("Next.js App Router", _detect_next_app_router, 92, "Verified"),
    ("Next.js Pages Router", _detect_next_pages_router, 90, "Likely"),
    ("React SPA", _detect_react_spa, 85, "Likely"),
    ("Cargo workspace", _detect_cargo_workspace, 80, "Verified"),
    ("Cargo binary", _detect_cargo_binary, 80, "Verified"),
    ("CLI tool", _detect_cli, 70, "Verified"),
    ("Next.js (router type unknown)", _detect_next_unknown_router, 88, "Likely"),
    ("Library", _detect_library, 50, "Verified"),
    ("Web app", _detect_generic_web, 45, "Likely"),
]

# Detectors that can fire but must never become the primary architecture label.
# They are recorded as supporting patterns only.
_ARCH_SUPPORTING_ONLY: list[tuple[str, Callable, int, str]] = [
    ("Pytest test suite", _detect_pytest, 60, "Verified"),
]


def _architecture(repo: Path, files: list[str], ctx: dict) -> tuple[str, list[str], list[tuple[str, str]], str]:
    fired: list[tuple[str, int, list[str], str]] = []
    for label, fn, prio, conf in _ARCH_DETECTORS:
        ev = fn(repo, files, ctx)
        if ev:
            fired.append((label, prio, ev, conf))
    for label, fn, prio, conf in _ARCH_SUPPORTING_ONLY:
        ev = fn(repo, files, ctx)
        if ev:
            # Record as supporting pattern but it cannot win primary selection.
            fired.append((label, -1, ev, conf))  # -1 priority -> never primary
    if not fired:
        return ("Unknown", ["No recognizable framework or manifest pattern matched"], [], "Unknown")
    # Primary = highest positive priority (supporting-only entries have -1).
    primary_entry = max((f for f in fired if f[1] >= 0), key=lambda x: x[1], default=None)
    if primary_entry is None:
        # Only supporting patterns fired (e.g. pytest present but no real arch).
        return ("Unknown", ["No primary architecture detected (only test tooling present)"], [], "Unknown")
    primary, _, primary_ev, primary_conf = primary_entry
    # Patterns list includes supporting entries too (clearly lower priority).
    patterns = [(label, "; ".join(ev)) for label, _, ev, _ in fired]
    confidence = primary_conf
    # If the chosen label is a "Likely" label but stronger corroboration is
    # missing, keep it Likely — do not upgrade to Verified.
    return (primary, primary_ev, patterns, confidence)


# ---------------------------------------------------------------------------
# 4. Component discovery
# ---------------------------------------------------------------------------


def _component(name: str, test: Callable[[dict], list[str]]) -> tuple[str, Callable[[dict], list[str]]]:
    return (name, test)


def _ctx_for_components(repo: Path, files: list[str], ext: set[str]) -> dict:
    # Pre-scan tokens needed by component detectors without re-reading all files.
    file_names_low = [f.lower() for f in files]
    name_blob = "\n".join(file_names_low)
    ext_set = set(ext)
    return {
        "repo": repo, "files": files, "file_names_low": file_names_low,
        "name_blob": name_blob, "ext": ext_set,
    }


def _c_auth(c: dict) -> list[str]:
    ev: list[str] = []
    auth_files = [f for f in c["file_names_low"] if re.search(r"(auth|login|session|oauth|jwt|token)", f)]
    if auth_files:
        ev.append("auth-related files: " + ", ".join(auth_files[:4]))
    libs = {"jwt", "pyjwt", "passlib", "bcrypt", "authlib", "python-jose",
            "next-auth", "@auth/core", "firebase-admin", "argon2", "supabase"}
    hit = libs & c["ext"]
    if hit:
        ev.append("auth libraries imported: " + ", ".join(sorted(hit)))
    return ev


def _c_db(c: dict) -> list[str]:
    """Database component — requires behavioral evidence, not just a filename.

    'has a db.py' / 'imports sqlite3' is NOT enough (audit §7): that is a concept,
    not reusable database logic. Real signals are an ORM abstraction or an actual
    schema/models/repository/migrations layer. A bare db.py with a low-level driver
    import emits NO Database component at all.
    """
    ev: list[str] = []
    # ORM libraries are themselves the abstraction -> behavioral DB evidence.
    orm_libs = {"sqlalchemy", "sqlmodel", "databases", "prisma", "mongoose",
                "typeorm", "drizzle-orm", "django.db"}
    orm_hit = orm_libs & c["ext"]
    # Low-level drivers need an accompanying schema/models layer to count.
    driver_libs = {"sqlite3", "psycopg", "psycopg2", "pymongo", "redis"}
    driver_hit = driver_libs & c["ext"]
    # NOTE: a file literally named db.py/database.py is the component name itself,
    # not evidence — excluded from the behavioral-file match on purpose.
    behavioral_files = any(
        re.search(r"(^|/)(models?|schema|orm|migrations?|repositories?)\.", f)
        for f in c["file_names_low"]
    ) or "/migrations/" in c["name_blob"] or "schema.prisma" in c["file_names_low"]
    if orm_hit:
        ev.append("ORM library imported: " + ", ".join(sorted(orm_hit)))
    if driver_hit and behavioral_files:
        ev.append("database driver with schema/models layer: " + ", ".join(sorted(driver_hit)))
    if "schema.prisma" in c["file_names_low"]:
        ev.append("schema.prisma present (Prisma schema)")
    return ev


def _c_config(c: dict) -> list[str]:
    """Configuration component — requires actual config loading behavior.

    A bare config.py that merely imports `os` is NOT evidence (audit §7:
    "Finding config.py does not prove Configuration"). Real signals are a config
    library import, or a module that actually reads the environment / parses a
    settings object.
    """
    ev: list[str] = []
    libs = {"dotenv", "python-dotenv", "pydantic-settings", "configparser",
            "environs", "viper", "config", "dynaconf"}
    hit = libs & c["ext"]
    if hit:
        ev.append("config libraries imported: " + ", ".join(sorted(hit)))
        return ev
    # No config library: require proof the file actually *loads* config — env
    # reads, settings objects, or a real .env/toml loader — not just a name.
    env_load = any(
        re.search(r"(getenv|environ|load_dotenv|load_env|parse_config|settings\s*=|\.env)", t)
        for f in c["files"]
        for t in (c["repo"] / f).read_text(encoding="utf-8", errors="ignore").splitlines()
        if f.lower().endswith((".py", ".ts", ".js"))
    ) if c["files"] else False
    if env_load:
        loaderish = [f for f in c["files"]
                     if re.search(r"(settings|config)\.(py|ts|js)$", f.lower())]
        if loaderish:
            ev.append("config loader modules: " + ", ".join(loaderish[:4]))
    return ev


def _c_routing(c: dict) -> list[str]:
    """Routing component — requires actual routes, not just a framework import."""
    ev: list[str] = []
    if any(re.search(r"(^|/)(routers?|routes?|urls)\.", f) or "/routes/" in f or "/routers/" in f
           for f in c["file_names_low"]):
        ev.append("routing modules/directories present")
    # Behavioral: route registration. We require the framework to be present AND
    # a routers/ directory or route decorators; a bare FastAPI import alone is not
    # enough (verified separately via AST in the architecture layer).
    libs = {"fastapi", "flask", "django", "react-router-dom", "express", "next/navigation", "vue-router"}
    hit = libs & c["ext"]
    if hit and any("routers" in f or "routes" in f for f in c["file_names_low"]):
        ev.append("routing libraries with route modules: " + ", ".join(sorted(hit)))
    return ev


def _c_storage(c: dict) -> list[str]:
    ev: list[str] = []
    if any("storage" in f or "uploads" in f for f in c["file_names_low"]):
        ev.append("storage-related files present")
    libs = {"boto3", "minio", "google-cloud-storage", "@aws-sdk/client-s3", "fsspec", "fs"}
    hit = libs & c["ext"]
    if hit:
        ev.append("storage libraries imported: " + ", ".join(sorted(hit)))
    return ev


def _c_logging(c: dict) -> list[str]:
    libs = {"logging", "loguru", "structlog", "winston", "zap", "slog",
            "log4j", "tracing", "@nestjs/common"}
    hit = libs & c["ext"]
    if hit:
        return ["logging libraries imported: " + ", ".join(sorted(hit))]
    return []


def _c_cli(c: dict) -> list[str]:
    libs = {"argparse", "click", "typer", "commander", "cobra", "clap", "optparse"}
    hit = libs & c["ext"]
    if hit:
        return ["CLI libraries imported: " + ", ".join(sorted(hit))]
    return []


def _c_llm(c: dict) -> list[str]:
    ev: list[str] = []
    # Token-bounded so "main.py" does not match the "ai" substring.
    llm_re = re.compile(r"(^|[-_/])(llm|ai|openai|anthropic|gpt|ml)([-_/.]|$)")
    llm_files = [f for f in c["file_names_low"] if llm_re.search(f)]
    if llm_files:
        ev.append("AI/LLM files: " + ", ".join(llm_files[:4]))
    libs = {"openai", "anthropic", "langchain", "llama-index", "transformers",
            "torch", "ollama", "google.generativeai", "cohere", "litellm",
            "@anthropic-ai/sdk", "ai"}
    hit = libs & c["ext"]
    if hit:
        ev.append("AI/LLM libraries imported: " + ", ".join(sorted(hit)))
    return ev


def _c_caching(c: dict) -> list[str]:
    libs = {"redis", "memcached", "cachetools", "aiocache", "@nestjs/cache-manager", "lru-cache"}
    hit = libs & c["ext"]
    if hit:
        return ["caching libraries imported: " + ", ".join(sorted(hit))]
    return []


def _c_networking(c: dict) -> list[str]:
    libs = {"requests", "httpx", "aiohttp", "urllib3", "axios", "grpc", "socket", "curl"}
    hit = libs & c["ext"]
    if len(hit) >= 2 or any(d in hit for d in ("grpc", "axios", "httpx", "aiohttp")):
        return ["networking libraries imported: " + ", ".join(sorted(hit))]
    return []


def _c_testing(c: dict) -> list[str]:
    """Testing component — requires actual test functions, not a bare conftest."""
    has_test_files = any(
        re.search(r"test_.*\.py$|_test\.py$|\.test\.(ts|tsx|js|jsx)$", f)
        for f in c["files"]
    )
    # A tests/ directory with only conftest/fixtures and no test_* fns is NOT a
    # test suite. Require at least one real test file to emit the component.
    if not has_test_files:
        return []
    ev = ["test files present (test_*/_test/*.test.*)"]
    libs = {"pytest", "jest", "vitest", "unittest", "mocha", "cucumber"}
    hit = libs & c["ext"]
    if hit:
        ev.append("test frameworks: " + ", ".join(sorted(hit)))
    return ev


_COMPONENTS: list[tuple[str, Callable[[dict], list[str]]]] = [
    _component("Authentication", _c_auth),
    _component("Database", _c_db),
    _component("Configuration", _c_config),
    _component("Routing", _c_routing),
    _component("Storage", _c_storage),
    _component("Logging", _c_logging),
    _component("CLI", _c_cli),
    _component("LLM interface", _c_llm),
    _component("Caching", _c_caching),
    _component("Networking", _c_networking),
    _component("Testing", _c_testing),
]


def _components(repo: Path, files: list[str], ext: set[str]) -> list[Component]:
    c = _ctx_for_components(repo, files, ext)
    out: list[Component] = []
    for name, fn in _COMPONENTS:
        ev = fn(c)
        if ev:
            out.append(
                Component(
                    name=name, evidence="; ".join(ev),
                    strength=_component_strength(name),
                )
            )
    return out


def _component_strength(name: str) -> str:
    return judgment.component_strength(name)


# ---------------------------------------------------------------------------
# 5. Entry points
# ---------------------------------------------------------------------------


# Directories that are never runtime entry points, even if they contain a
# main()/shebang. (Audit: tests/main() and examples/fixtures are NOT entries.)
_IGNORED_ENTRY_DIRS = {
    "tests", "test", "examples", "example", "fixtures", "fixture",
    "benchmarks", "benchmark", "tools", "tool", "docs",
}

# `scripts/` are maintenance/utility scripts, not application entry points.
_UTILITY_DIRS = {"scripts"}


def _is_runtime_path(rel: str) -> bool:
    """True if `rel` lives somewhere we treat as a real runtime entry point."""
    top = rel.split("/")[0]
    return top not in _IGNORED_ENTRY_DIRS and top not in _UTILITY_DIRS


def _entry_points(repo: Path, files: list[str], ctx: dict) -> list[EntryPoint]:
    eps: list[EntryPoint] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, detail: str, evidence: str) -> None:
        key = (kind, detail)
        if key not in seen:
            seen.add(key)
            eps.append(EntryPoint(kind=kind, detail=detail, evidence=evidence))

    # main() + __main__ guard (Python). Skip files under non-runtime dirs
    # (tests, examples, fixtures, benchmarks, tools, scripts).
    for rel in files:
        if not rel.lower().endswith(".py"):
            continue
        if not _is_runtime_path(rel):
            continue
        t = _rel_text(repo, rel) or ""
        has_main_fn = bool(re.search(r"\bdef\s+main\s*\(", t))
        has_guard = '__name__' in t and '__main__' in t
        if has_main_fn or has_guard:
            add("main()", rel, f"def main() / __main__ guard in {rel}")

    # CLI entry from manifest scripts.
    pyproj = _read_manifest(repo, "pyproject.toml") or ""
    setup = _read_manifest(repo, "setup.py") or ""
    if re.search(r"\[project\.scripts\]|console_scripts", pyproj + setup):
        add("CLI", "manifest [project.scripts]", "console_scripts entry point declared")

    # FastAPI / Flask app objects (AST call detection, not raw-text).
    for rel in files:
        if not rel.lower().endswith(".py"):
            continue
        if not _is_runtime_path(rel):
            continue
        t = _rel_text(repo, rel) or ""
        calls = _ast_call_names(t)
        if "FastAPI" in calls:
            add("FastAPI app", rel, f"FastAPI() instance in {rel}")
        if "Flask" in calls:
            add("Flask app", rel, f"Flask() instance in {rel}")

    # Next.js app entry.
    if "next" in ctx["ext"]:
        if (repo / "app").is_dir():
            add("Next.js app", "app/", "app/ directory (Next.js App Router)")
        elif (repo / "pages").is_dir():
            add("Next.js app", "pages/", "pages/ directory (Next.js Pages Router)")

    # Cargo binary.
    cargo = _read_manifest(repo, "Cargo.toml") or ""
    if re.search(r"^\s*\[\[bin\]\]", cargo, re.MULTILINE) or (repo / "src" / "main.rs").is_file():
        add("Cargo binary", "src/main.rs", "Cargo [[bin]] / src/main.rs")

    # Executable scripts (shebang). We already collected bin/, scripts/, tools/,
    # *.sh files in the walk. Classify by directory:
    #  - scripts/ (and tools/fixtures/etc excluded earlier): maintenance -> Utility.
    #  - bin/ and other runtime dirs: runtime -> Executable script.
    for rel in files:
        top = rel.split("/")[0]
        t = _rel_text(repo, rel) or ""
        if not t.startswith("#!"):
            continue
        if top in _UTILITY_DIRS:
            add("Utility script", rel, f"maintenance script (shebang) {rel}")
        elif _is_runtime_path(rel):
            add("Executable script", rel, f"shebang executable script {rel}")

    return eps


# ---------------------------------------------------------------------------
# 6. Summary assembly
# ---------------------------------------------------------------------------


def _summary(profile: ArchitectureProfile, ctx: dict) -> None:
    comp_names = {c.name for c in profile.components}
    # Data flow heuristic.
    flow: list[str] = []
    if "FastAPI REST API" in {p[0] for p in profile.patterns} or "Flask web app" in {p[0] for p in profile.patterns}:
        flow.append("Client request -> API route handler")
        if "Authentication" in comp_names:
            flow.append("Auth middleware validates request")
        if "Database" in comp_names:
            flow.append("Handler calls service/db layer -> persists via database")
    elif "Next.js App Router" in {p[0] for p in profile.patterns} or "React SPA" in {p[0] for p in profile.patterns}:
        flow.append("Browser loads SPA -> component renders")
        if "LLM interface" in comp_names:
            flow.append("Client calls backend API -> LLM service")
    elif "Cargo binary" in {p[0] for p in profile.patterns} or "CLI tool" in {p[0] for p in profile.patterns}:
        flow.append("User invokes CLI -> argument parser -> command handler")
    if "Database" in comp_names and not flow:
        flow.append("Application reads/writes through database layer")
    profile.data_flow = flow

    # Known patterns.
    kp: list[str] = []
    if ctx["packages"]:
        kp.append(f"Package boundaries: {', '.join(ctx['packages'][:6])}")
    if profile.layering:
        kp.append("; ".join(profile.layering))
    if "Configuration" in comp_names:
        kp.append("Configuration loaded from files/environment variables")
    if "Testing" in comp_names:
        kp.append("Automated test suite present")
    if profile.circular:
        kp.append("Circular dependencies detected: " + "; ".join(profile.circular))
    profile.known_patterns = kp

    # Complexity.
    n_files = len(ctx["files"])
    n_pkgs = len(ctx["packages"])
    if profile.circular:
        level = "High"
        reason = f"circular dependency: {'; '.join(profile.circular)}"
    elif n_files > 60 or n_pkgs >= 5:
        level = "High"
        reason = f"{n_files} source files across {n_pkgs} packages"
    elif n_files > 20 or n_pkgs >= 2:
        level = "Moderate"
        reason = f"{n_files} source files across {n_pkgs} packages"
    else:
        level = "Low"
        reason = f"{n_files} source files"
    suffix = "; no circular dependencies detected" if not profile.circular else ""
    profile.complexity = f"{level} — {reason}{suffix}"


def _layering(repo: Path, files: list[str], packages: list[str]) -> list[str]:
    out: list[str] = []
    if (repo / "src").is_dir() and (repo / "tests").is_dir():
        out.append("src/ + tests/ separation (code isolated from tests)")
    if any(p in ("core", "api", "db", "models", "services", "utils") for p in packages):
        out.append("layered modules: " + ", ".join(
            p for p in ("core", "api", "db", "models", "services", "utils") if p in packages
        ))
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _declared_external_deps(repo: Path, files: list[str]) -> dict[str, str]:
    """Library names declared in manifests -> evidence (for cross-checking).

    Mirrors `tech.py` intent but extracts the bare dependency names so they can
    be unioned into the imported-module set. Evidence cites the manifest.
    """
    out: dict[str, str] = {}

    def add(name: str, evidence: str) -> None:
        out[name.lower()] = evidence

    if "package.json" in files:
        txt = _read_manifest(repo, "package.json") or "{}"
        try:
            data = json.loads(txt)
        except json.JSONDecodeError:
            data = {}
        deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
        for name in deps:
            add(name, "package.json dependency")
    if "requirements.txt" in files:
        txt = _read_manifest(repo, "requirements.txt") or ""
        for line in txt.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("-"):
                continue
            name = re.split(r"[<>=!~ \[\]]", line, maxsplit=1)[0].strip().lower()
            if name:
                add(name, "requirements.txt")
    if "pyproject.toml" in files:
        txt = _read_manifest(repo, "pyproject.toml") or ""
        for m in re.finditer(r"^\s*([A-Za-z0-9_.\-]+)\s*[<>=!~\[]", txt, re.MULTILINE):
            add(m.group(1).lower(), "pyproject.toml dependency")
    if "Cargo.toml" in files:
        txt = _read_manifest(repo, "Cargo.toml") or ""
        for m in re.finditer(r"^\s*([A-Za-z0-9_\-]+)\s*=\s*[\"\']", txt, re.MULTILINE):
            # skip table headers like [dependencies]
            if m.group(1) in ("dependencies", "dev-dependencies"):
                continue
            add(m.group(1).lower(), "Cargo.toml dependency")
    return out


def analyze(repo_path: Path) -> ArchitectureProfile:
    repo = Path(repo_path)
    files = _source_files(repo)
    roots = _first_party_roots(repo, files)
    dep = _dependency_graph(repo, files, roots)
    declared = _declared_external_deps(repo, files)
    # Union imported roots with manifest-declared library names.
    ext_set = set(dep["external_dependencies"]) | set(declared)

    struct = _structure(repo, files)
    ctx_base = {
        "ext": ext_set,
        "packages": struct["packages"],
        "files": files,
    }
    arch_label, arch_ev, patterns, arch_conf = _architecture(repo, files, ctx_base)

    profile = ArchitectureProfile(path=str(repo))
    profile.top_level = struct["top_level"]
    profile.packages = struct["packages"]
    profile.apps = struct["apps"]
    profile.libraries = struct["libraries"]
    profile.modules = struct["modules"]
    profile.tests = struct["tests"]
    profile.config_files = struct["config_files"]
    profile.scripts = struct["scripts"]
    profile.architecture = arch_label
    profile.architecture_evidence = arch_ev
    profile.architecture_confidence = arch_conf
    profile.patterns = patterns
    profile.internal_imports = dep["internal_imports"]
    profile.package_boundaries = sorted(roots)
    profile.external_dependencies = dep["external_dependencies"]
    profile.important_libraries = dep["important_libraries"]
    profile.layering = _layering(repo, files, struct["packages"])
    profile.circular = dep["circular"]
    profile.architecture = arch_label
    profile.architecture_evidence = arch_ev
    profile.patterns = patterns
    profile.components = _components(repo, files, ext_set)
    profile.entry_points = _entry_points(repo, files, ctx_base)

    ctx = {"packages": struct["packages"], "files": files}
    _summary(profile, ctx)
    return profile


def analyze_and_store(conn, repo: Repo) -> ArchitectureProfile:
    """Extract architecture for `repo` and persist into the knowledge base."""
    from .db import (
        ComponentRow,
        EntryPointRow,
        replace_components,
        replace_entry_points,
        upsert_architecture,
    )

    profile = analyze(repo.path)
    # Resolve the repository row id (ingest must have created it already, but
    # the `analyze` CLI command also upserts a minimal row).
    row = conn.execute(
        "SELECT id FROM repositories WHERE path = ?", (str(repo.path),)
    ).fetchone()
    if row is None:
        from .gitmeta import collect
        from .db import upsert_repository

        meta = collect(repo)
        rid = upsert_repository(
            conn, name=meta.name, path=meta.path, default_branch=meta.default_branch,
            is_dirty=meta.is_dirty, first_commit_date=meta.first_commit_date,
            last_commit_date=meta.last_commit_date, remote_url=meta.remote_url,
            commit_count=meta.commit_count, readme_summary=None,
            license=meta.license, primary_author=meta.primary_author,
        )
    else:
        rid = row["id"]

    upsert_architecture(
        conn,
        repo_id=rid,
        architecture=profile.architecture,
        evidence="\n".join(profile.architecture_evidence),
        data_flow="\n".join(profile.data_flow) or None,
        known_patterns="\n".join(profile.known_patterns) or None,
        complexity=profile.complexity,
        confidence=profile.architecture_confidence,
    )
    replace_components(
        conn, rid,
        [ComponentRow(repo_id=rid, name=c.name, evidence=c.evidence, strength=c.strength)
         for c in profile.components],
    )
    replace_entry_points(
        conn, rid,
        [EntryPointRow(repo_id=rid, kind=e.kind, detail=e.detail, evidence=e.evidence)
         for e in profile.entry_points],
    )
    return profile
