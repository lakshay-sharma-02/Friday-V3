"""HUD parser tests — pure, no Textual import (hermetic)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from friday_v5.hud.parsers import parse_schedule, parse_notice_text, tail_log


def test_parse_schedule_skips_frontmatter_and_done(tmp_path):
    p = tmp_path / "schedule.md"
    p.write_text("---\nstatus: active\n---\n\n"
                 "- 2026-08-05 09:00 — Standup (15 min)\n"
                 "- [x] 2026-08-04 10:00 — done thing\n",
                 encoding="utf-8")
    items = parse_schedule(p)
    assert items == ["2026-08-05 09:00 — Standup (15 min)"]


def test_parse_schedule_missing_file(tmp_path):
    assert parse_schedule(tmp_path / "nope.md") == []


def test_parse_notice_text_strips_meta(tmp_path):
    p = tmp_path / "1700000000-hello.md"
    p.write_text("# Notice\n\n- **at**: 2023\n- **id**: 1700000000\n\n"
                 "standup at 9am\n", encoding="utf-8")
    assert parse_notice_text(p) == "standup at 9am"


def test_tail_log_last_lines(tmp_path):
    p = tmp_path / "2026-08-04.log"
    p.write_text("line1\nline2\nline3\n", encoding="utf-8")
    assert tail_log(p, 2) == ["line2", "line3"]
    assert tail_log(tmp_path / "missing.log", 2) == []
