"""Dogfooding hardening sprint — permanent benchmarks for every audit issue.

Format per the sprint deliverable: each benchmark encodes a Workspace setup,
a Question, the Expected answer, the Forbidden answer (a senior engineer would
reject), the Evidence required to make the claim, and the Reasoning.

These are regression tests: a reasoning bug must not be able to silently
return. They run WITHOUT an LLM, asserting on the deterministic evidence and
the rendered text that a senior engineer would actually see.

Audit mapping:
  B1 §2  Entry-point detection ignores tests/examples, flags scripts utility.
  B2 §3  Architecture answers "what is this project", not "what tooling".
  B3 §6  Next dep alone -> router type Unknown (not Pages/App).
  B4 §6  React dep alone -> NOT "React SPA" without SPA evidence.
  B5 §7  config.py import alone -> no reusable Database/Config overclaim.
  B6 §7  FastAPI import alone -> no Routing component without routes.
  B7 §8  Similarity compares implementations, not labels.
  B8 §5  Weak relationships marked Weak, not presented as architecture.
  B9 §4  Shared concept component -> no code-reuse recommendation.
  B10 §1 Evidence strength on every component / relationship.
  B11 reuse_opportunities surfaces potential-reuse (Medium stack overlap).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from friday.architecture import analyze
from friday.discovery import Repo
from friday import judgment
from friday.db import (
    ComponentRow,
    EntryPointRow,
    connect,
    get_entry_points,
    replace_all_relationships,
    replace_components,
    upsert_repository,
)
from friday.query import reuse_opportunities
from friday.summary import build_views, infer_relationship_rows
from friday.ask import ask, classify


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "kb.db")
    yield c
    c.close()


def _mk(tmp_path: Path, files: dict[str, str]) -> Repo:
    for rel, txt in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(txt)
    return Repo(path=tmp_path)


# --- B1 §2 Entry-point detection ---------------------------------------------


def test_b1_entry_points_ignore_tests_examples_utility(tmp_path):
    repo = _mk(tmp_path, {
        "tests/run.py": "def main():\n    pass\n",
        "examples/demo.py": "if __name__ == '__main__':\n    pass\n",
        "scripts/fix-layouts.sh": "#!/usr/bin/env bash\necho hi\n",
        "src/app.py": "def main():\n    pass\n",
    })
    p = analyze(repo.path)
    kinds = [(e.kind, e.detail) for e in p.entry_points]
    # Application entry point from src/ is honored.
    assert ("main()", "src/app.py") in kinds
    # tests/ and examples/ main() are NOT application entry points.
    assert not any(d.startswith("tests/") or d.startswith("examples/") for _, d in kinds)
    # scripts/ is a utility script, not an application/executable entry point.
    assert ("Utility script", "scripts/fix-layouts.sh") in kinds
    assert not any(k == "Executable script" for k, _ in kinds)


# --- B2 §3 Architecture answers "what is this project" -----------------------


def test_b2_pytest_not_primary_architecture(tmp_path):
    repo = _mk(tmp_path, {
        "pyproject.toml": "[project]\nname='lib'\n",
        "mylib/__init__.py": "",
        "mylib/core.py": "def f(): pass\n",
        "tests/test_core.py": "def test_f(): assert True\n",
    })
    p = analyze(repo.path)
    # A library with tests is still a Library, never a "Pytest test suite".
    assert p.architecture == "Library"


# --- B3 §6 Next dependency alone -> router type unknown ----------------------


def test_b3_next_without_router_dir_is_unknown(tmp_path):
    repo = _mk(tmp_path, {
        "package.json": '{"dependencies": {"next": "14", "react": "18"}}',
        "src/page.tsx": "export default () => null",
    })
    p = analyze(repo.path)
    # Must not overclaim App/Pages Router; router type must be stated unknown.
    assert p.architecture == "Next.js (router type unknown)"
    assert "router type unknown" in p.architecture


# --- B4 §6 React dependency alone -> NOT React SPA ---------------------------


def test_b4_react_alone_not_spa(tmp_path):
    repo = _mk(tmp_path, {
        "package.json": '{"dependencies": {"react": "18"}}',
        "src/index.tsx": "export const x = 1",
    })
    p = analyze(repo.path)
    # Bare react import without Vite/public/index.html/main.tsx is NOT a SPA.
    assert p.architecture != "React SPA"


# --- B5 §7 config.py import alone -> no Database/Config overclaim ------------


def test_b5_db_import_without_behavior_is_weak_only(tmp_path):
    repo = _mk(tmp_path, {
        "db.py": "import sqlite3\n",
        "config.py": "import os\n",
    })
    p = analyze(repo.path)
    names = {c.name for c in p.components}
    # A db.py that only imports sqlite3 (no models/ORM/queries) must NOT become a
    # reusable "Database" component. config.py without a loader must NOT become
    # a "Configuration" component.
    assert "Database" not in names
    assert "Configuration" not in names
    # Even when a component is emitted, it is Weak evidence (a concept).
    assert all(c.strength == "Weak" for c in p.components)


# --- B6 §7 FastAPI import alone -> no Routing without routes ----------------


def test_b6_fastapi_import_without_routes_is_not_routing(tmp_path):
    repo = _mk(tmp_path, {
        "main.py": "from fastapi import FastAPI\napp = FastAPI()\n",
    })
    p = analyze(repo.path)
    names = {c.name for c in p.components}
    # Routing requires actual route modules, not just a framework import.
    assert "Routing" not in names


# --- B7 §8 Similarity compares implementations, not labels -------------------


def test_b7_similar_layouts_is_label_only_not_reuse_claim(tmp_path):
    conn = connect(tmp_path / "kb.db")
    a = upsert_repository(conn, name="A", path=str(tmp_path / "A"), default_branch="main",
                          is_dirty=False, first_commit_date="2024-01-01",
                          last_commit_date="2026-01-01", remote_url=None,
                          commit_count=10, readme_summary=None, license=None,
                          primary_author=None)
    b = upsert_repository(conn, name="B", path=str(tmp_path / "B"), default_branch="main",
                          is_dirty=False, first_commit_date="2024-01-01",
                          last_commit_date="2026-01-01", remote_url=None,
                          commit_count=10, readme_summary=None, license=None,
                          primary_author=None)
    conn.execute("INSERT INTO architecture (repo_id, architecture, evidence) VALUES (?,?,?)",
                 (a, "Library", "x"))
    conn.execute("INSERT INTO architecture (repo_id, architecture, evidence) VALUES (?,?,?)",
                 (b, "Library", "y"))
    conn.commit()
    from friday.query import similar_layouts
    # Two "Library" labels match, but this is a Weak/Medium label proxy only and
    # must never be surfaced as a code-reuse recommendation on its own.
    assert ("A", "B") in similar_layouts(conn)
    assert reuse_opportunities(conn) == []


# --- B8 §5 Weak relationships marked Weak ------------------------------------


def test_b8_weak_relationships_flagged(conn):
    a = upsert_repository(conn, name="A", path="/a", default_branch="main",
                          is_dirty=False, first_commit_date="2024-01-01",
                          last_commit_date="2026-01-01", remote_url=None,
                          commit_count=10, readme_summary=None, license=None,
                          primary_author="dev@x.com")
    b = upsert_repository(conn, name="B", path="/b", default_branch="main",
                          is_dirty=False, first_commit_date="2024-01-01",
                          last_commit_date="2026-01-01", remote_url=None,
                          commit_count=10, readme_summary=None, license=None,
                          primary_author="dev@x.com")
    views = build_views(conn)
    rels = infer_relationship_rows(views)
    replace_all_relationships(conn, rels)
    weak = [r for r in rels if r.kind in ("shared-author", "shared-org", "shared-language")]
    assert weak, "expected at least one weak relationship from shared author"
    assert all(r.strength == "Weak" for r in weak)
    # Ask must mark Weak relationships separately, not as architectural insight.
    ans = ask("How is A related to B?", conn, verbose=False)
    assert "Weak" in ans.text


# --- B9 §4 Shared concept component -> no reuse recommendation ---------------


def test_b9_shared_concept_no_reuse_rec(conn):
    a = upsert_repository(conn, name="A", path="/a", default_branch="main",
                          is_dirty=False, first_commit_date="2024-01-01",
                          last_commit_date="2026-01-01", remote_url=None,
                          commit_count=10, readme_summary=None, license=None,
                          primary_author=None)
    b = upsert_repository(conn, name="B", path="/b", default_branch="main",
                          is_dirty=False, first_commit_date="2024-01-01",
                          last_commit_date="2026-01-01", remote_url=None,
                          commit_count=10, readme_summary=None, license=None,
                          primary_author=None)
    replace_components(conn, a, [ComponentRow(repo_id=a, name="Configuration",
                                             evidence="x", strength="Weak")])
    replace_components(conn, b, [ComponentRow(repo_id=b, name="Configuration",
                                             evidence="y", strength="Weak")])
    opps = reuse_opportunities(conn)
    assert not any("Configuration" in o for o in opps)


# --- B10 §1 Evidence strength on every component / relationship --------------


def test_b10_every_inference_carries_strength(tmp_path, conn):
    # Components: every emitted component has a strength.
    repo = _mk(tmp_path, {"main.py": "import jwt\n"})
    p = analyze(repo.path)
    assert all(c.strength in ("Weak", "Medium", "Strong") for c in p.components)
    # Relationships: every emitted relationship has a strength.
    a = upsert_repository(conn, name="A", path="/a", default_branch="main",
                          is_dirty=False, first_commit_date="2024-01-01",
                          last_commit_date="2026-01-01", remote_url=None,
                          commit_count=10, readme_summary=None, license=None,
                          primary_author=None)
    b = upsert_repository(conn, name="B", path="/b", default_branch="main",
                          is_dirty=False, first_commit_date="2024-01-01",
                          last_commit_date="2026-01-01", remote_url=None,
                          commit_count=10, readme_summary=None, license=None,
                          primary_author=None)
    rels = infer_relationship_rows(build_views(conn))
    replace_all_relationships(conn, rels)
    for r in rels:
        assert r.strength in ("Weak", "Medium", "Strong")


# --- B11 potential-reuse (Medium stack overlap) surfaces as candidate --------


def test_b11_potential_reuse_surfaces_with_evidence(conn):
    a = upsert_repository(conn, name="A", path="/a", default_branch="main",
                          is_dirty=False, first_commit_date="2024-01-01",
                          last_commit_date="2026-01-01", remote_url=None,
                          commit_count=10, readme_summary=None, license=None,
                          primary_author=None)
    b = upsert_repository(conn, name="B", path="/b", default_branch="main",
                          is_dirty=False, first_commit_date="2024-01-01",
                          last_commit_date="2026-01-01", remote_url=None,
                          commit_count=10, readme_summary=None, license=None,
                          primary_author=None)
    conn.execute(
        "INSERT INTO relationships (repo_a, repo_b, kind, evidence, priority, strength) "
        "VALUES (?,?,?,?,?,?)",
        (a, b, "potential-reuse", "Overlapping stack: FastAPI, Python, Pydantic", 65, "Medium"),
    )
    conn.commit()
    opps = reuse_opportunities(conn)
    assert any("FastAPI" in o and "Overlapping stack" in o for o in opps)
