"""Tests for Relationship & Personalization: depth and metrics."""

from datetime import datetime, timezone
import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def conn():
    """Create an in-memory DB with required tables."""
    from friday.db import connect
    conn = connect(":memory:")
    yield conn
    conn.close()


def _ensure_depth_table(conn):
    """Ensure the depth module's internal table exists."""
    from friday.operator.depth import _ensure_tone_table
    _ensure_tone_table(conn)


def _ensure_metrics_table(conn):
    """Ensure the relationship metrics table exists."""
    from friday.relationship import _ensure_table
    _ensure_table(conn)


# ---------------------------------------------------------------------------
# Relationship Depth
# ---------------------------------------------------------------------------


class TestComputeDepth:
    def test_no_data_returns_level_0(self, conn):
        """With no conversation log, depth should be 0 (Stranger)."""
        from friday.operator.depth import compute_relationship_depth
        depth = compute_relationship_depth(conn)
        assert depth.level == 0
        assert depth.label == "Stranger"
        assert depth.total_conversations == 0

    def test_name_known_boosts_to_level_1(self, conn):
        """If name is known, depth should be at least 1."""
        conn.execute(
            "INSERT INTO operator_preferences (key, value, set_at, source) "
            "VALUES ('name', 'Tony', ?, 'explicit')",
            (datetime.now(timezone.utc).isoformat(),),
        )
        conn.commit()

        from friday.operator.depth import compute_relationship_depth
        depth = compute_relationship_depth(conn)
        assert depth.level >= 1
        assert depth.name_known is True
        assert depth.label == "Acquaintance"

    def test_conversations_count(self, conn):
        """Total conversations should be reflected."""
        from friday.db import now_iso
        # Insert some conversation log entries.
        now = now_iso()
        for i in range(3):
            conn.execute(
                "INSERT INTO conversation_log "
                "(channel, channel_id, routing, user_message, friday_reply, conversation_at) "
                "VALUES ('cli', 'test', 'question', ?, ?, ?)",
                (f"message {i}", f"reply {i}", now),
            )
        conn.commit()

        from friday.operator.depth import compute_relationship_depth
        depth = compute_relationship_depth(conn)
        assert depth.total_conversations == 3

    def test_depth_monotonically_increasing(self, conn):
        """Depth should only increase with more conversations."""
        from friday.db import now_iso

        # 0 conversations → Level 0
        from friday.operator.depth import compute_relationship_depth
        d0 = compute_relationship_depth(conn)
        assert d0.level == 0

        now = now_iso()

        # Add 5 conversations → Level 1
        for i in range(5):
            conn.execute(
                "INSERT INTO conversation_log "
                "(channel, channel_id, routing, user_message, friday_reply, conversation_at) "
                "VALUES ('cli', 'test', 'chitchat', ?, ?, ?)",
                (f"test {i}", f"reply {i}", now),
            )
        conn.commit()
        d1 = compute_relationship_depth(conn)
        assert d1.level >= 1

        # Add preferences required for Level 2+
        for pref in [("preferred_technology", "Python"),
                     ("preferred_worker_types", '["worker:shell"]')]:
            conn.execute(
                "INSERT OR REPLACE INTO operator_preferences "
                "(key, value, set_at, source) VALUES (?, ?, ?, 'explicit')",
                (pref[0], pref[1], now),
            )
        conn.commit()

        # Add 20 more conversations → Level 2 (with 2 preferences)
        for i in range(20):
            conn.execute(
                "INSERT INTO conversation_log "
                "(channel, channel_id, routing, user_message, friday_reply, conversation_at) "
                "VALUES ('cli', 'test', 'chitchat', ?, ?, ?)",
                (f"test {i}", f"reply {i}", now),
            )
        conn.commit()
        d2 = compute_relationship_depth(conn)
        assert d2.level >= 2

    def test_preferences_boost_depth(self, conn):
        """Having multiple preferences should help depth progression."""
        from friday.db import now_iso
        now = now_iso()

        # Add 20 conversations + 3 preferences → should reach Level 2
        for i in range(20):
            conn.execute(
                "INSERT INTO conversation_log "
                "(channel, channel_id, routing, user_message, friday_reply, conversation_at) "
                "VALUES ('cli', 'test', 'chitchat', ?, ?, ?)",
                (f"test {i}", f"reply {i}", now),
            )

        for pref in [("preferred_technology", "Python"),
                     ("preferred_worker_types", '["worker:shell"]'),
                     ("no_notifications", "false")]:
            conn.execute(
                "INSERT OR REPLACE INTO operator_preferences "
                "(key, value, set_at, source) VALUES (?, ?, ?, 'explicit')",
                (pref[0], pref[1], now),
            )
        conn.commit()

        from friday.operator.depth import compute_relationship_depth
        depth = compute_relationship_depth(conn)
        assert depth.preferences_known >= 3


# ---------------------------------------------------------------------------
# Tone Parameters
# ---------------------------------------------------------------------------


class TestToneParams:
    def test_level_0_formal(self):
        """Level 0 should be formal with no humor."""
        from friday.operator.depth import get_tone_params
        tone = get_tone_params(0)
        assert tone.formality >= 0.7
        assert tone.humor_allowed is False
        assert tone.proactiveness == 0.0
        assert tone.use_name is False

    def test_level_1_warm(self):
        """Level 1 should use name but no humor."""
        from friday.operator.depth import get_tone_params
        tone = get_tone_params(1)
        assert tone.formality <= 0.7
        assert tone.warmth >= 0.4
        assert tone.use_name is True
        assert tone.humor_allowed is False

    def test_level_2_partner(self):
        """Level 2 should allow humor and be proactive."""
        from friday.operator.depth import get_tone_params
        tone = get_tone_params(2)
        assert tone.humor_allowed is True
        assert tone.proactiveness >= 0.3
        assert tone.use_name is True
        assert tone.use_past_references is True

    def test_level_4_trusted(self):
        """Level 4 should be very warm, high proactiveness."""
        from friday.operator.depth import get_tone_params
        tone = get_tone_params(4)
        assert tone.formality <= 0.3
        assert tone.warmth >= 0.8
        assert tone.humor_allowed is True
        assert tone.proactiveness >= 0.5
        assert tone.use_past_references is True

    def test_fallback_to_level_0(self):
        """Invalid level should fall back to Level 0."""
        from friday.operator.depth import get_tone_params
        tone = get_tone_params(99)
        assert tone.formality >= 0.7
        assert tone.humor_allowed is False

    def test_to_prompt_fragment(self):
        """Tone params should produce a sensible prompt fragment."""
        from friday.operator.depth import get_tone_params
        tone = get_tone_params(2)
        fragment = tone.to_prompt_fragment()
        assert isinstance(fragment, str)
        assert len(fragment) > 10
        assert "humor" in fragment.lower() or "conversational" in fragment.lower()

    def test_level_0_prompt_fragment_formal(self):
        """Level 0 prompt fragment should be formal."""
        from friday.operator.depth import get_tone_params
        tone = get_tone_params(0)
        fragment = tone.to_prompt_fragment()
        assert "formal" in fragment.lower() or "polite" in fragment.lower()


# ---------------------------------------------------------------------------
# Record Tone Use
# ---------------------------------------------------------------------------


class TestRecordToneUse:
    def test_record_tone(self, conn):
        """Recording tone usage should persist to the DB."""
        _ensure_depth_table(conn)
        from friday.operator.depth import record_tone_use
        record_tone_use(conn, "conv_test", 2, "casual_warm", 0.7)

        row = conn.execute(
            "SELECT conversation_id, depth_at_time, tone_used, user_sentiment_avg "
            "FROM tone_history"
        ).fetchone()
        assert row is not None
        assert row["conversation_id"] == "conv_test"
        assert row["depth_at_time"] == 2
        assert row["tone_used"] == "casual_warm"
        assert row["user_sentiment_avg"] == 0.7


# ---------------------------------------------------------------------------
# Relationship Metrics
# ---------------------------------------------------------------------------


class TestRelationshipMetrics:
    def test_empty_metrics(self, conn):
        """Metrics should return sensible defaults when no data exists."""
        _ensure_metrics_table(conn)
        from friday.relationship import (
            compute_interaction_frequency,
            compute_top_topics,
            compute_preferred_times,
            compute_average_response_length,
        )

        freq = compute_interaction_frequency(conn)
        assert freq["total_exchanges"] == 0
        assert freq["exchanges_per_day"] == 0.0

        topics = compute_top_topics(conn)
        assert topics == []

        times = compute_preferred_times(conn)
        assert times["peak_hour"] == -1

        length = compute_average_response_length(conn)
        assert length["avg_words"] == 0

    def test_metrics_with_data(self, conn):
        """Metrics should compute correctly with conversation data."""
        from friday.db import now_iso
        _ensure_metrics_table(conn)
        now = now_iso()

        # Add conversation log entries.
        for i in range(5):
            conn.execute(
                "INSERT INTO conversation_log "
                "(channel, channel_id, routing, user_message, friday_reply, conversation_at) "
                "VALUES ('cli', 'test', ?, ?, ?, ?)",
                ("question", f"Tell me about project {i}",
                 f"Here's what I know about project {i}." * 3, now),
            )
        conn.commit()

        from friday.relationship import compute_interaction_frequency
        freq = compute_interaction_frequency(conn)
        assert freq["total_exchanges"] == 5
        assert freq["exchanges_per_day"] > 0

        from friday.relationship import compute_average_response_length
        length = compute_average_response_length(conn)
        assert length["total_responses"] == 5
        assert length["avg_words"] > 0

    def test_compute_all(self, conn):
        """compute_all_metrics should run without error."""
        _ensure_metrics_table(conn)
        from friday.relationship import compute_all_metrics
        result = compute_all_metrics(conn)
        assert "interaction_frequency" in result
        assert "top_topics" in result
        assert "preferred_times" in result
        assert "avg_response_length" in result


# ---------------------------------------------------------------------------
# Prompt directive
# ---------------------------------------------------------------------------


class TestBuildDirective:
    def test_with_depth_and_tone(self):
        """build_directive() should include depth and tone when provided."""
        from friday.persona.prompts import build_directive
        result = build_directive(
            operator_name="Tony",
            preferences="Python preferred",
            relationship_depth="Level 2 — Partner",
            tone_directive="Be warm and conversational.",
            memories="Likes async patterns",
        )
        assert "Tony" in result
        assert "Level 2" in result
        assert "Partner" in result
        assert "warm" in result.lower()
        assert "async" in result

    def test_empty_returns_empty(self):
        """Empty inputs should return empty string."""
        from friday.persona.prompts import build_directive
        result = build_directive()
        assert result == ""
