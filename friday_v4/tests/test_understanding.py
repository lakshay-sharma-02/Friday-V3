"""Hermetic tests for the ``understanding`` compatibility shim (Wave 13a).

The Wave 9 deterministic NLU was replaced by :mod:`friday_v4.nlu` — the
ONE NLU point (LLM-first, deterministic fallback, single ``resolve()``).
``friday_v4.understanding`` is now a *thin shim* that re-exports every
name from ``nlu`` so legacy importers (``missions/planner.py``, older
call sites) keep working.

This suite verifies the shim contract:
- re-export identity: ``understanding.X is nlu.X`` for every public name
- ``resolve_with_llm``: the back-compat helper delegates to the ONE point
- the fallback path still behaves deterministically through the shim
- never-crash: empty/None/garbage input yields clarifications, not raises

Pure logic — no I/O, no DB, no network. The new API is tested in depth
in ``test_nlu.py``; here we only pin the shim's re-export contract.
"""

from __future__ import annotations

from friday_v4 import nlu as real_nlu
from friday_v4 import understanding

from friday_v4.understanding import (
    Assessment,
    Entity,
    EntityType,
    Intent,
    IntentResult,
    ResolvedAction,
    assess,
    classify,
    extract,
    find_type,
    resolve,
    resolve_with_llm,
)


# ==========================================================================
# Re-export identity — the shim re-exports nlu's names, not copies
# ==========================================================================


class TestShimIdentity:
    def test_every_public_name_is_nlu(self):
        """Every shim name must be the *same object* as nlu's name."""
        pairs = {
            "resolve": resolve,
            "classify": classify,
            "extract": extract,
            "find_type": find_type,
            "assess": assess,
            "Assessment": Assessment,
            "Entity": Entity,
            "EntityType": EntityType,
            "Intent": Intent,
            "IntentResult": IntentResult,
            "ResolvedAction": ResolvedAction,
        }
        for name, obj in pairs.items():
            assert getattr(understanding, name) is getattr(real_nlu, name), \
                f"shim {name} is not nlu.{name}"
            assert getattr(understanding, name) is obj, \
                f"imported {name} differs from the shim re-export"

    def test_is_available(self):
        assert understanding.is_available() is True

    def test_shim_resolve_is_one_point(self):
        """The shim's resolve IS nlu.resolve — one parser, no surface copies."""
        assert resolve is real_nlu.resolve

    def test_entity_type_is_shared_enum(self):
        assert EntityType is real_nlu.EntityType
        assert EntityType.PATH.value == "path"
        assert EntityType.FILE.value == "file"


# ==========================================================================
# resolve_with_llm — back-compat helper delegates to the ONE point
# ==========================================================================


class TestResolveWithLlm:
    def test_delegates_with_fake_llm(self):
        class FakeLLM:
            def parse_utterance(self, text: str):
                return {
                    "intent": "execute", "action_type": "testing",
                    "command": "", "target": None, "goal": None,
                    "entities": [], "needs_clarification": False,
                    "clarification": "", "confidence": 0.95,
                }

        action = resolve_with_llm("run the tests", llm=FakeLLM())
        assert action.intent == Intent.EXECUTE
        assert action.can_execute is True
        assert action.to_execution() == {
            "action_type": "testing", "command": "", "goal": "run the tests"}

    def test_none_llm_falls_back(self):
        action = resolve_with_llm("run the tests", llm=None)
        assert action.intent == Intent.EXECUTE
        assert action.action_type == "testing"


# ==========================================================================
# Fallback path through the shim (deterministic rules, LLM absent)
# ==========================================================================


class TestFallbackThroughShim:
    def test_execute(self):
        result = classify("run the tests", llm=None)
        assert result.intent == Intent.EXECUTE
        assert result.action_type == "testing"

    def test_execute_git_subcommand(self):
        result = classify("git status", llm=None)
        assert result.intent == Intent.EXECUTE
        assert result.action_type == "git"
        assert result.command == "status"

    def test_greeting(self):
        result = classify("hello friday", llm=None)
        assert result.intent == Intent.GREETING
        assert result.confidence == 1.0

    def test_plan(self):
        result = classify("plan the auth refactor", llm=None)
        assert result.intent == Intent.PLAN
        assert result.goal is not None

    def test_desktop(self):
        result = classify("focus my code editor", llm=None)
        assert result.intent == Intent.DESKTOP
        assert "editor" in (result.target or "")

    def test_unknown(self):
        result = classify("purple monkey dishwasher", llm=None)
        assert result.intent == Intent.UNKNOWN
        assert result.confidence == 0.0

    def test_empty(self):
        result = classify("", llm=None)
        assert result.intent == Intent.UNKNOWN


# ==========================================================================
# Resolver through the shim — canonical action + execution mapping
# ==========================================================================


class TestResolverThroughShim:
    def test_run_tests_maps_to_execution(self):
        action = resolve("run the tests")
        assert isinstance(action, ResolvedAction)
        assert action.can_execute is True
        assert action.to_execution() == {"action_type": "testing",
                                         "command": "",
                                         "goal": "run the tests"}

    def test_git_status_maps_to_execution(self):
        action = resolve("git status")
        assert action.to_execution()["action_type"] == "git"
        assert action.to_execution()["command"] == "status"

    def test_non_execute_has_no_execution(self):
        for text in ("hello", "what's new", "plan the refactor",
                     "focus my editor"):
            action = resolve(text)
            assert action.to_execution() is None
            assert action.can_execute is False

    def test_unknown_never_raises(self):
        action = resolve("purple monkey dishwasher")
        assert action.intent == Intent.UNKNOWN
        assert action.needs_clarification is True
        assert action.to_execution() is None

    def test_empty_never_raises(self):
        action = resolve("")
        assert action.needs_clarification is True

    def test_to_dict_is_json_safe(self):
        action = resolve("run the tests")
        data = action.to_dict()
        assert data["intent"] == "execute"
        assert data["action_type"] == "testing"
        assert data["can_execute"] is True
        assert isinstance(data["entities"], list)

    def test_research_target_threaded(self):
        """RESEARCH intent carries the LLM/fallback target through resolve."""
        class FakeLLM:
            def parse_utterance(self, text: str):
                return {
                    "intent": "research", "action_type": None,
                    "command": "", "target": "vivaha", "goal": None,
                    "entities": [{"type": "repo", "value": "vivaha"}],
                    "needs_clarification": False, "clarification": "",
                    "confidence": 0.9,
                }

        action = resolve("analyze vivaha", llm=FakeLLM())
        assert action.intent == Intent.RESEARCH
        assert action.target == "vivaha"
        assert any(e.type == EntityType.REPO for e in action.entities)

    def test_ask_target_threaded(self):
        """ASK intent carries the target through resolve (follow-up context)."""
        class FakeLLM:
            def parse_utterance(self, text: str):
                return {
                    "intent": "ask", "action_type": None, "command": "",
                    "target": "mission progress", "goal": None,
                    "entities": [], "needs_clarification": False,
                    "clarification": "", "confidence": 0.9,
                }

        action = resolve("what's the mission progress", llm=FakeLLM())
        assert action.intent == Intent.ASK
        assert action.target == "mission progress"


# ==========================================================================
# Confidence / ambiguity through the shim
# ==========================================================================


class TestConfidenceThroughShim:
    def test_clear_execute_acts(self):
        result = classify("run the tests", llm=None)
        a = assess(result)
        assert a.needs_clarification is False

    def test_unknown_clarifies(self):
        result = classify("purple monkey dishwasher", llm=None)
        a = assess(result)
        assert a.needs_clarification is True
        assert a.clarification

    def test_execute_without_action_type_clarifies(self):
        result = classify("run something weird", llm=None)
        a = assess(result)
        assert result.intent == Intent.EXECUTE
        assert result.action_type is None
        assert a.needs_clarification is True

    def test_llm_clarification_flows_through(self):
        class FakeLLM:
            def parse_utterance(self, text: str):
                return {
                    "intent": "execute", "action_type": None, "command": "",
                    "target": None, "goal": None, "entities": [],
                    "needs_clarification": True,
                    "clarification": "the auth tests or the full suite?",
                    "confidence": 0.4,
                }

        action = resolve("ambiguous request", llm=FakeLLM())
        assert action.needs_clarification
        assert action.clarification == "the auth tests or the full suite?"
