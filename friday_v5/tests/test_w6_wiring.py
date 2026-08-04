"""W6 wiring tests — notifier root/collision, proactive watch, HUD stream."""
import sys
import time
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from friday_v5.voice.notifier import VoiceNotifier
from friday_v5.proactive import Proactive


def test_notifier_default_root_is_vault(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    n = VoiceNotifier()  # no vault_root → default
    assert n.vault_root == Path(__file__).resolve().parent.parent / "vault"


def test_notifier_collision_disambiguates(tmp_path):
    n = VoiceNotifier(vault_root=tmp_path)
    n.speak = mock.Mock()
    with mock.patch("time.time", return_value=1700000000.0):
        p1 = n.notify("standup at 9am")
        p2 = n.notify("standup at 9am")
    assert p1 != p2
    assert len(list((tmp_path / "notices").glob("*.md"))) == 2


def test_proactive_seen_mark_seen(tmp_path):
    p = Proactive(vault_root=tmp_path, interval=0.05)
    nid = 1700000000
    (tmp_path / "notices").mkdir(parents=True, exist_ok=True)
    (tmp_path / "notices" / f"{nid}-hello.md").write_text("hi", encoding="utf-8")
    assert p.seen() == set()
    p.mark_seen(nid)
    assert p.seen() == {nid}
    assert p.check() == []  # already seen → no new


from friday_v5.hud.stream_panel import render_stream
from friday_v5.hud.commands_panel import render_commands


def test_render_stream_lines():
    out = render_stream([("you: standup at 9am", False),
                         ("ok, added", True)])
    assert "standup" in out and "ok, added" in out


def test_render_stream_empty():
    assert "(idle)" in render_stream([])


def test_render_commands():
    out = render_commands()
    assert "ask" in out and "perm" in out and "end" in out and "quit" in out


def test_prompt_panel_forwards_stream(tmp_path):
    from friday_v5.hud.prompt import PromptPanel
    engine = mock.Mock()
    engine.vault = mock.Mock()
    pp = PromptPanel(engine)
    got = []
    pp.on_output = got.append
    pp.push("ok, added", final=True)
    assert got == [("ok, added", True)]
    assert pp._output is not None  # composed Static exists
