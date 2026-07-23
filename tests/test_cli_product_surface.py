"""Regression: product-surface CLI for identity / portfolio / strategy.

Verifies the three new commands expose existing capabilities without new logic,
appear in `friday --help`, and that the insight pipeline routes correctly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from friday import cli as cli_mod
from friday.db import connect, upsert_repository

import os


def _friday_db(tmp_path: Path) -> str:
    return str(tmp_path / "kb.db")


def _seed(conn, name, *, readme, techs=(), arch=None, commit_count=10):
    rid = upsert_repository(
        conn, name=name, path=f"/{name.lower()}", default_branch="main",
        is_dirty=False, first_commit_date="2024-01-01", last_commit_date="2026-01-01",
        remote_url=None, commit_count=commit_count, readme_summary=readme,
        license=None, primary_author=None,
    )
    if arch:
        conn.execute(
            "INSERT INTO architecture (repo_id, architecture, evidence) VALUES (?,?,?)",
            (rid, arch, "x"),
        )
    for t in techs:
        conn.execute(
            "INSERT INTO technologies (repo_id, tech, evidence) VALUES (?,?,?)",
            (rid, t, "x"),
        )
    conn.commit()
    return rid


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "kb.db")
    yield c
    c.close()


@pytest.fixture
def seeded(tmp_path):
    c = connect(tmp_path / "kb.db")
    _seed(c, "Aether",
          readme="Purpose:\nAether is an AI-native operating system.\nValue: enables persistent local intelligence",
          techs=("Rust", "Python"), arch="Operating-system kernel")
    _seed(c, "Friday",
          readme="Purpose:\nFriday is an AI operating partner for engineers.\nValue: reduces onboarding time",
          techs=("Python", "FastAPI"), arch="FastAPI REST API", commit_count=40)
    yield c
    c.close()


def test_help_lists_new_product_commands(capsys):
    with pytest.raises(SystemExit) as exc:
        cli_mod.main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for cmd in ("identity", "portfolio", "strategy"):
        assert cmd in out, f"{cmd} missing from friday --help"


def test_help_for_each_new_command(capsys):
    for cmd in ("identity", "portfolio", "strategy"):
        with pytest.raises(SystemExit) as exc:
            cli_mod.main([cmd, "--help"])
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "usage" in out.lower()


def test_identity_list_and_explain(seeded, capsys):
    rc = cli_mod.cmd_identity(type("A", (), {})())
    assert rc == 0
    out = capsys.readouterr().out
    assert "Aether" in out and "Friday" in out

    rc = cli_mod.cmd_identity(type("A", (), {"project": "Aether"})())
    assert rc == 0
    out = capsys.readouterr().out
    assert "AI-native operating system" in out  # existing explain text
    assert "Confidence" in out


def test_identity_explain_unknown_project(seeded, capsys):
    rc = cli_mod.cmd_identity(type("A", (), {"project": "Nope"})())
    assert rc == 2
    err = capsys.readouterr().err
    assert "not found" in err


def test_portfolio_overview_and_subcommands(seeded, capsys):
    rc = cli_mod.cmd_portfolio_dispatch(type("A", (), {})())
    assert rc == 0
    out = capsys.readouterr().out
    assert "Workspace overview" in out

    for sub in ("themes", "overlap", "ranking", "recommendations", "integrations"):
        rc = cli_mod.cmd_portfolio_dispatch(type("A", (), {"token": sub})())
        assert rc == 0, sub
        capsys.readouterr()


def test_strategy_axes(seeded, capsys):
    # Default (no axis) -> converging thesis.
    rc = cli_mod.cmd_strategy(type("A", (), {})())
    assert rc == 0
    capsys.readouterr()
    for axis in ("impact", "platform", "learning", "opportunity", "priority", "merge", "converge"):
        rc = cli_mod.cmd_strategy(type("A", (), {"token": axis})())
        assert rc == 0, axis
        out = capsys.readouterr().out
        assert "Recommendation:" in out  # existing Judgment.render() output


def test_strategy_unknown_axis(seeded, capsys):
    rc = cli_mod.cmd_strategy(type("A", (), {"token": "bogus"})())
    assert rc == 2
    err = capsys.readouterr().err
    assert "unknown strategy axis" in err


def test_insight_routes_to_insight_engine():
    # The old insights.py has been replaced by the new insight/ package.
    # _p_insights (ask.py) and the objective layer now use InsightEngine.
    import inspect
    ask_src = inspect.getsource(__import__("friday.ask", fromlist=["x"]))
    # ask.py must use the new InsightEngine, not the old insights module.
    assert "from .insight import InsightEngine" in ask_src
    assert "from .insights import" not in ask_src
