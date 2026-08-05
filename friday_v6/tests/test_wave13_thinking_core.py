"""Hermetic tests for Wave 13 — the Thinking Core (LLM reasoning).

Covers the LLM synthesis provider in ``reasoning/providers.py`` (Law 6:
enhances, never gates):

- **Opt-in:** ``FRIDAY_V4_LLM`` env must be truthy; injected ``llm`` is
  an explicit opt-in.
- **Never gates:** no opt-in, LLM unavailable, network failure, garbage
  output, or no evidence → the deterministic floor stands unchanged.
- **Never fabricates:** citations are kept verbatim on the enhanced
  answer; "I don't know yet" (no evidence) is never sent to the LLM.
- **Conversation-capable:** history is threaded into the synthesis
  prompt; ``friday6 ask`` logs the Q&A exchange.
- **Wiring:** the engine applies the post-pass; ``friday6 ask``,
  ``friday6 talk``/voice, and web all inherit it via ``answer()``.

Every test is hermetic: tmp_path DB, a FakeLLM, no network, no real
``~/.friday`` writes.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from friday_v6 import db
from friday_v6.reasoning import answer, llm_provider, parse
from friday_v6.reasoning.evidence import Answer, Evidence
from friday_v6.reasoning.question import QuestionType


class FakeLLM:
    """Minimal stand-in for ``nlu.LLMClient`` (chat + available)."""

    def __init__(self, reply: str = "Synthesized reply.",
                 available: bool = True, raise_on_chat: bool = False):
        self.reply = reply
        self._available = available
        self.raise_on_chat = raise_on_chat
        self.calls: list[list[dict]] = []

    @property
    def available(self) -> bool:
        return self._available

    def chat(self, messages: list[dict], *, max_tokens: int = 400):
        self.calls.append(messages)
        if self.raise_on_chat:
            raise RuntimeError("proxy down")
        return self.reply


def _conn(tmp_path):
    return db.connect(tmp_path / "v4.db")


def _seed(tmp_path):
    """Seed realistic V4 state: missions + actions + memories."""
    conn = _conn(tmp_path)
    mid = db.create_mission(conn, "ship the auth refactor",
                            status="active")
    sid = db.add_mission_step(conn, mid, "migrate session handling")
    db.update_mission_step(conn, sid, status="completed")
    db.record_action(conn, "testing", goal="run the tests",
                     status="succeeded")
    db.record_action(conn, "git", goal="push origin main", status="denied")
    return conn


# ==========================================================================
# llm_provider — the provider in isolation
# ==========================================================================


class TestLLMProvider:
    def test_no_opt_in_returns_none(self, monkeypatch):
        """No FRIDAY_V4_LLM env and no injected llm → None (floor stands)."""
        monkeypatch.delenv("FRIDAY_V4_LLM", raising=False)
        q = parse("what's the status of my projects?")
        best = Answer(q.text, "floor", [Evidence("v4.self", "c")])
        assert llm_provider(q, conn=None, best=best) is None

    def test_no_evidence_returns_none(self, monkeypatch):
        """An unknown/evidence-less answer is never sent to the LLM."""
        monkeypatch.setenv("FRIDAY_V4_LLM", "1")
        q = parse("purple monkey dishwasher")
        best = Answer(q.text, "I don't know yet", [], known=False)
        assert llm_provider(q, conn=None, best=best) is None

    def test_unavailable_llm_returns_none(self, monkeypatch):
        monkeypatch.setenv("FRIDAY_V4_LLM", "1")
        q = parse("what's the status of my projects?")
        best = Answer(q.text, "floor", [Evidence("v4.self", "c")])
        llm = FakeLLM(available=False)
        assert llm_provider(q, conn=None, best=best, llm=llm) is None
        assert llm.calls == []  # never even asked

    def test_chat_failure_returns_none(self, monkeypatch):
        monkeypatch.setenv("FRIDAY_V4_LLM", "1")
        q = parse("what's the status of my projects?")
        best = Answer(q.text, "floor", [Evidence("v4.self", "c")])
        llm = FakeLLM(raise_on_chat=True)
        assert llm_provider(q, conn=None, best=best, llm=llm) is None

    def test_garbage_output_returns_none(self, monkeypatch):
        monkeypatch.setenv("FRIDAY_V4_LLM", "1")
        q = parse("what's the status of my projects?")
        best = Answer(q.text, "floor", [Evidence("v4.self", "c")])
        assert llm_provider(q, conn=None, best=best,
                            llm=FakeLLM(reply="")) is None
        assert llm_provider(q, conn=None, best=best,
                            llm=FakeLLM(reply="   \n ")) is None

    def test_enhances_keeping_citations(self, monkeypatch):
        """Synthesis rewrites text but the evidence list is unchanged."""
        monkeypatch.setenv("FRIDAY_V4_LLM", "1")
        q = parse("what's the status of my projects?")
        ev = [Evidence("v4.missions", "1 mission — active: 1")]
        best = Answer(q.text, "floor text", ev, QuestionType.STATUS,
                      confidence=0.9)
        out = llm_provider(q, conn=None, best=best,
                           llm=FakeLLM(reply="One mission is active."))
        assert out is not None
        assert out.text == "One mission is active."
        assert out.evidence == ev          # citations kept verbatim
        assert out.question_type == QuestionType.STATUS
        assert out.confidence == 0.9

    def test_fence_stripping(self, monkeypatch):
        monkeypatch.setenv("FRIDAY_V4_LLM", "1")
        q = parse("what's the status of my projects?")
        best = Answer(q.text, "floor", [Evidence("v4.self", "c")])
        out = llm_provider(q, conn=None, best=best,
                           llm=FakeLLM(reply='```text\nOne active mission.\n```'))
        assert out is not None
        assert out.text == "One active mission."

    def test_history_threaded_into_prompt(self, monkeypatch):
        monkeypatch.setenv("FRIDAY_V4_LLM", "1")
        q = parse("and the tests?")
        best = Answer(q.text, "floor", [Evidence("v4.actions", "tests ran")])
        llm = FakeLLM(reply="The tests ran fine.")
        history = [
            {"role": "user", "content": "what's the status?"},
            {"role": "friday", "content": "one mission active"},
        ]
        llm_provider(q, conn=None, best=best, llm=llm, history=history)
        assert llm.calls
        user_prompt = llm.calls[0][1]["content"]
        assert "Recent conversation" in user_prompt
        assert "what's the status?" in user_prompt
        assert "and the tests?" in user_prompt
        assert "Evidence" in user_prompt


# ==========================================================================
# Engine — the post-pass wiring
# ==========================================================================


class TestEngineEnhancement:
    def test_floor_without_llm(self, tmp_path, monkeypatch):
        """No opt-in → deterministic floor, unchanged behavior."""
        monkeypatch.delenv("FRIDAY_V4_LLM", raising=False)
        conn = _seed(tmp_path)
        try:
            a = answer("what's the status of my projects?", conn=conn)
            assert a.known
            assert a.text.startswith("Here's the state of things")
        finally:
            conn.close()

    def test_enhanced_with_injected_llm(self, tmp_path):
        """An injected llm (explicit opt-in) rewrites the floor."""
        conn = _seed(tmp_path)
        try:
            llm = FakeLLM(reply="You have one active mission.")
            a = answer("what's the status of my projects?", conn=conn,
                       llm=llm)
            assert a.known
            assert a.text == "You have one active mission."
            assert any("v4.missions" in c for c in a.citations)
        finally:
            conn.close()

    def test_env_opt_in_uses_client(self, tmp_path, monkeypatch):
        """FRIDAY_V4_LLM=1 builds a client from nlu (patched to FakeLLM)."""
        monkeypatch.setenv("FRIDAY_V4_LLM", "1")
        from friday_v6 import nlu as nlu_mod
        conn = _seed(tmp_path)
        try:
            monkeypatch.setattr(nlu_mod, "LLMClient",
                                lambda *a, **k: FakeLLM(
                                    reply="env-opted-in synthesis"))
            a = answer("what's the status of my projects?", conn=conn)
            assert a.known
            assert a.text == "env-opted-in synthesis"
        finally:
            conn.close()

    def test_llm_failure_keeps_floor(self, tmp_path):
        """LLM down → deterministic floor (never crash, never fabricate)."""
        conn = _seed(tmp_path)
        try:
            a = answer("what's the status of my projects?", conn=conn,
                       llm=FakeLLM(raise_on_chat=True))
            assert a.known
            assert a.text.startswith("Here's the state of things")
        finally:
            conn.close()

    def test_no_evidence_never_asks_llm(self, tmp_path):
        """'I don't know yet' stays real — no LLM call, no invention."""
        conn = _conn(tmp_path)
        try:
            llm = FakeLLM(reply="I made this up")
            a = answer("what's the status of my projects?", conn=conn,
                       llm=llm)
            assert not a.known
            assert "don't know" in a.text
            assert llm.calls == []  # never asked — nothing to enhance
        finally:
            conn.close()

    def test_history_passed_through_engine(self, tmp_path):
        """History flows engine → LLM prompt for a classifiable question."""
        conn = _seed(tmp_path)
        try:
            llm = FakeLLM(reply="Synthesis with context.")
            history = [{"role": "user", "content": "earlier question"}]
            a = answer("what's the status of my projects?", conn=conn,
                       llm=llm, history=history)
            assert a.known
            assert "earlier question" in llm.calls[0][1]["content"]
        finally:
            conn.close()

    def test_provider_override_still_works(self, tmp_path, monkeypatch):
        """Tests that inject fake providers keep working (no LLM)."""
        monkeypatch.delenv("FRIDAY_V4_LLM", raising=False)

        def fake_provider(_q, _conn):
            return Answer("q", "fake floor", [Evidence("v4.self", "c")])

        conn = _conn(tmp_path)
        try:
            a = answer("what's the status?", conn=conn,
                       providers=(fake_provider,))
            assert a.known
            assert a.text == "fake floor"
        finally:
            conn.close()


# ==========================================================================
# cli_ask — conversation-capable + exchange logging
# ==========================================================================


def _ask_args(tmp_path, question):
    return argparse.Namespace(
        question=question.split(),
        db=Path(tmp_path) / "v4.db",
        json=False,
    )


class TestCLIAsk:
    def test_ask_logs_exchange(self, tmp_path, monkeypatch):
        """friday6 ask records the Q&A in the conversation log (Wave 13)."""
        monkeypatch.delenv("FRIDAY_V4_LLM", raising=False)
        from friday_v6.cli_ask import cmd_ask
        conn = _seed(tmp_path)
        conn.close()
        code = cmd_ask(_ask_args(tmp_path, "what's the status?"))
        assert code == 0
        conn = _conn(tmp_path)
        try:
            rows = db.recent_exchanges(conn, limit=10) or []
            roles = [r.get("role") for r in rows]
            assert "user" in roles and "friday" in roles
            assert any(r.get("intent") == "ask" for r in rows)
        finally:
            conn.close()

    def test_ask_followup_has_context(self, tmp_path, monkeypatch):
        """A second ask sees the first as history (no crash, still answers)."""
        monkeypatch.delenv("FRIDAY_V4_LLM", raising=False)
        from friday_v6.cli_ask import cmd_ask
        _seed(tmp_path)
        conn = _conn(tmp_path)
        conn.close()
        assert cmd_ask(_ask_args(tmp_path, "what's the status?")) == 0
        code = cmd_ask(_ask_args(tmp_path, "and the tests?"))
        assert code in (0, 1)  # known or honest unknown — never a crash
