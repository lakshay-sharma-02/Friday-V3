"""Wave 21 — IDE Control: Friday *controls* the editor through NL.

The user directive: "control the ide, analyze which ide is it and adapt
accordingly… along with execution, claude code arms." Wave 6 shipped IDE
*analysis* ("what's wrong with main.py" → LSP/AST diagnostics); this
wave wires the controller primitives (``open_file`` / ``reveal`` /
``run_command``) through the ONE NLU point on every surface:

- "open src/main.py in the editor" → opens the file in the detected IDE
- "jump to line 42 of cli_talk.py" / "reveal auth.py:7" → reveals the line
- "open main.py and fix it" → NOT a silent open — the Claude arms take it
- "open brave" / "open youtube.com" → still desktop (no hijack)

All hermetic: tmp DB, fake detection, fake controller, tmp files.
"""

from __future__ import annotations

import pytest

from friday_v6 import db


def _conn(tmp_path):
    return db.connect(tmp_path / "v4.db")


class _FakeIDE:
    kind = "vscode"
    launcher = "code"
    name = "VS Code"

    def __repr__(self):
        return "VS Code (vscode)"


# ==========================================================================
# classifier — control phrases route to IDE, tasks and desktop stay put
# ==========================================================================


class TestIdeClassifier:
    @pytest.mark.parametrize("text", [
        "open main.py in the editor",
        "open src/main.py",
        "open main.py in vscode",
        "jump to line 42 of cli_talk.py",
        "go to line 10 in auth.py",
        "reveal auth.py",
        "what's wrong with main.py",
        "show me the errors in main.py",
    ])
    def test_ide_control_and_diagnostics(self, text):
        from friday_v6.nlu.intent import _fallback_classify
        assert _fallback_classify(text).intent.value == "ide", text

    @pytest.mark.parametrize("text", [
        "open brave",                    # desktop app, not a file
        "open youtube.com",              # web destination, not a source file
        "open main.py and fix it",       # work → the Claude arms, not a silent open
        "open the editor",               # no file target
    ])
    def test_not_ide(self, text):
        from friday_v6.nlu.intent import _fallback_classify
        assert _fallback_classify(text).intent.value != "ide", text

    def test_agentic_ide_phrase_is_execute(self):
        from friday_v6.nlu.intent import _fallback_classify
        r = _fallback_classify("open main.py and fix it")
        assert r.intent.value == "execute"


# ==========================================================================
# target / line / verb extraction
# ==========================================================================


class TestIdeParsing:
    def test_open_target(self):
        from friday_v6.nlu.intent import _ide_target
        assert _ide_target("open src/main.py in the editor") == "src/main.py"
        assert _ide_target("open main.py") == "main.py"

    def test_line_phrase_target(self):
        from friday_v6.nlu.intent import _ide_target, _ide_line
        assert _ide_target("jump to line 42 of cli_talk.py") == "cli_talk.py"
        assert _ide_line("jump to line 42 of cli_talk.py") == 42

    def test_reveal_target_and_line(self):
        from friday_v6.nlu.intent import _ide_target, _ide_line
        assert _ide_target("reveal auth.py:7") == "auth.py"
        assert _ide_line("reveal auth.py:7") == 7

    def test_diagnostics_are_not_control(self):
        from friday_v6.nlu.intent import _ide_control_verb
        assert _ide_control_verb("what's wrong with main.py") is None
        assert _ide_control_verb("show me the errors in main.py") is None


# ==========================================================================
# router — control executes against the detected IDE, adapted to it
# ==========================================================================


class _FakeController:
    """Records calls instead of shelling out (hermetic)."""

    def __init__(self):
        self.opens = []
        self.reveals = []
        self.ok = True

    def open_file(self, ide, path):
        self.opens.append((ide, str(path)))
        if not self.ok:
            return False, f"no such file: {path}"
        return True, f"opened {__import__('pathlib').Path(path).name} in {ide.name}"

    def reveal(self, ide, path, line):
        self.reveals.append((ide, str(path), line))
        if not self.ok:
            return False, f"no such file: {path}"
        return True, f"revealed {__import__('pathlib').Path(path).name}:{line} in {ide.name}"


@pytest.fixture
def ide_env(tmp_path, monkeypatch):
    """Hermetic IDE environment: tmp files + fake detection + fake controller."""
    root = tmp_path
    main = root / "src" / "main.py"
    main.parent.mkdir(parents=True)
    main.write_text("x = 1\n")
    auth = root / "auth.py"
    auth.write_text("y = 2\n")

    import friday_v6.desktop.ide.controller as ctl
    fake_ctl = _FakeController()
    monkeypatch.setattr(ctl, "open_file", fake_ctl.open_file)
    monkeypatch.setattr(ctl, "reveal", fake_ctl.reveal)
    monkeypatch.setattr(
        "friday_v6.desktop.ide.detection.detect", lambda: _FakeIDE())

    from friday_v6.nl_router import TextCommandHandler
    handler = TextCommandHandler(conn=_conn(tmp_path), cwd=str(root))
    return handler, fake_ctl, root


class TestIdeControlRouter:
    def test_open_file_in_editor(self, ide_env):
        handler, ctl, root = ide_env
        result = handler.handle("open src/main.py in the editor")
        assert result.action == "ide"
        assert result.status == "succeeded"
        assert "opened main.py" in result.response
        ide, path = ctl.opens[0]
        assert ide.kind == "vscode"          # adapted to the detected editor
        assert path.endswith("src/main.py")

    def test_jump_to_line_reveals(self, ide_env):
        handler, ctl, _ = ide_env
        result = handler.handle("jump to line 42 of src/main.py")
        assert result.action == "ide"
        assert result.status == "succeeded"
        assert "revealed main.py:42" in result.response
        _, path, line = ctl.reveals[0]
        assert line == 42
        assert path.endswith("main.py")

    def test_reveal_no_line_opens_file(self, ide_env):
        handler, ctl, _ = ide_env
        result = handler.handle("reveal auth.py")
        assert result.status == "succeeded"
        assert ctl.reveals == []             # no line → open, not reveal
        assert ctl.opens and ctl.opens[-1][1].endswith("auth.py")

    def test_missing_file_is_honest(self, ide_env):
        handler, ctl, _ = ide_env
        ctl.ok = False
        result = handler.handle("open nope.py in the editor")
        assert result.status == "failed"
        assert "no such file" in result.response

    def test_no_file_asks_which_file(self, tmp_path):
        from friday_v6.nl_router import TextCommandHandler
        h = TextCommandHandler(conn=_conn(tmp_path))
        result = h.handle("jump to line 42")   # IDE ask, no file named
        assert result.action == "clarification"
        assert "Which file" in result.response

    def test_open_the_editor_is_desktop_not_ide(self, tmp_path):
        """No file target → 'open the editor' focuses the app (desktop)."""
        from friday_v6.nl_router import TextCommandHandler
        calls = []
        h = TextCommandHandler(conn=_conn(tmp_path), cwd=str(tmp_path),
                               desktop_handler=lambda t: calls.append(t)
                               or "Focused Code Editor.")
        result = h.handle("open the editor")
        assert result.action == "desktop"
        assert calls == ["open the editor"]

    def test_diagnostics_untouched(self, ide_env):
        """'what's wrong with X' still analyzes — control didn't hijack it."""
        handler, ctl, root = ide_env
        result = handler.handle("what's wrong with src/main.py")
        assert result.action in ("chat", "ide")
        assert "issues found" in result.response or "issue" in result.response
        assert ctl.opens == [] and ctl.reveals == []  # never opened anything

    def test_llm_misclassifies_but_ide_control_wins(self, tmp_path,
                                                   monkeypatch):
        """Even an LLM that calls 'open main.py in the editor' DESKTOP
        still opens the editor — a source-file target + control verb is
        unambiguous (the same LLM-robustness the app-learning loop has)."""
        import friday_v6.desktop.ide.controller as ctl
        fake_ctl = _FakeController()
        monkeypatch.setattr(ctl, "open_file", fake_ctl.open_file)
        monkeypatch.setattr(
            "friday_v6.desktop.ide.detection.detect", lambda: _FakeIDE())

        root = tmp_path
        main = root / "main.py"
        main.write_text("x = 1\n")

        class _DesktopLLM:
            """A model that insists the phrase is a desktop launch."""
            def parse_utterance(self, text):
                return {"intent": "desktop", "action_type": None,
                        "command": "", "target": "main.py", "goal": None,
                        "entities": [], "needs_clarification": False,
                        "clarification": "", "confidence": 0.95}

        from friday_v6.nl_router import TextCommandHandler
        handler = TextCommandHandler(conn=_conn(tmp_path), cwd=str(root),
                                     llm=_DesktopLLM())
        result = handler.handle("open main.py in the editor")
        assert result.action == "ide"
        assert result.status == "succeeded"
        assert fake_ctl.opens and fake_ctl.opens[0][1].endswith("main.py")
