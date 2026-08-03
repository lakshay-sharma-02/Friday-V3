"""§2 hardening — NL entry points for security, collab, memory, desktop.

The MASTER_PLAN's §2 hardening rule: a shipped wave isn't done until it
speaks MCU — no capability is CLI-only. This file verifies the four
layers that were CLI-only now have natural-language entry points through
the ONE NLU point (``nlu.resolve``):

- **security** — "scan my repo" / "is my code secure" → ``Intent.SECURITY``
  → ``VulnerabilityScanner`` → graded report + high-severity findings
  pushed to the ambient bus (Wave 11 push).
- **collab** — "what's my team working on" → ASK → ``collab_provider``
  cites shared peer observations (read-only, perms respected).
- **memory** — "remember that I prefer Rust" stores a proposition;
  "forget that" removes it. Explicit operator consent only.
- **desktop** — ``desktop_text_command`` routes focus/workspace/open/
  launch/screenshot/status for the CLI and web chat (was voice-only).

Safety laws verified:
- Every path never crashes; missing subsystems degrade to honest text.
- Memory writes only on explicit consent ("remember that…").
- Security findings publish to the ambient bus (push, not CLI-only).
- All hermetic: tmp_path DBs + fake scanners/WMs — never the real machine.
"""

from __future__ import annotations

from friday_v4 import db


def _conn(tmp_path):
    return db.connect(tmp_path / "v4.db")


# ==========================================================================
# nlu — the ONE point classifies the new intents (LLM-first, rules fallback)
# ==========================================================================


class TestHardeningNlu:
    def test_scan_my_repo_classifies_security(self):
        from friday_v4.nlu import Intent, resolve
        assert resolve("scan my repo").intent == Intent.SECURITY
        assert resolve("is my code secure").intent == Intent.SECURITY
        assert resolve("check my dependencies").intent == Intent.SECURITY

    def test_security_target_extracts_path(self):
        from friday_v4.nlu import Intent, resolve
        action = resolve("scan the auth refactor")
        assert action.intent == Intent.SECURITY
        assert action.target == "auth refactor"

    def test_remember_classifies_memory(self):
        from friday_v4.nlu import Intent, resolve
        assert resolve("remember that I prefer Rust").intent == Intent.MEMORY
        assert resolve("forget that").intent == Intent.MEMORY

    def test_memory_goal_threads_fact_text(self):
        from friday_v4.nlu import Intent, resolve
        action = resolve("remember that I prefer Rust")
        assert action.intent == Intent.MEMORY
        assert action.goal  # the fact statement is threaded

    def test_team_question_stays_ask(self):
        from friday_v4.nlu import Intent, resolve
        assert resolve("what's my team working on").intent == Intent.ASK

    def test_llm_still_wins_over_keywords(self):
        from friday_v4.nlu import Intent, resolve

        class FakeLLM:
            def parse_utterance(self, text):
                return {"intent": "ask", "action_type": None, "command": "",
                        "target": "team", "goal": None, "entities": [],
                        "needs_clarification": False, "clarification": "",
                        "confidence": 0.96}

        action = resolve("scan my repo", llm=FakeLLM())
        assert action.intent == Intent.ASK  # the model's call, not keywords


# ==========================================================================
# security — "scan my repo" runs the scanner and pushes findings
# ==========================================================================


class _FakeReport:
    def __init__(self, findings):
        self.findings = findings

    def summary(self):
        return "A (100) — clean"

    def counts_by_severity(self):
        out = {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0}
        for f in self.findings:
            out[f.severity] = out.get(f.severity, 0) + 1
        return out


class TestHardeningSecurity:
    def _handler(self, tmp_path):
        from friday_v4.nl_router import TextCommandHandler
        return TextCommandHandler(conn=_conn(tmp_path), cwd=str(tmp_path))

    def test_scan_repo_runs_scanner_and_reports(self, tmp_path, monkeypatch):
        from friday_v4.security.reporter import Finding
        found = []

        class _FakeScanner:
            def scan_quick(self, path, threshold="low"):
                found.append(path)
                return _FakeReport([Finding(severity="low", title="nit")])

        monkeypatch.setattr(
            "friday_v4.security.scanner.VulnerabilityScanner",
            lambda: _FakeScanner())
        handler = self._handler(tmp_path)
        result = handler.handle("scan my repo")
        assert result.action == "security"
        assert result.status == "succeeded"
        assert "Security scan" in result.response
        assert found == [str(tmp_path)]  # scanned the working directory

    def test_high_findings_pushed_to_ambient_bus(self, tmp_path, monkeypatch):
        """High/critical findings become durable ambient events (Wave 11)."""
        from friday_v4.security.reporter import Finding

        class _FakeScanner:
            def scan_quick(self, path, threshold="low"):
                return _FakeReport([
                    Finding(severity="high", title="known vuln",
                            package="requests", detail="unpatched"),
                    Finding(severity="low", title="nit"),
                ])

        monkeypatch.setattr(
            "friday_v4.security.scanner.VulnerabilityScanner",
            lambda: _FakeScanner())
        handler = self._handler(tmp_path)
        result = handler.handle("scan my repo")
        assert "high" in result.response.lower()
        # The durable queue now carries the security event.
        events = db.recent_ambient_events(handler.conn, limit=10) or []
        assert any("security" in (e.get("topic") or "") for e in events)

    def test_scan_with_named_path(self, tmp_path, monkeypatch):
        found = []

        class _FakeScanner:
            def scan_quick(self, path, threshold="low"):
                found.append(path)
                return _FakeReport([])

        monkeypatch.setattr(
            "friday_v4.security.scanner.VulnerabilityScanner",
            lambda: _FakeScanner())
        handler = self._handler(tmp_path)
        result = handler.handle("scan the auth refactor")
        assert result.action == "security"
        assert found == ["auth refactor"]

    def test_scan_never_crashes_on_scanner_failure(self, tmp_path, monkeypatch):
        class _BoomScanner:
            def scan_quick(self, path, threshold="low"):
                raise RuntimeError("scanner exploded")

        monkeypatch.setattr(
            "friday_v4.security.scanner.VulnerabilityScanner",
            lambda: _BoomScanner())
        handler = self._handler(tmp_path)
        result = handler.handle("scan my repo")
        assert result.action == "failed"
        assert "couldn't scan" in result.response


# ==========================================================================
# collab — "what's my team working on" is answered with shared observations
# ==========================================================================


class _FakeCoordinator:
    def __init__(self, observations=None, peers=None):
        self._observations = observations or []
        self._peers = peers or []

    def observations(self, limit=None):
        return self._observations

    def peers(self):
        return self._peers


class _FakePeer:
    def __init__(self, peer_id):
        self.peer_id = peer_id


class TestHardeningCollab:
    def test_team_question_answered_from_peer_observations(self, tmp_path,
                                                           monkeypatch):
        monkeypatch.setattr(
            "friday_v4.collab.Coordinator",
            lambda: _FakeCoordinator(
                observations=[
                    {"peer_id": "alice-laptop",
                     "payload": {"subject": "vivaha", "kind": "commit"}},
                    {"peer_id": "bob-desktop",
                     "payload": {"subject": "mindwell", "kind": "test_run"}},
                ],
                peers=[_FakePeer("alice-laptop"), _FakePeer("bob-desktop")]))
        from friday_v4.reasoning.providers import collab_provider
        from friday_v4.reasoning.question import Question, QuestionType
        conn = _conn(tmp_path)
        try:
            ans = collab_provider(
                Question("what's my team working on", QuestionType.COLLAB),
                conn)
            assert ans is not None
            assert "vivaha" in ans.text and "mindwell" in ans.text
            sources = {e.source for e in ans.evidence}
            assert "v4.collab.observations" in sources
            assert "v4.collab.peers" in sources
        finally:
            conn.close()

    def test_team_question_no_collab_is_honest(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "friday_v4.collab.Coordinator",
            lambda: _FakeCoordinator([], []))
        from friday_v4.reasoning.providers import collab_provider
        from friday_v4.reasoning.question import Question, QuestionType
        conn = _conn(tmp_path)
        try:
            ans = collab_provider(
                Question("what's my team working on", QuestionType.COLLAB),
                conn)
            assert ans is None  # → the engine answers "I don't know yet"
        finally:
            conn.close()

    def test_team_question_through_the_router(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "friday_v4.collab.Coordinator",
            lambda: _FakeCoordinator(
                observations=[
                    {"peer_id": "alice-laptop",
                     "payload": {"subject": "vivaha", "kind": "commit"}}],
                peers=[_FakePeer("alice-laptop")]))
        from friday_v4.nl_router import TextCommandHandler
        handler = TextCommandHandler(conn=_conn(tmp_path))
        result = handler.handle("what's my team working on")
        assert result.action == "chat"
        assert "team" in result.response.lower() or "vivaha" in result.response


# ==========================================================================
# memory — "remember that X" stores, "forget that" removes (consent-first)
# ==========================================================================


class TestHardeningMemory:
    def _handler(self, tmp_path):
        from friday_v4.nl_router import TextCommandHandler
        return TextCommandHandler(conn=_conn(tmp_path))

    def test_remember_stores_fact(self, tmp_path):
        handler = self._handler(tmp_path)
        result = handler.handle("remember that I prefer Rust for tooling")
        assert result.action == "memory"
        assert "Noted" in result.response
        from friday_v4.memory import FactMemory
        facts = FactMemory(handler.conn).recall(subject="operator")
        assert facts and "prefer Rust" in facts[0].value
        assert facts[0].source == "talk"

    def test_remember_requires_statement(self, tmp_path):
        handler = self._handler(tmp_path)
        result = handler.handle("remember")
        assert result.action == "clarification"
        assert "What should I remember" in result.response

    def test_forget_removes_fact(self, tmp_path):
        handler = self._handler(tmp_path)
        handler.handle("remember that I prefer Rust for tooling")
        from friday_v4.memory import FactMemory
        assert FactMemory(handler.conn).count(subject="operator") == 1
        result = handler.handle("forget that")
        assert result.action == "memory"
        assert "forgotten" in result.response
        assert FactMemory(handler.conn).count(subject="operator") == 0

    def test_forget_nothing_stored_is_honest(self, tmp_path):
        handler = self._handler(tmp_path)
        result = handler.handle("forget that")
        assert result.action in ("chat", "memory")

    def test_remember_never_crashes_without_db(self):
        from friday_v4.nl_router import TextCommandHandler
        handler = TextCommandHandler(conn=None)
        result = handler.handle("remember that I prefer Rust")
        assert result.action == "failed"  # honest — no memory connected


# ==========================================================================
# desktop — text surfaces route focus/workspace/screenshot via the WM
# ==========================================================================


class _FakeWM:
    """Hermetic window-manager stand-in (never touches the real desktop)."""

    def __init__(self):
        self.focused = []
        self.launched = []
        self.switched = []
        self.screenshot_taken = False

    @property
    def is_available(self):
        return True

    def focus_smart(self, query):
        self.focused.append(query)
        return "Code" if "code" in query.lower() else None

    def launch_app(self, app):
        self.launched.append(app)
        return True

    def switch_workspace(self, ws_id):
        self.switched.append(ws_id)
        return True

    def take_screenshot(self):
        self.screenshot_taken = True
        return "/tmp/friday-shot.png"

    def list_windows(self):
        from friday_v4.desktop.wm_abstraction import WindowInfo
        return [WindowInfo(app_class="Code", title="main.py — friday_v4",
                           workspace_id=3, is_active=True)]

    def list_workspaces(self):
        from friday_v4.desktop.wm_abstraction import WorkspaceInfo
        return [WorkspaceInfo(id=3, name="dev", is_active=True)]

    def get_active_window(self):
        return self.list_windows()[0]


class TestHardeningDesktop:
    def _handler(self, tmp_path, wm):
        from friday_v4.nl_router import TextCommandHandler
        from friday_v4.desktop import wm_abstraction as wm_mod
        import friday_v4.desktop.wm_abstraction as _wm

        class _Router:
            def __call__(self, text):
                _orig = _wm.WindowManager
                try:
                    _wm.WindowManager = lambda *a, **k: wm
                    return wm_mod.desktop_text_command(text)
                finally:
                    _wm.WindowManager = _orig

        return TextCommandHandler(conn=_conn(tmp_path),
                                  desktop_handler=_Router())

    def test_focus_command_routes(self, tmp_path):
        wm = _FakeWM()
        handler = self._handler(tmp_path, wm)
        result = handler.handle("focus code editor")
        assert result.action == "desktop"
        assert "Focused Code" in result.response
        assert wm.focused == ["code editor"]

    def test_launch_command_routes(self, tmp_path):
        wm = _FakeWM()
        handler = self._handler(tmp_path, wm)
        result = handler.handle("launch firefox")
        assert result.action == "desktop"
        assert "Launching firefox" in result.response
        assert "firefox" in wm.launched

    def test_workspace_command_routes(self, tmp_path):
        wm = _FakeWM()
        handler = self._handler(tmp_path, wm)
        result = handler.handle("switch to workspace 3")
        assert result.action == "desktop"
        assert "workspace 3" in result.response
        assert 3 in wm.switched

    def test_screenshot_command_routes(self, tmp_path):
        wm = _FakeWM()
        handler = self._handler(tmp_path, wm)
        result = handler.handle("take a screenshot")
        assert result.action == "desktop"
        assert "Screenshot saved" in result.response
        assert wm.screenshot_taken

    def test_desktop_status_query_routes(self, tmp_path):
        wm = _FakeWM()
        handler = self._handler(tmp_path, wm)
        result = handler.handle("what's on my screen")
        assert result.action == "desktop"
        assert "Code" in result.response

    def test_desktop_unavailable_is_honest(self, tmp_path, monkeypatch):
        import friday_v4.desktop.wm_abstraction as wm_mod
        monkeypatch.setattr(
            wm_mod, "WindowManager",
            lambda *a, **k: _UnavailableWM())
        assert "isn't available" in wm_mod.desktop_text_command("focus code")

    def test_desktop_text_command_empty_on_no_match(self):
        from friday_v4.desktop.wm_abstraction import desktop_text_command
        assert desktop_text_command("hello there") == ""


class _UnavailableWM:
    @property
    def is_available(self):
        return False
