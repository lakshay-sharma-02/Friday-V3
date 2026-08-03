"""Hermetic tests for the Wave 11 synthesis + briefing layers."""

from __future__ import annotations

from pathlib import Path

import pytest

from friday_v4 import db
from friday_v4.synthesis import synthesize


# ── synthesis ─────────────────────────────────────────────────────────


def test_synthesis_deterministic():
    r1 = synthesize("Digest", {"vulns": ["2 high", "1 medium"]}, generated_at="t")
    r2 = synthesize("Digest", {"vulns": ["2 high", "1 medium"]}, generated_at="t")
    assert r1.to_dict() == r2.to_dict()


def test_synthesis_never_invents_empty_sections():
    r = synthesize("Empty", {"nothing": []}, generated_at="t")
    assert "nothing yet" in r.render()


def test_synthesis_render_has_title():
    r = synthesize("Security", {"vulns": ["1 critical"]}, generated_at="t")
    assert "# Security" in r.render()
    assert "1 critical" in r.render()


# ── briefing ──────────────────────────────────────────────────────────


@pytest.fixture
def conn(tmp_path: Path):
    c = db.connect(tmp_path / "v4.db")
    yield c
    c.close()


def test_briefing_real_state(conn):
    db.create_mission(conn, "auth refactor")
    db.record_action(conn, "shell", goal="run tests", command="pytest -q")
    db.finish_action(conn, "shell", "succeeded")
    from friday_v4.briefing import build_briefing
    b = build_briefing(conn, kind="morning")
    assert b.kind == "morning"
    assert "mission" in b.text.lower()
    assert b.sections  # built from real state


def test_briefing_quiet_when_empty(conn):
    from friday_v4.briefing import build_briefing
    b = build_briefing(conn, kind="evening")
    assert b.text  # honest "nothing to report" — never fluff


def test_narrative_from_audit(conn):
    db.record_action(conn, "git", goal="check status", command="status")
    db.finish_action(conn, "git", "succeeded")
    from friday_v4.briefing import day_narrative
    n = day_narrative(conn)
    assert n.entries  # the day's story from the audit log
