"""Hermetic tests for the screen layer — Friday's eyes and hands (Wave 23).

Never touches a real display: tesseract TSV is fed as strings, the
controller's subprocess work goes through a fake runner, and the NL
router is exercised with a tmp DB like every other surface. The one
thing NOT tested here is real capture/OCR/input (that needs a live
desktop) — the tool-gate logic that decides *how* Friday acts is
fully covered.
"""

from __future__ import annotations

import shutil
from pathlib import Path as _Path

import pytest

# ── fixtures ────────────────────────────────────────────────────────

_TSV = """level\tpage\tblock\tpar\tline\tword\tleft\ttop\twidth\theight\tconf\ttext
5\t1\t1\t1\t1\t1\t10\t20\t40\t15\t96\tLogin
5\t1\t1\t1\t1\t2\t55\t20\t50\t15\t95\tbutton
5\t1\t1\t1\t2\t1\t10\t60\t60\t15\t93\tSign
5\t1\t1\t1\t2\t2\t75\t60\t40\t15\t90\tup
5\t1\t1\t1\t3\t1\t10\t100\t70\t15\t88\tForgot
5\t1\t1\t1\t3\t2\t85\t100\t80\t15\t85\tpassword?
5\t1\t1\t2\t1\t1\t300\t200\t100\t20\t97\tSubmit
"""


@pytest.fixture
def words():
    from friday_v6.screen.parsers import parse_ocr_tsv
    return parse_ocr_tsv(_TSV)


class FakeRunner:
    """Replays canned (rc, stdout, stderr) per command, records calls."""

    def __init__(self, mapping):
        self._mapping = mapping
        self.calls: list[list[str]] = []

    def __call__(self, cmd, timeout=15):
        self.calls.append(cmd)
        for key, value in self._mapping.items():
            if cmd[0] == key:
                return value
        return (-1, "", f"no canned response for {cmd[0]}")


@pytest.fixture
def no_tools(monkeypatch):
    """Pin shutil.which → None so tests never find REAL desktop tools.

    The test machine HAS wtype/xdotool/tesseract installed — without
    this, InputController/ScreenController pick the real tools and the
    fake-runner assertions break. Every controller test that cares
    which tool is chosen pins which() explicitly.
    """
    monkeypatch.setattr("shutil.which", lambda _name: None)
    return monkeypatch


# ── parsers: OCR TSV ────────────────────────────────────────────────


class TestOcrParsing:
    def test_parses_words_with_boxes(self, words):
        assert len(words) == 7
        first = words[0]
        assert first.text == "Login"
        assert (first.left, first.top) == (10, 20)
        assert first.center == (10 + 20, 20 + 7)  # 30, 27

    def test_skips_non_word_rows_and_malformed(self):
        from friday_v6.screen.parsers import parse_ocr_tsv
        messy = "level\tpage\tblock\tpar\tline\tword\tleft\ttop\twidth\theight\tconf\ttext\n" \
                "4\t1\t1\t1\t1\t1\t10\t20\t40\t15\t96\tLine\n" \
                "5\t1\t1\t1\t2\t1\t10\t60\t60\t15\t93\tok\n" \
                "garbage-line\n" \
                "5\t1\t1\t1\t3\t1\t10\t100\t70\t15\t-1\tnegconf\n"
        out = parse_ocr_tsv(messy)
        assert [w.text for w in out] == ["ok"]

    def test_empty_input_is_empty(self):
        from friday_v6.screen.parsers import parse_ocr_tsv
        assert parse_ocr_tsv("") == []
        assert parse_ocr_tsv(None) == []

    def test_find_click_target_exact_word(self, words):
        from friday_v6.screen.parsers import find_click_target
        hit = find_click_target(words, "login")
        assert hit is not None and hit.text == "Login"

    def test_find_click_target_punct_normalized(self, words):
        from friday_v6.screen.parsers import find_click_target
        hit = find_click_target(words, "password?")
        assert hit is not None and hit.text == "password?"

    def test_find_click_target_never_substring(self, words):
        # "log" must NOT match "Login" (substring of a word).
        from friday_v6.screen.parsers import find_click_target
        assert find_click_target(words, "log") is None

    def test_find_phrase_region_spans_words(self, words):
        from friday_v6.screen.parsers import find_phrase_region
        region = find_phrase_region(words, "login button")
        assert region is not None
        assert [w.text for w in region] == ["Login", "button"]

    def test_find_phrase_missing_term_is_none(self, words):
        from friday_v6.screen.parsers import find_phrase_region
        assert find_phrase_region(words, "login banana") is None


# ── parsers: NL intent detection ────────────────────────────────────


class TestScreenIntentParsing:
    def test_read_phrases(self):
        from friday_v6.screen.parsers import parse_screen_intent
        for phrase in ("what's on my screen", "what is on my screen",
                       "read my screen"):
            intent = parse_screen_intent(phrase)
            assert intent is not None and intent.action == "read"

    def test_click_with_screen_target(self):
        from friday_v6.screen.parsers import parse_screen_intent
        intent = parse_screen_intent("click the login button")
        assert intent is not None
        assert intent.action == "click"
        assert intent.target == "login button"

    def test_click_variants(self):
        from friday_v6.screen.parsers import parse_screen_intent
        assert parse_screen_intent("click on the submit button").target \
            == "submit button"
        assert parse_screen_intent("tap the search box").target \
            == "search box"
        assert parse_screen_intent("select the save option").target \
            == "save option"

    def test_click_web_destination_never_hijacked(self):
        # "click on youtube" is a web destination the desktop layer opens.
        from friday_v6.screen.parsers import parse_screen_intent
        assert parse_screen_intent("click on youtube") is None
        assert parse_screen_intent("click here") is None

    def test_type_into_target(self):
        from friday_v6.screen.parsers import parse_screen_intent
        intent = parse_screen_intent("type hello into the search box")
        assert intent is not None
        assert intent.action == "type"
        assert intent.target == "search box"
        assert intent.detail == "hello"

    def test_type_plain(self):
        from friday_v6.screen.parsers import parse_screen_intent
        intent = parse_screen_intent("type hello world")
        assert intent is not None
        assert intent.action == "type"
        assert intent.target == ""
        assert intent.detail == "hello world"

    def test_scroll(self):
        from friday_v6.screen.parsers import parse_screen_intent
        assert parse_screen_intent("scroll down").detail == "down"
        assert parse_screen_intent("scroll up").detail == "up"

    def test_key_press(self):
        from friday_v6.screen.parsers import parse_screen_intent
        assert parse_screen_intent("press enter").target == "enter"
        assert parse_screen_intent("press ctrl+c").target == "ctrl+c"

    def test_ordinary_chat_not_hijacked(self):
        from friday_v6.screen.parsers import parse_screen_intent
        phrases = ("run the tests", "git status", "open whatsapp",
                   "ship the auth refactor",
                   ("what's the deal between "
                    "x and y"), "help me", "scroll of truth")
        for phrase in phrases:
            assert parse_screen_intent(phrase) is None


# ── controller: fake runner, honest degrades ────────────────────────


class TestScreenController:
    def test_ocr_parses_canned_tsv(self, monkeypatch):
        screen = _screen_with_tsv(_TSV, monkeypatch)
        res = screen.ocr("some.png")
        assert res.ok
        assert len(res.words or []) == 7

    def test_ocr_missing_tool_is_honest(self, monkeypatch):
        from friday_v6 import screen as screen_pkg
        from friday_v6.screen.controller import ScreenController
        monkeypatch.setattr(screen_pkg.controller.shutil, "which",
                            lambda _name: None)
        res = ScreenController(runner=FakeRunner({})).ocr("x.png")
        assert not res.ok
        assert "tesseract" in res.message

    def test_find_returns_center(self, monkeypatch):
        screen = _screen_with_tsv(_TSV, monkeypatch)
        res = screen.find("login button")
        assert res.ok
        # "Login button" line spans x 10..105, top 20..35 → center.
        assert res.position == ((10 + 105) // 2, (20 + 35) // 2)

    def test_find_missing_is_honest(self, monkeypatch):
        screen = _screen_with_tsv(_TSV, monkeypatch)
        res = screen.find("banana")
        assert not res.ok
        assert "banana" in res.message

    def test_capture_uses_grim(self, monkeypatch, tmp_path):
        from friday_v6 import screen as screen_pkg
        from friday_v6.screen.controller import ScreenController
        target = tmp_path / "shot.png"

        def _fake_which(name):
            return "/usr/bin/grim" if name == "grim" else None
        monkeypatch.setattr(screen_pkg.controller.shutil, "which",
                            _fake_which)

        def _fake_runner(cmd, timeout=15):
            if cmd[0] == "grim":
                _Path(cmd[1]).write_bytes(b"png")
                return 0, "", ""
            return -1, "", "no canned response"
        screen = ScreenController(runner=_fake_runner)
        res = screen.capture(str(target))
        assert res.ok and res.image_path == str(target)

    def test_capture_missing_tool_is_honest(self, no_tools):
        from friday_v6.screen.controller import ScreenController
        res = ScreenController(runner=FakeRunner({})).capture("/tmp/x.png")
        assert not res.ok
        assert "no screen capture tool" in res.message


def _screen_with_tsv(tsv: str, monkeypatch=None):
    from friday_v6.screen.controller import ScreenController
    if monkeypatch is not None:
        # Merge into any existing which() pin (tests that also pin the
        # input tools call this AFTER their own setattr).
        _prev = shutil.which

        def _which(name):
            mine = {"tesseract": "/usr/bin/tesseract",
                    "grim": "/usr/bin/grim"}.get(name)
            if mine:
                return mine
            return _prev(name)
        monkeypatch.setattr("shutil.which", _which)

    def _fake_runner(cmd, timeout=15):
        # Real grim writes the PNG; so does this fake (capture() checks
        # Path.exists(), exactly like production).
        if cmd[0] == "grim":
            _Path(cmd[1]).parent.mkdir(parents=True, exist_ok=True)
            _Path(cmd[1]).write_bytes(b"png")
            return 0, "", ""
        if cmd[0] == "tesseract":
            return 0, tsv, ""
        return -1, "", f"no canned response for {cmd[0]}"

    return ScreenController(runner=_fake_runner)



class TestInputController:
    def test_click_uses_ydotool_relative(self, no_tools):
        from friday_v6.screen.controller import InputController
        no_tools.setattr(
            "shutil.which",
            lambda n: "/usr/bin/ydotool" if n == "ydotool" else None)
        runner = FakeRunner({"ydotool": (0, "", "")})
        ctl = InputController(runner=runner, screen_size=(1920, 1080))
        res = ctl.click(960, 540)
        assert res.ok
        mousemove = runner.calls[0]
        assert mousemove[0] == "ydotool"
        assert mousemove[1] == "mousemove"
        # 960/1920 = 0.5 → 32767 (of 65535)
        assert int(mousemove[3]) == 32767

    def test_click_no_tool_is_honest(self, no_tools):
        from friday_v6.screen.controller import InputController
        res = InputController(runner=FakeRunner({})).click(10, 10)
        assert not res.ok
        assert "ydotool or xdotool" in res.message

    def test_type_uses_wtype(self, no_tools):
        from friday_v6.screen.controller import InputController
        no_tools.setattr(
            "shutil.which",
            lambda n: "/usr/bin/wtype" if n == "wtype" else None)
        runner = FakeRunner({"wtype": (0, "", "")})
        res = InputController(runner=runner).type_text("hello")
        assert res.ok
        assert runner.calls[0][0] == "wtype"
        assert runner.calls[0][1] == "hello"

    def test_press_maps_key(self, no_tools):
        from friday_v6.screen.controller import InputController
        no_tools.setattr(
            "shutil.which",
            lambda n: "/usr/bin/xdotool" if n == "xdotool" else None)
        runner = FakeRunner({"xdotool": (0, "", "")})
        res = InputController(runner=runner).press("enter")
        assert res.ok
        assert runner.calls[0] == ["xdotool", "key", "Return"]

    def test_press_unknown_key_is_honest(self, no_tools):
        from friday_v6.screen.controller import InputController
        res = InputController(runner=FakeRunner({})).press("banana")
        assert not res.ok
        assert "don't know the key" in res.message

    def test_scroll_failure_reports(self, no_tools):
        from friday_v6.screen.controller import InputController
        no_tools.setattr(
            "shutil.which",
            lambda n: "/usr/bin/xdotool" if n == "xdotool" else None)
        runner = FakeRunner({"xdotool": (1, "", "boom")})
        res = InputController(runner=runner).scroll("down")
        assert not res.ok


# ── NL handler: confirm gate + fallback ─────────────────────────────


class TestScreenTextHandler:
    def _handler(self, screen=None, input_ctl=None, fallback=None,
                 confirm=None):
        from friday_v6.screen.nl import ScreenTextHandler
        return ScreenTextHandler(screen=screen, input_ctl=input_ctl,
                                 desktop_fallback=fallback, confirm_fn=confirm)

    def test_read_returns_ocr_lines(self, monkeypatch):
        screen = _screen_with_tsv(_TSV, monkeypatch)
        reply = self._handler(screen=screen).handle("what's on my screen")
        assert "Here's what's on your screen" in reply
        assert "Login button" in reply

    def test_click_requires_confirm_and_acts_on_yes(self, monkeypatch):
        from friday_v6.screen.controller import InputController
        # Pin the WHOLE tool map (screen OCR + input) so no real tool is
        # ever consulted: tesseract+grim for the screen, ydotool for the
        # input controller. `_screen_with_tsv` adds tesseract+grim on
        # top (its setattr runs after this one).
        monkeypatch.setattr(
            "shutil.which",
            lambda n: "/usr/bin/ydotool" if n == "ydotool" else None)
        screen = _screen_with_tsv(_TSV, monkeypatch)
        input_ctl = InputController(runner=FakeRunner({"ydotool": (0, "", "")}),
                                    screen_size=(1920, 1080))
        asked = []
        handler = self._handler(
            screen=screen, input_ctl=input_ctl,
            confirm=lambda desc: asked.append(desc) or True)
        reply = handler.handle("click the login button")
        assert asked, "the action must ask first"
        assert reply == "Clicked login button."

    def test_click_without_confirm_refuses_honestly(self, monkeypatch):
        screen = _screen_with_tsv(_TSV, monkeypatch)
        reply = self._handler(screen=screen).handle("click the login button")
        assert "May I" in reply
        assert "login button" in reply

    def test_fallback_for_non_screen_phrase(self, no_tools):
        from friday_v6.screen.controller import InputController
        handler = self._handler(
            input_ctl=InputController(runner=FakeRunner({})),
            fallback=lambda t: f"desktop handled: {t}")
        assert handler.handle("open whatsapp") == "desktop handled: open whatsapp"

    def test_missing_target_is_honest(self, monkeypatch):
        screen = _screen_with_tsv(_TSV, monkeypatch)
        reply = self._handler(screen=screen).handle("click the banana button")
        assert "can't see" in reply

    def test_module_level_entry(self, monkeypatch):
        # screen_text_command with no confirm → click refuses; the read
        # path works through the injected screen (never real tesseract).
        from friday_v6.screen import nl as nl_mod
        screen = _screen_with_tsv(_TSV, monkeypatch)
        monkeypatch.setattr(nl_mod, "ScreenController", lambda **kw: screen)
        from friday_v6.screen.nl import screen_text_command
        reply = screen_text_command("click the login button")
        assert "May I" in reply


# ── NL router integration ───────────────────────────────────────────


class TestRouterIntegration:
    def test_screen_pre_dispatch_routes_to_screen_handler(self):
        calls = []

        def screen_handler(text):
            calls.append(text)
            return "screen said: ok"

        from friday_v6.nl_router import TextCommandHandler
        handler = TextCommandHandler(screen_handler=screen_handler)
        result = handler.handle("click the login button")
        assert result.intent == "screen"
        assert calls == ["click the login button"]

    def test_screen_read_works_end_to_end(self, monkeypatch):
        from friday_v6.nl_router import TextCommandHandler
        from friday_v6.screen import ScreenTextHandler
        screen = _screen_with_tsv(_TSV, monkeypatch)
        # screen_handler is a CALLABLE (str) -> str — wrap the instance.
        handler = TextCommandHandler(
            screen_handler=ScreenTextHandler(screen=screen).handle)
        result = handler.handle("what's on my screen")
        assert result.intent == "screen"
        assert "Here's what's on your screen" in result.response

    def test_desktop_phrases_unaffected_without_screen_handler(self):
        # No screen handler wired → screen phrases fall back to desktop
        # (which here is also unwired → the honest desktop message).
        from friday_v6.nl_router import TextCommandHandler
        handler = TextCommandHandler()
        result = handler.handle("open whatsapp")
        assert result.intent == "desktop"
        # And an explicit screen phrase with NO handler still answers
        # honestly (never crashes).
        result2 = handler.handle("click the login button")
        assert result2.intent == "desktop"
        assert result2.response  # honest desktop message


# ── CLI round-trip ──────────────────────────────────────────────────


class TestCliScreen:
    def test_status_lists_capabilities(self, capsys):
        from friday_v6.cli_screen import main
        assert main(["status"]) == 0
        out = capsys.readouterr().out
        assert "capture" in out or "ocr" in out

    def test_ocr_subcommand_parses(self, capsys, monkeypatch):
        screen = _screen_with_tsv(_TSV, monkeypatch)
        import friday_v6.cli_screen as cli
        monkeypatch.setattr(cli, "ScreenController",
                            lambda **kw: screen)
        assert cli.main(["ocr", "--out", "/tmp/screen"]) == 0
        out = capsys.readouterr().out
        assert "Login button" in out

    def test_find_subcommand_finds(self, capsys, monkeypatch):
        screen = _screen_with_tsv(_TSV, monkeypatch)
        import friday_v6.cli_screen as cli
        monkeypatch.setattr(cli, "ScreenController",
                            lambda **kw: screen)
        assert cli.main(["find", "login button", "--out", "/tmp/screen"]) == 0
        out = capsys.readouterr().out
        assert "Found 'login button'" in out

    def test_click_yes_flag_acts(self, capsys, no_tools):
        from friday_v6.screen.controller import InputController
        no_tools.setattr(
            "shutil.which",
            lambda n: "/usr/bin/ydotool" if n == "ydotool" else None)
        runner = FakeRunner({"ydotool": (0, "", "")})
        input_ctl = InputController(runner=runner, screen_size=(1920, 1080))
        import friday_v6.cli_screen as cli
        monkeypatch = no_tools
        monkeypatch.setattr(cli, "InputController", lambda: input_ctl)
        assert cli.main(["click", "960", "540", "--yes"]) == 0
        out = capsys.readouterr().out
        assert "clicked" in out

    def test_cli_screen_registered_in_integrated_cli(self):
        from friday_v6.cli_talk import _SUBCOMMANDS
        assert "screen" in _SUBCOMMANDS
