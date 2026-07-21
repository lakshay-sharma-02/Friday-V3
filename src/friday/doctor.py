"""friday doctor — system health check.

Checks: DB schema current, pyproject.toml deps match actual imports,
at least one worker registered and available, README isn't a stub.
"""

from __future__ import annotations

import argparse
import ast
import glob
import importlib.metadata
import os
import sys
from pathlib import Path
from typing import List, Tuple


def _check_imports() -> List[Tuple[str, str, str]]:
    """Check that pyproject.toml deps cover actual imports."""
    import tomllib
    issues: List[Tuple[str, str, str]] = []
    try:
        with open(Path(__file__).resolve().parents[2] / "pyproject.toml", "rb") as f:
            pyproj = tomllib.load(f)
    except Exception as e:
        issues.append(("pyproject.toml", "error", f"cannot read: {e}"))
        return issues

    declared = set(pyproj.get("project", {}).get("dependencies", []))
    src_dir = Path(__file__).resolve().parent
    stdlib = set(sys.stdlib_module_names) if hasattr(sys, "stdlib_module_names") else set()

    imports = set()
    for f in glob.glob(str(src_dir / "**/*.py"), recursive=True):
        with open(f) as fh:
            try:
                tree = ast.parse(fh.read())
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        for alias in node.names:
                            imports.add(alias.name.split(".")[0])
                    elif isinstance(node, ast.ImportFrom):
                        if node.module:
                            imports.add(node.module.split(".")[0])
            except SyntaxError:
                pass

    third_party = set()
    for mod in sorted(imports):
        if mod in stdlib or mod == "friday":
            continue
        try:
            spec = __import__(mod).__spec__
            origin = spec.origin if spec else ""
            if origin and "site-packages" in origin:
                third_party.add(mod)
        except (ImportError, ModuleNotFoundError):
            pass

    for tp in sorted(third_party):
        if not any(tp in dep for dep in declared):
            issues.append(("dependencies", "missing", f"{tp} imported but not in pyproject.toml"))

    return issues


def _check_readme(path: Path) -> List[Tuple[str, str, str]]:
    issues: List[Tuple[str, str, str]] = []
    readme = path / "README.md"
    if not readme.exists():
        issues.append(("README.md", "error", "not found"))
        return issues
    text = readme.read_text(encoding="utf-8")
    if len(text) < 200 or "# " not in text:
        issues.append(("README.md", "stub", "too short or no heading"))
    return issues


def _check_database(conn) -> List[Tuple[str, str, str]]:
    """Check DB schema is current."""
    issues: List[Tuple[str, str, str]] = []
    try:
        cursor = conn.execute("SELECT COUNT(*) FROM workers")
        worker_count = cursor.fetchone()[0]
        if worker_count == 0:
            issues.append(("workers", "empty", "no workers registered. Run `friday capability list` or execute a goal to bootstrap."))
        else:
            cursor = conn.execute("SELECT COUNT(*) FROM workers WHERE status='active' AND availability='available'")
            available = cursor.fetchone()[0]
            if available == 0:
                issues.append(("workers", "unavailable", f"{worker_count} registered but none available"))
    except Exception as e:
        issues.append(("database", "error", str(e)))
    return issues


def _check_env() -> List[Tuple[str, str, str]]:
    issues: List[Tuple[str, str, str]] = []
    llm = os.environ.get("FRIDAY_LLM_MODEL", "")
    if not llm:
        issues.append(("llm", "info", "FRIDAY_LLM_MODEL not set — offline mode only"))
    return issues


def cmd_doctor(args: argparse.Namespace) -> int:
    """Check system health.

    Exits 0 when all checks pass, 2 on any warning/error.
    """
    from .db import connect

    conn = connect()
    print("Friday doctor — system health check\n")

    all_issues: List[Tuple[str, str, str]] = []

    # Imports check
    issues = _check_imports()
    all_issues.extend(issues)

    # DB check
    issues = _check_database(conn)
    all_issues.extend(issues)
    conn.close()

    # README check
    repo_root = Path(__file__).resolve().parents[2]
    issues = _check_readme(repo_root)
    all_issues.extend(issues)

    # Env check
    issues = _check_env()
    all_issues.extend(issues)

    # Report
    if not all_issues:
        print("All checks passed. ✓")
        return 0

    has_error = False
    for category, severity, message in all_issues:
        mark = {"error": "✗", "missing": "✗", "stub": "!", "empty": "!", "unavailable": "!", "info": "i"}.get(severity, "?")
        print(f"  [{mark}] {category}: {message}")
        if severity in ("error", "missing"):
            has_error = True

    print(f"\n{len(all_issues)} issue(s) found.")
    return 2 if has_error else 0
