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
    # Concept component names are Weak evidence (audit: a name is not an
    # implementation). Mirror the production detector's strength assignment.
    from friday import judgment
    conn.executemany(
        "INSERT OR REPLACE INTO components (repo_id, name, evidence, strength) VALUES (?, ?, ?, ?)",
        [(rid, comp, f"{comp} evidence", judgment.component_strength(comp)) for comp in comps],
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


def test_shared_components_query_excludes_weak_concepts(conn):
    # Concept components are Weak; shared_components(min_strength=Medium) must
    # exclude them so they never drive a reuse recommendation.
    _seed_repo(conn, "A", "/a", "CLI tool", ["Authentication", "Configuration"], [("CLI", "x")])
    _seed_repo(conn, "B", "/b", "CLI tool", ["Authentication", "Database"], [("CLI", "y")])
    shared = shared_components(conn)  # default Medium -> excludes concepts
    assert "Authentication" not in shared
    assert "Configuration" not in shared
    assert "Database" not in shared
    # With Weak allowed, they reappear (plain inventory).
    weak = shared_components(conn, min_strength="Weak")
    assert "Authentication" in weak


def test_similar_layouts_query(conn):
    _seed_repo(conn, "A", "/a", "FastAPI REST API", [], [])
    _seed_repo(conn, "B", "/b", "FastAPI REST API", [], [])
    _seed_repo(conn, "C", "/c", "Library", [], [])
    pairs = similar_layouts(conn)
    pair_names = {frozenset(p) for p in pairs}
    assert frozenset({"A", "B"}) in pair_names
    assert frozenset({"A", "C"}) not in pair_names


def test_analyze_then_explain_library_bare_repo_no_crash(conn, tmp_path):
    # Regression: `friday analyze` upserts a repo row with readme_summary=None,
    # so build_identity -> recover_purpose hits its OWN fallback chain (not the
    # ingest path). A bare repo classified "Library" with a descriptive name and
    # no README must not crash on a 2-tuple return (was: "Medium" appended into
    # sources instead of returned as confidence). Reproduces the standalone
    # `friday analyze <path>` CLI path, which never populates readme_summary.
    repo_dir = tmp_path / "finance-tracker"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()
    # A [project] pyproject with source modules but NO [project.scripts] and no
    # `description` -> classifies as "Library" architecture, yet purpose recovery
    # finds no README and no manifest description, so it falls back to the layout
    # hint (Low) and then the architecture-hint lift to "Library"/Medium.
    (repo_dir / "pyproject.toml").write_text(
        "[project]\nname='finance-tracker'\nversion='0.1.0'\n"
    )
    (repo_dir / "__init__.py").write_text("")
    (repo_dir / "core.py").write_text("def run(): pass\n")
    analyze_and_store(conn, Repo(path=repo_dir))
    rid = conn.execute(
        "SELECT id FROM repositories WHERE path=?", (str(repo_dir),)
    ).fetchone()["id"]
    from friday.identity import build_identity, explain_project_from_conn

    ident = build_identity(conn, rid)  # must not raise the 2-tuple ValueError
    assert ident is not None
    assert ident.purpose_confidence in ("Low", "Medium", "None")
    text = explain_project_from_conn(conn, rid)  # and this must render
    assert text.strip()


def test_reuse_opportunities_excludes_concept_components(conn):
    # Two repos both having a db.py/Configuration must NOT become a reuse rec.
    _seed_repo(conn, "A", "/a", "CLI tool", ["Configuration"], [("CLI", "x")])
    _seed_repo(conn, "B", "/b", "CLI tool", ["Configuration"], [("CLI", "y")])
    opps = reuse_opportunities(conn)
    assert not any("Configuration" in o for o in opps)
    assert not any("Database" in o for o in opps)
    # But a shared *entry point* (e.g. CLI) is actionable.
    assert any("CLI entry point" in o for o in opps)


def test_reuse_opportunities_from_shared_framework(conn):
    # Two FastAPI repos with >=3 shared techs -> potential-reuse relationship
    # (Medium) -> surfaced as a shared-code candidate.
    for n, p in (("A", "/a"), ("B", "/b")):
        rid = upsert_repository(conn, name=n, path=p, default_branch="main", is_dirty=False,
            first_commit_date="2024-01-01", last_commit_date="2026-01-01", remote_url=None,
            commit_count=10, readme_summary=None, license=None, primary_author=None)
        conn.execute("INSERT INTO architecture (repo_id, architecture, evidence) VALUES (?,?,?)",
                     (rid, "FastAPI REST API", "f"))
        conn.executemany("INSERT INTO technologies (repo_id, tech, evidence) VALUES (?,?,?)",
                         [(rid, t, "x") for t in ("FastAPI", "Python", "Pydantic")])
    conn.execute("""INSERT INTO relationships (repo_a, repo_b, kind, evidence, priority, strength)
                    VALUES (1,2,'potential-reuse','Overlapping stack: FastAPI, Python, Pydantic',65,'Medium')""")
    conn.commit()
    opps = reuse_opportunities(conn)
    assert any("share" in o and "FastAPI" in o for o in opps)


# --- Ask intents ------------------------------------------------------------


def test_classify_architecture_question(conn, monkeypatch):
    monkeypatch.setattr("friday.ask.llm_enabled", lambda: False)
    assert classify("Explain Friday's architecture.", conn) == "architecture"
    assert classify("How does Vivaha start?", conn) == "architecture"


def test_classify_similarity_question(conn, monkeypatch):
    monkeypatch.setattr("friday.ask.llm_enabled", lambda: False)
    assert classify("Which projects duplicate configuration loading?", conn) == "similarity"
    assert classify("Which projects could realistically share code?", conn) == "similarity"
    assert classify("Which repositories have similar layouts?", conn) == "similarity"


def test_ask_architecture_explains_from_evidence(conn, tmp_path, monkeypatch):
    monkeypatch.setattr("friday.ask.llm_enabled", lambda: False)
    _seed_repo(conn, "Vivaha", "/v", "Next.js App Router",
               ["Authentication", "Routing"], [("Next.js app", "app/")])
    ans = ask("Explain Vivaha's architecture.", conn, verbose=False)
    assert not ans.used_llm
    assert "Next.js App Router" in ans.text
    assert "Authentication" in ans.text
    assert "Next.js app" in ans.text


def test_ask_similarity_honest_when_empty(conn, tmp_path, monkeypatch):
    monkeypatch.setattr("friday.ask.llm_enabled", lambda: False)
    _seed_repo(conn, "Vivaha", "/v", "Next.js App Router", ["Routing"], [])
    ans = ask("Which projects could realistically share code?", conn, verbose=False)
    assert not ans.used_llm
    # Only one repo -> no cross-repo similarity possible.
    assert "No evidence-backed" in ans.text or "similar" in ans.text.lower()


def test_ask_similarity_no_concept_components(conn, tmp_path, monkeypatch):
    monkeypatch.setattr("friday.ask.llm_enabled", lambda: False)
    # Config/Auth/Database are concepts; they must NOT appear as reuse recs.
    _seed_repo(conn, "A", "/a", "CLI tool", ["Configuration", "Authentication"], [("CLI", "x")])
    _seed_repo(conn, "B", "/b", "CLI tool", ["Configuration", "Database"], [("CLI", "y")])
    ans = ask("Which projects duplicate configuration loading?", conn, verbose=False)
    assert not ans.used_llm
    # Forbidden: recommending reuse purely from shared concept components.
    assert "Configuration" not in ans.text or "Realistic shared-code" not in ans.text
    # The shared CLI entry point IS actionable, so it can appear.
    assert "CLI entry point" in ans.text


def test_ask_architecture_data_flow_not_character_split(conn, tmp_path, monkeypatch):
    monkeypatch.setattr("friday.ask.llm_enabled", lambda: False)
    # data_flow / known_patterns are stored newline-joined; rendering must not
    # split the string character-by-character (regression from .join(string)).
    rid = _seed_repo(conn, "Vivaha", "/v", "CLI tool", ["Testing"], [("CLI", "x")])
    conn.execute(
        "UPDATE architecture SET data_flow=?, known_patterns=? WHERE repo_id=?",
        ("step one\nstep two", "p1\np2", rid),
    )
    conn.commit()
    ans = ask("Explain Vivaha's architecture.", conn, verbose=False)
    assert not ans.used_llm
    # Each line must appear as a whole, not broken into single characters.
    assert "step one" in ans.text
    assert "step two" in ans.text
    assert "\n- s\n- t" not in ans.text  # per-character split guard


def test_similarity_no_self_duplication(conn, tmp_path, monkeypatch):
    monkeypatch.setattr("friday.ask.llm_enabled", lambda: False)
    # One repo with multiple main() entry-point rows must NOT list itself N times.
    _seed_repo(conn, "Solo", "/s", "CLI tool", [], [("main()", "a.py"), ("main()", "b.py")])
    ans = ask("Which projects could realistically share code?", conn, verbose=False)
    assert not ans.used_llm
    assert "No evidence-backed" in ans.text
