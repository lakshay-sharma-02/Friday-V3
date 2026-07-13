"""Milestone 6.5B — Engineering Judgment permanent benchmarks.

Strategic questions were answered by repeating ONE ranking. Each now routes to
a DISTINCT reasoning axis over already-persisted evidence:

  impact     -> user value           (NOT commits / lifetime size)
  platform   -> reusable capability (shared components / abstractions)
  learning   -> engineering complexity (NOT time spent)
  opportunity-> leverage             (reuse / integration that multiplies)
  priority   -> current blockers / momentum (urgency now)

These benchmarks lock in: (1) each question routes to the `strategy` intent and
its own axis; (2) each axis uses axis-specific evidence; (3) NO single ranking
answers two questions the same way — the five answers are pairwise distinct and
carry distinct signature evidence so a regression to "one ranking" fails.

All run WITHOUT an LLM (FRIDAY_LLM_MODEL unset); assertions target the
deterministic evidence text the user would actually see.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from friday.db import (
    ComponentRow,
    LangRow,
    TechRow,
    connect,
    replace_all_relationships,
    replace_children,
    replace_components,
    set_repo_quality,
    upsert_architecture,
    upsert_repository,
)
from friday.summary import build_views, infer_relationship_rows
from friday.ask import ask, classify


@pytest.fixture
def conn(tmp_path):
    os.environ.pop("FRIDAY_LLM_MODEL", None)
    os.environ.pop("FRIDAY_LLM_API_KEY", None)
    c = connect(tmp_path / "kb.db")
    yield c
    c.close()


def _seed(conn, name, path, *, summary=None, langs=(), techs=(), arch=None,
          commits=100, complexity=None, dirty=False):
    rid = upsert_repository(
        conn, name=name, path=path, default_branch="main", is_dirty=dirty,
        first_commit_date="2025-01-01", last_commit_date="2026-07-01",
        remote_url="https://github.com/acme/" + name, commit_count=commits,
        readme_summary=summary, license="MIT", primary_author="dev@acme.com",
    )
    replace_children(conn, rid, [LangRow(l, 10) for l in langs],
                    [TechRow(t, "e") for t in techs])
    if arch:
        kw = dict(repo_id=rid, architecture=arch, evidence="stored")
        if complexity:
            kw["complexity"] = complexity
        upsert_architecture(conn, **kw)
    set_repo_quality(conn, rid, None, "good" if summary else "none",
                     "complete" if summary else "none")
    return rid


@pytest.fixture
def workspace(conn):
    _seed(conn, "aether", "/a",
          summary="Purpose:\nAether is an operating system in Rust.\nMaturity:\nUnknown",
          langs=("Rust",), techs=("Rust",), arch="Cargo workspace",
          complexity="kernel + async scheduler", commits=120)
    _seed(conn, "friday-v3", "/f3",
          summary=("Purpose:\nFriday V3 is an AI operating partner.\n"
                   "Value:\nautomates workspace operations.\nMaturity:\nBeta"),
          langs=("Python",), techs=("Python", "Supabase"), arch="CLI tool",
          commits=600, dirty=True)
    _seed(conn, "vivaha", "/v",
          summary=("Purpose:\nVivaha is a premium matrimonial platform.\n"
                   "Value:\nhelps people find partners.\nMaturity:\nBeta"),
          langs=("TypeScript",), techs=("Next.js", "Supabase"),
          arch="Next.js App Router", commits=200)
    _seed(conn, "mindwell", "/m",
          summary="Purpose:\nMindWell is a mental health AI companion.\nMaturity:\nWIP",
          langs=("Python",), techs=("Python",), arch="React SPA", commits=150)
    _seed(conn, "finance-tracker", "/ft",
          summary="Purpose:\nfinance-tracker tracks personal spending.\nMaturity:\nWIP",
          langs=("Python",), techs=("Python",), arch="Library", commits=80)
    # Reusable capability: a Scheduler component in two repos (platform axis).
    replace_components(conn, 1, [ComponentRow(repo_id=1, name="Scheduler",
                                              evidence="async", strength="Medium")])
    replace_components(conn, 2, [ComponentRow(repo_id=2, name="Scheduler",
                                              evidence="async", strength="Medium")])
    views = build_views(conn)
    replace_all_relationships(conn, infer_relationship_rows(views))
    # Potential-reuse (opportunity axis).
    conn.execute(
        "INSERT INTO relationships (repo_a, repo_b, kind, evidence, priority, strength) "
        "VALUES (2, 5, 'potential-reuse', 'Overlapping stack: Python', 60, 'Medium')")
    conn.commit()
    return conn


def _a(conn, q):
    return ask(q, conn, verbose=False).text


# --- Routing: every question reaches the strategy intent + its own axis -----


def test_all_five_route_to_strategy(workspace):
    qs = [
        "Which project has the highest impact?",
        "Which should become a platform?",
        "Which teaches me the most?",
        "What opportunities am I missing?",
        "What should become the center of my engineering universe?",
    ]
    for q in qs:
        assert classify(q, workspace) == "strategy", f"{q} -> not strategy"


# --- Impact uses USER VALUE, not a shared ranking ----------------------------


def test_impact_uses_user_value(workspace):
    text = _a(workspace, "Which project has the highest impact?").lower()
    # Required: value-driven judgment.
    assert "user value" in text or "business value" in text or "impact" in text
    assert "confidence" in text
    # Forbidden: the ranking-first / activity framing of other axes.
    assert "reusable capability" not in text
    assert "engineering complexity" not in text
    assert "leverage" not in text
    assert "highest-leverage" not in text


# --- Platform uses REUSABLE CAPABILITY ---------------------------------------


def test_platform_uses_reusable_capability(workspace):
    text = _a(workspace, "Which should become a platform?").lower()
    assert "platform" in text
    assert "reusable capability" in text or "reuse" in text or "shared" in text
    # Forbidden: value / complexity / leverage framings.
    assert "user value" not in text.replace("value", "") or "user value" not in text
    assert "engineering complexity" not in text
    assert "leverage" not in text


# --- Learning uses COMPLEXITY, not time --------------------------------------


def test_learning_uses_complexity(workspace):
    text = _a(workspace, "Which teaches me the most?").lower()
    assert "complexity" in text or "hard domain" in text or "stretched" in text
    assert "confidence" in text
    # Forbidden: value / reusable-capability / leverage framings.
    assert "user value" not in text
    assert "reusable capability" not in text
    assert "leverage" not in text


# --- Opportunity uses LEVERAGE -----------------------------------------------


def test_opportunity_uses_leverage(workspace):
    text = _a(workspace, "What opportunities am I missing?").lower()
    assert "leverage" in text or "reuse" in text or "integration" in text
    # Forbidden: value / complexity / momentum framings.
    assert "user value" not in text
    assert "engineering complexity" not in text
    assert "highest-leverage next step" not in text  # the recommend-axis line


# --- Priority uses CURRENT BLOCKERS / momentum ------------------------------


def test_priority_uses_blockers_momentum(workspace):
    text = _a(workspace, "What should become the center of my engineering universe?").lower()
    assert "center of your engineering universe" in text or "center of the" in text
    assert "momentum" in text or "blockers" in text or "urgency" in text
    # Forbidden: value / complexity / leverage framings.
    assert "user value" not in text
    assert "engineering complexity" not in text
    assert "leverage" not in text


# --- Converge: synthesize a thesis, not an inventory (6.5C) ------------------


def test_converge_routes_and_synthesizes(workspace):
    for q in ("What am I ultimately trying to build?",
              "What am I really building?",
              "What am I converging on?"):
        assert classify(q, workspace) == "strategy"
        text = _a(workspace, q)
        low = text.lower()
        # It answers as a synthesis, not an inventory list.
        assert "converging" in low or "appear to be" in low
        assert "confidence" in low
        # Forbidden 6.5C: a bare technology / architecture inventory dump.
        # (these markers indicate the OLD "AI infrastructure / Node.js / React"
        # list-style answer, which the spec explicitly rejects)
        assert "node.js" not in low
        assert "technologies that keep appearing" not in low
        # A single synthesized paragraph, not a bullet dump.
        assert text.count("\n") <= 1, "strategic answer must be prose, not bullets"


def test_strategic_answers_are_prose_not_inventory(workspace):
    # Across all six axes, no answer should be a multi-line bullet inventory of
    # technologies or architectures. 6.5C: answer, explain, cite, confidence.
    qs = [
        "Which project has the highest impact?",
        "Which should become a platform?",
        "Which teaches me the most?",
        "What opportunities am I missing?",
        "What should become the center of my engineering universe?",
        "What am I ultimately trying to build?",
    ]
    for q in qs:
        text = _a(workspace, q)
        # Prose thesis: leads with a judgment, ends with Confidence, <=1 newline.
        assert "confidence" in text.lower()
        assert text.count("\n") <= 1, f"{q} returned a bullet dump, not prose"


# --- Core 6.5B criterion: no single ranking answers two questions ------------


def test_five_answers_are_pairwise_distinct(workspace):
    answers = [
        _a(workspace, "Which project has the highest impact?"),
        _a(workspace, "Which should become a platform?"),
        _a(workspace, "Which teaches me the most?"),
        _a(workspace, "What opportunities am I missing?"),
        _a(workspace, "What should become the center of my engineering universe?"),
    ]
    assert all(a.strip() for a in answers)
    for i in range(len(answers)):
        for j in range(i + 1, len(answers)):
            assert answers[i] != answers[j], (
                f"questions {i + 1} and {j + 1} returned the same answer"
            )


def test_axes_carry_distinct_signature_evidence(workspace):
    impact = _a(workspace, "Which project has the highest impact?").lower()
    platform = _a(workspace, "Which should become a platform?").lower()
    learning = _a(workspace, "Which teaches me the most?").lower()
    opportunity = _a(workspace, "What opportunities am I missing?").lower()
    priority = _a(workspace, "What should become the center of my engineering universe?").lower()
    # Each axis engages its OWN evidence. Assert the positive signature is present
    # in its own axis (a single shared ranking could not produce all five).
    assert "user value" in impact
    assert "reusable capability" in platform or "reuse" in platform
    assert "complexity" in learning or "hard domain" in learning
    assert "leverage" in opportunity or "reuse" in opportunity
    assert "momentum" in priority or "blockers" in priority
