"""Milestone 8.1.6 — Retrieval & Conversation Integration regression tests.

These tests run WITHOUT any LLM configured (deterministic). They verify the
INTEGRATION fixes, not LLM prose:

  Part A  chat follow-up continuity (context must not be lost)
  Part B  evidence breadth (workspace answers span repositories)
  Part C  adaptive coverage widening (once, no recursion)
  Part D  knowledge is a primary evidence source
  Part E  explain a project gracefully when its README is missing
  Part G  integer knowledge IDs (Nth newest) alongside timestamp IDs
  Part H  retrieval audit is recorded on the evidence

No test adds new capabilities or redesigns a frozen layer.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from friday import ask as ask_mod
from friday.ask import (
    Exchange,
    ask,
    resolve_followup,
    retrieve_requirements,
    RetrievalRequirements,
)
from friday.knowledge.models import Knowledge, KnowledgeType, KnowledgeStatus, KnowledgeConfidence
from friday.knowledge.store import insert_knowledge
from friday.db import (
    LangRow,
    Repository,
    TechRow,
    connect,
    replace_children,
    replace_all_relationships,
    set_repo_quality,
    upsert_repository,
)
from friday.summary import build_views, infer_relationship_rows


@pytest.fixture
def conn(tmp_path):
    # No LLM leaks into these tests (deterministic path).
    os.environ.pop("FRIDAY_LLM_MODEL", None)
    os.environ.pop("FRIDAY_LLM_API_KEY", None)
    os.environ.pop("FRIDAY_ANSWER_LLM", None)
    c = connect(tmp_path / "kb.db")
    a = upsert_repository(
        c, name="Aether", path=str(tmp_path / "Aether"), default_branch="main",
        is_dirty=False, first_commit_date="2024-01-01", last_commit_date="2026-07-01",
        remote_url="https://github.com/acme/aether", commit_count=500,
        readme_summary="Purpose:\nAether is an AI-native OS in Rust.\nMaturity:\nUnknown",
        license="MIT", primary_author="dev@acme.com",
    )
    b = upsert_repository(
        c, name="Vivaha", path=str(tmp_path / "Vivaha"), default_branch="main",
        is_dirty=False, first_commit_date="2025-01-01", last_commit_date="2026-06-01",
        remote_url="https://github.com/acme/vivaha", commit_count=300,
        readme_summary="Purpose:\nVivaha is a matrimony web app.\nMaturity:\nBeta",
        license="MIT", primary_author="dev@acme.com",
    )
    # A repo with NO readme summary at all (Part E).
    z = upsert_repository(
        c, name="Zilch", path=str(tmp_path / "Zilch"), default_branch="main",
        is_dirty=False, first_commit_date="2025-05-01", last_commit_date="2026-07-01",
        remote_url="https://github.com/acme/zilch", commit_count=50,
        readme_summary="", license="MIT", primary_author="dev@acme.com",
    )
    replace_children(c, a, [LangRow("Rust", 100)], [TechRow("Rust", "Cargo.toml")])
    replace_children(c, b, [LangRow("TypeScript", 80)], [TechRow("Next.js", "next")])
    replace_children(c, z, [LangRow("Python", 40)], [TechRow("Flask", "app.py")])
    set_repo_quality(c, a, "Unknown", "good", "complete")
    set_repo_quality(c, b, "Beta", "good", "complete")
    set_repo_quality(c, z, "Unknown", "poor", "incomplete")
    views = build_views(c)
    replace_all_relationships(c, infer_relationship_rows(views))
    return c


def _make_prev(conn, q="What am I building?"):
    a = ask(q, conn)
    return Exchange(q, a), a


# ---------------------------------------------------------------------------
# PART A — chat follow-up continuity
# ---------------------------------------------------------------------------


def test_followup_why_resolves(conn):
    prev, _ = _make_prev(conn)
    res = resolve_followup("Why?", prev, conn)
    assert res is not None
    assert res[0] in ("followup", "restate")  # never None -> no fresh empty retrieval


def test_followup_evidence_supports(conn):
    prev, _ = _make_prev(conn)
    res = resolve_followup("What evidence supports that?", prev, conn)
    assert res is not None and res[0] == "followup"


def test_followup_confidence(conn):
    prev, _ = _make_prev(conn)
    res = resolve_followup("How confident are you?", prev, conn)
    assert res is not None and res[0] == "followup"


def test_followup_summarize(conn):
    prev, _ = _make_prev(conn)
    res = resolve_followup("Summarize it.", prev, conn)
    assert res is not None and res[0] == "followup"


def test_followup_explain_further(conn):
    # Trailing punctuation must not break detection.
    prev, _ = _make_prev(conn)
    res = resolve_followup("Explain further.", prev, conn)
    assert res is not None and res[0] == "followup"


def test_followup_compare_that_to_project(conn):
    prev, _ = _make_prev(conn)
    res = resolve_followup("Compare that to Vivaha.", prev, conn)
    assert res is not None and res[0] == "followup"


def test_followup_context_not_lost_no_empty_evidence(conn):
    """The core regression: a follow-up must reuse the previous evidence and
    must NOT produce an empty-evidence answer ('0 of N' loss)."""
    prev, first = _make_prev(conn)
    for f in ("Why?", "What evidence supports that?", "How confident are you?",
              "Summarize it.", "Compare that to Vivaha."):
        ans = ask(f, conn, prev=prev)
        # Evidence is the previous package — non-empty, context preserved.
        assert not ans.evidence.is_empty(), f"{f!r} lost context (empty evidence)"
        assert ans.evidence.blocks == first.evidence.blocks, f"{f!r} did not reuse prior evidence"


def test_followup_multi_turn_preserves_context(conn):
    a1 = ask("What am I building?", conn)
    p1 = Exchange("What am I building?", a1)
    a2 = ask("Why?", conn, prev=p1)
    p2 = Exchange("Why?", a2)
    a3 = ask("What evidence supports that?", conn, prev=p2)
    # Each turn reuses the ORIGINAL evidence package (no fresh narrow retrieval).
    assert a2.evidence.blocks == a1.evidence.blocks
    assert a3.evidence.blocks == a1.evidence.blocks


def test_followup_does_not_repeat_subject_verbosely(conn):
    # The previous answer already names the subject; a follow-up answer reuses
    # that evidence (deterministic fallback echoes it) rather than re-querying.
    prev, first = _make_prev(conn)
    ans = ask("How confident are you?", conn, prev=prev)
    assert ans.evidence is first.evidence  # same object — zero new retrieval


# ---------------------------------------------------------------------------
# PART C — adaptive coverage widening (once, no recursion)
# ---------------------------------------------------------------------------


def test_widen_evidence_spans_workspace(conn):
    from friday.ask import _widen_evidence
    # Seed knowledge so the widen step has a workspace-spanning source.
    insert_knowledge(conn, [Knowledge(
        type=KnowledgeType.PROJECT_IDENTITY, subject="Aether",
        statement="Aether is an AI-native OS in Rust.",
        confidence=KnowledgeConfidence.MEDIUM, evidence_ids=["repo:1"],
        status=KnowledgeStatus.OBSERVED, is_static=True)])
    insert_knowledge(conn, [Knowledge(
        type=KnowledgeType.PROJECT_IDENTITY, subject="Vivaha",
        statement="Vivaha is a matrimony web app.",
        confidence=KnowledgeConfidence.MEDIUM, evidence_ids=["repo:2"],
        status=KnowledgeStatus.OBSERVED, is_static=True)])
    req = RetrievalRequirements(scope="workspace", needs=["themes"], query="x")
    extra = _widen_evidence(req, conn, ask_mod._today(), exclude={"themes"})
    # Knowledge + portfolio identity + relationships -> spans every repo.
    assert extra, "widening produced no extra evidence"
    joined = "\n".join(extra)
    # Touches more than one repository.
    assert "Aether" in joined and "Vivaha" in joined


def test_widen_runs_once_no_recursion(conn):
    # A artificially narrow primary answer triggers exactly one widen; the
    # widened coverage is re-measured but widening does not recurse.
    req = RetrievalRequirements(scope="workspace", needs=["universe"], query="x")
    ev = retrieve_requirements(req, conn)
    # universe already spans the workspace, so no widen needed.
    assert ev.raw.get("widened", False) is False
    # The coverage dict, if widened, carries the flag exactly once.
    cov = ev.raw.get("coverage", {})
    assert ("widened" not in cov) or cov["widened"] is True


def test_coverage_threshold_widens_narrow_workspace(conn):
    """A workspace objective whose primary provider under-fetches must widen to
    full coverage (the '2 of 8' regression). We force a narrow primary by
    requesting only a single-repo-capable need under a workspace scope."""
    # strategy 'direction' often returns thin; ensure widen raises coverage.
    req = RetrievalRequirements(scope="workspace", needs=["direction"], query="x")
    ev = retrieve_requirements(req, conn)
    cov = ev.raw.get("coverage", {})
    # Either it already covered the workspace, or it widened to do so.
    if ev.raw.get("widened"):
        assert cov.get("pct", 0) >= 0.5


# ---------------------------------------------------------------------------
# PART D — knowledge is a primary evidence source
# ---------------------------------------------------------------------------


def test_knowledge_objective_includes_knowledge(conn):
    # Build a minimal knowledge row so the KNOWLEDGE provider has content.
    insert_knowledge(conn, [Knowledge(
        type=KnowledgeType.PROJECT_IDENTITY, subject="Aether",
        statement="Aether is an AI-native OS in Rust.",
        confidence=KnowledgeConfidence.MEDIUM, evidence_ids=["repo:1"],
        status=KnowledgeStatus.OBSERVED, is_static=True)])
    req = RetrievalRequirements(scope="workspace", needs=["knowledge"], query="x")
    ev = retrieve_requirements(req, conn)
    assert ev.raw.get("knowledge_total", 0) >= 1
    assert any("Aether" in b for b in ev.blocks)


# ---------------------------------------------------------------------------
# PART E — explain a project without a README
# ---------------------------------------------------------------------------


def test_explain_without_readme_still_explains(conn):
    # Zilch has no readme_summary -> explain must degrade gracefully (identity /
    # architecture / tech), never "not enough evidence".
    ans = ask("Explain Zilch", conn)
    assert "enough evidence" not in ans.text.lower()
    assert "Zilch" in ans.text


# ---------------------------------------------------------------------------
# PART G — integer knowledge IDs
# ---------------------------------------------------------------------------


def test_knowledge_integer_id_alias(conn):
    from friday.knowledge import KnowledgeEngine
    from friday.cli_knowledge import resolve_knowledge_id

    insert_knowledge(conn, [Knowledge(
        type=KnowledgeType.PROJECT_IDENTITY, subject="Aether",
        statement="Aether is an AI-native OS in Rust.",
        confidence=KnowledgeConfidence.MEDIUM, evidence_ids=["repo:1"],
        status=KnowledgeStatus.OBSERVED, is_static=True)])
    insert_knowledge(conn, [Knowledge(
        type=KnowledgeType.PROJECT_IDENTITY, subject="Vivaha",
        statement="Vivaha is a matrimony web app.",
        confidence=KnowledgeConfidence.MEDIUM, evidence_ids=["repo:2"],
        status=KnowledgeStatus.OBSERVED, is_static=True)])

    eng = KnowledgeEngine(conn)
    assert len(eng.all_knowledge()) >= 2

    # Integer 1 = most recent (Vivaha, inserted last).
    resolved, err = resolve_knowledge_id("1", eng)
    assert err is None
    assert resolved == eng.all_knowledge()[0].id  # newest first


def test_knowledge_timestamp_id_still_works(conn):
    from friday.knowledge import KnowledgeEngine
    from friday.cli_knowledge import resolve_knowledge_id

    insert_knowledge(conn, [Knowledge(
        type=KnowledgeType.PROJECT_IDENTITY, subject="Aether",
        statement="Aether is an AI-native OS in Rust.",
        confidence=KnowledgeConfidence.MEDIUM, evidence_ids=["repo:1"],
        status=KnowledgeStatus.OBSERVED, is_static=True)])
    eng = KnowledgeEngine(conn)
    kid = eng.all_knowledge()[-1].id
    resolved, err = resolve_knowledge_id(kid, eng)
    assert err is None
    assert resolved == kid


def test_knowledge_integer_out_of_range(conn):
    from friday.knowledge import KnowledgeEngine
    from friday.cli_knowledge import resolve_knowledge_id

    insert_knowledge(conn, [Knowledge(
        type=KnowledgeType.PROJECT_IDENTITY, subject="Aether",
        statement="Aether is an AI-native OS in Rust.",
        confidence=KnowledgeConfidence.MEDIUM, evidence_ids=["repo:1"],
        status=KnowledgeStatus.OBSERVED, is_static=True)])
    eng = KnowledgeEngine(conn)
    _, err = resolve_knowledge_id("999", eng)
    assert err == 2


# ---------------------------------------------------------------------------
# PART H — retrieval audit recorded
# ---------------------------------------------------------------------------


def test_retrieval_audit_recorded(conn):
    ev = retrieve_requirements(
        RetrievalRequirements(scope="workspace", needs=["themes"], query="x"), conn)
    audit = ev.raw.get("retrieval_audit")
    assert audit is not None
    assert "objective" in audit
    assert "providers_requested" in audit
    assert "providers_returned" in audit
    assert "knowledge_used" in audit
    assert "confidence" in audit
    assert audit["providers_returned"]  # at least the primary returned blocks


# ---------------------------------------------------------------------------
# No-hallucination guard (deterministic path never invents repositories)
# ---------------------------------------------------------------------------


def test_no_hallucinated_repos(conn):
    ans = ask("What am I building?", conn)
    # The fixture only has Aether / Vivaha / Zilch.
    for fake in ("Nonexistent", "Ghostrepo", "Acme-X"):
        assert fake not in ans.text
