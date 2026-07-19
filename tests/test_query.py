"""Query engine + identity cards (SQL retrieval, no LLM)."""

from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

from friday.db import (
    LangRow,
    Repository,
    TechRow,
    connect,
    get_languages,
    get_technologies,
    replace_all_relationships,
    replace_children,
    set_repo_quality,
    upsert_repository,
)
from friday import query as q
from friday.summary import RepoView, build_views, infer_relationship_rows


def _make_db(tmp_path: Path) -> sqlite3.Connection:
    conn = connect(tmp_path / "kb.db")
    a = upsert_repository(
        conn, name="Aether", path=str(tmp_path / "Aether"),
        default_branch="main", is_dirty=False,
        first_commit_date="2024-01-01", last_commit_date="2026-07-01",
        remote_url="https://github.com/acme/aether", commit_count=500,
        readme_summary="Purpose:\nAether is an OS in Rust.\nMaturity:\nUnknown",
        license="MIT", primary_author="dev@acme.com",
    )
    b = upsert_repository(
        conn, name="Vivaha", path=str(tmp_path / "Vivaha"),
        default_branch="main", is_dirty=False,
        first_commit_date="2025-01-01", last_commit_date="2026-07-10",
        remote_url="https://github.com/acme/vivaha", commit_count=300,
        readme_summary="Purpose:\nVivaha is a matrimony app.\nMaturity:\nBeta",
        license="MIT", primary_author="dev@acme.com",
    )
    replace_children(conn, a, [LangRow("Rust", 100)], [TechRow("Rust", "Cargo.toml"), TechRow("SQLite", "sqlite dep")])
    replace_children(conn, b, [LangRow("TypeScript", 80)], [TechRow("Next.js", "next"), TechRow("SQLite", "sqlite dep")])
    set_repo_quality(conn, a, maturity="Unknown", readme_quality="good", readme_completeness="complete")
    set_repo_quality(conn, b, maturity="Beta", readme_quality="good", readme_completeness="complete")
    # Persist relationships.
    views = build_views(conn)
    replace_all_relationships(conn, infer_relationship_rows(views))
    return conn


def test_projects_by_tech(tmp_path):
    conn = _make_db(tmp_path)
    rust = q.projects_by_tech(conn, "Rust")
    assert [r.name for r in rust] == ["Aether"]
    sqlite = q.projects_by_tech(conn, "SQLite")
    assert {r.name for r in sqlite} == {"Aether", "Vivaha"}


def test_inactive_and_abandoned(tmp_path):
    conn = _make_db(tmp_path)
    today = dt.date(2026, 7, 13)
    # Both repos committed within 90 days -> none inactive.
    assert q.inactive_repos(conn, today) == []
    # None abandoned either (last commit 2026-07-xx).
    assert q.abandoned_repos(conn, today) == []


def test_newest(tmp_path):
    conn = _make_db(tmp_path)
    newest = q.newest_repos(conn, 3)
    assert newest[0].name == "Vivaha"  # later first_commit_date


def test_identity_card(tmp_path):
    conn = _make_db(tmp_path)
    a = q.repo_by_name(conn, "aether")
    assert a is not None
    card = q.identity_card(conn, a.id, dt.date(2026, 7, 13))
    assert card is not None
    assert "Rust" in card.tech_names
    assert card.activity == "Active"
    assert any("README quality: good" in o for o in card.key_observations)


def test_relationships_between(tmp_path):
    conn = _make_db(tmp_path)
    a = q.repo_by_name(conn, "Aether")
    b = q.repo_by_name(conn, "Vivaha")
    rels = q.relationships_between(conn, a.id, b.id)
    kinds = {r.kind for r in rels}
    assert "shared-db" in kinds  # both use SQLite
    assert "shared-author" in kinds  # same primary_author


def test_duplicate_tech(tmp_path):
    conn = _make_db(tmp_path)
    dups = q.duplicate_tech(conn)
    assert "SQLite" in dups
    assert set(dups["SQLite"]) == {"Aether", "Vivaha"}
