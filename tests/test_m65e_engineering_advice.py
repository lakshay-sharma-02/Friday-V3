"""Milestone 6.5E — engineering-advice voice permanent benchmarks.

Friday must answer strategic questions as an experienced engineering partner,
not a repository analyzer. Every opinion carries: Recommendation, Reasoning,
Evidence, Confidence. No ranking dumps, no technology summaries, no commit-count
explanations, no repository descriptions.

Locked in (all run WITHOUT an LLM — deterministic resolver only):
  - Each strategy_* returns a Judgment with all four components.
  - Rendered text embeds Recommendation / Reasoning / Evidence / Confidence.
  - Insufficient evidence yields an honest "Insufficient" opinion, never a guess.
  - The spec's four example questions produce an engineering opinion, not a dump.
  - Forbidden leakage: commit counts, tech/architecture inventory, repo boilerplate.
"""

from __future__ import annotations

import os
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
from friday.strategy import (
    Judgment,
    strategy_converge,
    strategy_impact,
    strategy_learning,
    strategy_opportunity,
    strategy_platform,
    strategy_priority,
)


@pytest.fixture
def conn(tmp_path):
    os.environ.pop("FRIDAY_LLM_MODEL", None)
    os.environ.pop("FRIDAY_LLM_API_KEY", None)
    c = connect(tmp_path / "kb.db")
    yield c
    c.close()


def _seed(conn, name, path, *, summary=None, langs=(), techs=(), arch=None,
          commits=100, complexity=None, dirty=False, blockers=None):
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
        if blockers:
            kw["blockers"] = "; ".join(blockers)
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
    # Reusable capability: a Scheduler component in two repos (platform axis).
    replace_components(conn, 1, [ComponentRow(repo_id=1, name="Scheduler",
                                              evidence="async", strength="Medium")])
    replace_components(conn, 2, [ComponentRow(repo_id=2, name="Scheduler",
                                              evidence="async", strength="Medium")])
    views = build_views(conn)
    replace_all_relationships(conn, infer_relationship_rows(views))
    return conn


def _j(func, conn):
    """A strategy_* returns [rendered_str]; reconstruct the Judgment surface via
    the rendered text + the fact it was a single prose line with 4 components."""
    out = func(conn)
    return out[0]


# --- Every judgment renders the four required components ---------------------


def test_impact_carries_four_components(workspace):
    text = _j(strategy_impact, workspace).lower()
    assert "recommendation:" in text
    assert "reasoning:" in text
    assert "evidence:" in text
    assert "confidence:" in text
    # Axis signature present.
    assert "user value" in text or "impact" in text


def test_platform_carries_four_components(workspace):
    text = _j(strategy_platform, workspace).lower()
    for k in ("recommendation:", "reasoning:", "evidence:", "confidence:"):
        assert k in text
    assert "reusable capability" in text or "reuse" in text


def test_learning_carries_four_components(workspace):
    text = _j(strategy_learning, workspace).lower()
    for k in ("recommendation:", "reasoning:", "evidence:", "confidence:"):
        assert k in text
    assert "complexity" in text or "hard domain" in text or "stretched" in text


def test_opportunity_carries_four_components(workspace):
    text = _j(strategy_opportunity, workspace).lower()
    for k in ("recommendation:", "reasoning:", "evidence:", "confidence:"):
        assert k in text
    assert "leverage" in text or "reuse" in text or "integration" in text


def test_priority_carries_four_components(workspace):
    text = _j(strategy_priority, workspace).lower()
    for k in ("recommendation:", "reasoning:", "evidence:", "confidence:"):
        assert k in text
    assert "momentum" in text or "blockers" in text or "urgency" in text


def test_converge_carries_four_components(workspace):
    text = _j(strategy_converge, workspace).lower()
    for k in ("recommendation:", "reasoning:", "evidence:", "confidence:"):
        assert k in text
    assert "converg" in text or "appear to be" in text


# --- Confidence is one of the allowed levels --------------------------------


def test_confidence_levels_valid(workspace):
    for fn in (strategy_impact, strategy_platform, strategy_learning,
               strategy_opportunity, strategy_priority, strategy_converge):
        text = _j(fn, workspace)
        low = text.lower()
        assert any(level in low for level in
                   ("strong", "medium", "weak", "insufficient")), f"{fn} missing confidence level"


# --- Insufficient evidence: honest, never a guess ---------------------------


def test_insufficient_evidence_is_honest(conn):
    # Empty workspace — none of the axes have evidence.
    for fn in (strategy_impact, strategy_platform, strategy_learning,
               strategy_opportunity, strategy_priority, strategy_converge):
        text = _j(fn, conn)
        low = text.lower()
        assert "insufficient" in low, f"{fn} should admit insufficient evidence"
        assert "recommendation:" in low
        assert "couldn't" in low or "can't" in low or "don't see" in low or "i can't" in low


def test_insufficient_has_no_fabricated_evidence(conn):
    text = _j(strategy_impact, conn)
    # Evidence component should be empty / explicitly none.
    assert "(none yet" in text.lower() or "evidence: (none" in text.lower()


# --- Spec's four example questions produce an opinion, not a dump -----------


def test_spec_example_questions_are_opinions(workspace):
    qs = [
        "What opportunities am I missing?",
        "What project should become a platform?",
        "What would you do?",
        "What project should never merge?",
    ]
    for q in qs:
        text = ask(q, workspace).text
        low = text.lower()
        assert "recommendation:" in low, f"{q} -> no Recommendation"
        assert "reasoning:" in low, f"{q} -> no Reasoning"
        assert "evidence:" in low, f"{q} -> no Evidence"
        assert "confidence:" in low, f"{q} -> no Confidence"
        # Not a multi-line bullet inventory.
        assert text.count("\n") <= 1, f"{q} -> returned a bullet dump"


# --- Forbidden leakage: commit counts / tech inventory / repo boilerplate --


def test_no_commit_count_explanation(workspace):
    # Impact/learning/opportunity must NEVER explain by commit counts. Priority
    # may cite commit *frequency* as a momentum signal, so it is excluded here.
    for q in ("Which project has the highest impact?",
              "Which teaches me the most?",
              "What opportunities am I missing?"):
        text = ask(q, workspace).text.lower()
        assert "commit count" not in text
        assert "commit counts alone" not in text
        assert "commits" not in text  # no raw commit-count basis on these axes


def test_no_technology_inventory_dump(workspace):
    # Converge must synthesize a thesis, not list languages/frameworks.
    text = ask("What am I ultimately trying to build?", workspace).text.lower()
    assert "node.js" not in text
    assert "technologies that keep appearing" not in text
    assert "converg" in text or "appear to be" in text


def test_structured_judgment_object_usable(workspace):
    # The dataclass is importable and renders; lets future frontends show the
    # four parts as distinct UI elements (not just a blob of prose).
    j = Judgment(
        axis="impact", recommendation="x", reasoning="y",
        evidence=["e1"], confidence="Medium",
    )
    assert isinstance(j.render(), str)
    assert "Recommendation: x" in j.render()
