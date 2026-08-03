"""Pure-stdlib LSP client — JSON-RPC 2.0 over stdio (Wave 6).

A minimal, dependency-free client for the Language Server Protocol so
Friday can ask a real language server (pyright, pylsp, typescript-
language-server, gopls, rust-analyzer, …) for diagnostics and symbols
without pulling in ``pygls``/``lsprotocol``.

Protocol surface implemented:

- ``initialize`` / ``initialized`` handshake
- ``textDocument/didOpen`` (with the file's current text)
- ``textDocument/diagnostic`` (LSP 3.17 on-demand pull) with a fallback
  to ``textDocument/publishDiagnostics`` pushes
- ``textDocument/documentSymbol`` (hierarchical + flat forms)
- ``shutdown`` / ``exit``

Transport: Content-Length framed JSON messages on stdin/stdout; a
reader thread dispatches responses (by id) and ``publishDiagnostics``
notifications (by URI). Every operation is timeout-bounded and never
raises past :class:`LSPError` — a broken/missing server degrades to
``start() -> False`` so callers fall back to the AST analyzer.

Hermetic: no I/O at import; the server is a caller-provided command
(tests use a fake server script).
"""

from __future__ import annotations

import json
import logging
import queue
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("friday_v4.desktop.ide.lsp_client")

#: LSP severity → human name.
SEVERITY_NAMES = {1: "error", 2: "warning", 3: "info", 4: "hint"}
SEVERITY_WEIGHT = {1: 3, 2: 2, 3: 1, 4: 0}  # for sorting/counts


class LSPError(Exception):
    """A protocol/transport failure — callers degrade, never crash."""


def uri_for(path: Path) -> str:
    """file:// URI for a path (best-effort, never raises)."""
    return path.resolve().as_uri()


def _lsp_uri(path: str) -> str:
    return Path(path).resolve().as_uri()


@dataclass
class Diagnostic:
    """One issue a language server (or the AST fallback) found."""

    message: str
    severity: int = 1            # LSP: 1 error · 2 warning · 3 info · 4 hint
    line: int = 1                # 1-based (human-facing)
    character: int = 0           # 0-based column
    end_line: Optional[int] = None
    end_character: Optional[int] = None
    source: str = ""             # e.g. "pyright" / "ast"
    code: Optional[str] = None

    @property
    def severity_name(self) -> str:
        return SEVERITY_NAMES.get(self.severity, "info")

    @property
    def weight(self) -> int:
        return SEVERITY_WEIGHT.get(self.severity, 0)

    def brief(self, max_message: int = 120) -> str:
        """'line 4: message' — one line for CLI/NL surfaces."""
        msg = (self.message or "").strip().replace("\n", " ")
        if len(msg) > max_message:
            msg = msg[:max_message] + "…"
        return f"line {self.line}: {msg}"

    def to_dict(self) -> dict:
        return {
            "message": self.message,
            "severity": self.severity,
            "severity_name": self.severity_name,
            "line": self.line,
            "character": self.character,
            "end_line": self.end_line,
            "end_character": self.end_character,
            "source": self.source,
            "code": self.code,
        }


@dataclass
class SymbolInfo:
    """One document symbol (from LSP documentSymbol)."""

    name: str
    kind: int = 0
    kind_name: str = "unknown"
    line: int = 1
    end_line: Optional[int] = None
    container: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "kind": self.kind,
            "kind_name": self.kind_name,
            "line": self.line,
            "end_line": self.end_line,
            "container": self.container,
        }


#: LSP SymbolKind ints → names (the common ones we display).
_SYMBOL_KINDS = {
    2: "module", 3: "namespace", 4: "package", 5: "class", 6: "method",
    7: "property", 8: "field", 9: "constructor", 10: "enum", 11: "interface",
    12: "function", 13: "variable", 14: "constant", 15: "string",
    16: "number", 17: "boolean", 18: "array", 19: "object", 20: "key",
    21: "null", 22: "enum_member", 23: "struct", 24: "event",
    25: "operator", 26: "type_parameter",
}


def _kind_name(kind: int) -> str:
    return _SYMBOL_KINDS.get(kind, "symbol")


class LSPClient:
    """A stdio LSP session. ``start()`` → work → ``shutdown()``.

    Usage::

        client = LSPClient(["pyright-langserver", "--stdio"], root=repo)
        if client.start():
            try:
                client.open_file(path)
                diags = client.diagnostics(path)
                syms = client.symbols(path)
            finally:
                client.shutdown()

    Never raises past :class:`LSPError`; ``start()`` returns False on any
    failure so the caller falls back to static analysis.
    """

    def __init__(self, server_command: list[str], root: str | Path,
                 timeout: float = 20.0, name: str = "friday-lsp") -> None:
        self.server_command = list(server_command)
        self.root = str(Path(root).resolve())
        self.timeout = timeout
        self.name = name
        self._proc: Optional[subprocess.Popen] = None
        self._responses: queue.Queue = queue.Queue()
        self._pushed: dict[str, list[dict]] = {}
        self._next_id = 1
        self._write_lock = threading.Lock()
        self._server_capabilities: dict = {}
        self._closed = False
        self._reader: Optional[threading.Thread] = None

    # ── lifecycle ─────────────────────────────────────────────────────

    def start(self) -> bool:
        """Spawn the server and complete the initialize handshake."""
        try:
            self._proc = subprocess.Popen(
                self.server_command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                cwd=self.root,
                start_new_session=True,
            )
        except (OSError, ValueError) as exc:
            logger.debug(f"lsp spawn failed: {exc}")
            self._closed = True
            return False
        if self._proc.stdin is None or self._proc.stdout is None:
            self._closed = True
            return False
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        try:
            result = self._request("initialize", {
                "processId": None,
                "clientInfo": {"name": "friday-v4", "version": "6"},
                "rootUri": _lsp_uri(self.root),
                "capabilities": {
                    "textDocument": {
                        "publishDiagnostics": {"relatedInformation": True},
                    },
                },
                "workspaceFolders": [{"uri": _lsp_uri(self.root),
                                      "name": "friday"}],
            })
            self._server_capabilities = (result or {}).get("capabilities") or {}
            self._notify("initialized", {})
            return True
        except LSPError as exc:
            logger.debug(f"lsp initialize failed: {exc}")
            self.shutdown()
            return False

    def shutdown(self) -> None:
        """Politely shut the server down (best-effort, never raises)."""
        if self._closed:
            return
        self._closed = True
        try:
            self._request("shutdown", None, timeout=3.0)
            self._notify("exit", None)
        except LSPError:
            pass
        finally:
            try:
                if self._proc is not None and self._proc.poll() is None:
                    self._proc.terminate()
            except Exception:  # pragma: no cover - defensive
                pass

    def is_running(self) -> bool:
        return (self._proc is not None and not self._closed
                and self._proc.poll() is None)

    def __enter__(self) -> "LSPClient":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.shutdown()

    # ── analysis ──────────────────────────────────────────────────────

    def open_file(self, path: str | Path) -> None:
        """didOpen — announce the file with its current text."""
        p = Path(path)
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        self._notify("textDocument/didOpen", {
            "textDocument": {
                "uri": uri_for(p),
                "languageId": _language_hint(p),
                "version": 1,
                "text": text,
            },
        })

    def diagnostics(self, path: str | Path) -> list[Diagnostic]:
        """Diagnostics for an open file — pull (3.17) or pushed fallback.

        Uses ``textDocument/diagnostic`` when the server advertised it;
        otherwise returns what the server pushed via
        ``textDocument/publishDiagnostics`` after ``open_file``.
        """
        p = Path(path)
        uri = uri_for(p)
        self.open_file(p)
        provider = self._server_capabilities.get(
            "textDocumentDiagnosticProvider") or {}
        if provider:
            try:
                result = self._request("textDocument/diagnostic", {
                    "textDocument": {"uri": uri},
                    "identifier": provider.get("identifier"),
                })
                items = (result or {}).get("items") or []
                return [self._to_diagnostic(i, "lsp") for i in items]
            except LSPError as exc:
                logger.debug(f"diagnostic pull failed ({exc}) — using pushes")
        pushed = self._pushed.get(uri) or []
        return [self._to_diagnostic(i, "lsp") for i in pushed]

    def symbols(self, path: str | Path) -> list[SymbolInfo]:
        """Document symbols — hierarchical and flat forms, top level."""
        p = Path(path)
        self.open_file(p)
        try:
            result = self._request("textDocument/documentSymbol", {
                "textDocument": {"uri": uri_for(p)},
            })
        except LSPError as exc:
            logger.debug(f"documentSymbol failed: {exc}")
            return []
        if not result:
            return []
        items = result if isinstance(result, list) else result.get("result")
        if not isinstance(items, list):
            return []
        return [self._to_symbol(i) for i in items]

    # ── transport ─────────────────────────────────────────────────────

    def _request(self, method: str, params,
                 timeout: Optional[float] = None) -> Optional[dict]:
        """A request/response round trip (raises LSPError on failure)."""
        if not self.is_running():
            raise LSPError("server not running")
        rid = self._next_id
        self._next_id += 1
        self._send({"jsonrpc": "2.0", "id": rid, "method": method,
                    "params": params})
        deadline = timeout if timeout is not None else self.timeout
        try:
            msg = self._responses.get(timeout=deadline)
        except queue.Empty as exc:
            raise LSPError(f"timeout waiting for {method}") from exc
        # The reader thread pushes None as the EOF sentinel when the
        # server closes — check before touching it (None.get would
        # AttributeError).
        if msg is None:
            raise LSPError("server closed")
        while msg.get("id") != rid:
            try:
                msg = self._responses.get(timeout=0.5)
            except queue.Empty as exc:
                raise LSPError(f"timeout waiting for {method}") from exc
            if msg is None:
                raise LSPError("server closed")
        if "error" in msg:
            raise LSPError(f"{method} error: {msg['error']}")
        return msg.get("result")

    def _notify(self, method: str, params) -> None:
        if not self.is_running():
            return
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _send(self, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        try:
            with self._write_lock:
                assert self._proc is not None
                assert self._proc.stdin is not None
                self._proc.stdin.write(header)
                self._proc.stdin.write(body)
                self._proc.stdin.flush()
        except (OSError, ValueError) as exc:
            raise LSPError(f"write failed: {exc}") from exc

    def _read_loop(self) -> None:
        """Read messages until EOF; dispatch responses + pushes."""
        try:
            assert self._proc is not None
            fp = self._proc.stdout
            while True:
                msg = _read_message(fp)
                if msg is None:
                    break
                if "id" in msg and isinstance(msg.get("id"), int):
                    self._responses.put(msg)
                elif msg.get("method") == "textDocument/publishDiagnostics":
                    params = msg.get("params") or {}
                    uri = params.get("uri", "")
                    self._pushed[uri] = params.get("diagnostics") or []
                else:
                    logger.debug(f"lsp notification: {msg.get('method')}")
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug(f"lsp reader ended: {exc}")
        finally:
            self._responses.put(None)  # unblock any waiting request

    # ── mapping ───────────────────────────────────────────────────────

    @staticmethod
    def _to_diagnostic(item: dict, source: str = "lsp") -> Diagnostic:
        rng = item.get("range") or {}
        start = rng.get("start") or {}
        end = rng.get("end") or start
        line = int((start.get("line") or 0)) + 1
        end_line = int((end.get("line") or 0)) + 1 if end else None
        return Diagnostic(
            message=str(item.get("message") or "issue"),
            severity=int(item.get("severity") or 1),
            line=line,
            character=int(start.get("character") or 0),
            end_line=end_line,
            end_character=int(end.get("character") or 0) if end else None,
            source=str(item.get("source") or source),
            code=item.get("code"),
        )

    @staticmethod
    def _to_symbol(item: dict) -> SymbolInfo:
        if "location" in item:  # flat SymbolInformation
            rng = item.get("location", {}).get("range") or {}
        else:                   # hierarchical DocumentSymbol
            rng = item.get("range") or {}
        start = rng.get("start") or {}
        end = rng.get("end") or {}
        return SymbolInfo(
            name=str(item.get("name") or "?"),
            kind=int(item.get("kind") or 0),
            kind_name=_kind_name(int(item.get("kind") or 0)),
            line=int((start.get("line") or 0)) + 1,
            end_line=int((end.get("line") or 0)) + 1 if end else None,
            container=str(item.get("containerName") or ""),
        )


def _read_message(fp) -> Optional[dict]:
    """One Content-Length framed JSON message from a binary stream."""
    try:
        headers: dict[str, str] = {}
        while True:
            line = fp.readline()
            if not line:
                return None
            line = line.decode("utf-8", errors="replace")
            if line in ("\r\n", "\n", ""):
                break
            key, _, value = line.partition(":")
            headers[key.strip().lower()] = value.strip()
        length = int(headers.get("content-length", "0"))
        if length <= 0:
            return None
        body = fp.read(length)
        return json.loads(body.decode("utf-8", errors="replace"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        logger.debug(f"lsp read failed: {exc}")
        return None


def _language_hint(path: Path) -> str:
    """Best-effort languageId from the file extension."""
    ext = path.suffix.lower()
    return {
        ".py": "python", ".pyi": "python", ".ts": "typescript",
        ".tsx": "typescriptreact", ".js": "javascript",
        ".jsx": "javascriptreact", ".go": "go", ".rs": "rust",
        ".java": "java", ".kt": "kotlin", ".c": "c", ".h": "c",
        ".cpp": "cpp", ".hpp": "cpp", ".cs": "csharp", ".rb": "ruby",
        ".php": "php", ".json": "json", ".yaml": "yaml",
        ".yml": "yaml", ".toml": "toml", ".md": "markdown",
        ".html": "html", ".css": "css", ".sh": "shellscript",
    }.get(ext, "plaintext")


__all__ = ["LSPClient", "Diagnostic", "SymbolInfo", "LSPError",
           "SEVERITY_NAMES", "uri_for"]
