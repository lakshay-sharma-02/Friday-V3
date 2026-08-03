"""Static analyzer — the always-works fallback (Wave 6).

When no LSP server is available (or the project has no known language
server), Friday still diagnoses code with the standard library's ``ast``:

- **Syntax errors** — a real ``SyntaxError`` with its line/column.
- **Undefined names** — names loaded but never bound in scope (an
  F821-class error: a reference that will raise ``NameError`` at runtime).
- **Unused imports** — top-level imports never referenced.
- **Shadowed builtins** — a module-level binding that hides a builtin
  (``list = 5``), the classic footgun.

Pure stdlib, deterministic, hermetic — no subprocesses, no network.
Deliberately conservative: only *provable* issues are reported so the
fallback never floods Friday with noise (false positives erode trust).

The result list is sorted by line and capped so a catastrophic file
can't produce a wall of text.
"""

from __future__ import annotations

import ast
import builtins
import logging
from pathlib import Path
from typing import Optional

from .lsp_client import Diagnostic

logger = logging.getLogger("friday_v4.desktop.ide.ast_analyzer")

#: The default max issues returned by one analysis pass.
MAX_ISSUES = 12

#: Names that are legitimately pre-bound in module/class bodies and must
#: never be flagged as undefined.
_ALWAYS_OK = frozenset({
    "self", "cls", "super", "__name__", "__file__", "__package__",
    "__doc__", "__builtins__", "__all__", "__class__", "__module__",
    "True", "False", "None", "NotImplemented", "Ellipsis", "typing",
    "print",  # builtin, but listed explicitly for safety
})


def _defined_names(tree: ast.AST) -> set[str]:
    """Every name bound anywhere in the tree (imports, defs, stores)."""
    defined: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defined.add(node.name)
            _add_args(defined, node.args)
        elif isinstance(node, ast.Lambda):
            _add_args(defined, node.args)
        elif isinstance(node, ast.ClassDef):
            defined.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                defined.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            defined.add(node.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            # ``except ValueError as err`` binds ``err`` — the name is a
            # plain string attribute, NOT an ast.Name node, so it must
            # be collected explicitly or ``return err`` gets flagged as
            # an undefined name (the most common Python idiom).
            defined.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            defined.update(node.names)
    return defined


def _add_args(defined: set[str], args) -> None:
    """Add every argument name of a function's arguments node."""
    all_args = (list(getattr(args, "posonlyargs", []) or [])
                + list(args.args or [])
                + list(getattr(args, "kwonlyargs", []) or []))
    for a in all_args:
        if getattr(a, "arg", None):
            defined.add(a.arg)
    if getattr(args, "vararg", None) is not None:
        defined.add(args.vararg.arg)
    if getattr(args, "kwarg", None) is not None:
        defined.add(args.kwarg.arg)


def _used_names(tree: ast.AST) -> dict[str, int]:
    """{name: first-use line} for every Load-context Name."""
    usage: dict[str, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id not in usage:
                usage[node.id] = node.lineno
    return usage


def _unused_imports(tree: ast.AST, used: dict[str, int]) -> list[tuple[int, str]]:
    """(line, alias) pairs for module-level imports never referenced."""
    unused: list[tuple[int, str]] = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                raw = alias.asname or alias.name
                if raw.startswith("*"):
                    continue  # ``from x import *`` — not a single unused name
                name = raw.split(".")[0]
                if name not in used:
                    unused.append((node.lineno, name))
    return unused


def _shadowed_builtins(tree: ast.AST, builtin_names: set[str],
                       used: dict[str, int]) -> list[tuple[int, str]]:
    """(line, name) pairs for module-level builtin shadowing."""
    shadowed: list[tuple[int, str]] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            continue  # scoped shadows are fine
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]  # type: ignore[attr-defined]
            for target in targets:
                if isinstance(target, ast.Name) and target.id in builtin_names:
                    shadowed.append((node.lineno, target.id))
    return shadowed


def analyze_source(source: str, filename: str = "<string>",
                   *, max_issues: int = MAX_ISSUES) -> list[Diagnostic]:
    """Analyze source text; returns issues sorted by line (never raises)."""
    if not isinstance(source, str):
        source = ""
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as exc:
        line = exc.lineno or 1
        col = max(int(exc.offset or 1) - 1, 0)
        msg = (exc.msg or "syntax error").strip()
        return [Diagnostic(
            message=f"SyntaxError: {msg}", severity=1, line=line,
            character=col, end_line=line, end_character=col + 1,
            source="ast", code="E999")]

    used = _used_names(tree)
    defined = _defined_names(tree)
    builtin_names = set(dir(builtins))
    issues: list[Diagnostic] = []

    # Undefined names — the real runtime errors.
    undefined = sorted(
        ((ln, name) for name, ln in used.items()
         if name not in defined and name not in builtin_names
         and name not in _ALWAYS_OK),
        key=lambda t: t[0])
    for line, name in undefined:
        issues.append(Diagnostic(
            message=f"undefined name {name!r}", severity=1, line=line,
            source="ast", code="F821"))
        if len(issues) >= max_issues:
            break

    if len(issues) < max_issues:
        for line, name in _unused_imports(tree, used):
            issues.append(Diagnostic(
                message=f"imported but unused: {name}", severity=2,
                line=line, source="ast", code="F401"))
            if len(issues) >= max_issues:
                break

    if len(issues) < max_issues:
        for line, name in _shadowed_builtins(tree, builtin_names, used):
            issues.append(Diagnostic(
                message=f"shadowed builtin: {name}", severity=2,
                line=line, source="ast", code="A002"))

    issues.sort(key=lambda d: (d.line, d.weight))
    return issues[:max_issues]


def analyze_file(path: str | Path) -> list[Diagnostic]:
    """Analyze a file on disk; unreadable/missing → [] (never raises)."""
    p = Path(path)
    try:
        if not p.is_file():
            return []
        source = p.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.debug(f"ast analyze_file {p}: {exc}")
        return []
    return analyze_source(source, filename=str(p))


__all__ = ["analyze_source", "analyze_file", "MAX_ISSUES"]
