"""Milestone 6.6 — Engineering Judgment layer permanent benchmarks.

Every judgment failure from dogfooding is encoded as a regression guard:
Question / Workspace / Expected objective / Forbidden answer / Reasoning.

All run WITHOUT an LLM (FRIDAY_LLM_MODEL unset) — assertions target the
deterministic objective + rendered evidence text, so a regression toward
"return the wrong evidence" or "collapse distinct questions into one shape"
fails. These tests prove the judgment layer fixes the failures, not just the
benchmark questions: each spec failure maps to a distinct OBJECTIVE, and the
objectives that used to collapse now produce different answers.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from friday import objective as obj
from friday.db import (
    LangRow, TechRow, connect, replace_all_relationships, replace_children,
    set_repo_quality, upsert_repository,
)
from friday.summary import build_views, infer_relationship_rows
from friday.ask import RetrievalRequirements, ask
from friday import query as q


@pytest.fixture
def conn(tmp_path):
    os.environ.pop("FRIDAY_LLM_MODEL", None)
    os.environ.pop("FRIDAY_LLM_API_KEY", None)
    c = connect(tmp_path / "kb.db")
    return c


def _seed(conn, name, path, *, summary=None, langs=(), techs=(), arch=None,
          commits=100, dirty=False):
    rid = upsert_repository(
        conn, name=name, path=path, default_branch="main", is_dirty=dirty,
        first_commit_date="2025-01-01", last_commit_date="2026-07-01",
        remote_url="https://github.com/acme/" + name, commit_count=commits,
        readme_summary=summary, license="MIT", primary_author="dev@acme.com",
    )
    replace_children(conn, rid, [LangRow(l, 10) for l in langs],
                     [TechRow(t, "e") for t in techs])
    if arch:
        conn.execute(
            "INSERT INTO architecture (repo_id, architecture, evidence) VALUES (?,?,?)",
            (rid, arch, "stored"))
        conn.commit()
    set_repo_quality(conn, rid, None, "good" if summary else "none",
                     "complete" if summary else "none")
    return rid


@pytest.fixture
def workspace(conn):
    # Two near-identical AI operating partners + an OS + a matrimony app + a
    # mental-health app — enough purpose/themes for the reflection objectives.
    _seed(conn, "Friday", "/f",
          summary="Purpose:\nFriday is an AI operating partner.\nValue:\nautomates workspace ops.\nMaturity:\nBeta",
          langs=("Python",), techs=("Python", "Supabase"), arch="CLI tool",
          commits=600, dirty=True)
    _seed(conn, "Friday V3", "/f3",
          summary="Purpose:\nFriday V3 is an AI operating partner.\nMaturity:\nBeta",
          langs=("Python",), techs=("Python", "Supabase"), arch="CLI tool",
          commits=200)
    _seed(conn, "Aether", "/a",
          summary="Purpose:\nAether is an operating system in Rust.\nMaturity:\nUnknown",
          langs=("Rust",), techs=("Rust",), arch="Cargo workspace")
    _seed(conn, "Vivaha", "/v",
          summary="Purpose:\nVivaha is a premium matrimonial platform.\nMaturity:\nBeta",
          langs=("TypeScript",), techs=("Next.js", "Supabase"),
          arch="Next.js App Router")
    _seed(conn, "MindWell", "/m",
          summary="Purpose:\nMindWell is a mental health AI companion.\nMaturity:\nWIP",
          langs=("Python",), techs=("Python", "Supabase"), arch="React SPA")
    views = build_views(conn)
    replace_all_relationships(conn, infer_relationship_rows(views))
    return conn


# ---------------------------------------------------------------------------
# 1. judge() names the right objective (the heart of the layer)
# ---------------------------------------------------------------------------


def _objective_for(needs, lens=None):
    req = RetrievalRequirements(needs=list(needs), lens=lens)
    return obj.judge(req).objective


def test_judge_disambiguates_broad_bags():
    # These three used to collapse into one themes dump; the objective layer
    # must split them by which engineering question is asked. Uses realistic
    # LLM-style bags (the model omits the explicit `themes` need and leans on
    # identity/purpose pairings).
    assert _objective_for(("identity", "purpose", "activity", "direction")) == obj.Objective.THEMES
    assert _objective_for(("themes", "theme-repeat", "purpose", "insights")) == obj.Objective.THEME_REPEAT
    assert _objective_for(("engineering-profile", "strengths", "themes", "habits")) == obj.Objective.PROFILE
    # Reflection questions with overlapping context stay distinct.
    assert _objective_for(("themes", "insights", "converge", "purpose")) == obj.Objective.THEME_REPEAT
    assert _objective_for(("engineering-profile", "identity", "themes",
                           "purpose")) == obj.Objective.PROFILE


def test_judge_prefers_explicit_canonical_need():
    # The canonical need that owns an objective's answer must win even inside a
    # broad bag, so THEMES and THEME_REPEAT never collapse.
    assert obj.Objective.THEMES in {_objective_for(("themes",))}
    d = obj.judge(RetrievalRequirements(needs=("theme-repeat",)))
    assert d.objective == obj.Objective.THEME_REPEAT
    assert "theme-repeat" in d.needs
    assert d.needs[0] == "theme-repeat"


def test_judge_lens_authoritative():
    assert _objective_for(("themes",), lens="building") == obj.Objective.THEMES
    assert _objective_for(("themes",), lens="effort") == obj.Objective.EFFORT
    assert _objective_for(("themes",), lens="identity") == obj.Objective.PROFILE
    assert _objective_for(("converge",), lens="converge") == obj.Objective.DIRECTION
    assert _objective_for(("priority",), lens="priority") == obj.Objective.PRIORITIZE
    assert _objective_for(("merge",), lens="merge") == obj.Objective.MERGE
    assert _objective_for(("platform",), lens="platform") == obj.Objective.PLATFORM


# ---------------------------------------------------------------------------
# 2. The spec's judgment failures now produce the right objective + framing
# ---------------------------------------------------------------------------


def test_explain_is_explain_not_themes(workspace):
    ans = ask("Explain Friday", workspace, verbose=False)
    assert not ans.used_llm
    assert ans.evidence.raw["objective"] == obj.Objective.EXPLAIN
    # Leads with the project's identity, not portfolio themes.
    head = ans.text[:120].lower()
    assert "friday" in head and ("ai operating partner" in ans.text.lower()
                                 or "purpose" in head)
    # Forbidden: a portfolio themes / what-am-i-building dump.
    assert "recurring themes across your projects" not in ans.text


def test_compare_is_structured_not_two_descriptions(workspace):
    ans = ask("Compare Friday and Friday V3", workspace, verbose=False)
    assert not ans.used_llm
    assert ans.evidence.raw["objective"] == obj.Objective.COMPARE
    low = ans.text.lower()
    # The 6-part compare contract is present.
    assert "shared goal" in low
    assert "different goals" in low
    assert "architecture differences" in low
    assert "technology differences" in low
    assert "current maturity" in low
    assert "recommendation" in low
    # Forbidden: two concatenated explain-project dumps (no "purpose:" cards).
    assert low.count("purpose:") <= 1


def test_platform_is_platform_not_recommend(workspace):
    ans = ask("Which project should become a platform?", workspace, verbose=False)
    assert not ans.used_llm
    assert ans.evidence.raw["objective"] == obj.Objective.PLATFORM
    low = ans.text.lower()
    assert "recommendation:" in low and "reusable capability" in low
    # Forbidden: a plain "continue X" recommendation framing.
    assert "highest-leverage next step" not in low
    assert "continue " + "friday" not in low.replace("grow friday", "")


def test_merge_is_merge_not_relationship_dump(workspace):
    ans = ask("Which projects should never merge?", workspace, verbose=False)
    assert not ans.used_llm
    assert ans.evidence.raw["objective"] == obj.Objective.MERGE
    low = ans.text.lower()
    assert "recommendation:" in low and "merge" in low
    # Forbidden: a relationship inventory ("Friday and Friday V3: shared ...").
    assert "shared architecture" not in low.split("evidence")[0]


def test_profile_is_profile_not_portfolio_summary(workspace):
    ans = ask("What kind of engineer am I?", workspace, verbose=False)
    assert not ans.used_llm
    assert ans.evidence.raw["objective"] == obj.Objective.PROFILE
    low = ans.text.lower()
    # Engineering-profile framing: domains / breadth / decisions, not themes-led.
    assert "engineering domains" in low or "breadth" in low or "specialization" in low
    # The profile must mention engineering domains, not just a theme list dump
    # identical to "what am i building".
    assert "engineering domains you operate across" in low


# ---------------------------------------------------------------------------
# 3. Answer collapse is gone — these now differ from one another
# ---------------------------------------------------------------------------


def test_no_answer_collapse_across_reflection_questions(workspace):
    building = ask("What am I building?", workspace, verbose=False).text
    themes = ask("What themes keep repeating?", workspace, verbose=False).text
    profile = ask("What kind of engineer am I?", workspace, verbose=False).text
    effort = ask("Where is my engineering effort going?", workspace, verbose=False).text
    opportunities = ask("What opportunities am I missing?", workspace, verbose=False).text
    assumptions = ask("What assumptions keep repeating?", workspace, verbose=False).text
    lessons = ask("What engineering lessons keep repeating?", workspace, verbose=False).text
    surprises = ask("What surprises you?", workspace, verbose=False).text
    evolve = ask("How would you evolve my portfolio?", workspace, verbose=False).text
    center = ask("What should become the center of my engineering universe?",
                 workspace, verbose=False).text

    answers = [building, themes, profile, effort, opportunities, assumptions,
               lessons, surprises, evolve, center]
    # No two answers are byte-identical (the core collapse bug).
    assert len(set(answers)) == len(answers), "two reflection questions collapsed"
    # "themes" and "theme-repeat" are meaningfully different structures.
    assert "themes that are not repeating" in themes.lower()
    assert "themes that are not repeating" not in building.lower()
    # assumptions is its own framing, not a theme list.
    assert "assumptions that keep showing up" in assumptions.lower()
    assert "assumptions that keep showing up" not in themes.lower()


def test_themes_vs_theme_repeat_distinct_objectives(workspace):
    a = ask("What am I building?", workspace, verbose=False)
    b = ask("What themes keep repeating?", workspace, verbose=False)
    assert a.evidence.raw["objective"] == obj.Objective.THEMES
    assert b.evidence.raw["objective"] == obj.Objective.THEME_REPEAT
    assert a.text != b.text


def test_assumptions_distinct_from_themes(workspace):
    a = ask("What themes keep repeating?", workspace, verbose=False)
    b = ask("What assumptions keep repeating?", workspace, verbose=False)
    assert a.evidence.raw["objective"] == obj.Objective.THEME_REPEAT
    assert b.evidence.raw["objective"] == obj.Objective.ASSUMPTIONS
    assert a.text != b.text


def test_lessons_distinct_from_themes(workspace):
    a = ask("What themes keep repeating?", workspace, verbose=False)
    b = ask("What engineering lessons keep repeating?", workspace, verbose=False)
    assert a.evidence.raw["objective"] == obj.Objective.THEME_REPEAT
    assert b.evidence.raw["objective"] == obj.Objective.LESSONS
    assert a.text != b.text


# ---------------------------------------------------------------------------
# 4. The new objectives route to real evidence (no silent fallback)
# ---------------------------------------------------------------------------


def test_new_objectives_do_not_bounce_to_general(workspace):
    for q in (
        "What themes keep repeating?",
        "What assumptions keep repeating?",
        "What engineering lessons keep repeating?",
        "What surprises you?",
        "How would you evolve my portfolio?",
        "Which project has drifted most from its purpose?",
    ):
        ans = ask(q, workspace, verbose=False)
        assert ans.evidence.raw["objective"] != obj.Objective.GENERAL
        assert "don't have enough evidence to answer that" not in ans.text.lower(), q
        assert "intent not recognized" not in ans.text.lower(), q


def test_surprise_is_non_obvious_only(workspace):
    # With seeded data there are no engineering insights -> honest "nothing yet".
    ans = ask("What surprises you?", workspace, verbose=False)
    assert ans.evidence.raw["objective"] == obj.Objective.SURPRISE
    # It must not fall back to a themes/portfolio dump.
    assert "recurring themes across your projects" not in ans.text.lower()
    assert "newest repositories" not in ans.text.lower()


def test_evolve_synthesizes_forward_view(workspace):
    ans = ask("How would you evolve my portfolio?", workspace, verbose=False)
    assert ans.evidence.raw["objective"] == obj.Objective.EVOLVE
    low = ans.text.lower()
    assert "where to invest" in low
    assert "what to consolidate" in low
    assert "what to let go" in low
    # Not a single-axis dump.
    assert low.count("recommendation:") == 0 or "leverage" in low


# ---------------------------------------------------------------------------
# 5. The judgment layer is deterministic and offline-safe
# ---------------------------------------------------------------------------


def test_judge_is_pure(conn):
    req = RetrievalRequirements(needs=("themes", "purpose", "universe"))
    d1 = obj.judge(req)
    d2 = obj.judge(req)
    assert d1.objective == d2.objective
    assert d1.needs == d2.needs


def test_every_objective_has_a_contract():
    for o in (v for v in vars(obj.Objective).values()
              if isinstance(v, str) and not v.startswith("_")):
        # Every objective carries a contract (possibly empty for free-form).
        assert isinstance(obj.contract_for(o), list)
