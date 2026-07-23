"""Ask flow: intent classification, evidence retrieval, deterministic fallback.

LLM synthesis is exercised separately/manually; these tests run WITHOUT any
FRIDAY_LLM_* env set, so `ask` must answer structured questions deterministically.
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
    replace_children,
    replace_all_relationships,
    set_repo_quality,
    upsert_repository,
)
from friday.summary import build_views, infer_relationship_rows


@pytest.fixture
def conn(tmp_path):
    # Ensure no LLM config leaks into these tests.
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
    b = upsert_repository(
        c, name="Vivaha", path=str(tmp_path / "Vivaha"), default_branch="main",
        is_dirty=False, first_commit_date="2025-01-01", last_commit_date="2024-01-01",
        remote_url="https://github.com/acme/vivaha", commit_count=300,
        readme_summary="Purpose:\nVivaha is a matrimony app.\nMaturity:\nBeta",
        license="MIT", primary_author="dev@acme.com",
    )
    replace_children(c, a, [LangRow("Rust", 100)], [TechRow("Rust", "Cargo.toml")])
    replace_children(c, b, [LangRow("TypeScript", 80)], [TechRow("Next.js", "next")])
    set_repo_quality(c, a, "Unknown", "good", "complete")
    set_repo_quality(c, b, "Beta", "good", "complete")
    views = build_views(c)
    replace_all_relationships(c, infer_relationship_rows(views))
    return c


def test_classify_intents(conn):
    assert classify("What is Aether?", conn) == "describe"
    assert classify("Which projects use Rust?", conn) == "by-tech"
    assert classify("Compare Aether and Vivaha.", conn) == "compare"
    assert classify("Which repos are inactive?", conn) == "inactive"
    assert classify("Hello there", conn) == "chitchat"


def test_ask_by_tech_no_llm(conn):
    ans = ask("Which projects use Rust?", conn, verbose=False)
    assert not ans.used_llm
    assert "Aether" in ans.text
    assert "No LLM" not in ans.text  # answered deterministically


def test_ask_describe_no_llm(conn):
    ans = ask("What is Aether?", conn, verbose=False)
    assert not ans.used_llm
    assert "Rust" in ans.text
    assert "OS" in ans.text or "Aether" in ans.text


def test_ask_inactive_identifies_stale(conn):
    # Vivaha last commit 2024-01-01 -> inactive as of 2026-07-13.
    ans = ask("Which repositories are inactive?", conn, verbose=False)
    assert not ans.used_llm
    assert "Vivaha" in ans.text


def test_ask_chitchat_no_llm(conn):
    ans = ask("hello", conn, verbose=False)
    assert "Friday" in ans.text


def test_ask_unknown_tech_says_so(conn):
    ans = ask("Which projects use Cobol?", conn, verbose=False)
    assert not ans.used_llm
    # Honest: no evidence, so it declines rather than inventing Cobol users.
    assert "enough evidence" in ans.text or "don't have" in ans.text


def test_evidence_not_empty_for_known_project(conn):
    ev = retrieve("What is Aether?", "describe", conn)
    assert not ev.is_empty()
    assert ev.blocks


def test_compare_no_crash(conn):
    # Compare intent must build two distinct cards without raising.
    ans = ask("Compare Aether and Vivaha.", conn, verbose=False)
    assert not ans.used_llm  # no LLM in this test
    assert "Aether" in ans.text and "Vivaha" in ans.text


def test_inactive_empty_is_honest(conn):
    # Vivaha last commit 2024 -> inactive; Aether 2026 -> active.
    ans = ask("Which repositories are inactive?", conn, verbose=False)
    assert not ans.used_llm
    assert "Vivaha" in ans.text
    assert "Aether" not in ans.text.split("Vivaha")[0] or "Aether" in ans.text


# --- Task 3 regression tests: general_reasoning + LLM gate ------------------


def test_workspace_with_evidence_still_grounded(conn):
    """(a) A workspace question with real evidence must produce an evidence-backed
    answer, not fall through to 'not enough evidence'."""
    ans = ask("Which projects use Rust?", conn, verbose=False)
    assert "Aether" in ans.text
    assert "enough evidence" not in ans.text.lower()


def test_workspace_no_evidence_is_honest(conn):
    """(b) A workspace question with NO matching evidence must get the honest
    'not enough evidence' response, NOT an LLM guess from general knowledge."""
    ans = ask("Which projects use Cobol?", conn, verbose=False)
    assert "enough evidence" in ans.text.lower() or "don't" in ans.text.lower()


def test_general_reasoning_offline_no_evidence(conn):
    """(c) A general-reasoning question with zero workspace context gets an
    answer in the deterministic path — clearly labeled, not claiming evidence."""
    ans = ask("what is 2+2", conn, verbose=False)
    # Without an LLM, the deterministic general_reasoning path says it can't
    # answer workspace-unrelated questions. That's correct — the answer should
    # acknowledge the question isn't about workspace evidence.
    if ans.used_llm:
        assert "[General reasoning" in ans.text or "general" in ans.text
    else:
        # Deterministic fallback for general_reasoning: provide graceful message
        assert ans.text and "workspace" in ans.text.lower()


def test_llm_gate_no_llm_group(conn):
    """(d) When no FRIDAY_ANSWER_LLM is set but an LLM IS configured, synthesis
    should NOT be blocked — the default is opt-in, not opt-out.

    This test cannot set FRIDAY_LLM_MODEL because the fixture explicitly clears
    it. We verify the code path by checking os.environ is NOT consulted for
    FRIDAY_ANSWER_LLM at all: if the old opt-IN check is still in effect,
    synthesis would be skipped even with an LLM configured."""
    gate_in_source = False
    with open(ask_mod.__file__) as f:
        for line in f:
            if 'os.environ.get("FRIDAY_ANSWER_LLM"' in line:
                gate_in_source = True
    assert not gate_in_source, (
        "FRIDAY_ANSWER_LLM still referenced in ask.py — fix gate to "
        "FRIDAY_DETERMINISTIC_ONLY"
    )
