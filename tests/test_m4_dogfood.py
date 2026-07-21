"""Milestone 4.0 — knowledge completion & identity hardening benchmarks.

Regression guards proving: (a) no repository is left with empty purpose after
ingest (Part A), (b) purpose recovery degrades honestly by confidence (Part B),
(c) stated intent is computed, not persisted (Part C), (d) explanations lead
with meaning and state confidence (Part E/G), (e) the 9 acceptance questions
produce natural, non-analyzer answers.

Run WITHOUT an LLM — assertions target deterministic evidence + rendered text.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from friday import ask as ask_mod
from friday import query as q
from friday.ask import ask, classify
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
from friday.readme import process, recover_purpose_fallback
from friday.discovery import Repo
from friday.identity import build_identity, explain_project_from_conn
from friday.portfolio import stated_intent, all_stated_intents


@pytest.fixture
def conn(tmp_path):
    os.environ.pop("FRIDAY_LLM_MODEL", None)
    os.environ.pop("FRIDAY_LLM_API_KEY", None)
    c = connect(tmp_path / "kb.db")
    # Mix of README states that previously produced empty purpose:
    #  - Friday V3-like: manifest description recoverable
    #  - Vivaha-like: real README purpose
    #  - finance-tracker-like: empty README + placeholder manifest -> name fallback
    #  - Aether-like: README purpose (OS)
    #  - MindWell-like: README purpose (mental health)
    vivaha = upsert_repository(
        c, name="vivaha", path=str(tmp_path / "vivaha"), default_branch="main",
        is_dirty=False, first_commit_date="2025-01-01", last_commit_date="2026-07-01",
        remote_url="https://github.com/acme/vivaha", commit_count=900,
        readme_summary="Purpose:\nVivaha is a matrimony app.\nMaturity:\nBeta\nRoadmap:\n- Launch payments\n- Mobile app",
        license="MIT", primary_author="dev@acme.com",
    )
    aether = upsert_repository(
        c, name="Aether", path=str(tmp_path / "Aether"), default_branch="main",
        is_dirty=False, first_commit_date="2024-01-01", last_commit_date="2026-06-01",
        remote_url="https://github.com/acme/aether", commit_count=400,
        readme_summary="Purpose:\nAether is an operating system in Rust.\nMaturity:\nUnknown",
        license="MIT", primary_author="dev@acme.com",
    )
    mindwell = upsert_repository(
        c, name="MindWell", path=str(tmp_path / "MindWell"), default_branch="main",
        is_dirty=False, first_commit_date="2025-03-01", last_commit_date="2026-07-05",
        remote_url="https://github.com/acme/mindwell", commit_count=250,
        readme_summary="Purpose:\nMindWell is a mental health AI companion.\nMaturity:\nWIP",
        license="MIT", primary_author="dev@acme.com",
    )
    # Empty readme_summary (the gap M4 closes) — purpose must be recovered.
    (tmp_path / "Friday V3").mkdir(exist_ok=True)
    friday_v3 = upsert_repository(
        c, name="Friday V3", path=str(tmp_path / "Friday V3"), default_branch="main",
        is_dirty=True, first_commit_date="2026-01-01", last_commit_date="2026-07-10",
        remote_url="https://github.com/acme/friday", commit_count=600,
        readme_summary=None,  # intentionally None, like real V3 before recovery
        license="MIT", primary_author="dev@acme.com",
    )
    # finance-tracker: real on-disk dir with no README/manifest/docs, so purpose
    # recovery falls back to the name/layout hint (Low) — the honest M4 path.
    (tmp_path / "finance-tracker").mkdir(exist_ok=True)
    finance = upsert_repository(
        c, name="finance-tracker", path=str(tmp_path / "finance-tracker"),
        default_branch="main", is_dirty=False, first_commit_date="2025-06-01",
        last_commit_date="2026-02-01", remote_url="https://github.com/acme/ft",
        commit_count=30, readme_summary=None,  # gap
        license="MIT", primary_author="dev@acme.com",
    )
    for rid, arch, techs, langs, q_ in (
        (vivaha, "Next.js (router type unknown)", ["Next.js", "Supabase"], ["TypeScript"], "good"),
        (aether, "Cargo workspace", ["Rust"], ["Rust"], "good"),
        (mindwell, "React SPA", ["Python", "Supabase"], ["Python"], "good"),
        (friday_v3, "CLI tool", ["Python", "Supabase"], ["Python"], "good"),
        # finance-tracker: no architecture on purpose, so purpose recovery must
        # fall back to the name/layout hint (Low) — the honest M4 behavior.
        (finance, None, [], [], "none"),
    ):
        if arch:
            c.execute("INSERT INTO architecture (repo_id, architecture, evidence) VALUES (?,?,?)",
                      (rid, arch, "stored"))
            c.commit()
        replace_children(c, rid, [LangRow(l, 10) for l in langs],
                        [TechRow(t, "e") for t in techs])
        set_repo_quality(c, rid, None, q_, "complete" if q_ != "none" else "none")
    views = build_views(c)
    replace_all_relationships(c, infer_relationship_rows(views))
    return c


# --- Part A: every repo ends up with a purpose -------------------------------


def test_m4_all_repos_have_purpose(conn):
    for r in q.all_repositories(conn):
        ident = build_identity(conn, r.id)
        assert ident is not None
        assert ident.purpose, f"{r.name} has no recovered purpose"


def test_m4_purpose_recovery_from_manifest(conn, tmp_path):
    # Friday V3-like: pyproject description recoverable.
    d = tmp_path / "f3"
    d.mkdir()
    (d / "pyproject.toml").write_text('[project]\nname="f3"\ndescription="Friday V3 — persistent AI operating partner."\n')
    (d / ".git").mkdir()
    res = process(Repo(path=d))
    assert res is not None
    assert "AI operating partner" in res.summary


def test_m4_purpose_recovery_from_docs(conn, tmp_path):
    # V2-like: no README, but VISION.md carries purpose.
    d = tmp_path / "v2"
    d.mkdir()
    (d / "VISION.md").write_text("# Friday V2 Vision\n\n## Purpose\nFriday makes engineers more capable.\n")
    (d / ".git").mkdir()
    res = process(Repo(path=d))
    assert res is not None
    assert "engineers more capable" in res.summary


def test_m4_low_confidence_name_fallback_is_honest(tmp_path):
    # No README, no manifest, no docs -> only a descriptive name hint (Low).
    d = tmp_path / "finance-tracker"
    d.mkdir()
    (d / ".git").mkdir()
    purpose, source, conf = recover_purpose_fallback(str(d), "finance-tracker")
    assert purpose is not None
    assert conf == "Low"
    assert "name" in source


# --- Part B: confidence is exposed, never fabricated -------------------------


def test_m4_purpose_confidence_levels(conn):
    vivaha = build_identity(conn, q.repo_by_name(conn, "vivaha").id)
    assert vivaha.purpose_confidence == "High"  # explicit README purpose
    ft = build_identity(conn, q.repo_by_name(conn, "finance-tracker").id)
    # Only name/layout supported -> Low, not fabricated High.
    assert ft.purpose_confidence in ("Low", "None")


# --- Part C: stated intent is computed, not persisted -----------------------


def test_m4_stated_intent_computed(conn):
    si = stated_intent(conn, q.repo_by_name(conn, "vivaha").id)
    assert si is not None
    assert any("payments" in g or "Mobile" in g for g in si["goals"])
    # It is derived from the summary, not a new stored column.
    assert "stated_intent" not in [c[0] for c in conn.execute("PRAGMA table_info(repositories)")]


# --- Part E/G: explanations lead with meaning + state confidence ------------


def test_m4_explain_leads_with_purpose_not_implementation(conn):
    ans = ask("Explain Friday V3.", conn, verbose=False)
    head = ans.text[:140].lower()
    assert "friday v3" in head
    # Purpose/meaning first — not an architecture dump or entry-point list.
    assert "components:" not in head
    assert "entry point" not in head
    # Confidence reasoning appears somewhere.
    assert "confidence" in ans.text.lower()


def test_m4_explain_states_why(conn):
    ans = ask("Explain vivaha.", conn, verbose=False)
    # "why it exists" (business value) should surface for a project with one,
    # or at minimum the purpose-led framing.
    assert "vivaha is" in ans.text.lower() or "purpose" in ans.text.lower()


# --- Part H: the 9 acceptance questions all answer naturally ----------------


def test_m4_acceptance_questions_no_empty_answers(conn):
    questions = [
        "What am I building?",
        "What direction does my work seem to be heading?",
        "Which projects feel related?",
        "Explain Friday.",
        "Explain Friday V3.",
        "Which projects could realistically merge?",
        "Which project matters most?",
        "Why does vivaha matter most?",
        "What themes keep repeating?",
        "What engineering strengths am I developing?",
    ]
    for q in questions:
        ans = ask(q, conn, verbose=False)
        assert not ans.used_llm
        assert ans.text.strip(), f"empty answer for: {q}"
        # A senior engineer would never answer with a raw failure marker.
        assert "intent not recognized" not in ans.text.lower(), f"Q: {q}"
        assert "don't have enough evidence to answer that" not in ans.text.lower(), f"Q: {q}"


def test_m4_merge_candidates_evidence_backed(conn):
    ans = ask("Which projects could realistically merge?", conn, verbose=False)
    assert not ans.used_llm
    # Merges are reasoned from shared architecture/framework/purpose, not noise.
    assert "main()" not in ans.text
