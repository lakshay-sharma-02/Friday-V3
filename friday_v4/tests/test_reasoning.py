"""Hermetic tests for the Wave 9 reasoning layer (friday_v4.reasoning).

Covers the full answer pipeline:
- question: classification (identity/status/activity/mission/memory/
  conversation/unknown) + target
- evidence: citation formatting, unknown-text constant
- providers: status/activity/mission/memory pull real V4 DB state
- judgment: no-evidence answers become "I don't know yet" (never fabrication)
- engine: answer() never raises; known answers cite evidence; unknown
  question types degrade honestly; provider failure degrades

Every test is hermetic: tmp_path DB — never the real ~/.friday.
"""

from __future__ import annotations

import pytest

from friday_v4 import db
from friday_v4.reasoning import (
    Answer,
    Evidence,
    QuestionType,
    answer,
    classify,
    parse,
    validate,
)


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
    db.store_memory(conn, "operator.pref", "prefers pytest over nose",
                    source="voice", confidence=0.8)
    return conn


# ==========================================================================
# Question classification
# ==========================================================================


class TestQuestion:
    def test_identity(self):
        assert classify("who are you") == QuestionType.IDENTITY
        assert classify("what are you") == QuestionType.IDENTITY
        assert classify("who made you") == QuestionType.IDENTITY

    def test_status(self):
        assert classify("what's the status of my projects?") == \
            QuestionType.STATUS
        assert classify("how are things going") == QuestionType.STATUS

    def test_activity(self):
        assert classify("what did I do recently?") == \
            QuestionType.ACTIVITY
        assert classify("what happened yesterday") == QuestionType.ACTIVITY

    def test_mission(self):
        assert classify("how is the auth refactor going") == \
            QuestionType.MISSION
        assert classify("what's the mission progress") == QuestionType.MISSION

    def test_memory(self):
        assert classify("what do you know about the parser") == \
            QuestionType.MEMORY
        assert classify("do you remember my preferences") == \
            QuestionType.MEMORY

    def test_conversation(self):
        assert classify("what did we talk about?") == \
            QuestionType.CONVERSATION
        assert classify("recap our conversation") == \
            QuestionType.CONVERSATION
        assert classify("what did we discuss this week") == \
            QuestionType.CONVERSATION

    def test_conversation_vs_activity(self):
        # "what did we do" stays ACTIVITY; "what did we talk about" is
        # CONVERSATION (different trigger, no overlap).
        assert classify("what did we do yesterday") == \
            QuestionType.ACTIVITY
        assert classify("what did we talk about") == \
            QuestionType.CONVERSATION

    def test_unknown(self):
        assert classify("purple monkey dishwasher") == QuestionType.UNKNOWN

    def test_parse_target(self):
        q = parse("what's the status of vivaha?")
        assert q.type == QuestionType.STATUS
        assert q.target == "vivaha"


# ==========================================================================
# Evidence
# ==========================================================================


class TestEvidence:
    def test_citation(self):
        e = Evidence("v4.actions", "3 succeeded", "2026-08-01T10:00:00")
        assert e.cite().startswith("v4.actions")
        assert "3 succeeded" in e.cite()
        assert "2026-08-01" in e.cite()

    def test_citation_without_when(self):
        e = Evidence("v4.missions", "2 active")
        assert e.cite() == "v4.missions — 2 active"

    def test_answer_citations(self):
        a = Answer("q", "text", [Evidence("s", "claim", "2026-08-01")])
        assert a.citations == ["s — claim (2026-08-01)"]


# ==========================================================================
# Providers (evidence-scoped)
# ==========================================================================


class TestProviders:
    def test_identity_knows_itself(self, tmp_path):
        """'who are you' is answered from self-knowledge (v4.self), the
        same deterministic answer on every surface — never "I don't
        know yet" and never a voice-only canned line."""
        conn = _conn(tmp_path)
        try:
            a = answer("who are you", conn=conn)
            assert a.known
            assert a.question_type == QuestionType.IDENTITY
            assert "friday" in a.text.lower()
            assert any(c.startswith("v4.self") for c in a.citations)
        finally:
            conn.close()

    def test_status_from_state(self, tmp_path):
        conn = _seed(tmp_path)
        try:
            a = answer("what's the status of my projects?", conn=conn)
            assert a.known
            assert a.question_type == QuestionType.STATUS
            assert "mission" in a.text.lower()
            assert any("v4.actions" in c for c in a.citations)
        finally:
            conn.close()

    def test_activity_from_audit(self, tmp_path):
        conn = _seed(tmp_path)
        try:
            a = answer("what did I do recently?", conn=conn)
            assert a.known
            assert a.question_type == QuestionType.ACTIVITY
            assert any("v4.actions" in c for c in a.citations)
        finally:
            conn.close()

    def test_mission_progress(self, tmp_path):
        conn = _seed(tmp_path)
        try:
            a = answer("how is the auth refactor going?", conn=conn)
            assert a.known
            assert a.question_type == QuestionType.MISSION
            assert "auth refactor" in a.text.lower()
            assert any("v4.missions" in c for c in a.citations)
        finally:
            conn.close()

    def test_memory_recall(self, tmp_path):
        conn = _seed(tmp_path)
        try:
            a = answer("what do you remember?", conn=conn)
            assert a.known
            assert a.question_type == QuestionType.MEMORY
            assert any("v4.memories" in c for c in a.citations)
        finally:
            conn.close()

    def test_memory_target_scoped(self, tmp_path):
        """'tell me about X' cites only facts mentioning X (typed layer)."""
        conn = _conn(tmp_path)
        try:
            from friday_v4.memory import FactMemory
            facts = FactMemory(conn)
            facts.remember("operator", "name", "Lakshay",
                           source="voice:2026-08-01", confidence=0.95)
            facts.remember("project", "language", "python",
                           source="cli:2026-08-01", confidence=0.9)
            a = answer("tell me about python", conn=conn)
            assert a.known
            assert a.question_type == QuestionType.MEMORY
            assert "python" in a.text
            assert "Lakshay" not in a.text  # target-scoped, not all facts
            assert any("v4.memories" in c for c in a.citations)
        finally:
            conn.close()

    def test_conversation_from_exchanges(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            sid = db.start_session(conn, surface="talk")
            db.log_exchange(conn, sid, "user", "run the tests",
                            intent="execute")
            db.log_exchange(conn, sid, "friday", "Done — 1 passed.")
            db.log_exchange(conn, sid, "user", "ship the auth refactor",
                            intent="plan")
            a = answer("what did we talk about?", conn=conn)
            assert a.known
            assert a.question_type == QuestionType.CONVERSATION
            assert "3 exchange" in a.text
            assert any("v4.exchanges" in c for c in a.citations)
        finally:
            conn.close()

    def test_conversation_empty_answers_unknown(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            a = answer("what did we talk about?", conn=conn)
            assert not a.known
            assert "don't know" in a.text
        finally:
            conn.close()

    def test_empty_db_answers_unknown(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            a = answer("what's the status of my projects?", conn=conn)
            assert not a.known
            assert "don't know" in a.text
        finally:
            conn.close()

    def test_how_is_it_going_with_no_mission_is_honest_pointer(self,
                                                               tmp_path):
        """Wave 19 slice 2: 'how's it going' with zero missions answers
        'no mission in flight' + how to start one — not a bare "I don't
        know yet" (Friday did query the DB; 'none' is real state)."""
        conn = _conn(tmp_path)
        try:
            a = answer("how's it going", conn=conn)
            assert a.known
            assert a.question_type == QuestionType.MISSION
            assert "don't have a mission" in a.text.lower()
            assert "plan one" in a.text.lower()
            assert any(e.source == "v4.missions" for e in a.evidence)
        finally:
            conn.close()


# ==========================================================================
# Judgment — no fabrication
# ==========================================================================


class TestJudgment:
    def test_no_evidence_becomes_unknown(self):
        a = validate(Answer("q", "I think the tests pass", []))
        assert not a.known
        assert "don't know" in a.text

    def test_evidence_kept(self):
        a = validate(Answer("q", "1 mission active",
                            [Evidence("v4.missions", "1 active")]))
        assert a.known
        assert "mission" in a.text

    def test_empty_text_becomes_unknown(self):
        a = validate(Answer("q", "   ", [Evidence("s", "c")]))
        assert not a.known

    def test_provider_failure_never_crashes(self, tmp_path):
        def boom(_q, _conn):
            raise RuntimeError("db gone")

        a = answer("what's the status?", conn=_conn(tmp_path),
                   providers=(boom,))
        assert not a.known  # degraded honestly, no crash


# ==========================================================================
# Engine
# ==========================================================================


class TestEngine:
    def test_unknown_type_honest(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            a = answer("purple monkey dishwasher", conn=conn)
            assert not a.known
        finally:
            conn.close()

    def test_no_conn_never_crashes(self):
        a = answer("what's the status of my projects?", conn=None)
        assert a is not None
        assert a.text  # honest unknown reply

    def test_empty_question_never_crashes(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            a = answer("", conn=conn)
            assert a is not None
        finally:
            conn.close()

    def test_to_dict(self, tmp_path):
        conn = _seed(tmp_path)
        try:
            a = answer("what's the status of my projects?", conn=conn)
            d = a.to_dict()
            assert d["known"] is True
            assert d["question_type"] == "status"
            assert d["evidence"]
        finally:
            conn.close()
