"""Hermetic tests for Wave 17 — Adaptive Identity (tone-direction, MCU test #4).

"Be more casual, Tony" → tone shifts this session AND persists across
restarts, and Friday can explain *why* she talks the way she does.

Covers:
- db.py: set/get/clear tone direction (preferences JSON, untouched
  depth-derived tone column)
- relationship/tones.py: ToneDirection + effective tone/verbosity merge
- relationship/depth.py: engine set_direction/clear_direction/direction/
  explain_tone, status() reports effective values, refresh() preserves
- persona/engine.py: profile() tone follows the direction
- nlu: Intent.STYLE fallback + resolver target threading + LLM-still-wins
- nl_router: "be more casual" → stored; "be yourself again" → cleared;
  "why do you talk that way" → ASK → style_provider (evidence-cited)
- reasoning: style_provider cites the operator's request (Wiring Law)
- briefing: tone-adapts to the explicit direction
- cli_relationship: tone subcommand (set/reset/status shows direction)
- web/dashboard: relationship_state carries tone_direction

Safety laws verified:
- Consent-first: only an explicit direction is stored, verbatim.
- refresh() (daemon sweep) never clobbers an explicit direction.
- Every read degrades gracefully — never a crash.
"""

from __future__ import annotations

from friday_v6 import db


def _conn(tmp_path):
    return db.connect(tmp_path / "v4.db")


# ==========================================================================
# db.py — tone direction persistence
# ==========================================================================


class TestToneDirectionDb:
    def test_set_get_clear_roundtrip(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            assert db.set_tone_direction(
                conn, "operator", tone="casual", verbosity=2,
                request="be more casual, Tony")
            d = db.get_tone_direction(conn, "operator")
            assert d["tone"] == "casual"
            assert d["verbosity"] == 2
            assert d["request"] == "be more casual, Tony"
            assert d["set_at"]
            assert db.clear_tone_direction(conn, "operator")
            assert db.get_tone_direction(conn, "operator") is None
        finally:
            conn.close()

    def test_empty_direction_is_noop(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            assert not db.set_tone_direction(conn, "operator")
        finally:
            conn.close()

    def test_depth_tone_column_untouched(self, tmp_path):
        """The depth-derived tone column stays independent of direction."""
        conn = _conn(tmp_path)
        try:
            from friday_v6.relationship import RelationshipEngine
            RelationshipEngine(conn).refresh()
            before = db.get_relationship(conn, "operator")["tone"]
            db.set_tone_direction(conn, "operator", tone="casual",
                                  request="be casual")
            after = db.get_relationship(conn, "operator")["tone"]
            assert before == after  # column keeps the depth default
        finally:
            conn.close()


# ==========================================================================
# relationship/tones.py — effective merge (direction wins over depth)
# ==========================================================================


class TestEffectiveTone:
    def test_direction_wins_over_depth(self):
        from friday_v6.relationship.tones import (
            ToneDirection,
            effective_tone,
            effective_verbosity,
        )
        d = ToneDirection(tone="casual", verbosity=2)
        assert effective_tone(0.9, d) == "casual"  # depth would be 'close'
        assert effective_verbosity(0.9, d) == 2

    def test_no_direction_uses_depth(self):
        from friday_v6.relationship.tones import effective_tone
        assert effective_tone(0.9) == "close"
        assert effective_tone(0.1) == "neutral"

    def test_from_dict(self):
        from friday_v6.relationship.tones import ToneDirection
        d = ToneDirection.from_dict({"tone": "formal", "verbosity": 1,
                                     "request": "be formal", "set_at": "t"})
        assert d.tone == "formal"
        assert d.active
        assert ToneDirection.from_dict(None) is None
        assert ToneDirection.from_dict({}) is None


# ==========================================================================
# relationship/depth.py — engine methods + status + refresh preservation
# ==========================================================================


class TestRelationshipDirection:
    def test_set_direction_changes_effective_status(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            from friday_v6.relationship import RelationshipEngine
            engine = RelationshipEngine(conn)
            status = engine.set_direction(tone="casual", request="be casual")
            assert status["tone"] == "casual"
            assert status["tone_direction"]["tone"] == "casual"
            assert status["tone_direction"]["request"] == "be casual"
        finally:
            conn.close()

    def test_clear_direction_restores_depth_tone(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            from friday_v6.relationship import RelationshipEngine
            engine = RelationshipEngine(conn)
            engine.set_direction(tone="formal")
            status = engine.clear_direction()
            assert status["tone_direction"] is None
            assert status["tone"] in ("neutral", "warm")  # depth-derived
        finally:
            conn.close()

    def test_refresh_preserves_direction(self, tmp_path):
        """The daemon's periodic refresh must not clobber \"be more casual\"."""
        conn = _conn(tmp_path)
        try:
            from friday_v6.relationship import RelationshipEngine
            engine = RelationshipEngine(conn)
            engine.set_direction(tone="casual", verbosity=2,
                                 request="be more casual")
            status = engine.refresh()
            assert status["tone"] == "casual"
            assert status["tone_direction"]["tone"] == "casual"
        finally:
            conn.close()

    def test_explain_tone_with_request(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            from friday_v6.relationship import RelationshipEngine
            engine = RelationshipEngine(conn)
            engine.set_direction(tone="casual", request="be more casual")
            explanation = engine.explain_tone()
            assert "because you asked me" in explanation
            assert "be more casual" in explanation
        finally:
            conn.close()

    def test_explain_tone_without_direction_is_honest(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            from friday_v6.relationship import RelationshipEngine
            explanation = RelationshipEngine(conn).explain_tone()
            assert "adapts to how close we've become" in explanation
        finally:
            conn.close()


# ==========================================================================
# nlu — Intent.STYLE through the ONE point (LLM-first, rules fallback)
# ==========================================================================


class TestStyleNlu:
    def test_fallback_classifies_style(self):
        from friday_v6.nlu import Intent, resolve
        action = resolve("be more casual")
        assert action.intent == Intent.STYLE
        assert action.target == "casual"
        assert resolve("be more formal").target == "formal"
        assert resolve("be yourself again").target == "reset"

    def test_less_chatter_is_style(self):
        from friday_v6.nlu import Intent, resolve
        assert resolve("less chatter").intent == Intent.STYLE

    def test_llm_still_wins_over_keywords(self):
        """The LLM's interpretation wins even when keywords are present."""
        from friday_v6.nlu import Intent, resolve

        class FakeLLM:
            def parse_utterance(self, text):
                return {"intent": "ask", "action_type": None, "command": "",
                        "target": "", "goal": None, "entities": [],
                        "needs_clarification": False, "clarification": "",
                        "confidence": 0.97}

        action = resolve("be more casual", llm=FakeLLM())
        assert action.intent == Intent.ASK  # the model's call, not keywords


# ==========================================================================
# nl_router — "be more casual" / "be yourself again" / "why do you talk…"
# ==========================================================================


class TestStyleRouter:
    def _handler(self, tmp_path):
        from friday_v6.nl_router import TextCommandHandler
        return TextCommandHandler(conn=_conn(tmp_path))

    def test_be_more_casual_sets_and_persists(self, tmp_path):
        handler = self._handler(tmp_path)
        result = handler.handle("be more casual")
        assert result.action == "style"
        assert "more casual" in result.response
        from friday_v6.relationship import RelationshipEngine
        assert RelationshipEngine(handler.conn).direction().tone == "casual"

    def test_be_yourself_again_clears(self, tmp_path):
        handler = self._handler(tmp_path)
        handler.handle("be more formal")
        result = handler.handle("be yourself again")
        assert result.action == "style"
        from friday_v6.relationship import RelationshipEngine
        assert RelationshipEngine(handler.conn).direction() is None

    def test_vague_style_asks_clarification(self, tmp_path):
        handler = self._handler(tmp_path)
        result = handler.handle("change your tone")
        assert result.action == "clarification"

    def test_why_do_you_talk_that_way_explains(self, tmp_path):
        """MCU test #4: explainable identity through ASK → style_provider."""
        handler = self._handler(tmp_path)
        handler.handle("be more casual")
        result = handler.handle("why do you talk that way")
        assert result.action == "chat"
        assert "because you asked me" in result.response

    def test_style_works_without_db(self, tmp_path, monkeypatch):
        """Never crash — no DB, style still answers (can't persist).

        Hermetic: the conn=None path would fall back to ``db.connect()``
        (the real ~/.friday DB) — point it at a tmp path so no real
        writes ever happen.
        """
        from friday_v6 import db as db_mod
        from friday_v6.nl_router import TextCommandHandler
        monkeypatch.setattr(db_mod, "default_db_path",
                            lambda: tmp_path / "v4.db")
        result = TextCommandHandler(conn=None).handle("be more casual")
        assert result.action in ("style", "failed")
        assert result.response


# ==========================================================================
# reasoning — style_provider (Wiring Law: ASK cites the stored direction)
# ==========================================================================


class TestStyleProvider:
    def test_why_tone_cites_request(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            from friday_v6.relationship import RelationshipEngine
            RelationshipEngine(conn).set_direction(
                tone="casual", request="be more casual")
            from friday_v6.reasoning import answer
            ans = answer("why do you talk that way?", conn=conn)
            assert ans.known
            assert "because you asked me" in ans.text
            assert any(e.source == "v4.relationships" for e in ans.evidence)
        finally:
            conn.close()

    def test_style_question_type(self):
        from friday_v6.reasoning import QuestionType, classify
        assert classify("why do you talk that way") == QuestionType.STYLE
        assert classify("why are you so formal") == QuestionType.STYLE


# ==========================================================================
# briefing — tone adapts to the explicit direction
# ==========================================================================


class TestBriefingTone:
    def test_briefing_uses_explicit_direction(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            from friday_v6.relationship import RelationshipEngine
            RelationshipEngine(conn).set_direction(tone="casual",
                                                   request="be casual")
            from friday_v6.briefing import build_briefing
            b = build_briefing(conn, kind="morning")
            assert b.tone == "casual"
        finally:
            conn.close()

    def test_briefing_without_direction_uses_depth(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            from friday_v6.briefing import build_briefing
            b = build_briefing(conn, kind="morning")
            assert b.tone in ("neutral", "warm", "friendly")
        finally:
            conn.close()


# ==========================================================================
# cli_relationship — tone subcommand
# ==========================================================================


class TestToneCli:
    def _args(self, tmp_path, **kw):
        from types import SimpleNamespace
        base = {"db": tmp_path / "v4.db", "json": False, "tone": None,
                "verbosity": None, "reset": False}
        base.update(kw)
        return SimpleNamespace(**base)

    def test_set_tone(self, tmp_path, capsys):
        from friday_v6.cli_relationship import cmd_relationship_tone
        assert cmd_relationship_tone(
            self._args(tmp_path, tone="casual")) == 0
        out = capsys.readouterr().out
        assert "Tone direction set" in out
        conn = _conn(tmp_path)
        try:
            from friday_v6.relationship import RelationshipEngine
            assert RelationshipEngine(conn).direction().tone == "casual"
        finally:
            conn.close()

    def test_reset_tone(self, tmp_path, capsys):
        from friday_v6.cli_relationship import cmd_relationship_tone
        cmd_relationship_tone(self._args(tmp_path, tone="formal"))
        assert cmd_relationship_tone(self._args(tmp_path, reset=True)) == 0
        assert "cleared" in capsys.readouterr().out

    def test_invalid_tone(self, tmp_path, capsys):
        from friday_v6.cli_relationship import cmd_relationship_tone
        assert cmd_relationship_tone(
            self._args(tmp_path, tone="angry")) == 3
        assert "unknown tone" in capsys.readouterr().out


# ==========================================================================
# web/dashboard — relationship_state carries the direction
# ==========================================================================


class TestDashboardDirection:
    def test_relationship_state_includes_direction(self, tmp_path, monkeypatch):
        """Hermetic: point the dashboard at a tmp DB (read-only probe)."""
        db_path = tmp_path / "v4.db"
        conn = _conn(tmp_path)
        try:
            from friday_v6.relationship import RelationshipEngine
            RelationshipEngine(conn).set_direction(tone="formal",
                                                   request="be formal")
        finally:
            conn.close()
        monkeypatch.setattr(db, "default_db_path", lambda: db_path)
        from friday_v6.web import dashboard
        state = dashboard.relationship_state()
        assert isinstance(state, dict)
        assert state.get("tone_direction", {}).get("tone") == "formal"
