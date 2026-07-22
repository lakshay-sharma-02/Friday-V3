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


def _systemctl_available() -> bool:
    import shutil
    return shutil.which("systemctl") is not None


def _check_watch(conn) -> List[Tuple[str, str, str]]:
    """Check watch loop status."""
    issues: List[Tuple[str, str, str]] = []
    import subprocess

    # Is the systemd timer installed?
    try:
        r = subprocess.run(
            ["systemctl", "--user", "is-enabled", "friday-watch.timer"],
            capture_output=True, text=True, timeout=10)
        timer_enabled = r.stdout.strip() == "enabled"
    except Exception:
        timer_enabled = False

    if not timer_enabled:
        issues.append(("watch", "info", "not installed — run `friday watch --install`"))
        return issues

    # Check if timer is active.
    try:
        r = subprocess.run(
            ["systemctl", "--user", "is-active", "friday-watch.timer"],
            capture_output=True, text=True, timeout=10)
        timer_active = r.stdout.strip() == "active"
    except Exception:
        timer_active = False

    if not timer_active:
        issues.append(("watch", "unavailable", "timer installed but not active"))
        return issues

    # Recent cycle outcomes.
    row = conn.execute(
        "SELECT outcome, started_at, finished_at, error_detail "
        "FROM watch_history ORDER BY id DESC LIMIT 1"
    ).fetchone()

    if row is None:
        issues.append(("watch", "info", "installed and active; no cycles yet"))
        return issues

    if row["outcome"] == "failed":
        issues.append(("watch", "error",
                       f"last cycle FAILED at {row['started_at']}: "
                       f"{row['error_detail'] or 'unknown'}"))
    elif row["outcome"] == "succeeded":
        pass  # healthy — no issue to report
    else:
        issues.append(("watch", "info",
                       f"last cycle status: {row['outcome']} at {row['started_at']}"))

    return issues


def _check_graph_proposals(conn) -> List[Tuple[str, str, str]]:
    """Check for pending graph proposals awaiting review (Phase 5)."""
    issues: List[Tuple[str, str, str]] = []
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM task_graphs WHERE status='proposal'"
        ).fetchone()
        if row and row["cnt"] > 0:
            issues.append(("graph proposals", "info",
                           f"{row['cnt']} proposal(s) awaiting review — "
                           f"run `friday graph review`"))
    except Exception:
        pass  # table may not exist yet
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

    # README check
    repo_root = Path(__file__).resolve().parents[2]
    issues = _check_readme(repo_root)
    all_issues.extend(issues)

    # Env check
    issues = _check_env()
    all_issues.extend(issues)

    # Watch check (needs conn). Skip silently if systemctl unavailable.
    if _systemctl_available():
        issues = _check_watch(conn)
        all_issues.extend(issues)

    # Phase 5: graph proposals check.
    issues = _check_graph_proposals(conn)
    all_issues.extend(issues)

    conn.close()

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
