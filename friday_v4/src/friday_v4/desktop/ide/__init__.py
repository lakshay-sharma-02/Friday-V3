"""IDE Integration — LSP analysis, editor control, adaptive detection (Wave 6).

Friday lives inside the editor:

- **Detection** — ``detect()`` figures out which editor is present
  (VS Code / JetBrains / Neovim / Sublime / Emacs) from env, processes,
  and config dirs, and adapts launcher argv per kind.
- **Analysis** — ``analyze_file()`` asks a real language server
  (pyright / pylsp / typescript-language-server / gopls / rust-analyzer)
  for diagnostics + symbols over the pure-stdlib JSON-RPC client; with
  no server available it degrades to the built-in ``ast`` analyzer
  (syntax errors, undefined names, unused imports) — Friday *always*
  has an opinion about your code.
- **Control** — ``open()`` / ``reveal()`` open files and jump to lines
  in the detected editor; ``run()`` shells workspace commands.
- **Composition** — ``analyze_file`` feeds the reasoning ``code_provider``
  (\"what's wrong with auth.py\"), the NL router's IDE intent, and —
  when ``FRIDAY_V4_IDE_PREFLIGHT`` is opted in — the execution layer's
  Claude Code delegation (diagnostics ride along via
  ``--append-system-prompt``) and command preflight notes.

Design laws: never crashes (every path degrades), hermetic tests (no
real editor required — the fake LSP server + AST analyzer cover it),
stdlib-only transport.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .detection import DetectedIDE, detect, detect_all, is_available
from .lsp_client import LSPClient, Diagnostic, SymbolInfo
from .ast_analyzer import analyze_source
from . import ast_analyzer
from . import controller

logger = logging.getLogger("friday_v4.desktop.ide")

#: Compatibility marker kept from the Wave 6 stub era: the LSP client
#: *is* implemented now.
_LSP_AVAILABLE = True

#: Values that mean "explicitly off" for ``FRIDAY_V4_IDE_PREFLIGHT``.
_PREFLIGHT_OFF = ("", "0", "false", "no", "off", "none")

#: Project markers → language family → language server command
#: candidates. The first existing executable wins; ``--stdio``-style
#: flags match each server's CLI contract.
_LANGUAGE_SERVERS: tuple[tuple[tuple[str, ...], str, tuple[tuple[str, tuple[str, ...]], ...]], ...] = (
    (("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt",
      "Pipfile", "poetry.lock"),
     "py",
     (("pyright-langserver", ("--stdio",)),
      ("basedpyright-langserver", ("--stdio",)),
      ("pylsp", ()))),
    (("package.json",), "ts",
     (("typescript-language-server", ("--stdio",)),)),
    (("go.mod",), "go",
     (("gopls", ("serve",)),)),
    (("Cargo.toml",), "rs",
     (("rust-analyzer", ()),)),
)

#: Source extensions the preflight/analysis consider "code".
_SOURCE_EXTS = (".py", ".pyi", ".ts", ".tsx", ".js", ".jsx", ".go",
                ".rs", ".java", ".kt", ".c", ".h", ".cpp", ".hpp",
                ".cs", ".rb", ".php")


@dataclass
class AnalysisResult:
    """One file's analysis — which method ran and what it found."""

    path: str
    display_path: str
    method: str                      # "lsp" | "ast" | "none"
    diagnostics: list = field(default_factory=list)
    symbols: list = field(default_factory=list)
    ide: Optional[DetectedIDE] = None

    @property
    def error_count(self) -> int:
        return sum(1 for d in self.diagnostics if d.severity == 1)

    @property
    def issue_count(self) -> int:
        return len(self.diagnostics)

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "display_path": self.display_path,
            "method": self.method,
            "ide": (self.ide.name if self.ide else None),
            "diagnostics": [d.to_dict() for d in self.diagnostics],
            "symbols": [s.to_dict() for s in self.symbols],
            "error_count": self.error_count,
        }


def preflight_opted_in() -> bool:
    """Whether IDE preflight rides along with execution (explicit opt-in)."""
    return os.environ.get("FRIDAY_V4_IDE_PREFLIGHT",
                          "").strip().lower() not in _PREFLIGHT_OFF


def lsp_command_for_workspace(root: str | Path,
                              ide: Optional[DetectedIDE] = None,
                              filename: Optional[str] = None) -> Optional[list[str]]:
    """A language-server command for the workspace, or None (never raises).

    Project markers decide the language family; the first installed
    server executable wins. ``filename`` narrows the language check
    (a lone ``.ts`` file still gets TypeScript even without package.json
    markers, so single-file analysis works).
    """
    try:
        root_path = Path(root).resolve()
        ext = Path(filename or "").suffix.lower() if filename else ""
        ext_family = None
        if ext in (".ts", ".tsx", ".js", ".jsx"):
            ext_family = "ts"
        elif ext in (".py", ".pyi"):
            ext_family = "py"
        elif ext == ".go":
            ext_family = "go"
        elif ext == ".rs":
            ext_family = "rs"

        def _pick(servers: tuple[tuple[str, tuple[str, ...]], ...]):
            for exe, flags in servers:
                found = _find(exe)
                if found:
                    return [found, *flags]
            return None

        for markers, family, servers in _LANGUAGE_SERVERS:
            matched = any((root_path / m).exists() for m in markers)
            # Single-file analysis: a lone ``.ts`` in an otherwise
            # marker-less directory still gets TypeScript tooling.
            if not matched and ext_family and ext_family != family:
                continue
            if matched or (ext_family and ext_family == family):
                cmd = _pick(servers)
                if cmd:
                    return cmd
        return None
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug(f"lsp lookup failed: {exc}")
        return None


def _find(exe: str) -> Optional[str]:
    """PATH + venv-aware tool lookup (mirrors security.tooling.find_tool)."""
    import shutil
    found = shutil.which(exe)
    if found:
        return found
    try:
        from ..security.tooling import find_tool
        found = find_tool(exe)
    except Exception:
        found = None
    return found or None


def _resolve_path(path: str | Path, cwd: Optional[str | Path] = None) -> Path:
    p = Path(path).expanduser()
    if not p.is_absolute():
        base = Path(cwd).resolve() if cwd else Path.cwd()
        p = base / p
    return p.resolve()


def analyze_file(path: str | Path, cwd: Optional[str | Path] = None,
                 ide: Optional[DetectedIDE] = None) -> AnalysisResult:
    """Diagnose a file — LSP first, AST fallback. Never raises.

    ``method`` in the result tells the caller which analyzer produced
    the diagnostics (\"lsp\" / \"ast\" / \"none\" when the file is not
    analyzable). A live LSP server that reports zero issues is a real
    result (method \"lsp\"), not a fallback.
    """
    p = _resolve_path(path, cwd)
    display = str(path)
    if not p.is_file():
        return AnalysisResult(path=str(path), display_path=display,
                              method="none")

    ide_used = ide or detect()
    root = str(Path(cwd).resolve() if cwd else p.parent)
    server_cmd = lsp_command_for_workspace(root, ide_used, filename=p.name)

    if server_cmd:
        try:
            client = LSPClient(server_cmd, root=root)
            if client.start():
                try:
                    diags = client.diagnostics(p)
                    syms = client.symbols(p)
                    return AnalysisResult(
                        path=str(p), display_path=display, method="lsp",
                        diagnostics=diags, symbols=syms, ide=ide_used)
                finally:
                    client.shutdown()
        except Exception as exc:
            logger.debug(f"lsp analysis failed ({exc}) — AST fallback")

    try:
        diags = ast_analyzer.analyze_file(p)
        method = "ast" if diags else "ast"
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug(f"ast analysis failed: {exc}")
        return AnalysisResult(path=str(p), display_path=display,
                              method="none", ide=ide_used)
    return AnalysisResult(path=str(p), display_path=display, method=method,
                          diagnostics=diags, ide=ide_used)


def symbols(path: str | Path, cwd: Optional[str | Path] = None) -> list[SymbolInfo]:
    """Symbols for a file (LSP), falling back to a light AST outline."""
    p = _resolve_path(path, cwd)
    if not p.is_file():
        return []
    root = str(Path(cwd).resolve() if cwd else p.parent)
    server_cmd = lsp_command_for_workspace(root, None, filename=p.name)
    if server_cmd:
        try:
            client = LSPClient(server_cmd, root=root)
            if client.start():
                try:
                    return client.symbols(p)
                finally:
                    client.shutdown()
        except Exception as exc:
            logger.debug(f"lsp symbols failed ({exc}) — AST outline")
    return _ast_outline(p)


def _ast_outline(p: Path) -> list[SymbolInfo]:
    """A minimal function/class outline for Python files (never raises)."""
    if p.suffix.lower() != ".py":
        return []
    try:
        import ast
        tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"),
                         filename=str(p))
    except Exception:
        return []
    out: list[SymbolInfo] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append(SymbolInfo(name=node.name, kind=12,
                                  kind_name="function", line=node.lineno))
        elif isinstance(node, ast.ClassDef):
            out.append(SymbolInfo(name=node.name, kind=5, kind_name="class",
                                  line=node.lineno))
    return out


def open_in_ide(path: str | Path, ide: Optional[DetectedIDE] = None) -> tuple[bool, str]:
    """Open a file in the detected editor (or OS opener). Never raises."""
    return controller.open_file(ide or detect(), path)


def reveal_in_ide(path: str | Path, line: int,
                  ide: Optional[DetectedIDE] = None) -> tuple[bool, str]:
    """Reveal a file at a line in the detected editor. Never raises."""
    return controller.reveal(ide or detect(), path, line)


def run_in_workspace(command: str,
                     cwd: Optional[str | Path] = None) -> tuple[bool, str]:
    """Run a command in a workspace (raw runner). Never raises."""
    return controller.run_command(command, cwd=cwd)


def is_available() -> bool:
    """The IDE layer is always available (detection + AST analysis)."""
    return True


__all__ = [
    "AnalysisResult",
    "DetectedIDE",
    "Diagnostic",
    "SymbolInfo",
    "detect",
    "detect_all",
    "is_available",
    "analyze_file",
    "analyze_source",
    "symbols",
    "lsp_command_for_workspace",
    "preflight_opted_in",
    "open_in_ide",
    "reveal_in_ide",
    "run_in_workspace",
    "_LSP_AVAILABLE",
]
