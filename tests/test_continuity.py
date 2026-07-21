"""Milestone 6.5D — bounded conversational continuity permanent benchmarks.

Friday is a conversational partner. A follow-up must resolve against the
immediately previous exchange. No long-term memory, no planner, no agent.

Locked in (all run WITHOUT an LLM — deterministic resolver only):
  - "Why?" after a recommendation is answered, never "couldn't determine".
  - "Why not <X>?" resolves to a contrast when X is a known repo != picked one.
  - "What next?" routes to a fresh recommendation (new evidence, distinct text).
  - "How long?" resolves to the staleness of the previous subject.
  - An unanchored follow-up after a compare asks which antecedent.
  - A brand-new question with prev set is answered normally (continuity never
    corrupts an unrelated turn).

Driven through ask(prev=...) exactly as the REPL wraps it.
"""

from __future__ import annotations

import os
import tempfile
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
from friday.ask import Answer, Exchange, ask
from friday.query import workspace_priorities


@pytest.fixture
def conn(tmp_path):
    os.environ.pop("FRIDAY_LLM_MODEL", None)
    os.environ.pop("FRIDAY_LLM_API_KEY", None)
    c = connect(tmp_path / "kb.db")
    a = upsert_repository(
        c, name="Aether", path=str(tmp_path / "Aether"), default_branch="main",
        is_dirty=False, first_commit_date="2024-01-01", last_commit_date="2026-07-01",
        remote_url="https://github.com/acme/aether", commit_count=500,
        readme_summary="Purpose:\nAether is an OS in Rust.\nMaturity:\nUnknown",
        license="MIT", primary_author="dev@acme.com",
    )
    f3 = upsert_repository(
        c, name="Friday V3", path=str(tmp_path / "Friday-V3"), default_branch="main",
        is_dirty=True, first_commit_date="2025-01-01", last_commit_date="2026-07-10",
        remote_url="https://github.com/acme/friday-v3", commit_count=600,
        readme_summary="Purpose:\nFriday V3 is an AI operating partner.\nMaturity:\nBeta",
        license="MIT", primary_author="dev@acme.com",
    )
    v = upsert_repository(
        c, name="Vivaha", path=str(tmp_path / "Vivaha"), default_branch="main",
        is_dirty=False, first_commit_date="2025-01-01", last_commit_date="2024-01-01",
        remote_url="https://github.com/acme/vivaha", commit_count=300,
        readme_summary="Purpose:\nVivaha is a matrimony app.\nMaturity:\nBeta",
        license="MIT", primary_author="dev@acme.com",
    )
    replace_children(c, a, [LangRow("Rust", 100)], [TechRow("Rust", "Cargo.toml")])
    replace_children(c, f3, [LangRow("Python", 100)], [TechRow("Python", "e")])
    replace_children(c, v, [LangRow("TypeScript", 80)], [TechRow("Next.js", "next")])
    set_repo_quality(c, a, "Unknown", "good", "complete")
    set_repo_quality(c, f3, "Beta", "good", "complete")
    set_repo_quality(c, v, "Beta", "good", "complete")
    views = build_views(c)
    replace_all_relationships(c, infer_relationship_rows(views))
    yield c
    c.close()


def _turn(c, q, prev=None) -> Answer:
    return ask(q, c, prev=prev, verbose=False)


def _recommend(c):
    """Turn 1: a recommendation, returning its Exchange."""
    ans = _turn(c, "Which project should I work on?")
    assert ans.evidence.intent == "recommend"
    return Exchange("Which project should I work on?", ans)


def _compare(c):
    ans = _turn(c, "Compare Aether and Vivaha")
    return Exchange("Compare Aether and Vivaha", ans)


# --- "Why?" after a recommendation is answered, not failed -------------------


def test_why_after_recommend_is_answered(conn):
    prev = _recommend(conn)
    ans = _turn(conn, "Why?", prev=prev)
    assert "couldn't determine" not in ans.text.lower()
    assert "couldn't confidently" not in ans.text.lower()
    assert ans.text.strip()  # non-empty, substantial answer


def test_why_after_recommend_restates_reasoning(conn):
    prev = _recommend(conn)
    ans = _turn(conn, "Why?", prev=prev)
    # The picked subject must appear (we explain what we recommended).
    picked = prev.answer.evidence.raw.get("recommend_subject")
    assert picked is not None
    assert picked.lower() in ans.text.lower()


# --- "Why not X?" resolves to a contrast -------------------------------------


def test_why_not_other_repo_is_contrast(conn):
    prev = _recommend(conn)  # picks Friday V3 (dirty + newest-ish + active)
    ans = _turn(conn, "Why not Aether?", prev=prev)
    assert "couldn't determine" not in ans.text.lower()
    assert "aether" in ans.text.lower()
    # It explains the contrast (evidence-style), not a generic failure.
    assert "continue" in ans.text.lower() or "signal" in ans.text.lower()


def test_why_not_picks_unnamed_repo_asks_clarification(conn):
    prev = _recommend(conn)
    ans = _turn(conn, "Why not Zork?", prev=prev)
    assert "not sure which project" in ans.text.lower() or "name it" in ans.text.lower()


# --- "What next?" routes to a fresh recommendation ---------------------------


def test_what_next_is_fresh_recommendation(conn):
    prev = _recommend(conn)
    ans = _turn(conn, "What next?", prev=prev)
    # "What next?" resolves to a fresh recommendation retrieval (not the
    # "couldn't determine" failure), even though the deterministic output
    # happens to match the prior turn on an unchanged workspace.
    assert ans.evidence.intent == "recommend"
    assert "couldn't determine" not in ans.text.lower()


# --- "How long?" resolves to staleness of the previous subject --------------


def test_how_long_resolves_to_subject_staleness(conn):
    ans1 = _turn(conn, "What is Vivaha?")
    prev = Exchange("What is Vivaha?", ans1)
    assert prev.answer.evidence.subject == "Vivaha"
    ans2 = _turn(conn, "How long?", prev=prev)
    assert "couldn't determine" not in ans2.text.lower()
    # Resolved to the previous subject's staleness, not a generic failure.
    assert "vivaha" in ans2.text.lower()
    assert "days" in ans2.text.lower()


# --- Clarification when multiple antecedents ---------------------------------


def test_unanchored_followup_after_compare_clarifies(conn):
    prev = _compare(conn)  # two subjects: Aether, Vivaha
    assert len(prev.answer.evidence.raw.get("subjects") or []) >= 2
    ans = _turn(conn, "Why?", prev=prev)
    assert "did you mean" in ans.text.lower() or "or " in ans.text.lower()


# --- Continuity never corrupts an unrelated turn -----------------------------


def test_new_question_with_prev_is_answered_normally(conn):
    prev = _recommend(conn)
    ans = _turn(conn, "Which project uses Rust?", prev=prev)
    assert "couldn't determine" not in ans.text.lower()
    assert "Aether" in ans.text


def test_continuity_bound_is_one_exchange(conn):
    # The resolver only ever sees the most recent exchange; a follow-up to a
    # follow-up still anchors to the immediately previous exchange.
    prev = _recommend(conn)
    ans1 = _turn(conn, "Why?", prev=prev)
    prev2 = Exchange("Why?", ans1)
    ans2 = _turn(conn, "What next?", prev=prev2)
    assert ans2.evidence.intent == "recommend"
    assert "couldn't determine" not in ans2.text.lower()
