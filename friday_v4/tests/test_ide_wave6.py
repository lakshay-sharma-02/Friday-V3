"""Wave 6 — IDE Integration: hermetic tests.

Covers the whole layer without a real editor or LSP server: a fake
LSP server script exercises the pure-stdlib JSON-RPC client, the AST
analyzer runs on tmp files, detection is monkeypatched (env / process /
config), and the NL / reasoning / CLI / capability wiring is asserted
against the real modules.

No real ``~/.friday`` writes: every DB use is a tmp_path connection.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from friday_v4 import db
from friday_v4.nlu import Intent, resolve
from friday_v4.reasoning.question import QuestionType, classify as qclassify

# ── fixtures ──────────────────────────────────────────────────────────


FAKE_LSP_SERVER = r'''import json, sys

def send(payload):
    body = json.dumps(payload).encode()
    sys.stdout.buffer.write(
        f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
    sys.stdout.buffer.flush()

def read_msg():
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        line = line.decode()
        if line in ("\r\n", "\n", ""):
            break
        k, _, v = line.partition(":")
        headers[k.lower()] = v.strip()
    if "content-length" not in headers:
        return None
    return json.loads(sys.stdin.buffer.read(int(headers["content-length"])))

while True:
    msg = read_msg()
    if msg is None:
        break
    m = msg.get("method")
    if m == "initialize":
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {
            "capabilities": {
                "textDocumentSync": {"openClose": True},
                "documentSymbolProvider": True,
                "textDocumentDiagnosticProvider": {
                    "identifier": "fake", "intervals": True}}}})
    elif m == "initialized":
        pass
    elif m == "textDocument/diagnostic":
        uri = msg["params"]["textDocument"]["uri"]
        path = uri.replace("file://", "")
        try:
            content = open(path).read()
        except Exception:
            content = ""
        items = []
        if "SENTINEL_ISSUE" in content:
            items.append({"range": {"start": {"line": 3, "character": 0},
                                    "end": {"line": 3, "character": 8}},
                          "severity": 1, "source": "fake-lsp",
                          "message": "SENTINEL_ISSUE is undefined",
                          "code": "E001"})
        send({"jsonrpc": "2.0", "id": msg["id"],
              "result": {"kind": "full", "items": items}})
    elif m == "textDocument/documentSymbol":
        send({"jsonrpc": "2.0", "id": msg["id"], "result": [
            {"name": "main", "kind": 12,
             "range": {"start": {"line": 0, "character": 0},
                       "end": {"line": 9, "character": 0}},
             "selectionRange": {"start": {"line": 0, "character": 0},
                                "end": {"line": 0, "character": 4}},
             "children": []}]})
    elif m == "shutdown":
        send({"jsonrpc": "2.0", "id": msg["id"], "result": None})
    elif m == "exit":
        break
sys.exit(0)
'''


@pytest.fixture
def fake_lsp(tmp_path):
    """(fake_server_script, workspace) — run the server with sys.executable."""
    script = tmp_path / "fake_server.py"
    script.write_text(FAKE_LSP_SERVER)
    return script, tmp_path


@pytest.fixture
def force_ast(monkeypatch):
    """Never resolve a real language server (keeps AST tests hermetic)."""
    monkeypatch.setattr(
        "friday_v4.desktop.ide.lsp_command_for_workspace", lambda *a, **k: None)
    return monkeypatch


# ── LSP client (pure-stdlib JSON-RPC) ─────────────────────────────────


class TestLSPClient:
    def test_initialize_and_pull_diagnostics(self, fake_lsp):
        from friday_v4.desktop.ide.lsp_client import LSPClient
        script, work = fake_lsp
        target = work / "target.py"
        target.write_text("x = 1\n\n\nSENTINEL_ISSUE\n")
        client = LSPClient([sys.executable, str(script)], root=str(work),
                           timeout=10.0)
        assert client.start() is True
        try:
            diags = client.diagnostics(target)
            assert len(diags) == 1
            d = diags[0]
            assert d.severity_name == "error"
            assert d.line == 4            # 0-based 3 → 1-based 4
            assert "SENTINEL_ISSUE" in d.message
            assert d.source == "fake-lsp"
        finally:
            client.shutdown()
        assert client.is_running() is False

    def test_document_symbols(self, fake_lsp):
        from friday_v4.desktop.ide.lsp_client import LSPClient
        script, work = fake_lsp
        target = work / "target.py"
        target.write_text("x = 1\n")
        client = LSPClient([sys.executable, str(script)], root=str(work),
                           timeout=10.0)
        assert client.start() is True
        try:
            syms = client.symbols(target)
            assert any(s.name == "main" and s.kind_name == "function"
                       and s.line == 1 for s in syms)
        finally:
            client.shutdown()

    def test_missing_server_binary_degrades(self, tmp_path):
        from friday_v4.desktop.ide.lsp_client import LSPClient
        client = LSPClient(["/nonexistent/lsp-server"], root=str(tmp_path))
        assert client.start() is False
        assert client.is_running() is False

    def test_diagnostic_brief_is_one_line(self):
        from friday_v4.desktop.ide.lsp_client import Diagnostic
        d = Diagnostic(message="line one\nline two", severity=1, line=7)
        assert d.brief() == "line 7: line one line two"

    def test_server_closing_mid_session_raises_lsp_error(self, tmp_path):
        """Review fix: the reader's None (EOF) sentinel must raise a clean
        LSPError, never AttributeError on None.get('id'). Exercises the
        request path directly — ``diagnostics()`` swallows LSPError by
        design (pull → pushed fallback), so the sentinel guard is tested
        at ``_request``."""
        from friday_v4.desktop.ide.lsp_client import LSPClient, LSPError, uri_for
        server = tmp_path / "die_early.py"
        server.write_text(
            "import json, sys\n"
            "def send(p):\n"
            "    b = json.dumps(p).encode()\n"
            "    sys.stdout.buffer.write("
            "f'Content-Length: {len(b)}\\r\\n\\r\\n'.encode() + b); "
            "sys.stdout.buffer.flush()\n"
            "def read():" + "\n"
            "    h = {}\n"
            "    while True:\n"
            "        l = sys.stdin.buffer.readline()\n"
            "        if not l: return None\n"
            "        l = l.decode()\n"
            "        if l in ('\\r\\n', '\\n', ''): break\n"
            "        k, _, v = l.partition(':'); h[k.lower()] = v.strip()\n"
            "    if 'content-length' not in h: return None\n"
            "    return json.loads(sys.stdin.buffer.read("
            "int(h['content-length'])))\n"
            "while True:\n"
            "    m = read()\n"
            "    if m is None: break\n"
            "    if m.get('method') == 'initialize':\n"
            "        send({'jsonrpc': '2.0', 'id': m['id'], 'result': "
            "{'capabilities': {'textDocumentDiagnosticProvider': "
            "{'identifier': 'fake', 'intervals': True}}}})\n"
            "    else:\n"
            "        sys.exit(0)  # die before answering anything else\n"
        )
        target = tmp_path / "t.py"
        target.write_text("x = 1\n")
        client = LSPClient([sys.executable, str(server)], root=str(tmp_path),
                           timeout=5.0)
        assert client.start() is True
        try:
            with pytest.raises(LSPError):
                client._request("textDocument/diagnostic",
                                {"textDocument": {"uri": uri_for(target)}})
        finally:
            client.shutdown()


# ── AST analyzer (always-works fallback) ──────────────────────────────


class TestASTAnalyzer:
    def test_syntax_error(self):
        from friday_v4.desktop.ide.ast_analyzer import analyze_source
        diags = analyze_source("def broken(:\n    pass\n", "bad.py")
        assert len(diags) == 1
        assert diags[0].severity == 1
        assert "SyntaxError" in diags[0].message

    def test_undefined_name_and_unused_import(self):
        from friday_v4.desktop.ide.ast_analyzer import analyze_source
        source = ("import os\n"
                  "def login(user):\n"
                  "    return get_token(user)\n")
        diags = analyze_source(source, "auth.py")
        codes = {d.code for d in diags}
        assert "F821" in codes          # undefined name
        assert "F401" in codes          # unused import

    def test_shadowed_builtin(self):
        from friday_v4.desktop.ide.ast_analyzer import analyze_source
        diags = analyze_source("list = [1, 2]\n", "s.py")
        assert any(d.code == "A002" and "shadowed builtin" in d.message
                   for d in diags)

    def test_except_handler_name_not_flagged_undefined(self):
        """Review fix: ``except ValueError as err`` binds ``err`` — the
        most common Python idiom must never be a false F821."""
        from friday_v4.desktop.ide.ast_analyzer import analyze_source
        source = ("def load(path):\n"
                  "    try:\n"
                  "        return open(path).read()\n"
                  "    except OSError as err:\n"
                  "        return err\n")
        diags = analyze_source(source, "io.py")
        assert not any(d.code == "F821" and "err" in d.message
                       for d in diags)

    def test_starred_import_not_flagged_unused(self):
        """Review fix: ``from x import *`` is not a single unused name."""
        from friday_v4.desktop.ide.ast_analyzer import analyze_source
        diags = analyze_source("from math import *\n"
                               "value = sqrt(4)\n", "m.py")
        assert not any(d.code == "F401" for d in diags)

    def test_clean_file(self):
        from friday_v4.desktop.ide.ast_analyzer import analyze_source
        diags = analyze_source("def ok():\n    return 1\n", "ok.py")
        assert diags == []

    def test_analyze_file_missing_returns_empty(self, tmp_path):
        from friday_v4.desktop.ide.ast_analyzer import analyze_file
        assert analyze_file(tmp_path / "nope.py") == []


# ── Facade: analyze_file (LSP → AST degrade) ──────────────────────────


class TestAnalyzeFile:
    def test_ast_fallback_when_no_server(self, tmp_path, force_ast):
        from friday_v4.desktop.ide import analyze_file
        bad = tmp_path / "auth.py"
        bad.write_text("def login(u):\n    return get_token(u)\n")
        res = analyze_file(bad)
        assert res.method == "ast"
        assert any(d.code == "F821" for d in res.diagnostics)

    def test_missing_file_method_none(self, tmp_path, force_ast):
        from friday_v4.desktop.ide import analyze_file
        res = analyze_file(tmp_path / "missing.py")
        assert res.method == "none"

    def test_lsp_path_when_server_resolvable(self, tmp_path, fake_lsp,
                                             monkeypatch):
        from friday_v4.desktop.ide import analyze_file
        script, work = fake_lsp
        target = work / "target.py"
        target.write_text("x = 1\n\n\nSENTINEL_ISSUE\n")
        monkeypatch.setattr(
            "friday_v4.desktop.ide.lsp_command_for_workspace",
            lambda *a, **k: [sys.executable, str(script)])
        res = analyze_file(target)
        assert res.method == "lsp"
        assert any("SENTINEL_ISSUE" in d.message for d in res.diagnostics)

    def test_symbols_ast_outline(self, tmp_path, force_ast):
        from friday_v4.desktop.ide import symbols
        f = tmp_path / "mod.py"
        f.write_text("class Thing:\n    pass\n\ndef run():\n    pass\n")
        out = symbols(f)
        names = {(s.name, s.kind_name) for s in out}
        assert ("Thing", "class") in names
        assert ("run", "function") in names

    def test_lsp_command_lookup(self, tmp_path, monkeypatch):
        from friday_v4.desktop.ide import lsp_command_for_workspace
        (tmp_path / "pyproject.toml").write_text("")
        monkeypatch.setattr(
            "friday_v4.desktop.ide._find",
            lambda exe: f"/venv/bin/{exe}" if exe.startswith("pyright") else None)
        cmd = lsp_command_for_workspace(str(tmp_path))
        assert cmd and cmd[0].endswith("pyright-langserver")
        assert "--stdio" in cmd

    def test_no_marker_no_server(self, tmp_path, monkeypatch):
        from friday_v4.desktop.ide import lsp_command_for_workspace
        monkeypatch.setattr("friday_v4.desktop.ide._find", lambda exe: None)
        assert lsp_command_for_workspace(str(tmp_path)) is None


# ── Detection ─────────────────────────────────────────────────────────


class TestDetection:
    def test_env_vscode(self, monkeypatch):
        from friday_v4.desktop.ide import detection
        monkeypatch.setattr(detection, "_config_signals", lambda: [])
        monkeypatch.setattr(detection, "_running_processes", lambda: [])
        monkeypatch.setenv("TERM_PROGRAM", "vscode")
        monkeypatch.setenv("VSCODE_IPC_HOOK_CLI", "/tmp/socket")
        found = detection.detect_all()
        assert found and found[0].kind == "vscode"
        assert found[0].source == "env"
        assert found[0].confidence == 1.0

    def test_config_neovim(self, monkeypatch, tmp_path):
        from friday_v4.desktop.ide import detection
        monkeypatch.setattr(detection, "_config_signals",
                            lambda: [("neovim", 0.6)])
        monkeypatch.setattr(detection, "_running_processes", lambda: [])
        for key in ("TERM_PROGRAM", "NVIM", "GIO_LAUNCHED_DESKTOP_FILE"):
            monkeypatch.delenv(key, raising=False)
        found = detection.detect_all()
        assert found and found[0].kind == "neovim"
        assert found[0].source == "config"

    def test_process_jetbrains(self, monkeypatch):
        from friday_v4.desktop.ide import detection
        monkeypatch.setattr(detection, "_config_signals", lambda: [])
        monkeypatch.setattr(detection, "_running_processes",
                            lambda: [("jetbrains", 0.8)])
        for key in ("TERM_PROGRAM", "NVIM", "GIO_LAUNCHED_DESKTOP_FILE"):
            monkeypatch.delenv(key, raising=False)
        found = detection.detect_all()
        assert found and found[0].kind == "jetbrains"

    def test_no_ide_detected(self, monkeypatch):
        from friday_v4.desktop.ide import detection
        monkeypatch.setattr(detection, "_config_signals", lambda: [])
        monkeypatch.setattr(detection, "_running_processes", lambda: [])
        for key in ("TERM_PROGRAM", "NVIM", "GIO_LAUNCHED_DESKTOP_FILE"):
            monkeypatch.delenv(key, raising=False)
        assert detection.detect() is None
        assert detection.is_available() is False

    def test_preflight_opt_in(self, monkeypatch):
        from friday_v4.desktop.ide import preflight_opted_in
        monkeypatch.delenv("FRIDAY_V4_IDE_PREFLIGHT", raising=False)
        assert preflight_opted_in() is False
        monkeypatch.setenv("FRIDAY_V4_IDE_PREFLIGHT", "1")
        assert preflight_opted_in() is True
        monkeypatch.setenv("FRIDAY_V4_IDE_PREFLIGHT", "off")
        assert preflight_opted_in() is False


# ── NL: the IDE intent ────────────────────────────────────────────────


class TestNLIntent:
    def test_ide_phrases_classify_ide(self):
        for text in ("what's wrong with src/main.py", "diagnose auth.py",
                     "lint src/main.py", "why won't this compile",
                     "analyze src/main.py"):
            a = resolve(text)
            assert a.intent == Intent.IDE, text
            if "src/main.py" in text or "auth.py" in text:
                assert a.target and a.target.endswith((".py"))

    def test_existing_intents_not_hijacked(self):
        cases = {
            "run the tests": Intent.EXECUTE,
            "diagnose the memory leak": Intent.EXECUTE,   # agentic → claude
            "analyze vivaha": Intent.RESEARCH,
            "what's the deal between X and Y": Intent.RESEARCH,
            "check my deps": Intent.SECURITY,
            "what's the deal with my security scan": Intent.SECURITY,
            "git status": Intent.EXECUTE,
            "read notes.txt": Intent.EXECUTE,
        }
        for text, expected in cases.items():
            a = resolve(text)
            assert a.intent == expected, f"{text!r} → {a.intent.value}"

    def test_handler_answers_with_diagnostics(self, tmp_path, force_ast):
        from friday_v4.nl_router import TextCommandHandler
        bad = tmp_path / "auth.py"
        bad.write_text("def login(u):\n    return get_token(u)\n")
        conn = db.connect(tmp_path / "v4.db")
        r = TextCommandHandler(conn, cwd=str(tmp_path)).handle(
            "what's wrong with auth.py")
        assert r.intent == "ide"
        assert r.action == "ide"
        assert "undefined name" in r.response
        assert r.status == "succeeded"

    def test_handler_clean_file(self, tmp_path, force_ast):
        from friday_v4.nl_router import TextCommandHandler
        good = tmp_path / "ok.py"
        good.write_text("def ok():\n    return 1\n")
        conn = db.connect(tmp_path / "v4.db")
        r = TextCommandHandler(conn, cwd=str(tmp_path)).handle(
            "what's wrong with ok.py")
        assert "no issues found" in r.response

    def test_handler_asks_which_file(self, tmp_path):
        from friday_v4.nl_router import TextCommandHandler
        r = TextCommandHandler(None).handle("why won't this compile")
        assert r.action == "clarification"
        assert "Which file" in r.response


# ── Reasoning: the CODE backstop ──────────────────────────────────────


class TestReasoningCode:
    def test_question_type_backstop(self):
        assert qclassify("what's wrong with src/main.py") == QuestionType.CODE
        assert qclassify("is my code clean") == QuestionType.CODE

    def test_code_provider_answers(self, tmp_path, force_ast):
        from friday_v4.reasoning import answer
        bad = tmp_path / "auth.py"
        bad.write_text("def login(u):\n    return get_token(u)\n")
        conn = db.connect(tmp_path / "v4.db")
        ans = answer(f"what's wrong with {bad}", conn=conn)
        assert ans.known and "undefined name" in ans.text
        assert any(e.source == "v4.ide.ast" for e in ans.evidence)

    def test_code_provider_clean(self, tmp_path, force_ast):
        from friday_v4.reasoning import answer
        good = tmp_path / "ok.py"
        good.write_text("def ok():\n    return 1\n")
        conn = db.connect(tmp_path / "v4.db")
        ans = answer(f"what's wrong with {good}", conn=conn)
        assert ans.known and "no issues found" in ans.text

    def test_code_provider_unknown_target_silent(self, tmp_path):
        from friday_v4.reasoning import answer
        conn = db.connect(tmp_path / "v4.db")
        ans = answer("what's wrong with the auth refactor", conn=conn)
        assert not ans.known  # not a file → honest "I don't know yet"


# ── Wiring: capability registry + status probe ────────────────────────


class TestWiring:
    def test_capability_registry_has_ide(self, tmp_path):
        from friday_v4.capability import CapabilityRegistry
        conn = db.connect(tmp_path / "v4.db")
        reg = CapabilityRegistry(conn)
        ids = {c.id for c in reg.list()}
        assert "surface:ide" in ids
        assert "provider:code" in ids
        assert "intent:ide" in ids

    def test_status_probe_ide(self, monkeypatch):
        from friday_v4 import cli_status
        monkeypatch.setattr(cli_status, "_probe_ide",
                            lambda: (True, "detected: VS Code"))
        ok, detail = cli_status._probe_ide()
        assert ok is True
        assert "VS Code" in detail
        assert ("ide", "_probe_ide") in cli_status.STATUS_PROBES


# ── Composition: preflight rides with execution & Claude ──────────────


class _RecordingSandbox:
    """Sandbox stand-in that records argv and returns a fake claude JSON."""

    def __init__(self):
        self.calls: list[list[str]] = []

    def run(self, args, cwd=None, timeout=None):
        self.calls.append(args)
        from friday_v4.execution.sandbox import SandboxResult
        return SandboxResult(
            result_code=0,
            stdout=json.dumps({"result": "done", "is_error": False,
                               "terminal_reason": ""}))


class TestClaudePreflight:
    def test_ide_context_appended_when_opted_in(self, tmp_path, monkeypatch):
        from friday_v4.execution.executors import ClaudeCodeExecutor
        bad = tmp_path / "broken.py"
        bad.write_text("def f():\n    return missing_var\n")
        monkeypatch.setenv("FRIDAY_V4_IDE_PREFLIGHT", "1")
        monkeypatch.setattr("friday_v4.execution.executors.find_tool",
                            lambda name: "/fake/claude")
        sandbox = _RecordingSandbox()
        executor = ClaudeCodeExecutor(sandbox=sandbox)
        result = executor.run("fix the bug in broken.py", cwd=str(tmp_path))
        assert result.result_code == 0
        args = sandbox.calls[0]
        assert "--append-system-prompt" in args
        ctx = args[args.index("--append-system-prompt") + 1]
        assert "preflight" in ctx and "broken.py" in ctx

    def test_no_context_when_opt_out(self, tmp_path, monkeypatch):
        from friday_v4.execution.executors import ClaudeCodeExecutor
        (tmp_path / "broken.py").write_text("def f():\n    return 1\n")
        monkeypatch.delenv("FRIDAY_V4_IDE_PREFLIGHT", raising=False)
        monkeypatch.setattr("friday_v4.execution.executors.find_tool",
                            lambda name: "/fake/claude")
        sandbox = _RecordingSandbox()
        executor = ClaudeCodeExecutor(sandbox=sandbox)
        executor.run("fix the bug in broken.py", cwd=str(tmp_path))
        assert "--append-system-prompt" not in sandbox.calls[0]

    def test_preflight_note_via_router(self, tmp_path, monkeypatch):
        from friday_v4.nl_router import TextCommandHandler
        bad = tmp_path / "broken.py"
        bad.write_text("def f():\n    return missing_var\n")
        monkeypatch.setenv("FRIDAY_V4_IDE_PREFLIGHT", "1")
        handler = TextCommandHandler(None, cwd=str(tmp_path))
        note = handler._ide_preflight_note("shell", "run broken.py", str(tmp_path))
        assert "issue" in note or "error" in note
        assert "broken.py" in note
        monkeypatch.delenv("FRIDAY_V4_IDE_PREFLIGHT")
        assert handler._ide_preflight_note("shell", "run broken.py",
                                           str(tmp_path)) == ""


# ── CLI: friday4 ide ──────────────────────────────────────────────────


class TestCLI:
    def test_main_ide_detect_runs(self, capsys):
        from friday_v4.cli_talk import main
        rc = main(["ide", "detect"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "IDE Detection" in out

    def test_main_ide_diagnose_ast(self, tmp_path, force_ast, capsys):
        from friday_v4.cli_talk import main
        bad = tmp_path / "auth.py"
        bad.write_text("def login(u):\n    return get_token(u)\n")
        rc = main(["ide", "diagnose", str(bad), "--cwd", str(tmp_path)])
        out = capsys.readouterr().out
        assert rc == 0
        assert "undefined name" in out
        assert "via ast" in out

    def test_cli_ide_registered(self, capsys):
        from friday_v4.cli_talk import main
        with pytest.raises(SystemExit):
            main(["--help"])
        out = capsys.readouterr().out
        assert "ide" in out

    def test_cli_ide_importable(self):
        from friday_v4.cli_ide import (build_ide_parser, cmd_ide,
                                       cmd_diagnose, cmd_detect)
        assert build_ide_parser is not None
        assert cmd_ide is not None
        assert cmd_diagnose is not None
        assert cmd_detect is not None

    def test_package_importable(self):
        import friday_v4.desktop.ide as ide
        assert ide.is_available() is True
        for name in ("analyze_file", "detect", "DetectedIDE", "Diagnostic",
                     "SymbolInfo", "open_in_ide", "reveal_in_ide"):
            assert getattr(ide, name) is not None
