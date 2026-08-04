"""Notifier tests — pure, TTS + time mocked (no audio, no SDK)."""
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from friday_v5.voice.notifier import VoiceNotifier


def _make(tmp_path):
    n = VoiceNotifier(vault_root=tmp_path)
    n.speak = mock.Mock()
    return n


def test_writes_notice_and_speaks(tmp_path):
    n = _make(tmp_path)
    with mock.patch("time.time", return_value=1700000000.0):
        n.notify("standup at 9am", on_notice=None)
    files = list((tmp_path / "notices").glob("*.md"))
    assert len(files) == 1
    assert "standup at 9am" in files[0].read_text()
    n.speak.assert_called_once_with("standup at 9am")


def test_on_notice_callback_fired(tmp_path):
    n = _make(tmp_path)
    got = []
    n.notify("hello", on_notice=got.append)
    assert len(got) == 1 and got[0]["text"] == "hello"
