"""HUD panel render tests — pure formatting, Textual not required."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from friday_v5.hud.schedule_panel import render_schedule
from friday_v5.hud.activity_panel import render_activity
from friday_v5.hud.notices_panel import render_notices


def test_render_schedule_lines():
    out = render_schedule(["2026-08-05 09:00 — Standup (15 min)"])
    assert "Standup" in out and "09:00" in out


def test_render_schedule_empty():
    assert "nothing scheduled" in render_schedule([])


def test_render_activity_lines():
    out = render_activity(["[09:12] user  standup at 9am"])
    assert "[09:12]" in out


def test_render_notices():
    out = render_notices([{"text": "standup at 9am"}])
    assert "standup at 9am" in out
