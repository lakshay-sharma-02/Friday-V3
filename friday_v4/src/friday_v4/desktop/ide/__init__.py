"""IDE Integration — VS Code extension, JetBrains plugin, LSP client.

Extends Friday into the editor, enabling inline code review, quick actions,
status bar integration, and deep code analysis via LSP.

**Status:** Wave 6 stub. ``lsp_client`` is not implemented yet, so the
imports below are guarded — importing this package must never crash the
desktop suite.

Components:
    - LSP Client: code analysis, diagnostics, symbol lookup
    - VS Code Extension: sidebar, commands, status bar
    - JetBrains Plugin: tool window, actions, notifications
"""

from __future__ import annotations

try:
    from .lsp_client import LSPClient, Diagnostic, SymbolInfo
    _LSP_AVAILABLE = True
except ImportError:  # pragma: no cover - Wave 6 stub
    LSPClient = None  # type: ignore
    Diagnostic = None  # type: ignore
    SymbolInfo = None  # type: ignore
    _LSP_AVAILABLE = False

__all__ = [
    "LSPClient",
    "Diagnostic",
    "SymbolInfo",
    "_LSP_AVAILABLE",
]
