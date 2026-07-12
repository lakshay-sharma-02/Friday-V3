"""`friday analyze` CLI + cross-repository architectural similarity."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from friday import cli as cli_mod
from friday.architecture import analyze_and_store
from friday.ask import ask, classify
from friday.db import (
    ArchitectureRow,
    ComponentRow,
    EntryPointRow,
    connect,
    get_architecture,
    get_components,
    get_entry_points,
    upsert_repository,
)
from friday.query import reuse_opportunities, shared_components, similar_layouts
from friday.discovery import Repo


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "kb.db")
    yield c
    c.close()


def _seed_repo(conn, name: str, path: str, arch: str, comps: list[str], eps: list[tuple[str, str]]):
    rid = upsert_repository(
        conn, name=name, path=path, default_branch="main", is_dirty=False,
        first_commit_date="2024-01-01", last_commit_date="2026-01-01",
        remote_url=None, commit_count=10, readme_summary=None,
        license=None, primary_author=None,
    )
    conn.execute(
        "INSERT INTO architecture (repo_id, architecture, evidence) VALUES (?, ?, ?)",
        (rid, arch, f"evidence for {arch}"),
    )
    conn.executemany(
        "INSERT OR REPLACE INTO components (repo_id, name, evidence) VALUES (?, ?, ?)",
        [(rid, comp, f"{comp} evidence") for comp in comps],
    )
    conn.executemany(
        "INSERT OR REPLACE INTO entry_points (repo_id, kind, detail, evidence) VALUES (?, ?, ?, ?)",
        [(rid, kind, detail, f"{kind} evidence") for kind, detail in eps],
    )
    conn.commit()
    return rid


def test_analyze_and_store_persists(conn, tmp_path):
    repo_dir = tmp_path / "proj"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()
    (repo_dir / "cli.py").write_text("import click\n\ndef main(): pass\n")
    (repo_dir / "pyproject.toml").write_text(
        "[project]\nname='proj'\n[project.scripts]\nf='proj:main'\n"
    )
    (repo_dir / "auth.py").write_text("import jwt\n")
    profile = analyze_and_store(conn, Repo(path=repo_dir))
    rid = conn.execute("SELECT id FROM repositories WHERE path=?", (str(repo_dir),)).fetchone()["id"]
    arch = get_architecture(conn, rid)
    assert isinstance(arch, ArchitectureRow)
    assert arch.architecture == "CLI tool"
    comp_names = {c.name for c in get_components(conn, rid)}
    assert "Authentication" in comp_names
    ep_kinds = {e.kind for e in get_entry_points(conn, rid)}
    assert "CLI" in ep_kinds


def test_cli_analyze_command(tmp_path, capsys):
    repo_dir = tmp_path / "proj"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()
    (repo_dir / "cli.py").write_text("import click\n\ndef main(): pass\n")
    (repo_dir / "pyproject.toml").write_text(
        "[project]\nname='proj'\n[project.scripts]\nf='proj:main'\n"
    )
    db = tmp_path / "kb.db"
    args = type("A", (), {"repository": str(repo_dir)})()
    # Point friday at a temp DB via env so the real connect() hits it.
    import os
    old = os.environ.get("FRIDAY_DB")
    os.environ["FRIDAY_DB"] = str(db)
    try:
        rc = cli_mod.cmd_analyze(args)
    finally:
        if old is None:
            os.environ.pop("FRIDAY_DB", None)
        else:
            os.environ["FRIDAY_DB"] = old
    assert rc == 0
    out = capsys.readouterr().out
    assert "Architecture: CLI tool" in out


def test_cli_analyze_rejects_non_git(tmp_path, capsys):
    not_repo = tmp_path / "plain"
    not_repo.mkdir()
    args = type("A", (), {"repository": str(not_repo)})()
    rc = cli_mod.cmd_analyze(args)
    assert rc == 2


# --- Cross-repository similarity (query layer) ------------------------------


def test_shared_components_query(conn):
    _seed_repo(conn, "A", "/a", "CLI tool", ["Authentication", "Configuration"], [("CLI", "x")])
    _seed_repo(conn, "B", "/b", "CLI tool", ["Authentication", "Database"], [("CLI", "y")])
    shared = shared_components(conn)
    assert "Authentication" in shared
    assert set(shared["Authentication"]) == {"A", "B"}


def test_similar_layouts_query(conn):
    _seed_repo(conn, "A", "/a", "FastAPI REST API", [], [])
    _seed_repo(conn, "B", "/b", "FastAPI REST API", [], [])
    _seed_repo(conn, "C", "/c", "Library", [], [])
    pairs = similar_layouts(conn)
    pair_names = {frozenset(p) for p in pairs}
    assert frozenset({"A", "B"}) in pair_names
    assert frozenset({"A", "C"}) not in pair_names


def test_reuse_opportunities_query(conn):
    _seed_repo(conn, "A", "/a", "CLI tool", ["Configuration"], [])
    _seed_repo(conn, "B", "/b", "CLI tool", ["Configuration"], [])
    opps = reuse_opportunities(conn)
    assert any("Configuration" in o for o in opps)


# --- Ask intents ------------------------------------------------------------


def test_classify_architecture_question(conn):
    assert classify("Explain Friday's architecture.", conn) == "architecture"
    assert classify("How does Vivaha start?", conn) == "architecture"


def test_classify_similarity_question(conn):
    assert classify("Which projects duplicate configuration loading?", conn) == "similarity"
    assert classify("Which projects could realistically share code?", conn) == "similarity"
    assert classify("Which repositories have similar layouts?", conn) == "similarity"


def test_ask_architecture_explains_from_evidence(conn, tmp_path):
    _seed_repo(conn, "Vivaha", "/v", "Next.js App Router",
               ["Authentication", "Routing"], [("Next.js app", "app/")])
    ans = ask("Explain Vivaha's architecture.", conn, verbose=False)
    assert not ans.used_llm
    assert "Next.js App Router" in ans.text
    assert "Authentication" in ans.text
    assert "Next.js app" in ans.text


def test_ask_similarity_honest_when_empty(conn, tmp_path):
    _seed_repo(conn, "Vivaha", "/v", "Next.js App Router", ["Routing"], [])
    ans = ask("Which projects could realistically share code?", conn, verbose=False)
    assert not ans.used_llm
    # Only one repo -> no cross-repo similarity possible.
    assert "No evidence-backed" in ans.text or "similar" in ans.text.lower()


def test_ask_similarity_finds_shared_components(conn, tmp_path):
    _seed_repo(conn, "A", "/a", "CLI tool", ["Configuration", "Authentication"], [("CLI", "x")])
    _seed_repo(conn, "B", "/b", "CLI tool", ["Configuration", "Database"], [("CLI", "y")])
    ans = ask("Which projects duplicate configuration loading?", conn, verbose=False)
    assert not ans.used_llm
    assert "Configuration" in ans.text
    assert "A" in ans.text and "B" in ans.text
