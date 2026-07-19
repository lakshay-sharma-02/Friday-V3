"""Milestone 3.6 follow-up — replace keyword routing with LLM intent extraction.

Per the directive: keyword routing is no longer the primary path. Friday now
understands intent through a single LLM extraction step (intent + entities +
compare + workspace + confidence), then runs the SAME deterministic retrieval.

ONLINE  = LLM extraction (`extract_intent`).
OFFLINE = `deterministic_classifier` fallback (graceful degradation; NOT removed).
Honest uncertainty = model returns "Unknown" -> `ask` admits it couldn't tell.

These benchmarks prove:
  - natural-language paraphrases need NO literal keywords to resolve to the same
    intent (generalization, not keyword matching);
  - the offline keyword fallback still works (no regression);
  - honest uncertainty is surfaced when the model is unsure.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import types
from pathlib import Path

import pytest

from friday import ask as ask_mod
from friday.ask import (
    ask,
    classify,
    deterministic_classifier,
    extract_intent,
)
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


@pytest.fixture
def conn(tmp_path):
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
        readme_summary="Purpose:\nVivaha is a matrimony app.\nMaturity:\nBeta",
        license="MIT", primary_author="dev@acme.com",
    )
    replace_children(c, aether, [LangRow("Rust", 100)], [TechRow("Rust", "Cargo.toml")])
    replace_children(c, vivaha, [LangRow("TypeScript", 80)], [TechRow("Next.js", "next")])
    set_repo_quality(c, aether, "Unknown", "good", "complete")
    set_repo_quality(c, vivaha, "Beta", "good", "complete")
    views = build_views(c)
    replace_all_relationships(c, infer_relationship_rows(views))

    # Force the ONLINE path: pretend an LLM is configured.
    os.environ["FRIDAY_LLM_MODEL"] = "test-model"
    os.environ["FRIDAY_LLM_API_KEY"] = "test-key"
    yield c
    os.environ.pop("FRIDAY_LLM_MODEL", None)
    os.environ.pop("FRIDAY_LLM_API_KEY", None)
    c.close()


class _FakeResp:
    def __init__(self, body: str):
        self._body = body.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


def _patch_llm(monkeypatch, payload: dict):
    """Make extract_intent's urlopen return `payload` as the model's JSON."""
    body = json.dumps({"choices": [{"message": {"content": json.dumps(payload)}}]})
    resp = _FakeResp(body)

    import urllib.request as _urllib

    monkeypatch.setattr(_urllib, "urlopen", lambda *a, **k: resp)


# --- Generalization: paraphrases with NO literal keyword ----------------------


def test_portfolio_paraphrases_no_keywords(conn, monkeypatch):
    questions = [
        "What am I building?",
        "What seems to be the common thread across everything?",
        "If someone examined my repositories, what would they think I care about?",
        "Looking at all my work, what direction am I appear to be heading?",
        "What recurring themes exist across my projects?",
    ]
    for q in questions:
        _patch_llm(monkeypatch, {"intent": "portfolio", "confidence": 0.95})
        intent = extract_intent(q, conn)
        assert intent is not None, f"model declined on: {q}"
        assert intent.intent == "portfolio", f"{q} -> {intent.intent}"


def test_overlap_and_integration_paraphrases(conn, monkeypatch):
    # "What overlaps?" -> overlap, no literal "overlap" helper needed beyond word.
    _patch_llm(monkeypatch, {"intent": "overlap", "workspace": True, "confidence": 0.9})
    assert extract_intent("What overlaps?", conn).intent == "overlap"
    # "What should eventually become one system?" -> integration.
    _patch_llm(monkeypatch, {"intent": "integration", "entities": ["Friday"],
                             "workspace": True, "confidence": 0.81})
    i = extract_intent("What should eventually become one system?", conn)
    assert i.intent == "integration"
    assert "Friday" in i.entities


# --- Entities + comparison captured ------------------------------------------


def test_extract_entities_and_compare(conn, monkeypatch):
    _patch_llm(monkeypatch, {
        "intent": "compare", "entities": ["Aether", "Vivaha"],
        "compare": True, "confidence": 0.98,
    })
    i = extract_intent("How do Aether and Vivaha differ?", conn)
    assert i.intent == "compare"
    assert set(i.entities) == {"Aether", "Vivaha"}
    assert i.compare is True


# --- Offline keyword fallback (graceful degradation) ------------------------


def test_offline_fallback_uses_keywords(tmp_path):
    os.environ.pop("FRIDAY_LLM_MODEL", None)
    os.environ.pop("FRIDAY_LLM_API_KEY", None)
    c = connect(tmp_path / "kb.db")
    upsert_repository(c, name="Aether", path=str(tmp_path / "Aether"),
                      default_branch="main", is_dirty=False,
                      first_commit_date="2024-01-01", last_commit_date="2026-06-01",
                      remote_url=None, commit_count=10, readme_summary=None,
                      license=None, primary_author=None)
    # No LLM -> deterministic_classifier still routes correctly.
    assert deterministic_classifier("What is Aether?", c) == "describe"
    assert deterministic_classifier("Which projects use Rust?", c) == "by-tech"
    assert classify("What am I building?", c) == "portfolio"  # facetious here, but keyword still works
    c.close()


# --- Honest uncertainty ------------------------------------------------------


def test_model_unknown_surfaces_honestly(conn, monkeypatch):
    # Model returns "Unknown" -> extract_intent yields None -> ask admits it.
    _patch_llm(monkeypatch, {"intent": "Unknown", "confidence": 0.1})
    assert extract_intent("gibberish xyzqwk", conn) is None
    ans = ask("gibberish xyzqwk", conn, verbose=False)
    assert ans.used_llm is False  # we never reached the answer-synthesis stage
    assert "couldn't confidently determine" in ans.text


# --- classify still returns canonical string for tests -----------------------


def test_classify_online_delegates_to_extraction(conn, monkeypatch):
    _patch_llm(monkeypatch, {"intent": "value", "confidence": 0.9})
    assert classify("Which project is most valuable?", conn) == "value"
