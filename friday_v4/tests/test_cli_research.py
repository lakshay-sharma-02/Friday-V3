"""Hermetic tests for the Wave 11 research CLI + NL routing."""

from __future__ import annotations

from pathlib import Path

import pytest

from friday_v4 import cli_research
from friday_v4.nl_router import TextCommandHandler


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "vivaha"
    (r / "src").mkdir(parents=True)
    (r / "src" / "auth.py").write_text("from fastapi import FastAPI\n")
    (r / "pyproject.toml").write_text("[project]\ndeps = [\"fastapi\"]\n")
    return r


def test_cli_analyze(repo: Path, capsys):
    rc = cli_research.cmd_analyze(__import__("argparse").Namespace(
        repo=str(repo), json=False))
    out = capsys.readouterr().out
    assert rc == 0
    assert "vivaha" in out


def test_cli_analyze_json(repo: Path, capsys):
    rc = cli_research.cmd_analyze(__import__("argparse").Namespace(
        repo=str(repo), json=True))
    assert rc == 0
    assert '"available": true' in capsys.readouterr().out


def test_cli_correlate(repo: Path, tmp_path: Path, capsys):
    b = tmp_path / "mindwell"
    (b / "src").mkdir(parents=True)
    (b / "src" / "auth.py").write_text("import fastapi\n")
    rc = cli_research.cmd_correlate(__import__("argparse").Namespace(
        a=str(repo), b=str(b), json=False))
    out = capsys.readouterr().out
    assert rc == 0
    assert "auth.py" in out


def test_cli_briefing(tmp_path: Path, capsys):
    from friday_v4 import db
    conn = db.connect(tmp_path / "v4.db")
    db.create_mission(conn, "ship auth")
    conn.close()
    rc = cli_research.cmd_briefing(__import__("argparse").Namespace(
        kind="morning", db=tmp_path / "v4.db", json=False))
    assert rc == 0
    assert "Friday" in capsys.readouterr().out


def test_cli_narrative(tmp_path: Path, capsys):
    rc = cli_research.cmd_narrative(__import__("argparse").Namespace(
        date="", db=tmp_path / "v4.db", json=False))
    assert rc == 0


def test_cli_report(capsys):
    rc = cli_research.cmd_report(__import__("argparse").Namespace(
        title="Security", items=["vulns=2 high", "grade=A"], json=False,
        daily=False, weekly=False))
    assert rc == 0
    assert "# Security" in capsys.readouterr().out


def test_cli_research_main_standalone(repo: Path, capsys):
    """`friday4 research analyze X` works (regression: the standalone
    `main()` used to pass a bare ArgumentParser to build_research_parser
    and crashed with AttributeError on every invocation)."""
    rc = cli_research.main(["analyze", str(repo)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "vivaha" in out


def test_cli_research_main_briefing(tmp_path: Path, capsys):
    from friday_v4 import db
    conn = db.connect(tmp_path / "v4.db")
    db.create_mission(conn, "ship auth")
    conn.close()
    rc = cli_research.main(["briefing", "morning", "--db",
                            str(tmp_path / "v4.db")])
    assert rc == 0
    assert "Friday" in capsys.readouterr().out


def test_nl_research_routes_to_correlate(tmp_path: Path, capsys):
    """Law 1: `friday4 talk "analyze X vs Y"` reaches the research layer."""
    from friday_v4 import db
    conn = db.connect(tmp_path / "v4.db")
    a = tmp_path / "vivaha"
    b = tmp_path / "mindwell"
    for r in (a, b):
        (r / "src").mkdir(parents=True)
        (r / "src" / "auth.py").write_text("import fastapi\n")
    handler = TextCommandHandler(conn, llm=None)  # deterministic fallback
    result = handler.handle(f"analyze {a} vs {b}")
    assert result.intent == "research"
    assert result.action == "chat"
    assert "auth.py" in result.response
    conn.close()


def test_nl_briefing_route(tmp_path: Path):
    from friday_v4 import db
    conn = db.connect(tmp_path / "v4.db")
    db.create_mission(conn, "ship auth")
    handler = TextCommandHandler(conn, llm=None)
    result = handler.handle("brief me this morning")
    assert result.intent == "research"
    assert "mission" in result.response.lower()
    conn.close()
