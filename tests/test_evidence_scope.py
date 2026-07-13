"""EvidenceScope hardening — permanent regression benchmarks.

Locks in the fix for the final evidence-assembly failure class: a workspace-wide
question must never be answered from one repository's describe dump, and the
evidence span must be verifiable. Every assertion targets GENERAL, deterministic
fields exposed on Evidence.raw (scope / coverage / bias / missing) and the
objective->scope mapping — never a hardcoded repository name, never the LLM.

All run WITHOUT an LLM (FRIDAY_LLM_* unset).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from friday import objective as obj
from friday.ask import RetrievalRequirements, ask
from friday.db import (
    LangRow, SnapshotRow, TechRow, connect, insert_snapshot, replace_all_relationships,
    replace_children, set_repo_quality, upsert_architecture, upsert_repository,
)
from friday.summary import build_views, infer_relationship_rows


@pytest.fixture
def conn(tmp_path):
    os.environ.pop("FRIDAY_LLM_MODEL", None)
    os.environ.pop("FRIDAY_LLM_API_KEY", None)
    c = connect(tmp_path / "kb.db")
    yield c
    c.close()


def _seed(conn, name, path, *, summary=None, langs=(), techs=(), arch=None,
          complexity=None, commits=100, dirty=False, first="2025-01-01",
          last="2026-07-01"):
    rid = upsert_repository(
        conn, name=name, path=path, default_branch="main", is_dirty=dirty,
        first_commit_date=first, last_commit_date=last,
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
          complexity="kernel + async scheduler", commits=120, first="2025-03-01")
    _seed(conn, "friday-v3", "/f3",
          summary=("Purpose:\nFriday V3 is an AI operating partner.\n"
                   "Value:\nautomates workspace operations.\nMaturity:\nBeta"),
          langs=("Python",), techs=("Python", "Supabase"), arch="CLI tool",
          dirty=True, commits=600, first="2025-06-01")
    _seed(conn, "vivaha", "/v",
          summary=("Purpose:\nVivaha is a premium matrimonial platform.\n"
                   "Value:\nhelps people find partners.\nMaturity:\nBeta"),
          langs=("TypeScript",), techs=("Next.js", "Supabase"),
          arch="Next.js App Router", commits=200, first="2025-02-01")
    _seed(conn, "mindwell", "/m",
          summary="Purpose:\nMindWell is a mental health AI companion.\nMaturity:\nWIP",
          langs=("Python",), techs=("Python",), arch="React SPA",
          commits=150, first="2025-04-01")
    _seed(conn, "finance-tracker", "/ft",
          summary="Purpose:\nfinance-tracker tracks personal spending.\nMaturity:\nWIP",
          langs=("Python",), techs=("Python",), arch="Library",
          commits=80, first="2025-05-01")
    views = build_views(conn)
    replace_all_relationships(conn, infer_relationship_rows(views))
    insert_snapshot(conn, SnapshotRow(
        observed_at="2026-07-10", repo_path="/f3", repo_name="friday-v3",
        default_branch="main", commit_count=600, last_commit_date="2026-07-01",
        is_dirty=1, readme_hash="r", architecture_hash="a", identity_hash="i"))
    conn.commit()
    return conn


# --- 1. objective -> EvidenceScope mapping (deterministic, no keywords) -------


def test_scope_mapping_is_deterministic():
    assert obj.scope_for(obj.Objective.EXPLAIN) == obj.EvidenceScope.PROJECT
    assert obj.scope_for(obj.Objective.COMPARE) == obj.EvidenceScope.RELATIONSHIP
    assert obj.scope_for(obj.Objective.THEMES) == obj.EvidenceScope.WORKSPACE
    assert obj.scope_for(obj.Objective.THEME_REPEAT) == obj.EvidenceScope.WORKSPACE
    assert obj.scope_for(obj.Objective.PROFILE) == obj.EvidenceScope.PORTFOLIO
    assert obj.scope_for(obj.Objective.STRENGTHS) == obj.EvidenceScope.PORTFOLIO
    assert obj.scope_for(obj.Objective.DRIFT) == obj.EvidenceScope.TIMELINE
    assert obj.scope_for(obj.Objective.EFFORT) == obj.EvidenceScope.WORKSPACE
    assert obj.secondary_scopes(obj.Objective.EFFORT) == [obj.EvidenceScope.OBSERVATION]
    assert obj.secondary_scopes(obj.Objective.ASSUMPTIONS) == [obj.EvidenceScope.TIMELINE]


# --- 2. Explain uses PROJECT scope --------------------------------------------


def test_explain_uses_project_scope(workspace):
    ans = ask("Explain friday-v3", workspace, verbose=False)
    assert not ans.used_llm
    assert ans.evidence.raw["objective"] == obj.Objective.EXPLAIN
    assert ans.evidence.raw["scope"] == obj.EvidenceScope.PROJECT
    # A single project is the subject; the answer is about that one repo.
    assert ans.evidence.subject == "friday-v3"


# --- 3. Compare uses RELATIONSHIP scope, exactly two repos -------------------


def test_compare_uses_relationship_scope_and_two_repos(workspace):
    ans = ask("Compare Aether and Vivaha", workspace, verbose=False)
    assert not ans.used_llm
    assert ans.evidence.raw["objective"] == obj.Objective.COMPARE
    assert ans.evidence.raw["scope"] == obj.EvidenceScope.RELATIONSHIP
    cov = ans.evidence.raw["coverage"]
    # Both named repos are represented in the evidence.
    assert cov["represented"] >= 2
    # Relationship evidence is mandatory: the two subjects are recorded.
    assert set(ans.evidence.raw.get("subjects") or []) >= {"aether", "vivaha"}


def test_relationship_excludes_unrelated_repos(workspace):
    ans = ask("Compare Aether and Vivaha", workspace, verbose=False)
    # A third, unrelated repo must NOT appear as a subject of the comparison.
    subs = set(ans.evidence.raw.get("subjects") or [])
    assert "MindWell" not in subs
    assert "finance-tracker" not in subs


# --- 4. Themes uses WORKSPACE scope, includes multiple repos ------------------


def test_themes_uses_workspace_scope(workspace):
    ans = ask("What themes keep repeating?", workspace, verbose=False)
    assert not ans.used_llm
    assert ans.evidence.raw["objective"] == obj.Objective.THEME_REPEAT
    assert ans.evidence.raw["scope"] == obj.EvidenceScope.WORKSPACE


def test_workspace_question_includes_multiple_repositories(workspace):
    ans = ask("What am I building?", workspace, verbose=False)
    # The evidence must reference MORE than one repository (the regression: it
    # used to collapse to a single repo's describe dump).
    repo_names = [r.name for r in __import__("friday.query", fromlist=["all_repositories"]).all_repositories(workspace) if r.id is not None]
    mentioned = [n for n in repo_names if n.lower() in ans.text.lower()]
    assert len(mentioned) >= 3, f"workspace answer cited too few repos: {mentioned}"


def test_profile_uses_portfolio_scope(workspace):
    ans = ask("What kind of engineer am I?", workspace, verbose=False)
    assert not ans.used_llm
    assert ans.evidence.raw["objective"] == obj.Objective.PROFILE
    assert ans.evidence.raw["scope"] == obj.EvidenceScope.PORTFOLIO


def test_drift_uses_timeline_scope(workspace):
    ans = ask("Which project has drifted most from its purpose?", workspace, verbose=False)
    assert ans.evidence.raw["objective"] == obj.Objective.DRIFT
    assert ans.evidence.raw["scope"] == obj.EvidenceScope.TIMELINE


# --- 5. Coverage warning when a workspace answer under-represents repos -------


def test_coverage_warning_when_underrepresented(conn):
    # Five repos, none with a stated purpose -> the WORKSPACE themes answer
    # still names them all via a weak maturity signal, but the evidence is
    # thin. The missing-evidence report must name exactly what is absent
    # (Step 5), and the answer must not pretend to rest on real purpose/README
    # evidence.
    for n in ("r1", "r2", "r3", "r4", "r5"):
        _seed(conn, n, "/" + n)
    ans = ask("What am I building?", conn, verbose=False)
    assert ans.evidence.raw["scope"] == obj.EvidenceScope.WORKSPACE
    # Specific missing-evidence kinds are reported (not a bare refusal).
    missing = ans.evidence.raw["missing"]
    assert missing
    assert any("README/purpose summary" in m for m in missing)
    # The user-facing answer surfaces the missing-evidence report.
    assert "missing evidence" in ans.text.lower()


# --- 6. Single-project bias is detected ---------------------------------------


def test_single_project_bias_detected(conn):
    # One repo dominates every theme; the rest have no purpose. The bias guard
    # must flag that one repo holds the majority of cited evidence.
    _seed(conn, "dominant", "/d",
          summary=("Purpose:\nDominant is an AI operating system kernel in Rust.\n"
                   "Value:\ncore infrastructure.\nMaturity:\nBeta"),
          langs=("Rust",), techs=("Rust", "PyTorch"), arch="Cargo workspace")
    for n in ("quiet1", "quiet2", "quiet3"):
        _seed(conn, n, "/" + n)
    ans = ask("What themes keep repeating?", conn, verbose=False)
    bias = ans.evidence.raw["bias"]
    assert bias["flagged"] is True
    assert bias["dominant"] == "dominant"
    assert bias["pct"] > 0.5


# --- 7. No benchmark passes merely because one repo dominates retrieval -------


def test_workspace_answer_not_single_repo_collapse(workspace):
    # The canonical regression: a workspace question must NOT produce a
    # single-repo describe dump. Assert the answer objective is workspace-wide
    # and the evidence spans more than one repository.
    ans = ask("What am I building?", workspace, verbose=False)
    assert ans.evidence.raw["scope"] == obj.EvidenceScope.WORKSPACE
    cov = ans.evidence.raw["coverage"]
    assert cov["represented"] >= 2
    assert ans.evidence.raw["objective"] != obj.Objective.EXPLAIN
