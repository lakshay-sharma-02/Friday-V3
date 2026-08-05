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

from friday_v6 import db


def _conn(tmp_path):
    return db.connect(tmp_path / "v4.db")


# ==========================================================================
# nlu — the ONE point classifies the new intents (LLM-first, rules fallback)
# ==========================================================================


class TestHardeningNlu:
    def test_scan_my_repo_classifies_security(self):
        from friday_v6.nlu import Intent, resolve
        assert resolve("scan my repo").intent == Intent.SECURITY
        assert resolve("is my code secure").intent == Intent.SECURITY
        assert resolve("check my dependencies").intent == Intent.SECURITY

    def test_security_target_extracts_path(self):
        from friday_v6.nlu import Intent, resolve
        action = resolve("scan the auth refactor")
        assert action.intent == Intent.SECURITY
        assert action.target == "auth refactor"

    def test_remember_classifies_memory(self):
        from friday_v6.nlu import Intent, resolve
        assert resolve("remember that I prefer Rust").intent == Intent.MEMORY
        assert resolve("forget that").intent == Intent.MEMORY

    def test_memory_goal_threads_fact_text(self):
        from friday_v6.nlu import Intent, resolve
        action = resolve("remember that I prefer Rust")
        assert action.intent == Intent.MEMORY
        assert action.goal  # the fact statement is threaded

    def test_team_question_stays_ask(self):
        from friday_v6.nlu import Intent, resolve
        assert resolve("what's my team working on").intent == Intent.ASK

    def test_llm_still_wins_over_keywords(self):
        from friday_v6.nlu import Intent, resolve

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
        from friday_v6.nl_router import TextCommandHandler
        return TextCommandHandler(conn=_conn(tmp_path), cwd=str(tmp_path))

    def test_scan_repo_runs_scanner_and_reports(self, tmp_path, monkeypatch):
        from friday_v6.security.reporter import Finding
        found = []

        class _FakeScanner:
            def scan_quick(self, path, threshold="low"):
                found.append(path)
                return _FakeReport([Finding(severity="low", title="nit")])

        monkeypatch.setattr(
            "friday_v6.security.scanner.VulnerabilityScanner",
            lambda: _FakeScanner())
        handler = self._handler(tmp_path)
        result = handler.handle("scan my repo")
        assert result.action == "security"
        assert result.status == "succeeded"
        assert "Security scan" in result.response
        assert found == [str(tmp_path)]  # scanned the working directory

    def test_high_findings_pushed_to_ambient_bus(self, tmp_path, monkeypatch):
        """High/critical findings become durable ambient events (Wave 11)."""
        from friday_v6.security.reporter import Finding

        class _FakeScanner:
            def scan_quick(self, path, threshold="low"):
                return _FakeReport([
                    Finding(severity="high", title="known vuln",
                            package="requests", detail="unpatched"),
                    Finding(severity="low", title="nit"),
                ])

        monkeypatch.setattr(
            "friday_v6.security.scanner.VulnerabilityScanner",
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
            "friday_v6.security.scanner.VulnerabilityScanner",
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
            "friday_v6.security.scanner.VulnerabilityScanner",
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
            "friday_v6.collab.Coordinator",
            lambda: _FakeCoordinator(
                observations=[
                    {"peer_id": "alice-laptop",
                     "payload": {"subject": "vivaha", "kind": "commit"}},
                    {"peer_id": "bob-desktop",
                     "payload": {"subject": "mindwell", "kind": "test_run"}},
                ],
                peers=[_FakePeer("alice-laptop"), _FakePeer("bob-desktop")]))
        from friday_v6.reasoning.providers import collab_provider
        from friday_v6.reasoning.question import Question, QuestionType
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
            "friday_v6.collab.Coordinator",
            lambda: _FakeCoordinator([], []))
        from friday_v6.reasoning.providers import collab_provider
        from friday_v6.reasoning.question import Question, QuestionType
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
            "friday_v6.collab.Coordinator",
            lambda: _FakeCoordinator(
                observations=[
                    {"peer_id": "alice-laptop",
                     "payload": {"subject": "vivaha", "kind": "commit"}}],
                peers=[_FakePeer("alice-laptop")]))
        from friday_v6.nl_router import TextCommandHandler
        handler = TextCommandHandler(conn=_conn(tmp_path))
        result = handler.handle("what's my team working on")
        assert result.action == "chat"
        assert "team" in result.response.lower() or "vivaha" in result.response


# ==========================================================================
# memory — "remember that X" stores, "forget that" removes (consent-first)
# ==========================================================================


class TestHardeningMemory:
    def _handler(self, tmp_path):
        from friday_v6.nl_router import TextCommandHandler
        return TextCommandHandler(conn=_conn(tmp_path),
                                  vault_root=str(tmp_path / "vault"))

    def test_remember_stores_fact(self, tmp_path):
        handler = self._handler(tmp_path)
        result = handler.handle("remember that I prefer Rust for tooling")
        assert result.action == "memory"
        assert "Noted" in result.response
        from friday_v6.memory import FactMemory
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
        from friday_v6.memory import FactMemory
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
        from friday_v6.nl_router import TextCommandHandler
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
        from friday_v6.desktop.wm_abstraction import WindowInfo
        return [WindowInfo(app_class="Code", title="main.py — friday_v6",
                           workspace_id=3, is_active=True)]

    def list_workspaces(self):
        from friday_v6.desktop.wm_abstraction import WorkspaceInfo
        return [WorkspaceInfo(id=3, name="dev", is_active=True)]

    def get_active_window(self):
        return self.list_windows()[0]


class TestHardeningDesktop:
    def _handler(self, tmp_path, wm):
        from friday_v6.nl_router import TextCommandHandler
        from friday_v6.desktop import wm_abstraction as wm_mod
        import friday_v6.desktop.wm_abstraction as _wm

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

    def test_launch_command_routes(self, tmp_path, monkeypatch):
        wm = _FakeWM()
        handler = self._handler(tmp_path, wm)
        # Hermetic: pretend firefox is installed regardless of the
        # machine — the NL layer gates "Launching X" on a resolvable
        # binary.
        import friday_v6.desktop.wm_abstraction as wm_mod
        monkeypatch.setattr(wm_mod.shutil, "which",
                            lambda name: "/usr/bin/" + name)
        result = handler.handle("launch firefox")
        assert result.action == "desktop"
        assert "Launching firefox" in result.response
        assert "firefox" in wm.launched

    def test_open_uninstalled_app_falls_through_to_web(self, tmp_path,
                                                       monkeypatch):
        """"open whatsapp" with no local binary must open the web
        destination (web.whatsapp.com), never claim "Launching"."""
        import friday_v6.desktop.wm_abstraction as wm_mod
        wm = _FakeWM()
        monkeypatch.setattr(wm_mod, "WindowManager",
                            lambda *a, **k: wm)
        monkeypatch.setattr(wm_mod.shutil, "which", lambda name: None)
        monkeypatch.setattr(wm_mod, "_open_in_browser",
                            lambda wm_, url, browser=None, label=None:
                            f"Opened {label} in Brave.")
        result = wm_mod.desktop_text_command("open whatsapp")
        assert "Opened" in result and "WhatsApp" in result
        assert "web.whatsapp.com" in wm_mod._WEB_DESTINATIONS["whatsapp"]
        assert wm.launched == []  # never pretended to launch

    def test_open_unresolvable_target_web_searches(self, tmp_path,
                                                   monkeypatch):
        """No installed app + no known destination → honest web search."""
        import friday_v6.desktop.wm_abstraction as wm_mod
        wm = _FakeWM()
        monkeypatch.setattr(wm_mod, "WindowManager",
                            lambda *a, **k: wm)
        monkeypatch.setattr(wm_mod.shutil, "which", lambda name: None)
        captured = {}
        monkeypatch.setattr(
            wm_mod, "_open_in_browser",
            lambda wm_, url, browser=None, label=None:
            captured.setdefault("url", url) or "Opened it in Brave.")
        result = wm_mod.desktop_text_command(
            "open c++ compiler of programiz")
        assert "search" in captured["url"].lower()
        assert "c%2B%2B" in captured["url"] or "c%2b%2b" in captured["url"]
        assert wm.launched == []

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
        import friday_v6.desktop.wm_abstraction as wm_mod
        monkeypatch.setattr(
            wm_mod, "WindowManager",
            lambda *a, **k: _UnavailableWM())
        assert "isn't available" in wm_mod.desktop_text_command("focus code")

    def test_desktop_text_command_empty_on_no_match(self):
        from friday_v6.desktop.wm_abstraction import desktop_text_command
        assert desktop_text_command("hello there") == ""

    # ── open-ended "do everything" contract (no hardcoded workflows) ──

    def _interpreter(self, monkeypatch, wm):
        """A hermetic interpreter: fake WM + controlled binaries/browser."""
        import friday_v6.desktop.wm_abstraction as wm_mod
        monkeypatch.setattr(wm_mod, "WindowManager",
                            lambda *a, **k: wm)
        monkeypatch.setattr(wm_mod.shutil, "which", lambda name: None)
        return wm_mod

    def test_task_phrase_falls_through_to_brain(self, monkeypatch):
        """"open a python venv and install requests" is NOT a desktop
        command — the interpreter returns "" so the brain's EXECUTE /
        Claude Code arms take it. No web-search of a task."""
        import friday_v6.desktop.wm_abstraction as wm_mod
        wm = _FakeWM()
        self._interpreter(monkeypatch, wm)
        for t in ("open a python venv and install requests",
                  "clone the repo and open it in my editor",
                  "open a fresh project for a discord bot"):
            assert wm_mod.desktop_text_command(t) == "", t
        assert wm.launched == []
        assert wm.focused == []

    def test_explicit_search_web_searches(self, monkeypatch):
        """"search for X" / "look up X" / "google X" → real web search."""
        import friday_v6.desktop.wm_abstraction as wm_mod
        wm = _FakeWM()
        self._interpreter(monkeypatch, wm)
        captured = []
        monkeypatch.setattr(
            wm_mod, "_open_in_browser",
            lambda wm_, url, browser=None, label=None:
            captured.append(url) or "Opened it.")
        for t, expect in (("search for the best rust web framework",
                           "the+best+rust+web+framework"),
                          ("look up fastapi docs", "fastapi+docs"),
                          ("google hyprland docs", "hyprland+docs")):
            captured.clear()
            result = wm_mod.desktop_text_command(t)
            assert result != "", t
            assert captured and expect in captured[0], (t, captured)

    def test_compound_workspace_and_browser_qualifiers(self, monkeypatch):
        """One utterance, many commands: "open chrome on workspace 3 and
        open whatsapp" → workspace switch + chrome search + WhatsApp."""
        import friday_v6.desktop.wm_abstraction as wm_mod
        wm = _FakeWM()
        self._interpreter(monkeypatch, wm)
        urls = []
        monkeypatch.setattr(
            wm_mod, "_open_in_browser",
            lambda wm_, url, browser=None, label=None:
            urls.append(url) or f"Opened {label} in Brave.")
        result = wm_mod.desktop_text_command(
            "open chrome on workspace 3 and open whatsapp")
        assert "WhatsApp" in result
        assert wm.switched == [3]          # workspace qualifier honored
        assert urls and urls[-1] == wm_mod._WEB_DESTINATIONS["whatsapp"]

    def test_compound_split_includes_search_verbs(self, monkeypatch):
        """"open brave and search for rust" splits on the search verb too
        (the splitter's lookahead derives from _DESKTOP_VERBS, so verb
        extraction and splitting can never drift apart)."""
        import friday_v6.desktop.wm_abstraction as wm_mod
        wm = _FakeWM()
        self._interpreter(monkeypatch, wm)
        urls = []
        monkeypatch.setattr(
            wm_mod, "_open_in_browser",
            lambda wm_, url, browser=None, label=None:
            urls.append(url) or f"Opened {label} in Brave.")
        result = wm_mod.desktop_text_command(
            "open brave and search for rust web framework")
        assert urls, "the search part must produce a browser URL"
        assert "rust+web+framework" in urls[-1]

    def test_site_search_in_browser(self, monkeypatch):
        """"open youtube and cristiano ronaldo channel in it" searches
        YouTube, not a desktop app, and resolves in the default browser."""
        import friday_v6.desktop.wm_abstraction as wm_mod
        wm = _FakeWM()
        self._interpreter(monkeypatch, wm)
        captured = {}

        def fake_open(wm_, url, browser=None, label=None):
            captured["url"] = url
            return f"Opened {label}."

        monkeypatch.setattr(wm_mod, "_open_in_browser", fake_open)
        result = wm_mod.desktop_text_command(
            "open youtube and cristiano ronaldo channel in it")
        assert "youtube.com/results" in captured["url"]
        assert "cristiano+ronaldo" in captured["url"]
        assert "searching" in result
        assert "cristiano ronaldo channel" in result


class _UnavailableWM:
    @property
    def is_available(self):
        return False
