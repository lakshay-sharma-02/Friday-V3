"""Hermetic tests for the Wave 10 persona layer (friday_v6.persona).

Operator-directed design: **no keywords, no regex extraction**. Identity
is a *verbatim view over the conversation log* — Friday records what you
actually said, word-for-word (with provenance), and answers identity
questions by quoting it back, cited as v4.exchanges.

Covers:
- learn.py: verbatim record/recent/statement_count over the exchanges log
- engine.py: IdentityEngine remembers verbatim; profile is a view over
  the log; identity_answer quotes statements
- prompts.py: persona context block quotes the operator's own words
- reasoning wiring: "who am I" answers from the log (v4.exchanges),
  "who are you" stays Friday's self-knowledge (v4.self)
- nl_router wiring: every utterance flows into the conversation log
  through the shared brain (feeds conversation + persona)
- CLI: friday6 persona profile / remember

Every test is hermetic: tmp_path DB — never the real ~/.friday.
"""

from __future__ import annotations

import json
import types

from friday_v6 import db
from friday_v6.persona import (
    IdentityEngine,
    recent_statements,
    record_statement,
    statement_count,
)


def _conn(tmp_path):
    return db.connect(tmp_path / "v4.db")


# ==========================================================================
# learn.py — verbatim statement memory (no keywords)
# ==========================================================================


class TestRecordStatements:
    def test_record_and_recent_roundtrip(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            assert record_statement(conn, "call me Lakshay") is True
            stmts = recent_statements(conn, limit=5)
            assert len(stmts) == 1
            assert stmts[0]["content"] == "call me Lakshay"
            assert stmts[0]["when"]  # provenance recorded
            assert statement_count(conn) == 1
        finally:
            conn.close()

    def test_recorded_verbatim_never_parsed(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            record_statement(conn, "I prefer Rust for perf-critical code")
            record_statement(conn, "my name is Jordan")
            stmts = recent_statements(conn, limit=5)
            assert len(stmts) == 2
            # The exact words are the memory — nothing extracted, no
            # name/preference slots invented.
            assert stmts[0]["content"] == "my name is Jordan"
            assert stmts[1]["content"] == "I prefer Rust for perf-critical code"
        finally:
            conn.close()

    def test_newest_first(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            record_statement(conn, "first")
            record_statement(conn, "second")
            stmts = recent_statements(conn, limit=5)
            assert [s["content"] for s in stmts] == ["second", "first"]
        finally:
            conn.close()

    def test_empty_and_none_never_raise(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            assert record_statement(conn, "") is False
            assert record_statement(conn, "   ") is False
            assert record_statement(None, "x") is False
            assert recent_statements(None) == []
            assert statement_count(None) == 0
        finally:
            conn.close()


# ==========================================================================
# engine.py — IdentityEngine
# ==========================================================================


class TestIdentityEngine:
    def test_remember_records_verbatim(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            engine = IdentityEngine(conn)
            ack = engine.remember("call me Lakshay")
            assert ack is not None
            profile = engine.profile()
            assert profile["statements"][0]["content"] == "call me Lakshay"
        finally:
            conn.close()

    def test_profile_empty_when_unknown(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            profile = IdentityEngine(conn).profile()
            assert profile["statements"] == []
        finally:
            conn.close()

    def test_remember_empty_is_none(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            assert IdentityEngine(conn).remember("") is None
            assert IdentityEngine(conn).remember("   ") is None
        finally:
            conn.close()

    def test_identity_answer_quotes_statements(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            engine = IdentityEngine(conn)
            assert engine.identity_answer()["facts"] == []
            engine.remember("call me Lakshay")
            engine.remember("I prefer Python")
            data = engine.identity_answer()
            assert any("call me Lakshay" in f for f in data["facts"])
            assert any("I prefer Python" in f for f in data["facts"])
        finally:
            conn.close()

    def test_greeting_neutral_when_unknown(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            greeting = IdentityEngine(conn).greeting()
            assert "Friday" in greeting
        finally:
            conn.close()


# ==========================================================================
# prompts.py
# ==========================================================================


class TestPrompts:
    def test_context_empty_when_unknown(self, tmp_path):
        from friday_v6.persona import build_persona_context
        assert build_persona_context(None) == ""
        conn = _conn(tmp_path)
        try:
            assert build_persona_context(IdentityEngine(conn).profile()) == ""
        finally:
            conn.close()

    def test_context_quotes_verbatim(self, tmp_path):
        from friday_v6.persona import build_persona_context
        conn = _conn(tmp_path)
        try:
            engine = IdentityEngine(conn)
            engine.remember("call me Lakshay")
            engine.remember("I prefer Rust")
            block = build_persona_context(engine.profile())
            assert "call me Lakshay" in block
            assert "I prefer Rust" in block
        finally:
            conn.close()


# ==========================================================================
# Reasoning wiring — identity answers quote what you said
# ==========================================================================


class TestReasoningWiring:
    def test_who_am_i_answers_from_log(self, tmp_path):
        from friday_v6.reasoning import answer
        conn = _conn(tmp_path)
        try:
            IdentityEngine(conn).remember("call me Lakshay")
            IdentityEngine(conn).remember("I prefer Python")
            a = answer("who am I", conn=conn)
            assert a.known
            assert a.question_type.value == "identity"
            assert "call me Lakshay" in a.text
            assert any("v4.exchanges" in c for c in a.citations)
        finally:
            conn.close()

    def test_who_am_i_unknown_when_nothing_said(self, tmp_path):
        from friday_v6.reasoning import answer
        conn = _conn(tmp_path)
        try:
            a = answer("who am I", conn=conn)
            assert not a.known
        finally:
            conn.close()

    def test_who_are_you_stays_friday_self_knowledge(self, tmp_path):
        from friday_v6.reasoning import answer
        conn = _conn(tmp_path)
        try:
            a = answer("who are you", conn=conn)
            assert a.known
            assert "friday" in a.text.lower()
            assert any("v4.self" in c for c in a.citations)
        finally:
            conn.close()

    def test_classify_identity(self):
        from friday_v6.reasoning import classify, QuestionType
        assert classify("who am I") == QuestionType.IDENTITY
        assert classify("what do you know about me") == QuestionType.IDENTITY


# ==========================================================================
# NLU / nl_router wiring — every utterance flows into the conversation log
# ==========================================================================


class TestNlRouterWiring:
    def test_no_persona_keyword_intent(self):
        """Intent.PERSONA must not exist — no keyword table for persona."""
        from friday_v6.understanding import Intent, classify
        assert not hasattr(Intent, "PERSONA")
        # "call me X" is not special-cased by keywords; it flows through
        # the ordinary conversation path (logged verbatim).
        result = classify("call me Lakshay")
        assert result.intent != "persona"

    def test_talk_records_exchange_verbatim(self, tmp_path):
        from friday_v6.nl_router import TextCommandHandler
        conn = _conn(tmp_path)
        try:
            TextCommandHandler(conn).handle("call me Lakshay")
            # The utterance landed in the conversation log, word-for-word.
            stmts = recent_statements(conn, limit=5)
            assert any(s["content"] == "call me Lakshay" for s in stmts)
        finally:
            conn.close()

    def test_who_am_i_after_talk(self, tmp_path):
        from friday_v6.nl_router import TextCommandHandler
        from friday_v6.reasoning import answer
        conn = _conn(tmp_path)
        try:
            TextCommandHandler(conn).handle("call me Lakshay")
            TextCommandHandler(conn).handle("I prefer Python for tooling")
            a = answer("who am I", conn=conn)
            assert a.known
            assert "call me Lakshay" in a.text
            assert "I prefer Python" in a.text
        finally:
            conn.close()

    def test_ask_reaches_reasoning_identity(self, tmp_path):
        from friday_v6.nl_router import TextCommandHandler
        conn = _conn(tmp_path)
        try:
            TextCommandHandler(conn).handle("call me Lakshay")
            result = TextCommandHandler(conn).handle("who am I")
            assert result.intent == "ask"
            assert "call me Lakshay" in result.response
        finally:
            conn.close()


# ==========================================================================
# CLI
# ==========================================================================


class TestCliPersona:
    def _run(self, argv, tmp_path):
        from friday_v6.cli_persona import main
        return main([*argv, "--db", str(tmp_path / "v4.db")])

    def test_profile_empty(self, capsys, tmp_path):
        rc = self._run(["profile"], tmp_path)
        out = capsys.readouterr().out
        assert rc == 0
        assert "don't know who you are" in out

    def test_profile_after_remember(self, capsys, tmp_path):
        self._run(["remember", "call me Lakshay"], tmp_path)
        capsys.readouterr()  # discard the remember logo/ack output
        rc = self._run(["profile"], tmp_path)
        out = capsys.readouterr().out
        assert rc == 0
        assert "call me Lakshay" in out

    def test_profile_json(self, capsys, tmp_path):
        self._run(["remember", "call me Lakshay"], tmp_path)
        capsys.readouterr()  # discard so the JSON payload parses cleanly
        rc = self._run(["profile", "--json"], tmp_path)
        payload = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert any("call me Lakshay" in s["content"]
                   for s in payload["statements"])

    def test_remember_rejects_empty(self, capsys, tmp_path):
        rc = self._run(["remember", ""], tmp_path)
        assert rc != 0

    def test_remember_roundtrip(self, capsys, tmp_path):
        rc = self._run(["remember", "I prefer Rust"], tmp_path)
        out = capsys.readouterr().out
        assert rc == 0
        assert "Noted" in out

    def test_cmd_handlers_direct(self, tmp_path, capsys):
        """Command handlers work without the CLI entry point (hermetic)."""
        from friday_v6.cli_persona import cmd_persona_profile, \
            cmd_persona_remember
        dbp = tmp_path / "v4.db"
        args = types.SimpleNamespace(text="call me Lakshay", db=dbp, json=True)
        rc = cmd_persona_remember(args)
        assert rc == 0
        args2 = types.SimpleNamespace(db=dbp, json=True)
        rc2 = cmd_persona_profile(args2)
        out = capsys.readouterr().out
        assert rc2 == 0
        assert "call me Lakshay" in out
