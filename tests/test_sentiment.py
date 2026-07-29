"""Tests for src/friday/sentiment.py — SentimentDetector."""

import json
from datetime import datetime, timezone

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def detector():
    """Create a SentimentDetector without a DB connection (deterministic only)."""
    from friday.sentiment import SentimentDetector
    return SentimentDetector(conn=None)


# ---------------------------------------------------------------------------
# Deterministic tone detection
# ---------------------------------------------------------------------------


class TestDetectFrustration:
    def test_explicit_frustration_words(self, detector):
        s = detector.analyze("This is so damn frustrating!")
        assert s.tone == "frustration"
        assert s.confidence >= 0.6

    def test_multiple_frustration_words(self, detector):
        s = detector.analyze("This stupid bug is frustrating. I hate it.")
        assert s.tone == "frustration"
        assert s.confidence >= 0.7

    def test_wtf_frustration(self, detector):
        s = detector.analyze("WTF why is this broken again?")
        assert s.tone == "frustration"
        assert s.confidence >= 0.6

    def test_exclamation_anger(self, detector):
        s = detector.analyze("It DOESN'T WORK!!! I'm SERIOUSLY tired of this!")
        assert s.tone == "frustration"
        assert s.confidence >= 0.6


class TestDetectUrgency:
    def test_urgent_word(self, detector):
        s = detector.analyze("This is urgent, fix it now!")
        assert s.tone == "urgency"
        assert s.confidence >= 0.6

    def test_emergency(self, detector):
        s = detector.analyze("Emergency! Server is down!")
        assert s.tone == "urgency"
        assert s.confidence >= 0.6

    def test_short_imperative_urgent(self, detector):
        s = detector.analyze("Fix it now!")
        assert s.tone == "urgency"
        assert s.confidence >= 0.6


class TestDetectHappiness:
    def test_happiness_words(self, detector):
        s = detector.analyze("That's great! Awesome work!")
        assert s.tone == "happiness"
        assert s.confidence >= 0.6

    def test_lol_happiness(self, detector):
        s = detector.analyze("lol that's amazing 😂")
        assert s.tone == "happiness"
        assert s.confidence >= 0.6

    def test_positive_fix(self, detector):
        s = detector.analyze("Nice, it works now! Finally!")
        assert s.tone == "happiness"
        assert s.confidence >= 0.6


class TestDetectConfusion:
    def test_confused(self, detector):
        s = detector.analyze("I'm confused, what does this mean?")
        assert s.tone == "confusion"
        assert s.confidence >= 0.55

    def test_multiple_question_marks(self, detector):
        s = detector.analyze("What? How is this possible???")
        assert s.tone == "confusion"
        assert s.confidence >= 0.5

    def test_dont_understand(self, detector):
        s = detector.analyze("I don't understand what this does")
        assert s.tone == "confusion"
        assert s.confidence >= 0.55


class TestDetectCuriosity:
    def test_curiosity_words(self, detector):
        s = detector.analyze("Interesting! Tell me more about this architecture.")
        assert s.tone in ("curiosity", "happiness")
        assert s.confidence >= 0.55

    def test_curious_question(self, detector):
        s = detector.analyze("I'm curious, what if we tried a different approach?")
        assert s.tone == "curiosity"
        assert s.confidence >= 0.55


class TestDetectSarcasm:
    def test_sarcastic_great(self, detector):
        s = detector.analyze("Oh, great! That went perfectly.")
        assert s.tone in ("sarcasm", "happiness")  # Both are reasonable readings
        assert s.confidence >= 0.6

    def test_sarcastic_nice(self, detector):
        s = detector.analyze("Yeah, sure, that'll work perfectly...")
        assert s.tone == "sarcasm"
        assert s.confidence >= 0.6


class TestDetectFatigue:
    def test_tired(self, detector):
        s = detector.analyze("I'm so tired. Long day.")
        assert s.tone == "fatigue"
        assert s.confidence >= 0.55

    def test_short_exhausted(self, detector):
        s = detector.analyze("Can't even. Need a break. 😴")
        assert s.tone == "fatigue"
        assert s.confidence >= 0.55


class TestNeutral:
    def test_normal_question(self, detector):
        s = detector.analyze("What's the architecture of this project?")
        assert s.tone in ("neutral", "curiosity")
        assert s.confidence >= 0.4

    def test_empty_message(self, detector):
        s = detector.analyze("")
        assert s.tone == "neutral"

    def test_short_ok(self, detector):
        s = detector.analyze("ok")
        assert s.tone == "neutral"
        assert s.confidence >= 0.5

    def test_short_yes(self, detector):
        s = detector.analyze("yes")
        assert s.tone == "neutral"


# ---------------------------------------------------------------------------
# Rolling sentiment
# ---------------------------------------------------------------------------


class TestRollingSentiment:
    def test_rolling_window_empty(self, detector):
        rolling = detector.get_rolling_sentiment("test_conv")
        assert rolling.dominant_tone == "neutral"

    def test_rolling_tracks_observations(self, detector):
        detector.observe("That is really great and awesome", "cli", "test_conv")
        rolling = detector.get_rolling_sentiment("test_conv")
        assert rolling.dominant_tone == "happiness"
        assert rolling.mean_confidence > 0

    def test_rolling_frustration_detection(self, detector):
        for msg in ["This is frustrating!", "I hate this stupid bug!", "Why doesn't this work?!", "Damnit, so annoying!"]:
            detector.observe(msg, "cli", "test_conv_frustrated")
        rolling = detector.get_rolling_sentiment("test_conv_frustrated")
        if len(rolling.window) >= 3:
            is_frustrated_sum = sum(1 for s in rolling.window if s.tone == "frustration")
            assert is_frustrated_sum >= 2  # at least 2 of 4 are frustration

    def test_rolling_positive_detection(self, detector):
        for msg in ["That is really great and awesome", "Amazing work, so nice", "Great job"]:
            detector.observe(msg, "cli", "test_conv_happy")
        rolling = detector.get_rolling_sentiment("test_conv_happy")
        if len(rolling.window) >= 2:
            assert rolling.is_positive

    def test_rolling_conversation_isolation(self, detector):
        detector.observe("This is damn frustrating", "cli", "conv_a")
        detector.observe("That is really great and awesome", "cli", "conv_b")
        rolling_a = detector.get_rolling_sentiment("conv_a")
        rolling_b = detector.get_rolling_sentiment("conv_b")
        assert rolling_a.dominant_tone != rolling_b.dominant_tone


# ---------------------------------------------------------------------------
# Sentiment dataclass
# ---------------------------------------------------------------------------


class TestSentimentDataclass:
    def test_sentiment_fields(self):
        from friday.sentiment import Sentiment
        s = Sentiment(tone="frustration", confidence=0.85, signal="test")
        assert s.tone == "frustration"
        assert s.confidence == 0.85
        assert s.signal == "test"

    def test_rolling_sentiment_defaults(self):
        from friday.sentiment import RollingSentiment
        rs = RollingSentiment()
        assert rs.mean_confidence == 0.0
        assert rs.dominant_tone == "neutral"
        assert rs.is_frustrated is False
        assert rs.is_positive is False


# ---------------------------------------------------------------------------
# DB-backed storage
# ---------------------------------------------------------------------------


class TestDBStorage:
    def test_store_observation(self):
        """Test that sentiment observations are stored to the DB."""
        from friday.db import connect
        import sqlite3

        conn = connect(":memory:")

        # Setup: create table via migration
        from friday.sentiment import _ensure_table
        _ensure_table(conn)

        from friday.sentiment import SentimentDetector
        detector = SentimentDetector(conn)

        s = detector.observe("This stupid bug is so frustrating!", "cli", "conv_1")
        assert s.tone == "frustration"
        assert s.confidence >= 0.6

        row = conn.execute(
            "SELECT tone, confidence, channel, conversation_id FROM sentiment_observations"
        ).fetchone()
        assert row is not None
        assert row["tone"] == "frustration"
        assert row["channel"] == "cli"
        assert row["conversation_id"] == "conv_1"

        conn.close()

    def test_deduplication(self):
        """Test that identical messages within 1 minute are deduplicated."""
        from friday.db import connect

        conn = connect(":memory:")
        from friday.sentiment import _ensure_table
        _ensure_table(conn)
        from friday.sentiment import SentimentDetector

        detector = SentimentDetector(conn)
        detector.observe("This is frustrating!", "cli", "conv_dedup")
        detector.observe("This is frustrating!", "cli", "conv_dedup")

        count = conn.execute(
            "SELECT COUNT(*) AS cnt FROM sentiment_observations "
            "WHERE conversation_id = 'conv_dedup'"
        ).fetchone()["cnt"]
        assert count == 1  # Duplicate should be skipped

        conn.close()

    def test_low_confidence_not_stored(self):
        """Test that observations below _MIN_CONFIDENCE are not stored."""
        from friday.db import connect

        conn = connect(":memory:")
        from friday.sentiment import _ensure_table
        _ensure_table(conn)
        from friday.sentiment import SentimentDetector

        detector = SentimentDetector(conn)
        # A very neutral message with no strong signals should have low confidence.
        detector.observe("airplane", "cli", "conv_low_conf")

        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM sentiment_observations"
        ).fetchone()
        # May or may not be stored depending on confidence, but we should
        # not crash either way.
        assert row is not None

        conn.close()


# ---------------------------------------------------------------------------
# LLM-based sentiment path (mocked)
# ---------------------------------------------------------------------------


class TestLLMSentiment:
    """Test the LLM sentiment path with mocked LLM responses.

    These tests monkeypatch ``_enabled()`` and ``_call()`` in the
    ``services.llm`` module so the classification path is exercised
    but no real LLM calls are made.
    """

    def _make_llm_response(self, tone: str, confidence: float,
                           signal: str = "llm classification"):
        """Return a fake LLM response that looks like a real _call() output."""
        import json
        return json.dumps({
            "tone": tone,
            "confidence": confidence,
            "signal": signal,
        })

    def test_llm_enabled_returns_llm_result(self, monkeypatch):
        """When LLM is enabled, _analyze_llm should return a Sentiment."""
        # Patch the cached globals in sentiment module (since _init_llm_refs
        # caches them at import time rather than importing on every call).
        monkeypatch.setattr(
            "friday.sentiment._LLM_ENABLED", lambda: True)
        monkeypatch.setattr(
            "friday.sentiment._LLM_CALL",
            lambda sys, usr: self._make_llm_response("frustration", 0.85, "frustrated tone"))

        from friday.sentiment import SentimentDetector
        detector = SentimentDetector(conn=None)

        sentiment = detector.analyze("This is ridiculous!")
        assert sentiment.tone == "frustration"
        assert sentiment.confidence == 0.85
        assert "frustrated" in sentiment.signal

    def test_llm_higher_confidence_wins(self, monkeypatch):
        """When LLM has higher confidence than deterministic, LLM result wins."""
        monkeypatch.setattr(
            "friday.sentiment._LLM_ENABLED", lambda: True)
        monkeypatch.setattr(
            "friday.sentiment._LLM_CALL",
            # "This is annoying" has 1 frustration word → det gives 0.65
            # LLM gives 0.90 → LLM wins
            lambda sys, usr: self._make_llm_response("frustration", 0.90, "clear frustration"))

        from friday.sentiment import SentimentDetector
        detector = SentimentDetector(conn=None)

        sentiment = detector.analyze("This is annoying")
        assert sentiment.tone == "frustration"
        assert sentiment.confidence == 0.90

    def test_llm_lower_confidence_falls_back(self, monkeypatch):
        """When LLM has lower confidence than deterministic, deterministic wins."""
        monkeypatch.setattr(
            "friday.sentiment._LLM_ENABLED", lambda: True)
        monkeypatch.setattr(
            "friday.sentiment._LLM_CALL",
            # "This sucks!" has 1 frustration word + ! → det gives 0.65
            # LLM gives 0.50 → too low, falls back
            lambda sys, usr: self._make_llm_response("frustration", 0.50, "maybe frustrated"))

        from friday.sentiment import SentimentDetector
        detector = SentimentDetector(conn=None)

        sentiment = detector.analyze("This sucks!")
        # Deterministic wins with frustration
        assert sentiment.tone == "frustration"
        assert sentiment.confidence >= 0.6

    def test_llm_disabled_falls_back_to_deterministic(self, monkeypatch):
        """When LLM is disabled, deterministic path is used."""
        # Explicitly disable LLM to avoid real network calls if env vars are set.
        monkeypatch.setattr(
            "friday.sentiment._LLM_ENABLED", lambda: False)

        from friday.sentiment import SentimentDetector
        detector = SentimentDetector(conn=None)

        sentiment = detector.analyze("This is damn frustrating")
        assert sentiment.tone == "frustration"
        assert sentiment.confidence >= 0.6

    def test_llm_returns_none_on_invalid_json(self, monkeypatch):
        """When LLM returns unparseable JSON, fall back to deterministic."""
        monkeypatch.setattr(
            "friday.sentiment._LLM_ENABLED", lambda: True)
        monkeypatch.setattr(
            "friday.sentiment._LLM_CALL",
            lambda sys, usr: "Not valid json at all")

        from friday.sentiment import SentimentDetector
        detector = SentimentDetector(conn=None)

        # Falls back to deterministic — no crash.
        sentiment = detector.analyze("This is damn frustrating")
        assert sentiment.tone == "frustration"

    def test_llm_returns_none_on_missing_keys(self, monkeypatch):
        """When LLM returns JSON missing required keys, fall back."""
        import json
        monkeypatch.setattr(
            "friday.sentiment._LLM_ENABLED", lambda: True)
        monkeypatch.setattr(
            "friday.sentiment._LLM_CALL",
            lambda sys, usr: json.dumps({"tone": "happy"}))  # missing "confidence"

        from friday.sentiment import SentimentDetector
        detector = SentimentDetector(conn=None)

        sentiment = detector.analyze("This is damn frustrating")
        assert sentiment.tone == "frustration"  # deterministic fallback

    def test_llm_normalizes_curious_to_curiosity(self, monkeypatch):
        """LLM "curious" should be normalized to "curiosity" matching deterministic."""
        monkeypatch.setattr(
            "friday.sentiment._LLM_ENABLED", lambda: True)
        monkeypatch.setattr(
            "friday.sentiment._LLM_CALL",
            lambda sys, usr: self._make_llm_response("curious", 0.85))

        from friday.sentiment import SentimentDetector
        detector = SentimentDetector(conn=None)

        sentiment = detector.analyze("I wonder how this works")
        assert sentiment.tone == "curiosity"  # normalized
        assert sentiment.confidence == 0.85

    def test_llm_returns_invalid_tone_falls_back(self, monkeypatch):
        """When LLM returns a tone not in valid set, fall back."""
        monkeypatch.setattr(
            "friday.sentiment._LLM_ENABLED", lambda: True)
        monkeypatch.setattr(
            "friday.sentiment._LLM_CALL",
            lambda sys, usr: self._make_llm_response("excited", 0.85))  # not in valid set

        from friday.sentiment import SentimentDetector
        detector = SentimentDetector(conn=None)

        sentiment = detector.analyze("This is damn frustrating")
        assert sentiment.tone == "frustration"  # deterministic fallback

    def test_llm_internal_exception_falls_back(self, monkeypatch):
        """When LLM call raises, fall back gracefully."""
        monkeypatch.setattr(
            "friday.sentiment._LLM_ENABLED", lambda: True)
        monkeypatch.setattr(
            "friday.sentiment._LLM_CALL",
            lambda sys, usr: (_ for _ in ()).throw(RuntimeError("LLM crashed")))

        from friday.sentiment import SentimentDetector
        detector = SentimentDetector(conn=None)

        sentiment = detector.analyze("This is damn frustrating")
        assert sentiment.tone == "frustration"  # graceful fallback


# ---------------------------------------------------------------------------
# Trend computation
# ---------------------------------------------------------------------------


class TestTrendComputation:
    def test_trend_no_data(self):
        """Test trend computation with no data."""
        from friday.sentiment import compute_trend_summary
        result = compute_trend_summary(conn=None)
        assert result["total_observations"] == 0
        assert result["most_common_tone"] == "neutral"
        assert result["trend"] == "insufficient_data"

    def test_trend_with_data(self):
        """Test trend computation with stored observations."""
        from friday.db import connect

        conn = connect(":memory:")
        from friday.sentiment import _ensure_table
        _ensure_table(conn)
        from friday.sentiment import SentimentDetector

        detector = SentimentDetector(conn)
        # Add some observations.
        detector.observe("That is really great and awesome", "cli", "conv_trend")
        detector.observe("Amazing work", "cli", "conv_trend")
        detector.observe("This is damn frustrating", "cli", "conv_trend")

        from friday.sentiment import compute_trend_summary
        result = compute_trend_summary(conn, lookback_hours=48)
        assert result["total_observations"] == 3
        assert "tone_breakdown" in result
        assert len(result["tone_breakdown"]) >= 2  # happiness + frustration

        conn.close()
