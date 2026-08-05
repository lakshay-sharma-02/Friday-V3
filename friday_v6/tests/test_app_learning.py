"""Wave 20 app-learning loop — Friday remembers personal apps.

The user request: "open my todo app" should resolve to the installed
binary *once it's been taught*, on every surface. This file verifies the
learning loop hermetically (tmp alias store, fake WM, patched PATH):

- First "open my todo app" → a teaching prompt, never a useless web
  search of the operator's own app.
- "my todo app is obsidian" (and the use/set/with frames) teaches the
  mapping; only *resolvable* binaries are ever saved (honesty law).
- After teaching, "open my todo app" / "open todo app" launch obsidian.
- The mapping persists to disk and survives a fresh read.
- The classifier routes learning phrases to DESKTOP on the offline path,
  and the NL router routes them to the desktop handler even when an LLM
  would call them ASK (LLM-robustness).
- Forget removes the mapping; the CLI surface exposes aliases/teach/forget.

All hermetic: never touches ~/.friday, never touches the real desktop.
"""

from __future__ import annotations

import shutil

import pytest

import friday_v6.desktop.app_aliases as A


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Point the alias store at a temp file; pretend obsidian/joplin exist."""
    store_file = tmp_path / "aliases.json"
    monkeypatch.setattr(A, "_DEFAULT_ALIAS_FILE", store_file)
    real_which = shutil.which

    def fake_which(name):
        if name in ("obsidian", "joplin"):
            return "/usr/bin/" + name
        return real_which(name)

    monkeypatch.setattr(A.shutil, "which", fake_which)
    return store_file


@pytest.fixture
def wm(monkeypatch):
    """Hermetic interpreter: fake WM + fake PATH for wm_abstraction too."""
    import friday_v6.desktop.wm_abstraction as wm_mod
    real_which = shutil.which

    class _FakeWM:
        def __init__(self):
            self.focused = []
            self.launched = []
            self.is_available = True

        def focus_smart(self, query):
            self.focused.append(query)
            return None

        def launch_app(self, app):
            self.launched.append(app)
            return True

    fake = _FakeWM()
    monkeypatch.setattr(wm_mod, "WindowManager", lambda *a, **k: fake)
    monkeypatch.setattr(
        wm_mod.shutil, "which",
        lambda name: "/usr/bin/" + name if name in ("obsidian", "joplin")
        else real_which(name))
    return wm_mod, fake


# ==========================================================================
# parser — the teaching frames
# ==========================================================================


class TestLearningPhraseParser:
    @pytest.mark.parametrize("text,name,binary", [
        ("my todo app is obsidian", "todo app", "obsidian"),
        ("todo app is obsidian", "todo app", "obsidian"),
        ("The notes app is joplin", "notes app", "joplin"),
        ("use obsidian for my todo app", "todo app", "obsidian"),
        ("use joplin for todo app", "todo app", "joplin"),
        ("set my todo app to obsidian", "todo app", "obsidian"),
        ("open my todo app with obsidian", "todo app", "obsidian"),
        ("my todo app as obsidian", "todo app", "obsidian"),
    ])
    def test_frames_parse(self, text, name, binary):
        assert A.parse_learning_phrase(text) == (name, binary)

    @pytest.mark.parametrize("text", [
        "my code is broken",          # not an app-like name
        "what is the weather",        # ask, not teaching
        "open my todo app",           # the command, not the teaching
        "forget my todo app",         # not a learning frame
        "is obsidian my todo app",    # wrong order
        "use sudo for that",          # pronoun name → never a junk alias
        "use git for this",           # pronoun name
        "set my alarm to 7",          # numeric "binary"
    ])
    def test_non_learning_phrases(self, text):
        assert A.parse_learning_phrase(text) is None

    def test_compound_teach_still_teaches(self, store, wm):
        """A teaching frame inside a compound utterance still teaches."""
        wm_mod, fake = wm
        result = wm_mod.desktop_text_command(
            "open my todo app with obsidian and open whatsapp")
        assert "Got it" in result          # the with-frame taught
        assert A.resolve_learned("todo app") == "/usr/bin/obsidian"
        assert fake.launched == ["/usr/bin/obsidian"]


# ==========================================================================
# store — save / resolve / forget / persist / honesty
# ==========================================================================


class TestAliasStore:
    def test_learn_and_resolve_normalized(self, store):
        assert A.learn_alias("my todo app", "obsidian") == "/usr/bin/obsidian"
        assert A.resolve_learned("my todo app") == "/usr/bin/obsidian"
        assert A.resolve_learned("todo app") == "/usr/bin/obsidian"
        assert A.resolve_learned("TODO  APP") == "/usr/bin/obsidian"

    def test_persists_to_disk(self, store):
        A.learn_alias("todo app", "obsidian")
        # A fresh read (new store object) sees the same data.
        fresh = A._read(store)
        assert fresh == {"todo app": "/usr/bin/obsidian"}
        assert store.exists()

    def test_never_learns_unresolvable_binary(self, store):
        assert A.learn_alias("todo app", "not-installed-xyz") is None
        assert A.learned_aliases() == {}

    def test_forget_removes(self, store):
        A.learn_alias("todo app", "obsidian")
        assert A.forget_alias("my todo app") is True
        assert A.learned_aliases() == {}
        assert A.forget_alias("todo app") is False  # already gone

    def test_corrupt_store_reads_empty(self, store):
        store.write_text("{ not json !!!")
        assert A.learned_aliases() == {}


# ==========================================================================
# interpreter — the full learning loop
# ==========================================================================


class TestLearningLoop:
    def test_first_open_is_a_teaching_prompt_not_a_web_search(self, store, wm):
        wm_mod, fake = wm
        urls = []
        wm_mod._open_in_browser = lambda *a, **k: urls.append(a[1]) or "Opened"
        result = wm_mod.desktop_text_command("open my todo app")
        assert "I don't know what 'todo app' is yet" in result
        assert "Teach me once" in result
        assert urls == []                # never web-searched a personal app
        assert fake.launched == []

    def test_teach_once_then_open_always(self, store, wm):
        wm_mod, fake = wm
        taught = wm_mod.desktop_text_command("my todo app is obsidian")
        assert "Got it" in taught and "obsidian" in taught
        assert fake.launched == ["/usr/bin/obsidian"]  # opened on teach

        fake.launched.clear()
        result = wm_mod.desktop_text_command("open my todo app")
        assert "Launching" in result
        assert fake.launched == ["/usr/bin/obsidian"]  # resolved from memory

        fake.launched.clear()
        result = wm_mod.desktop_text_command("open todo app")  # no "my"
        assert fake.launched == ["/usr/bin/obsidian"]

    def test_use_frame_teaches_and_opens(self, store, wm):
        wm_mod, fake = wm
        result = wm_mod.desktop_text_command("use joplin for notes app")
        assert "Got it" in result and "notes app" in result
        assert A.resolve_learned("notes app") == "/usr/bin/joplin"
        assert fake.launched == ["/usr/bin/joplin"]

    def test_open_with_frame_teaches_and_opens(self, store, wm):
        wm_mod, fake = wm
        result = wm_mod.desktop_text_command("open my todos with joplin")
        assert "Got it" in result and "'todos'" in result
        assert A.resolve_learned("todos") == "/usr/bin/joplin"

    def test_unresolvable_binary_refused_honestly(self, store, wm):
        wm_mod, _ = wm
        result = wm_mod.desktop_text_command("my todo app is not-installed-xyz")
        assert "couldn't find 'not-installed-xyz'" in result
        assert "haven't saved" in result
        assert A.learned_aliases() == {}

    def test_re_teach_overwrites(self, store, wm):
        A.learn_alias("todo app", "obsidian")
        wm_mod, fake = wm
        wm_mod.desktop_text_command("use joplin for todo app")
        assert A.resolve_learned("todo app") == "/usr/bin/joplin"
        assert fake.launched == ["/usr/bin/joplin"]

    def test_learned_app_focuses_when_running(self, store, wm):
        A.learn_alias("todo app", "obsidian")
        wm_mod, fake = wm
        fake.focus_smart = lambda q: "obsidian"  # it IS running
        result = wm_mod.desktop_text_command("open my todo app")
        assert "Focused obsidian" in result
        assert fake.launched == []

    def test_non_personal_target_still_web_searches(self, store, wm):
        wm_mod, _ = wm
        urls = []
        wm_mod._open_in_browser = lambda *a, **k: urls.append(a[1]) or "Opened"
        result = wm_mod.desktop_text_command("open c++ compiler of programiz")
        assert "search" in (urls[0] if urls else "")
        assert "I don't know what" not in result


# ==========================================================================
# classifier — learning phrases route to DESKTOP (offline / voice path)
# ==========================================================================


class TestClassifierRouting:
    def test_learning_phrases_classify_desktop(self):
        from friday_v6.nlu.intent import _fallback_classify
        for t in ("my todo app is obsidian", "use obsidian for my todo app",
                  "set my todo app to obsidian",
                  "open my todo app with obsidian"):
            assert _fallback_classify(t).intent.value == "desktop", t

    def test_non_learning_phrases_unchanged(self):
        from friday_v6.nlu.intent import _fallback_classify
        assert _fallback_classify("my code is broken").intent.value == "ask"
        assert _fallback_classify("what is the weather").intent.value == "ask"


# ==========================================================================
# router — learning works even when an LLM would call it ASK
# ==========================================================================


class TestRouterLearning:
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

    def test_learning_phrase_reaches_desktop_handler(self, tmp_path,
                                                     monkeypatch, store):
        """Even with an LLM that calls it ASK, the phrase still teaches."""
        import friday_v6.desktop.wm_abstraction as wm_mod
        wm = _FakeWM()
        monkeypatch.setattr(wm_mod, "WindowManager", lambda *a, **k: wm)
        monkeypatch.setattr(
            wm_mod.shutil, "which",
            lambda name: "/usr/bin/obsidian" if name == "obsidian" else None)
        monkeypatch.setattr(wm_mod, "_open_in_browser",
                            lambda *a, **k: "Opened.")

        class _AskLLM:
            """A model that insists the phrase is a question."""
            def parse_utterance(self, text):
                return {"intent": "ask", "action_type": None, "command": "",
                        "target": "obsidian", "goal": None, "entities": [],
                        "needs_clarification": False, "clarification": "",
                        "confidence": 0.9}

        from friday_v6.nl_router import TextCommandHandler
        handler = TextCommandHandler(
            conn=_conn(tmp_path),
            desktop_handler=wm_mod.desktop_text_command,
            llm=_AskLLM())
        result = handler.handle("my todo app is obsidian")
        assert result.action == "desktop"
        assert "Got it" in result.response
        assert A.resolve_learned("todo app") == "/usr/bin/obsidian"

    def test_command_routes_to_desktop_intent(self, tmp_path, store):
        handler = self._handler(tmp_path, _FakeWM())
        result = handler.handle("open my todo app")
        assert result.action == "desktop"


# ==========================================================================
# CLI — friday6 desktop aliases / teach / forget
# ==========================================================================


class TestDesktopCli:
    def test_teach_learns(self, store, capsys):
        from friday_v6.cli_desktop import cmd_desktop_teach
        import argparse
        args = argparse.Namespace(name=["todo", "app"], binary="obsidian",
                                  store=None)
        assert cmd_desktop_teach(args) == 0
        assert A.resolve_learned("todo app") == "/usr/bin/obsidian"

    def test_teach_refuses_unresolvable(self, store, capsys):
        from friday_v6.cli_desktop import cmd_desktop_teach
        import argparse
        args = argparse.Namespace(name=["todo", "app"],
                                  binary="not-installed-xyz", store=None)
        assert cmd_desktop_teach(args) == 1
        assert A.learned_aliases() == {}

    def test_aliases_lists_and_forget_removes(self, store, capsys):
        from friday_v6.cli_desktop import (cmd_desktop_aliases,
                                           cmd_desktop_forget)
        import argparse
        A.learn_alias("todo app", "obsidian")
        assert cmd_desktop_aliases(argparse.Namespace(store=None)) == 0
        out = capsys.readouterr().out
        assert "todo app" in out and "obsidian" in out
        assert cmd_desktop_forget(argparse.Namespace(name=["todo", "app"],
                                                     store=None)) == 0
        assert A.learned_aliases() == {}

    def test_aliases_json_contract(self, store, capsys):
        """The VS Code extension's sidebar contract: pure JSON, no ANSI."""
        from friday_v6.cli_desktop import cmd_desktop_aliases
        import argparse
        import json as _json
        A.learn_alias("todo app", "obsidian")
        A.learn_alias("notes", "joplin")
        assert cmd_desktop_aliases(
            argparse.Namespace(store=None, json=True)) == 0
        data = _json.loads(capsys.readouterr().out)
        assert data == {"notes": "/usr/bin/joplin",
                        "todo app": "/usr/bin/obsidian"}


class TestCollabSync:
    """Cross-machine continuity: learned aliases ride the collab bus.

    "todo app" taught on the laptop should work on the desktop: the
    aliases publish as CRDT observations (``alias:<name>`` keys), merge
    onto peers, and only *installable* binaries are ever launched here.
    """

    def test_aliases_as_observations_keyed(self, store):
        A.learn_alias("todo app", "obsidian")
        A.learn_alias("notes", "joplin")
        obs = A.aliases_as_observations()
        assert len(obs) == 2
        first, second = obs  # sorted by name: notes < todo app
        assert first["source"] == A.ALIAS_OBSERVATION_SOURCE
        assert first["subject"] == "notes"
        assert first["aspect"] == "binary"
        assert first["kind"] == "alias"
        assert first["payload"] == {"binary": "/usr/bin/joplin"}
        assert second["subject"] == "todo app"
        assert second["payload"] == {"binary": "/usr/bin/obsidian"}

    def test_apply_merges_remote_aliases(self, store):
        remote = [
            {"source": A.ALIAS_OBSERVATION_SOURCE, "subject": "notes",
             "aspect": "binary", "payload": {"binary": "joplin"}},
            {"source": A.ALIAS_OBSERVATION_SOURCE, "subject": "todo app",
             "aspect": "binary", "payload": {"binary": "obsidian"}},
        ]
        assert A.apply_collab_observations(remote) == 2
        assert A.learned_aliases() == {"notes": "joplin",
                                       "todo app": "obsidian"}

    def test_apply_ignores_foreign_sources_and_junk(self, store):
        A.learn_alias("notes", "joplin")
        remote = [
            {"source": "someone.else", "subject": "notes",
             "payload": {"binary": "evil"}},   # foreign source
            {"source": A.ALIAS_OBSERVATION_SOURCE, "subject": "bad",
             "payload": None},                     # malformed payload
            {"source": A.ALIAS_OBSERVATION_SOURCE, "subject": "",
             "payload": {"binary": "x"}},        # empty name
            "not-a-dict",                           # junk entry
            None,
        ]
        assert A.apply_collab_observations(remote) == 0
        assert A.learned_aliases() == {"notes": "/usr/bin/joplin"}

    def test_apply_noop_on_empty(self, store):
        assert A.apply_collab_observations([]) == 0
        assert A.apply_collab_observations(None) == 0

    def test_synced_alias_uninstalled_falls_through(self, store,
                                                     monkeypatch):
        """A synced alias for an app NOT on this machine never dead-launches."""
        import friday_v6.desktop.wm_abstraction as wm_mod
        A.learn_alias("todo app", "/usr/bin/does-not-exist-here")
        monkeypatch.setattr(wm_mod.shutil, "which", lambda _: None)
        assert wm_mod._resolve_app("todo app") is None
        assert wm_mod._resolve_app("todo app") is None  # stays None

    def test_synced_alias_installed_resolves(self, store, wm):
        """A synced alias whose binary exists here resolves to it."""
        wm_mod, _ = wm
        A.learn_alias("todo app", "obsidian")
        assert wm_mod._resolve_app("todo app") == "/usr/bin/obsidian"

    def test_cli_sync_pushes_local_and_applies_remote(self, store, capsys,
                                                      monkeypatch):
        import argparse
        import friday_v6.collab as collab_mod
        from friday_v6.cli_desktop import cmd_desktop_aliases_sync

        A.learn_alias("todo app", "obsidian")
        pushed_ids = []

        class _FakeCoordinator:
            def start(self):
                return True

            def stop(self):
                return None

            def add_observation(self, payload, obs_id=None):
                pushed_ids.append(obs_id)
                return obs_id

            def sync_once(self):
                return {"peers": 1, "applied": 1}

            def observations(self):
                return [{"source": A.ALIAS_OBSERVATION_SOURCE,
                         "subject": "notes", "aspect": "binary",
                         "payload": {"binary": "joplin"}}]

        monkeypatch.setattr(collab_mod, "Coordinator", _FakeCoordinator)
        assert cmd_desktop_aliases_sync(
            argparse.Namespace(store=None)) == 0
        assert pushed_ids == ["alias:todo app"]
        # the remote "notes" alias landed in the local store
        assert A.learned_aliases() == {"notes": "joplin",
                                       "todo app": "/usr/bin/obsidian"}
        assert "Alias sync done" in capsys.readouterr().out

    def test_cli_sync_degrades_when_collab_down(self, store, capsys,
                                                monkeypatch):
        import argparse
        import friday_v6.collab as collab_mod
        from friday_v6.cli_desktop import cmd_desktop_aliases_sync

        class _BrokenCoordinator:
            def start(self):
                raise RuntimeError("collab offline")

        monkeypatch.setattr(collab_mod, "Coordinator", _BrokenCoordinator)
        assert cmd_desktop_aliases_sync(
            argparse.Namespace(store=None)) == 1
        assert "sync" in capsys.readouterr().out.lower()


# ==========================================================================
# helpers
# ==========================================================================


def _conn(tmp_path):
    from friday_v6 import db
    return db.connect(tmp_path / "v4.db")


class _FakeWM:
    """Hermetic window-manager stand-in (never touches the real desktop)."""

    def __init__(self):
        self.focused = []
        self.launched = []
        self.switched = []

    @property
    def is_available(self):
        return True

    def focus_smart(self, query):
        self.focused.append(query)
        return None

    def launch_app(self, app):
        self.launched.append(app)
        return True

    def switch_workspace(self, ws_id):
        self.switched.append(ws_id)
        return True
