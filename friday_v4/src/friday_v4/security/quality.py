"""QualityGate — code quality & static checks for Python projects.

Wave 3 (Security & Quality). Two layers:

1. **Built-in (always available, pure stdlib):** per-file AST analysis of
   Python sources — syntax errors, unused imports, undefined names, bare
   ``except:`` clauses, over-long lines, and TODO/FIXME markers.

2. **Optional subprocess:** if ``ruff`` is installed, run
   ``ruff check --output-format json``; if ``mypy`` is installed, run
   ``mypy --json`` — and merge findings. Degrades silently when absent.

The ruff pass runs with an explicit ``--select E4,E7,E9,F,W``
(``_RUFF_SELECT``): ruff 0.16's default select is a broad modern style
set that drowns a security scan in taste-based findings. The gate
reports real defects — pyflakes errors (F), pycodestyle errors/warnings
(E4/E7/E9, W) — and deliberately does NOT include bandit ``S`` rules;
secrets/vulnerabilities are covered by the dedicated scanners. Findings
carry the rule code as ``snippet`` and map severity from the rule/error
type (F/E = high, W = medium, others = low).
"""

from __future__ import annotations

import ast
import builtins
import io
import json
import logging
import subprocess
import tokenize
from pathlib import Path

from .tooling import find_tool, tool_available

logger = logging.getLogger("friday_v4.security.quality")

_SKIP_DIRS = {
    ".git", ".hg", ".svn", ".venv", "venv", "env", "node_modules",
    "__pycache__", ".tox", ".nox", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", "dist", "build", ".egg-info", "site-packages",
    "archive",  # archived code is not part of the active product
}

#: Ruff rules the quality gate runs. Ruff's DEFAULT select on 0.16+ is a
#: broad modern set (UP045, BLE001, PLW1510, …) that flags stylistic
#: preferences — noise for a security scan. Pin to error-level pycodestyle
#: + pyflakes + warnings so the gate reports real defects, not taste.
_RUFF_SELECT = ("E4", "E7", "E9", "F", "W")

#: Line length threshold for the built-in over-long-line check.
_MAX_LINE_LENGTH = 100


class QualityGate:
    """Static quality checks for a Python project."""

    detector_name = "quality"

    def __init__(self, subprocess_timeout: float = 120.0):
        self.subprocess_timeout = subprocess_timeout
        self.tools: dict[str, bool] = {}

    # -- built-in AST checks ----------------------------------------------

    def _iter_py_files(self, root: Path):
        root = Path(root)
        if root.is_file():
            if root.suffix == ".py":
                yield root
            return
        for path in root.rglob("*.py"):
            if any(part in _SKIP_DIRS for part in path.relative_to(root).parts):
                continue
            yield path

    def _check_file(self, path: Path) -> list[dict]:
        findings: list[dict] = []
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return findings

        rel = str(path)
        # Syntax
        try:
            tree = ast.parse(text)
        except SyntaxError as exc:
            findings.append({
                "severity": "high",
                "title": "Syntax error",
                "detail": f"File does not parse: {exc.msg}",
                "file": rel,
                "line": exc.lineno or 0,
                "snippet": "syntax",
                "detector": "builtin",
            })
            return findings  # can't analyze further

        # Undefined names (best-effort scope analysis).
        # ``ast.walk`` visits every Name — Store-context Names (assignments,
        # for-loop vars, comprehension targets, with-as, except-as) are all
        # collected as defined. Tuple targets are walked element-by-element,
        # so `for a, b in pairs` defines both a and b.
        defined: set[str] = set(dir(builtins))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    defined.add((alias.asname or alias.name).split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    defined.add(alias.asname or alias.name)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                   ast.ClassDef)):
                defined.add(node.name)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                defined.add(node.id)
            elif isinstance(node, ast.arg):
                defined.add(node.arg)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                defined.add(node.name)
        undefined: list[tuple[int, str]] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                if node.id.startswith("__") and node.id.endswith("__"):
                    continue  # dunder names (__file__, __name__) are language-provided
                if node.id not in defined:
                    undefined.append((node.lineno, node.id))
        for lineno, name in sorted(set(undefined))[:10]:
            findings.append({
                "severity": "low",
                "title": f"Possibly undefined name: {name}",
                "detail": f"'{name}' is used but not defined or imported in "
                          "this file (best-effort check).",
                "file": rel,
                "line": lineno,
                "snippet": "undefined-name",
                "detector": "builtin",
            })

        # Unused imports — skipped for ``__init__.py``, where imports ARE
        # the package's public API (re-exports). ruff's F401 applies the
        # same exemption; the naive built-in checker must too, or every
        # package __init__ is flagged as unused-import noise.
        if path.name != "__init__.py":
            imported: dict[str, int] = {}
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        imported[(alias.asname or alias.name).split(".")[0]] = node.lineno
                elif isinstance(node, ast.ImportFrom):
                    for alias in node.names:
                        if alias.name != "*":
                            imported[alias.asname or alias.name] = node.lineno
            used = {n.id for n in ast.walk(tree)
                    if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
            # An inline noqa comment suppresses the check (matches ruff):
            # availability-probe imports (e.g. `import PIL` annotated with an
            # inline "noqa: F401") are intentional even when the name is
            # never used. NB: this prose deliberately omits the leading '#'
            # token, which ruff would parse as a directive.
            noqa_lines = {i for i, line in enumerate(text.splitlines(), start=1)
                          if "# noqa" in line}
            for name, lineno in imported.items():
                if lineno in noqa_lines:
                    continue
                if name not in used and name != "annotations":
                    findings.append({
                        "severity": "low",
                        "title": f"Unused import: {name}",
                        "detail": f"'{name}' is imported but never used.",
                        "file": rel,
                        "line": lineno,
                        "snippet": "unused-import",
                        "detector": "builtin",
                    })

        # Bare except
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler) and node.type is None:
                findings.append({
                    "severity": "medium",
                    "title": "Bare except clause",
                    "detail": "A bare `except:` swallows every exception — "
                              "catch specific exception types instead.",
                    "file": rel,
                    "line": getattr(node, "lineno", 0),
                    "snippet": "bare-except",
                    "detector": "builtin",
                })

        # Token-aware line checks: skip the line-length rule for lines that
        # live inside a multi-line string literal (embedded HTML/CSS/PowerShell
        # templates can't be wrapped), and only flag task-marker words inside
        # real `#` comments — string data and this checker's own source must
        # not self-flag as markers. (Prose here deliberately avoids the literal
        # marker tokens: a comment containing them would be flagged.)
        string_lines: set[int] = set()
        comment_lines: set[int] = set()
        try:
            for tok in tokenize.generate_tokens(io.StringIO(text).readline):
                if tok.type == tokenize.STRING and tok.end[0] > tok.start[0]:
                    string_lines.update(range(tok.start[0], tok.end[0] + 1))
                elif tok.type == tokenize.COMMENT and (
                        "TODO" in tok.string or "FIXME" in tok.string):
                    comment_lines.add(tok.start[0])
        except (tokenize.TokenError, ValueError, IndentationError):
            pass  # malformed input — fall back to the naive per-line check

        for lineno, line in enumerate(text.splitlines(), start=1):
            if lineno not in string_lines and len(line) > _MAX_LINE_LENGTH:
                findings.append({
                    "severity": "info",
                    "title": f"Line too long ({len(line)} > {_MAX_LINE_LENGTH})",
                    "detail": "Long lines hurt readability; wrap or refactor.",
                    "file": rel,
                    "line": lineno,
                    "snippet": "line-length",
                    "detector": "builtin",
                })
            if lineno in comment_lines:
                findings.append({
                    "severity": "info",
                    "title": "Task marker in comment",
                    "detail": line.strip()[:80],
                    "file": rel,
                    "line": lineno,
                    "snippet": "todo",
                    "detector": "builtin",
                })
        return findings

    # -- optional ruff / mypy ---------------------------------------------

    def scan_with_ruff(self, root: Path) -> list[dict]:
        binary = find_tool("ruff")
        if not binary or not Path(root).exists():
            return []
        try:
            result = subprocess.run(
                [binary, "check", "--output-format=json",
                 "--select", ",".join(_RUFF_SELECT), str(root)],
                capture_output=True, text=True, timeout=self.subprocess_timeout)
            data = json.loads(result.stdout or "[]")
        except Exception as exc:
            logger.debug(f"ruff failed: {exc}")
            return []
        findings: list[dict] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            code = str(item.get("code", ""))
            sev = "high" if code[:1] in ("F", "E") else (
                "medium" if code[:1] == "W" else "low")
            # ruff's JSON schema puts the path at the TOP level ("filename")
            # — ``location`` only carries row/column. Reading location.file
            # produced empty paths on every finding.
            filename = str(item.get("filename") or "")
            row = int(item.get("location", {}).get("row") or 0)
            findings.append({
                "severity": sev,
                "title": f"ruff {code}: {item.get('message', '')}",
                "detail": f"Lint finding at {filename}:{row}.",
                "file": filename,
                "line": row,
                "snippet": code,
                "detector": "ruff",
            })
        return findings

    def scan_with_mypy(self, root: Path) -> list[dict]:
        binary = find_tool("mypy")
        if not binary or not Path(root).exists():
            return []
        try:
            result = subprocess.run(
                [binary, "--no-error-summary", "--output=json", str(root)],
                capture_output=True, text=True, timeout=self.subprocess_timeout)
            data = json.loads(result.stdout or "[]")
        except Exception as exc:
            logger.debug(f"mypy failed: {exc}")
            return []
        findings: list[dict] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            sev = "high" if item.get("severity") == "error" else "low"
            findings.append({
                "severity": sev,
                "title": f"mypy: {item.get('message', '')}",
                "detail": f"{item.get('file', '')}:{item.get('line', 0)}.",
                "file": item.get("file", ""),
                "line": item.get("line", 0),
                "snippet": item.get("severity", ""),
                "detector": "mypy",
            })
        return findings

    # -- public API -------------------------------------------------------

    def scan(self, root: Path) -> tuple[list[dict], dict]:
        """Scan a project for quality issues; returns (findings, tools)."""
        root = Path(root)
        findings: list[dict] = []
        for path in self._iter_py_files(root):
            findings.extend(self._check_file(path))
        findings.extend(self.scan_with_ruff(root))
        findings.extend(self.scan_with_mypy(root))
        tools = {
            "ruff": tool_available("ruff"),
            "mypy": tool_available("mypy"),
            "builtin-quality": True,
        }
        return findings, tools
