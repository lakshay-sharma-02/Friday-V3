"""Milestone 3.6 — workspace intelligence dogfooding benchmarks (spec §12).

Permanent regression guards for the reasoning bugs dogfooding surfaced. Each
benchmark wires a realistic Workspace (several themed projects) and asserts on
the DETERMINISTIC answer a senior engineer would see — no LLM configured.

Forbidden answers per §12:
  "What do I seem to be building?"  -> "Intent not recognized." (or no intent)
  "Which project is most valuable?" -> "I don't have enough evidence." (when evidence exists)
  "Explain Friday."                -> starts with implementation (components/entry points)
  "Explain Friday V3."             -> mostly explains some other project
  "What parts of my projects overlap?" -> overlap based on main()/syntax noise
  "Which project should integrate with Friday?" -> "No evidence." (when identity supports a candidate)
"""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import pytest

from friday import ask as ask_mod
from friday.ask import ask, classify, retrieve
from friday.db import (
    LangRow,
    Repository,
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
    # No LLM — exercise the deterministic reasoning path.
    os.environ.pop("FRIDAY_LLM_MODEL", None)
    os.environ.pop("FRIDAY_LLM_API_KEY", None)
    c = connect(tmp_path / "kb.db")

    aether = upsert_repository(
        c, name="Aether", path=str(tmp_path / "Aether"), default_branch="main",
        is_dirty=False, first_commit_date="2024-01-01", last_commit_date="2026-06-01",
        remote_url="https://github.com/acme/aether", commit_count=400,
        readme_summary="Purpose:\nAether is an operating system written in Rust.\nMaturity:\nUnknown",
        license="MIT", primary_author="dev@acme.com",
    )
    vivaha = upsert_repository(
        c, name="Vivaha", path=str(tmp_path / "Vivaha"), default_branch="main",
        is_dirty=False, first_commit_date="2025-01-01", last_commit_date="2026-07-01",
        remote_url="https://github.com/acme/vivaha", commit_count=900,
        readme_summary=(
            "Purpose:\nVivaha is a matrimony app.\n"
            "Maturity:\nBeta\n"
            "Business value:\nSubscription product for matchmaking."
        ),
        license="MIT", primary_author="dev@acme.com",
    )
    mindwell = upsert_repository(
        c, name="MindWell", path=str(tmp_path / "MindWell"), default_branch="main",
        is_dirty=False, first_commit_date="2025-03-01", last_commit_date="2026-07-05",
        remote_url="https://github.com/acme/mindwell", commit_count=250,
        readme_summary="Purpose:\nMindWell is a mental health AI companion.\nMaturity:\nWIP",
        license="MIT", primary_author="dev@acme.com",
    )
    friday = upsert_repository(
        c, name="Friday V3", path=str(tmp_path / "Friday V3"), default_branch="main",
        is_dirty=True, first_commit_date="2026-01-01", last_commit_date="2026-07-10",
        remote_url="https://github.com/acme/friday", commit_count=600,
        readme_summary=(
            "Purpose:\nFriday is an AI operating partner for your workspace.\n"
            "Maturity:\nBeta"
        ),
        license="MIT", primary_author="dev@acme.com",
    )
    devtool = upsert_repository(
        c, name="RepoLint", path=str(tmp_path / "RepoLint"), default_branch="main",
        is_dirty=False, first_commit_date="2025-06-01", last_commit_date="2026-02-01",
        remote_url="https://github.com/acme/repolint", commit_count=30,
        readme_summary="Purpose:\nA CLI developer tool for repository linting.\nMaturity:\nAlpha",
        license="MIT", primary_author="dev@acme.com",
    )

    replace_children(c, aether, [LangRow("Rust", 100)], [TechRow("Rust", "Cargo.toml")])
    replace_children(c, vivaha, [LangRow("TypeScript", 80)],
                    [TechRow("Next.js", "next"), TechRow("Supabase", "supabase"),
                     TechRow("SQLite", "sqlite")])
    replace_children(c, mindwell, [LangRow("Python", 60)],
                    [TechRow("Python", "python"), TechRow("Supabase", "supabase"),
                     TechRow("SQLite", "sqlite")])
    replace_children(c, friday, [LangRow("Python", 120)],
                    [TechRow("Python", "python"), TechRow("Supabase", "supabase"),
                     TechRow("SQLite", "sqlite")])
    replace_children(c, devtool, [LangRow("Rust", 40)], [TechRow("Rust", "Cargo.toml")])

    for rid, arch in (
        (aether, "Operating system"),
        (vivaha, "Web application"),
        (mindwell, "AI service"),
        (friday, "AI application"),
        (devtool, "CLI tool"),
    ):
        c.execute(
            "INSERT INTO architecture (repo_id, architecture, evidence) VALUES (?,?,?)",
            (rid, arch, "stored from analyze"),
        )
    c.commit()

    set_repo_quality(c, aether, "Unknown", "good", "complete")
    set_repo_quality(c, vivaha, "Beta", "good", "complete")
    set_repo_quality(c, mindwell, "WIP", "good", "complete")
    set_repo_quality(c, friday, "Beta", "good", "complete")
    set_repo_quality(c, devtool, "Alpha", "poor", "boilerplate")

    views = build_views(c)
    replace_all_relationships(c, infer_relationship_rows(views))
    return c


# --- §12 B12: "What do I seem to be building?" --------------------------------


def test_b12_what_am_i_building_recognized(conn):
    q = "What do I seem to be building?"
    assert classify(q, conn) == "portfolio"
    ans = ask(q, conn, verbose=False)
    assert not ans.used_llm
    assert "Intent not recognized" not in ans.text
    # It must say something about what's being built — themes or project names.
    assert ("theme" in ans.text.lower() or "Aether" in ans.text
            or "Vivaha" in ans.text or "MindWell" in ans.text)


# --- §12 B13: "Which project is most valuable?" --------------------------------


def test_b13_most_valuable_states_confidence(conn):
    ans = ask("Which project is most valuable?", conn, verbose=False)
    assert not ans.used_llm
    assert "don't have enough evidence" not in ans.text.lower()
    assert "Confidence:" in ans.text


# --- §12 B14: "Explain Friday." — purpose first, not implementation ------------


def test_b14_explain_friday_not_implementation_first(conn):
    ans = ask("Explain Friday.", conn, verbose=False)
    assert not ans.used_llm
    head = ans.text[:120].lower()
    assert "components:" not in head
    assert "entry point" not in head
    assert "architecturally" not in head
    # Purpose-led: the AI operating-partner description should appear early.
    assert "ai operating partner" in ans.text.lower() or "Friday" in ans.text


# --- §12 B15: "Explain Friday V3." — primary subject dominates ----------------


def test_b15_explain_friday_v3_primary_subject(conn):
    ans = ask("Explain Friday V3.", conn, verbose=False)
    assert not ans.used_llm
    # Repo name in the opening sentence.
    assert "friday" in ans.text[:80].lower()
    # If relationships are mentioned, they must sit in the tail, not dominate.
    if "Related projects" in ans.text:
        idx = ans.text.index("Related projects")
        # The related section must be in the latter half of the answer.
        assert idx > len(ans.text) // 2


# --- §12 B16: "What parts of my projects overlap?" — meaningful, not main() ---


def test_b16_overlap_is_meaningful_not_syntax(conn):
    ans = ask("What parts of my projects overlap?", conn, verbose=False)
    assert not ans.used_llm
    assert "main()" not in ans.text
    # Must report a meaningful dimension, not just "they both have a main()".
    meaningful = ("architecture", "framework", "persistence", "business goal",
                  "configuration", "shared", "deploy")
    assert any(w in ans.text.lower() for w in meaningful)


# --- §12 B17: "Which project should integrate with Friday?" --------------------


def test_b17_integration_names_candidate(conn):
    ans = ask("Which project should integrate with Friday?", conn, verbose=False)
    assert not ans.used_llm
    # Identity supports candidates (MindWell/AI, Vivaha/tech overlap, etc.) — must
    # not bail out with a bare "No evidence."
    assert ans.text.strip() != "No evidence."
    assert "No evidence" not in ans.text or "Confidence:" in ans.text
    # And it must actually name a candidate project.
    assert any(n in ans.text for n in ("MindWell", "Vivaha", "Aether", "RepoLint"))


# --- Sanity: existing intents still classify correctly ------------------------


def test_classify_3_6_intents(conn):
    assert classify("What themes exist across my projects?", conn) == "portfolio"
    assert classify("Which project is most valuable?", conn) == "value"
    assert classify("How do my projects overlap?", conn) == "overlap"
    assert classify("Which should integrate with Friday?", conn) == "integration"
    assert classify("What is my engineering universe?", conn) == "workspace"
    assert classify("What is Aether?", conn) == "describe"
    assert classify("Which projects use Rust?", conn) == "by-tech"


# --- B18: overlap must never surface main() as a meaningful dimension ---------


def test_b18_overlap_excludes_main_entry_point(conn):
    from friday.portfolio import meaningful_overlap

    results = meaningful_overlap(conn)
    for o in results:
        assert "main()" not in "; ".join(o.dimensions), (
            f"{o.a}/{o.b} overlap leaked main(): {o.dimensions}"
        )


# --- B19: theme/integration matching uses whole tokens (no 'ai' in 'domain') ---


def test_b19_ai_token_match_is_whole_word(tmp_path):
    from friday.portfolio import _matches

    # 'ai' as a standalone token / prefix should match...
    assert _matches("an ai-native operating system", "ai")
    assert _matches("purpose: ai assistant", "ai")
    # ...but NOT as a substring of unrelated words.
    assert not _matches("a domain-specific language", "ai")
    assert not _matches("please email me", "ai")
