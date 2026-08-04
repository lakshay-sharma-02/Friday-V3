"""CLI hud command test — parse args, mock run_hud (no Textual)."""
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from friday_v5.cli import build_parser


def test_hud_command_parses():
    args = build_parser().parse_args(["hud"])
    assert args.cmd == "hud"


def test_hud_command_calls_run_hud(tmp_path, monkeypatch):
    from friday_v5 import cli
    fake = mock.Mock(return_value=0)
    monkeypatch.setitem(sys.modules, "friday_v5.hud", mock.Mock(run_hud=fake))
    rc = cli._cmd_hud(None)
    assert rc == 0
    fake.assert_called_once()
