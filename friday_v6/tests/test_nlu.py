"""Hermetic tests for the ONE NLU point (Wave 13a).

LLM-first with a fake LLM client injected; deterministic rules verified
as the fallback when the LLM is absent/unavailable.
"""

from __future__ import annotations

import pytest

from friday_v6.nlu import (
    EntityType,
    Intent,
    assess,
    classify,
    extract,
    resolve,
)


class FakeLLM:
    """Deterministic fake for the LLM client (the ONE point's provider)."""

    def __init__(self, responses=None):
        self.responses = responses or {}
        self.available = True

    def parse_utterance(self, text: str):
        for prefix, data in self.responses.items():
            if text.lower().startswith(prefix):
                return data
        return {
            "intent": "unknown", "action_type": None, "command": "",
            "target": None, "goal": None, "entities": [],
            "needs_clarification": True, "clarification": "rephrase?",
            "confidence": 0.0,
        }


class UnavailableLLM:
    """Simulates an offline/absent LLM → deterministic fallback."""

    available = False

    def parse_utterance(self, text: str):
        return None


# ── LLM-first ─────────────────────────────────────────────────────────


def test_llm_execute_resolves_action():
    llm = FakeLLM({"run the tests": {
        "intent": "execute", "action_type": "testing", "command": "",
        "target": None, "goal": None, "entities": [],
        "needs_clarification": False, "clarification": "",
        "confidence": 0.95,
    }})
    action = resolve("run the tests", llm=llm)
    assert action.intent == Intent.EXECUTE
    assert action.can_execute
    assert action.to_execution() == {
        "action_type": "testing", "command": "", "goal": "run the tests"}


def test_llm_ask_intent():
    llm = FakeLLM({"what's the deal": {
        "intent": "ask", "action_type": None, "command": "",
        "target": None, "goal": None, "entities": [],
        "needs_clarification": False, "clarification": "",
        "confidence": 0.9,
    }})
    action = resolve("what's the deal between vivaha and MindWell", llm=llm)
    assert action.intent == Intent.ASK


def test_llm_entities_flow_through():
    llm = FakeLLM({"analyze vivaha": {
        "intent": "research", "action_type": None, "command": "",
        "target": "vivaha", "goal": None,
        "entities": [{"type": "repo", "value": "vivaha"}],
        "needs_clarification": False, "clarification": "",
        "confidence": 0.9,
    }})
    action = resolve("analyze vivaha", llm=llm)
    assert action.intent == Intent.RESEARCH
    assert action.target == "vivaha"
    assert any(e.type == EntityType.REPO for e in action.entities)


def test_llm_clarification_flows_through():
    llm = FakeLLM({"ambiguous": {
        "intent": "execute", "action_type": None, "command": "",
        "target": None, "goal": None, "entities": [],
        "needs_clarification": True,
        "clarification": "the auth tests or the full suite?",
        "confidence": 0.4,
    }})
    action = resolve("ambiguous request", llm=llm)
    assert action.needs_clarification
    assert action.clarification == "the auth tests or the full suite?"


def test_llm_parse_utterance_handles_sse_shape():
    """The 9router SSE-trailer quirk — data: chunks parse into content."""
    from friday_v6.nlu.llm import _extract_text
    raw = 'data: {"choices":[{"message":{"content":"{\\"intent\\": \\"help\\"}"}}]}'
    text = _extract_text(raw)
    assert text is not None
    assert "help" in text


# ── deterministic fallback (LLM absent) ───────────────────────────────


def test_fallback_execute():
    action = resolve("run the tests", llm=UnavailableLLM())
    assert action.intent == Intent.EXECUTE
    assert action.action_type == "testing"
    assert action.can_execute


def test_fallback_greeting():
    action = resolve("hello friday", llm=UnavailableLLM())
    assert action.intent == Intent.GREETING


def test_fallback_plan():
    action = resolve("ship the auth refactor by friday", llm=UnavailableLLM())
    assert action.intent == Intent.PLAN
    assert action.goal


def test_fallback_unknown_asks_clarification():
    action = resolve("blorptastic quibblewomp", llm=UnavailableLLM())
    assert action.intent == Intent.UNKNOWN
    assert action.needs_clarification


def test_empty_utterance_unknown():
    action = resolve("  ", llm=UnavailableLLM())
    assert action.intent == Intent.UNKNOWN


def test_entities_fallback():
    ents = extract("analyze /home/lakshay/Projects/vivaha", None)
    assert any(e.type == EntityType.PATH for e in ents)


def test_assess_unknown():
    result = classify("zzz nonsense", llm=UnavailableLLM())
    a = assess(result)
    assert a.needs_clarification
