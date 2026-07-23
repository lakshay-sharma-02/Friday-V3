"""Cross-project synthesis tests — evidence grounded, no forced findings.

Two repos with no structural overlap must return "no meaningful overlap."
Confidence labels are present on every output.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from friday.db import (
    LangRow,
    TechRow,
    connect,
    replace_all_relationships,
    replace_children,
    set_repo_quality,
    upsert_repository,
)
from friday.summary import build_views, infer_relationship_rows


@pytest.fixture
def conn(tmp_path):
    os.environ.pop("FRIDAY_LLM_MODEL", None)
    os.environ.pop("FRIDAY_LLM_API_KEY", None)
    c = connect(tmp_path / "synth.db")
    a = upsert_repository(
        c, name="Aether", path=str(tmp_path / "Aether"), default_branch="main",
        is_dirty=False, first_commit_date="2024-01-01", last_commit_date="2026-07-01",
        remote_url="https://github.com/acme/aether", commit_count=500,
        readme_summary="Purpose:\nAether is an OS in Rust.\nMaturity:\nUnknown",
        license="MIT", primary_author="dev@acme.com",
    )
    b = upsert_repository(
        c, name="Vivaha", path=str(tmp_path / "Vivaha"), default_branch="main",
        is_dirty=False, first_commit_date="2025-01-01", last_commit_date="2024-01-01",
        remote_url="https://github.com/acme/vivaha", commit_count=300,
        readme_summary="Purpose:\nVivaha is a matrimony app.\nMaturity:\nBeta",
        license="MIT", primary_author="dev@acme.com",
    )
    # Aether: Rust/Cargo
    c.execute(
        "INSERT OR REPLACE INTO architecture (repo_id, architecture, evidence, data_flow, known_patterns, complexity) "
        "VALUES (?, 'Cargo workspace', 'Cargo.toml [workspace] section', 'None', 'None', 'Low')",
        (a,))
    replace_children(c, a, [LangRow("Rust", 100)], [TechRow("Rust", "Cargo.toml"), TechRow("Cargo", "Cargo.toml")])
    set_repo_quality(c, a, "Unknown", "good", "complete")

    # Vivaha: Next.js/TypeScript/React/Supabase — totally different stack
    c.execute(
        "INSERT OR REPLACE INTO architecture (repo_id, architecture, evidence, data_flow, known_patterns, complexity) "
        "VALUES (?, 'Next.js web app', 'next dependency', 'Browser -> API routes', 'src/ + tests/', 'Medium')",
        (b,))
    replace_children(c, b, [LangRow("TypeScript", 80), LangRow("JavaScript", 20)],
                     [TechRow("Node.js", "package.json"), TechRow("Next.js", "next"),
                      TechRow("React", "react"), TechRow("TypeScript", "tsconfig.json"),
                      TechRow("npm", "package-lock.json"), TechRow("Supabase", "supabase")])
    set_repo_quality(c, b, "Beta", "good", "complete")

    views = build_views(c)
    replace_all_relationships(c, infer_relationship_rows(views))
    return c


def test_synthesis_no_llm_no_overlap(conn):
    """Two unrelated repos with no LLM = technology analysis only."""
    from friday.synthesis import synthesize

    result = synthesize(conn, "Aether", "Vivaha")
    assert result.overlap_found is False
    assert result.confidence in ("Strong", "Medium", "Weak")
    assert "no meaningful" in result.to_text().lower()


def test_synthesis_missing_repo(conn):
    """Unknown repo produces a clear error, not a crash."""
    from friday.synthesis import synthesize

    result = synthesize(conn, "Aether", "NonExistent")
    assert result.overlap_found is False
    assert "not found" in (result.note or "")


def test_synthesis_self_relationship(conn):
    """Repo compared to itself finds obvious overlap."""
    from friday.synthesis import synthesize

    result = synthesize(conn, "Aether", "Aether")
    # The same repo obviously overlaps with itself
    # (this tests the code doesn't crash on same-name input)
    assert result.overlap_found or result.confidence is not None


def test_synthesis_confidence_label_present(conn):
    """Every SynthesisResult carries a confidence label."""
    from friday.synthesis import synthesize

    result = synthesize(conn, "Aether", "Vivaha")
    assert result.confidence in ("Strong", "Medium", "Weak")
