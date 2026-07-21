"""Milestone 6.5A — Evidence Selection regression benchmarks.

Systemic dogfooding failure: the four portfolio questions returned near-
identical evidence packages. Each question must retrieve a *different* evidence
set. These benchmarks lock that in: every question must produce substantially
different evidence, and each must contain only the evidence it should (and not
the evidence it explicitly must NOT contain, per the spec).

All run WITHOUT an LLM (FRIDAY_LLM_MODEL unset); assertions target the
deterministic evidence text the user would actually see.

Questions under test:
  Q1 "What am I building?"            -> building   (purpose / themes / identity)
  Q2 "What engineering strengths...?" -> strengths (architectures / patterns /
                                         languages / systems / complexity)
  Q3 "Where is my engineering effort going?" -> effort (observations / dirty /
                                         activity / commit history / recent)
  Q4 "What kind of engineer am I?"    -> identity  (domains / decisions /
                                         breadth / specialization / direction)
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from friday.db import (
    LangRow,
    SnapshotRow,
    TechRow,
    connect,
    insert_snapshot,
    replace_children,
    set_repo_quality,
    upsert_architecture,
    upsert_repository,
)
from friday.summary import build_views, infer_relationship_rows
from friday.ask import ask


@pytest.fixture
def conn(tmp_path):
    os.environ.pop("FRIDAY_LLM_MODEL", None)
    os.environ.pop("FRIDAY_LLM_API_KEY", None)
    c = connect(tmp_path / "kb.db")
    yield c
    c.close()


def _seed(conn, name, path, *, summary=None, langs=(), techs=(), arch=None,
          dirty=False, commits=100, complexity=None, first="2025-01-01",
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
    # A workspace with enough breadth that the four questions genuinely diverge.
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
    # A Medium engineering decision shared between two repos (for Q4).
    conn.execute(
        "INSERT INTO relationships (repo_a, repo_b, kind, evidence, priority, strength) "
        "VALUES (2, 5, 'shared-framework', 'both Python CLIs', 70, 'Medium')")
    conn.commit()
    # One observation snapshot so Q3 has observation history to cite.
    insert_snapshot(conn, SnapshotRow(
        observed_at="2026-07-10", repo_path="/f3", repo_name="friday-v3",
        default_branch="main", commit_count=600, last_commit_date="2026-07-01",
        is_dirty=1, readme_hash="r", architecture_hash="a", identity_hash="i"))
    conn.commit()
    return conn


def _ans(conn, q):
    return ask(q, conn, verbose=False).text


# --- Q1: What am I building? ------------------------------------------------


def test_q1_building(workspace):
    text = _ans(workspace, "What am I building?").lower()
    # REQUIRED evidence: themes + per-project purpose (portfolio identity).
    assert "recurring themes" in text
    assert "what each project is" in text
    # FORBIDDEN per spec: languages / architectures / activity are NOT the
    # answer to "what am I building".
    assert "language breadth" not in text
    assert "systems and architectures you've built" not in text
    assert "highest recent commit velocity" not in text
    assert "uncommitted changes" not in text


# --- Q2: What engineering strengths am I developing? ------------------------


def test_q2_strengths(workspace):
    text = _ans(workspace, "What engineering strengths am I developing?").lower()
    # REQUIRED evidence: built systems / architectures / languages / patterns.
    assert ("systems and architectures" in text
            or "engineering patterns" in text
            or "language breadth" in text)
    assert "language breadth" in text
    # FORBIDDEN per spec: portfolio purpose/themes ('what am I building').
    assert "recurring themes" not in text
    assert "what each project is" not in text
    # FORBIDDEN per spec: current activity ('where is my effort going').
    assert "highest recent commit velocity" not in text
    assert "uncommitted changes" not in text


# --- Q3: Where is my engineering effort going? -----------------------------


def test_q3_effort(workspace):
    text = _ans(workspace, "Where is my engineering effort going?").lower()
    # REQUIRED evidence: current activity / dirty / velocity / observation.
    assert ("uncommitted changes" in text
            or "highest recent commit velocity" in text
            or "last recorded observation" in text)
    # FORBIDDEN per spec: technology stack.
    assert "technologies that keep appearing" not in text
    assert "duplicate tech" not in text
    # FORBIDDEN: portfolio purpose ('what am I building').
    assert "recurring themes" not in text


# --- Q4: What kind of engineer am I? ----------------------------------------


def test_q4_identity(workspace):
    text = _ans(workspace, "What kind of engineer am I?").lower()
    # REQUIRED evidence: domains / breadth / repeated decisions / direction.
    assert ("engineering domains" in text
            or "engineering decisions you keep making" in text
            or "portfolio direction" in text)
    assert "broad" in text or "focused" in text
    # FORBIDDEN per spec: current activity ('where is my effort going').
    assert "highest recent commit velocity" not in text
    assert "uncommitted changes" not in text
    assert "last recorded observation" not in text
    # FORBIDDEN: portfolio purpose ('what am I building') — its per-project
    # purpose block, specifically, must not appear here.
    assert "what each project is" not in text


# --- Cross-question divergence: the core 6.5A success criterion -------------


def test_all_four_substantially_different(workspace):
    q1 = _ans(workspace, "What am I building?")
    q2 = _ans(workspace, "What engineering strengths am I developing?")
    q3 = _ans(workspace, "Where is my engineering effort going?")
    q4 = _ans(workspace, "What kind of engineer am I?")
    answers = [q1, q2, q3, q4]
    # None are empty, none are identical to another.
    assert all(a.strip() for a in answers)
    for i in range(len(answers)):
        for j in range(i + 1, len(answers)):
            assert answers[i] != answers[j], (
                f"questions {i + 1} and {j + 1} produced identical evidence"
            )


def test_q1_vs_q2_differ_on_forbidden_signals(workspace):
    q1 = _ans(workspace, "What am I building?").lower()
    q2 = _ans(workspace, "What engineering strengths am I developing?").lower()
    # The two must not both carry the other's signature evidence.
    assert ("language breadth" not in q1) and ("language breadth" in q2)
    assert ("recurring themes" in q1) and ("recurring themes" not in q2)


def test_q4_offline_routes_to_portfolio(tmp_path):
    # Offline (no LLM): "What kind of engineer am I?" must still resolve to the
    # portfolio evidence path (not fall through to a generic/empty answer).
    os.environ.pop("FRIDAY_LLM_MODEL", None)
    conn = connect(tmp_path / "kb.db")
    _seed(conn, "aether", "/a",
          summary="Purpose:\nAether is an operating system in Rust.\nMaturity:\nUnknown",
          langs=("Rust",), techs=("Rust",), arch="Cargo workspace", commits=120)
    text = _ans(conn, "What kind of engineer am I?").lower()
    # Not a generic fallback; it reasoned about the body of work.
    assert "engineering domains" in text or "focused" in text or "broad" in text
    conn.close()
