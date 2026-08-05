"""Hermetic tests for Wave 15 — One Presence (shared context session).

The master sentence's first clause: a conversation started in the
terminal continues on the web dashboard and in voice. This slice makes
that real at the data level:

- **One shared session** — every conversational surface (talk / ask /
  voice / web chat) appends to the SAME ``sessions`` row
  (``surface='shared'``, one per UTC day) via
  ``db.get_or_create_shared_session``, instead of a new per-utterance
  session.
- **Time-window recall** — "what did we talk about this morning?" is
  answered from the conversation log filtered to that window
  (``conversation_provider`` + ``db.recent_exchanges_since``), from any
  surface — the MCU test.
- **Classification** — "what did we talk about yesterday/this week"
  stays a CONVERSATION question (the CONVERSATION rule precedes
  ACTIVITY's bare "yesterday"/"today" triggers).

Everything is hermetic: tmp_path DBs, no network, no real ~/.friday,
no subprocesses (utterances used are chat/ask, never execute).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from friday_v6 import db


def _conn(tmp_path):
    return db.connect(tmp_path / "v4.db")


def _utc(hour: int, day_offset: int = 0) -> str:
    """An ISO UTC timestamp at ``hour`` on (today + day_offset)."""
    base = datetime.now(timezone.utc)
    day = (base + timedelta(days=day_offset)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return (day + timedelta(hours=hour)).isoformat(timespec="microseconds")


def _log_at(conn, session_id: str, role: str, content: str, ts: str,
            intent: str = "") -> str:
    """Log an exchange and backdate it to ``ts`` (deterministic tests)."""
    eid = db.log_exchange(conn, session_id, role, content, intent=intent)
    assert eid
    conn.execute("UPDATE exchanges SET created_at = ? WHERE id = ?",
                 (ts, eid))
    conn.commit()
    return eid


# ─────────────────────────────────────────────────────────────────────
# The shared session — one presence across surfaces
# ─────────────────────────────────────────────────────────────────────


class TestSharedSession:
    def test_single_thread_across_surfaces(self, tmp_path):
        """Talk + ask + persona statements all append to ONE session."""
        from friday_v6.nl_router import TextCommandHandler
        from friday_v6.persona.learn import record_statement

        conn = _conn(tmp_path)
        try:
            # Terminal surface.
            terminal = TextCommandHandler(conn, cwd=str(tmp_path))
            terminal.handle("good morning")
            # Voice surface (voice constructs its own handler, same DB).
            voice = TextCommandHandler(conn, cwd=str(tmp_path))
            voice.handle("what did we talk about")
            # Persona learning surface.
            record_statement(conn, "I prefer Rust", surface="voice")

            shared = [s for s in db.list_sessions(conn)
                      if s["surface"] == "shared"]
            assert len(shared) == 1          # ONE presence, not N sessions
            sid = shared[0]["id"]

            all_user = [e for e in db.recent_exchanges(conn, limit=100)
                        if e["role"] == "user"]
            assert all_user, "exchanges must be recorded"
            # Every exchange lives under the shared thread.
            assert all(e["session_id"] == sid for e in all_user)
            assert len(db.session_exchanges(conn, sid, limit=100)) >= 4
        finally:
            conn.close()

    def test_rolls_over_daily(self, tmp_path):
        """One shared session per UTC day — a new day starts a new one,
        the old one stays addressable."""
        conn = _conn(tmp_path)
        try:
            d1 = "2026-08-03T09:00:00+00:00"
            d1b = "2026-08-03T18:00:00+00:00"
            d2 = "2026-08-04T09:00:00+00:00"
            s1 = db.get_or_create_shared_session(conn, now=d1)
            assert s1 == db.get_or_create_shared_session(conn, now=d1b)
            s2 = db.get_or_create_shared_session(conn, now=d2)
            assert s1 != s2
            assert len(db.list_sessions(conn)) == 2
        finally:
            conn.close()

    def test_recent_exchanges_since_window(self, tmp_path):
        """The time-window query: at/after a bound, exclusive end."""
        conn = _conn(tmp_path)
        try:
            sid = db.get_or_create_shared_session(conn)
            e1 = _log_at(conn, sid, "user", "morning note", ts=_utc(9))
            e2 = _log_at(conn, sid, "user", "afternoon note", ts=_utc(15))
            after_noon = [r["id"] for r in
                          db.recent_exchanges_since(conn, _utc(12))]
            assert after_noon == [e2]
            morning = [r["id"] for r in
                       db.recent_exchanges_since(conn, _utc(0),
                                                 until_iso=_utc(12))]
            assert morning == [e1]
        finally:
            conn.close()


# ─────────────────────────────────────────────────────────────────────
# Time-window recall — "what did we talk about this morning?"
# ─────────────────────────────────────────────────────────────────────


class TestTimeWindowRecall:
    def test_this_morning_excludes_afternoon(self, tmp_path):
        from friday_v6.reasoning import answer

        conn = _conn(tmp_path)
        try:
            sid = db.get_or_create_shared_session(conn)
            _log_at(conn, sid, "user", "we are fixing the auth module",
                    ts=_utc(9), intent="execute")
            _log_at(conn, sid, "friday", "done", ts=_utc(9),
                    intent="execute")
            _log_at(conn, sid, "user", "deploy checklist review",
                    ts=_utc(15), intent="ask")
            _log_at(conn, sid, "friday", "reviewed", ts=_utc(15),
                    intent="ask")

            a = answer("what did we talk about this morning?", conn=conn)
            assert a.known
            assert "morning" in a.text
            assert "auth module" in a.text
            assert "deploy checklist" not in a.text

            a2 = answer("what did we talk about this afternoon?", conn=conn)
            assert a2.known
            assert "deploy checklist" in a2.text
        finally:
            conn.close()

    def test_yesterday_vs_today(self, tmp_path):
        from friday_v6.reasoning import answer

        conn = _conn(tmp_path)
        try:
            sid = db.get_or_create_shared_session(conn)
            _log_at(conn, sid, "user", "yesterday's standup notes",
                    ts=_utc(10, day_offset=-1))
            _log_at(conn, sid, "user", "today's planning",
                    ts=_utc(10, day_offset=0))

            a = answer("what did we talk about yesterday?", conn=conn)
            assert a.known
            assert "yesterday's standup" in a.text
            assert "today's planning" not in a.text
        finally:
            conn.close()

    def test_empty_window_is_honest_unknown(self, tmp_path):
        from friday_v6.reasoning import answer

        conn = _conn(tmp_path)
        try:
            sid = db.get_or_create_shared_session(conn)
            _log_at(conn, sid, "user", "an evening note", ts=_utc(20))
            a = answer("what did we talk about this morning?", conn=conn)
            assert not a.known
            assert "don't know" in a.text
        finally:
            conn.close()

    def test_no_window_falls_back_to_recent(self, tmp_path):
        from friday_v6.reasoning import answer

        conn = _conn(tmp_path)
        try:
            sid = db.get_or_create_shared_session(conn)
            _log_at(conn, sid, "user", "the latest topic", ts=_utc(19))
            a = answer("what did we talk about?", conn=conn)
            assert a.known
            assert "latest topic" in a.text
        finally:
            conn.close()


# ─────────────────────────────────────────────────────────────────────
# Classification — time-windowed conversation stays CONVERSATION
# ─────────────────────────────────────────────────────────────────────


class TestClassification:
    def test_time_windowed_conversation_questions(self):
        from friday_v6.reasoning.question import QuestionType, classify

        for q in ("what did we talk about yesterday",
                  "what did we talk about this morning?",
                  "what did we discuss this week",
                  "what did we talk about last night"):
            assert classify(q) == QuestionType.CONVERSATION, q
        # "what did we DO" stays ACTIVITY (no conversation trigger).
        assert classify("what did we do yesterday") == QuestionType.ACTIVITY

    def test_window_bounds(self):
        from friday_v6.reasoning.providers import _conversation_window

        now = datetime(2026, 8, 3, 15, 30, tzinfo=timezone.utc)
        morning = _conversation_window("what did we talk about this "
                                       "morning?", now)
        assert morning[0] == "this morning"
        assert morning[1].startswith("2026-08-03T00:00:00")
        assert morning[2].startswith("2026-08-03T12:00:00")

        yesterday = _conversation_window("what did we discuss yesterday",
                                         now)
        assert yesterday[0] == "yesterday"
        assert yesterday[1].startswith("2026-08-02")
        assert yesterday[2].startswith("2026-08-03")

        # No window named → recent-N behavior.
        assert _conversation_window("what did we talk about?", now) is None


# ─────────────────────────────────────────────────────────────────────
# The MCU test — terminal conversation, recalled from "voice"
# ─────────────────────────────────────────────────────────────────────


class TestMcuOnePresence:
    def test_terminal_conversation_recalled_from_voice(self, tmp_path):
        """'what did we talk about this morning?' from voice returns the
        conversation that happened in the terminal this morning."""
        from friday_v6.nl_router import TextCommandHandler
        from friday_v6.reasoning import answer

        conn = _conn(tmp_path)
        try:
            # Morning, in the terminal.
            terminal = TextCommandHandler(conn, cwd=str(tmp_path))
            terminal.handle("we're migrating the auth module to Rust")
            sid = db.get_or_create_shared_session(conn)
            for e in db.session_exchanges(conn, sid, limit=100):
                conn.execute(
                    "UPDATE exchanges SET created_at = ? WHERE id = ?",
                    (_utc(9), e["id"]))
            conn.commit()

            # Voice surface asks directly against the same DB.
            a = answer("what did we talk about this morning?", conn=conn)
            assert a.known
            assert "auth module" in a.text
            assert any("v4.exchanges" in c for c in a.citations)

            # And through the voice handler (the real surface path).
            voice = TextCommandHandler(conn, cwd=str(tmp_path))
            result = voice.handle("what did we talk about this morning?")
            assert result.intent == "ask"
            assert "auth module" in result.response
        finally:
            conn.close()
