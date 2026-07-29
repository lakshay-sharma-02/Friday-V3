"""SentimentDetector — emotional state detection for Relationship & Personalization.

Analyzes user messages for emotional tone using deterministic heuristics
(keyword/punctuation patterns) with an optional LLM-powered path for deeper
analysis. Tracks rolling sentiment over the last N exchanges and stores
observations in the ``sentiment_observations`` table.

Sentiment is a SIGNAL for other systems (tone modulation, proactivity gating,
relationship depth) — never a standalone decision-maker.
"""

from __future__ import annotations

import hashlib
import re
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from .db import now_iso

# ---------------------------------------------------------------------------
# Sentiment dataclasses
# ---------------------------------------------------------------------------


@dataclass
class Sentiment:
    """The emotional tone detected in a user message.

    Attributes:
        tone: One of ``frustration``, ``urgency``, ``happiness``, ``confusion``,
            ``neutral``, ``sarcasm``, ``curious``, ``fatigue``.
        confidence: Float 0.0–1.0 how sure the detector is.
        signal: Human-readable explanation of why this tone was chosen.
    """
    tone: str
    confidence: float
    signal: str


@dataclass
class RollingSentiment:
    """Rolling window of recent sentiment observations for trend tracking."""

    window: deque = field(default_factory=lambda: deque(maxlen=_ROLLING_WINDOW))
    _last_mean: Optional[float] = None

    @property
    def mean_confidence(self) -> float:
        """Mean confidence across the rolling window."""
        if not self.window:
            return 0.0
        return sum(s.confidence for s in self.window) / len(self.window)

    @property
    def dominant_tone(self) -> str:
        """The most frequent tone in the rolling window."""
        if not self.window:
            return "neutral"
        tones: dict[str, int] = {}
        for s in self.window:
            tones[s.tone] = tones.get(s.tone, 0) + 1
        return max(tones, key=tones.get)

    @property
    def is_frustrated(self) -> bool:
        """Whether the rolling window shows persistent frustration."""
        if len(self.window) < 3:
            return False
        frustrated = sum(1 for s in self.window if s.tone == "frustration")
        return frustrated / len(self.window) >= 0.5

    @property
    def is_positive(self) -> bool:
        """Whether the rolling window shows persistent positivity."""
        if len(self.window) < 2:
            return False
        positive = sum(1 for s in self.window if s.tone in ("happiness", "curious"))
        return positive / len(self.window) >= 0.5


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Rolling window size for trend tracking.
_ROLLING_WINDOW = 10

#: Minimum confidence threshold — observations below this are not stored.
_MIN_CONFIDENCE = 0.6

#: DB table DDL (also created via migration 027).
_SENTIMENT_TABLE = """
CREATE TABLE IF NOT EXISTS sentiment_observations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT NOT NULL,
    channel         TEXT NOT NULL DEFAULT '',
    message_hash    TEXT NOT NULL,
    tone            TEXT NOT NULL,
    confidence      REAL NOT NULL,
    signal          TEXT NOT NULL DEFAULT '',
    conversation_id TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_sentiment_timestamp
    ON sentiment_observations(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_sentiment_conversation
    ON sentiment_observations(conversation_id);
CREATE INDEX IF NOT EXISTS idx_sentiment_tone
    ON sentiment_observations(tone);
"""

# ---------------------------------------------------------------------------
# Sentinel words and patterns
# ---------------------------------------------------------------------------

#: Words that strongly indicate frustration.
_FRUSTRATION_WORDS = frozenset({
    "frustrating", "frustrated", "frustrates", "annoying", "annoyed",
    "pissed", "damn", "damnit", "dammit", "hell", "what the hell",
    "why the", "for god", "for heaven", "jesus", "christ",
    "stupid", "ridiculous", "absurd", "useless", "terrible", "awful",
    "worst", "hate", "hating", "sucks", "suck", "broken", "broke",
    "not working", "doesn't work", "doesnt work", "won't work",
    "wont work", "fails", "failed", "crash", "crashed", "crashing",
    "bug", "buggy", "wtf", "wth", "seriously", "enough",
    "fed up", "sick of", "tired of", "giving up", "give up",
})

#: Words that indicate urgency.
_URGENCY_WORDS = frozenset({
    "urgent", "asap", "emergency", "immediately", "right now",
    "quick", "hurry", "hurry up", "fast", "critical", "critical",
    "deadline", "overdue", "now!", "immediately", "stat",
    "priority", "high priority", "blocker", "blocking",
})

#: Words that indicate happiness/positivity.
_HAPPINESS_WORDS = frozenset({
    "great", "awesome", "amazing", "fantastic", "wonderful", "excellent",
    "perfect", "lovely", "beautiful", "nice", "cool", "sweet",
    "happy", "glad", "delighted", "thrilled", "love", "loved",
    "lol", "haha", "hahaha", "hehe", "😂", "🎉", "✨", "💪",
    "wonderful", "brilliant", "super", "fantastic", "yay", "woohoo",
    "finally", "works", "working", "solved", "fixed",
})

#: Words that indicate confusion.
_CONFUSION_WORDS = frozenset({
    "confused", "confusing", "what", "huh", "i don't understand",
    "i dont understand", "don't get it", "dont get it",
    "not clear", "unclear", "what do you mean", "meaning",
    "explain", "why", "how", "what's this", "whats this",
    "clarify", "elaborate", "??", "???", "????",
})

#: Words that indicate curiosity (positive-valence exploration).
_CURIOSITY_WORDS = frozenset({
    "interesting", "curious", "wonder", "what if", "how about",
    "tell me more", "explore", "discover", "learn", "understand",
    "fascinating", "intriguing", "tell me about",
})

#: Words that indicate sarcasm.
_SARCASM_PATTERNS = [
    re.compile(r"\b(?:oh\s+)?(?:really|sure|great|nice|perfect)\s*!", re.IGNORECASE),
    re.compile(r"\byeah\s*,\s*(?:right|sure)\b", re.IGNORECASE),
    re.compile(r"\bas\s+if\b", re.IGNORECASE),
    re.compile(r"\b(?:sure|fine)\s+(?:thing|whatever)\b", re.IGNORECASE),
]

#: Words that indicate fatigue (slightly different from frustration — lower energy).
_FATIGUE_WORDS = frozenset({
    "tired", "exhausted", "sleepy", "long day", "burned out",
    "burnt out", "drained", "overwhelmed", "too much",
    "can't even", "cant even", "done for the day", "call it a day",
    "need a break", "break time", "rest", "zzz", "😴",
})

#: Short responses that could indicate neutrality or dismissal (context-dependent).
_SHORT_RESPONSES_THRESHOLD = 3  # Words or fewer

#: Cache for the LLM service module reference (imported once, used on every call).
#: The import is deferred to import time but cached to avoid repeated module
#: evaluation overhead (FALLBACK_PROVIDERS list, etc.).
# These store function references imported from services.llm at module load time.
# _LLM_ENABLED: Callable[[], bool] | None
# _LLM_CALL: Callable[[str, str], str | None] | None
# _LLM_PARSE_JSON: Callable[[str, list | None], dict | list | None] | None
_LLM_ENABLED = None
_LLM_CALL = None
_LLM_PARSE_JSON = None

def _init_llm_refs() -> None:
    """Initialize cached references to the LLM service module.

    Called once at module load time. Does NOT attempt network connections —
    just stores function references. The actual LLM call is made only when
    ``_analyze_llm()`` is invoked and ``_enabled()`` returns True.
    """
    global _LLM_ENABLED, _LLM_CALL, _LLM_PARSE_JSON
    try:
        from .services.llm import _enabled, _call, _parse_json_response
        _LLM_ENABLED = _enabled
        _LLM_CALL = _call
        _LLM_PARSE_JSON = _parse_json_response
    except Exception:
        pass


# Initialize LLM references at module load time.
_init_llm_refs()


# ---------------------------------------------------------------------------
# SentimentDetector
# ---------------------------------------------------------------------------


def _ensure_table(conn) -> None:
    """Create the sentiment_observations table if it doesn't exist."""
    try:
        conn.executescript(_SENTIMENT_TABLE)
        conn.commit()
    except Exception:
        pass


def _hash_message(text: str) -> str:
    """Return a stable hash of the message text for deduplication."""
    return hashlib.sha256(text.strip().lower().encode()).hexdigest()[:16]


class SentimentDetector:
    """Detect emotional tone in user messages.

    Two analysis paths:
    1. **Deterministic** (always available): keyword + punctuation heuristics.
    2. **LLM** (optional): lightweight classification prompt.

    Always returns the best-effort ``Sentiment``. Stores observations only
    when confidence >= ``_MIN_CONFIDENCE``.

    Usage::

        detector = SentimentDetector(conn)
        sentiment = detector.analyze("This is so frustrating!")
        # → Sentiment(tone="frustration", confidence=0.85, signal="matched 3 frustration words")

        rolling = detector.get_rolling_sentiment("conversation_123")
        # → RollingSentiment with last 10 tones

        detector.observe("This is so frustrating!", "cli", "conversation_123")
        # → analyzes AND stores to DB
    """

    def __init__(self, conn=None) -> None:
        self._conn = conn
        if conn is not None:
            _ensure_table(conn)
        # In-memory rolling windows per conversation_id.
        self._rolling: dict[str, RollingSentiment] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def analyze(self, text: str) -> Sentiment:
        """Analyze a user message for emotional tone.

        Two analysis paths, combined:
        1. **Deterministic** (always available): keyword + punctuation heuristics.
        2. **LLM** (optional, when enabled): lightweight classification prompt
           for deeper semantic understanding.

        When both paths are available, the higher-confidence result is used.
        The LLM path uses a cached classification prompt, so repeated or
        similar messages hit the response cache (10 min TTL).

        Args:
            text: The user's message.

        Returns:
            A ``Sentiment`` with the detected tone, confidence, and signal.
            Always returns a value (never None) — defaults to ``neutral``
            with low confidence when nothing is detected.
        """
        text = text.strip()
        if not text:
            return Sentiment(tone="neutral", confidence=0.5, signal="empty message")

        # Always run deterministic heuristics (fast, always available).
        det = self._analyze_deterministic(text)

        # When LLM is enabled, try the deeper analysis path.
        # Uses the LLM service's response cache for speed on repeated/similar messages.
        llm = self._analyze_llm(text)

        # Pick the higher-confidence result.
        if llm and llm.confidence > det.confidence and llm.confidence >= _MIN_CONFIDENCE:
            return llm

        return det

    def _analyze_deterministic(self, text: str) -> Sentiment:
        """Run deterministic keyword/punctuation heuristics only.

        Always returns a ``Sentiment`` (never None) — defaults to ``neutral``
        with low confidence when nothing is detected.
        """
        scores: dict[str, float] = {}
        signals: dict[str, str] = {}

        self._score_frustration(text, scores, signals)
        self._score_urgency(text, scores, signals)
        self._score_happiness(text, scores, signals)
        self._score_confusion(text, scores, signals)
        self._score_curiosity(text, scores, signals)
        self._score_sarcasm(text, scores, signals)
        self._score_fatigue(text, scores, signals)

        # Short-response detection — modifies existing scores.
        self._score_short_response(text, scores, signals)

        # Select the tone with the highest score.
        if not scores:
            return Sentiment(tone="neutral", confidence=0.5, signal="no strong signals")

        best_tone = max(scores, key=scores.get)
        best_score = scores[best_tone]
        best_signal = signals.get(best_tone, "detected")

        confidence = min(1.0, max(0.0, best_score))
        if confidence < _MIN_CONFIDENCE:
            return Sentiment(tone="neutral", confidence=confidence, signal=best_signal)

        return Sentiment(tone=best_tone, confidence=confidence, signal=best_signal)

    def _analyze_llm(self, text: str) -> Optional[Sentiment]:
        """Run LLM-based sentiment classification.

        Uses a lightweight classification prompt with structured JSON output.
        The LLM service caches responses (10 min TTL), so repeated or similar
        messages are fast. LLM service references are cached at module load
        time rather than imported on every call.

        Returns a ``Sentiment`` when the LLM is enabled and returns a valid
        classification with confidence >= 0.6. Returns ``None`` when the LLM
        is unavailable or returns unparseable output.
        """
        try:
            _enabled, _call, _parse_json_response = _LLM_ENABLED, _LLM_CALL, _LLM_PARSE_JSON
            if not _enabled or not _call:
                return None
            if not _enabled():
                return None

            system = (
                "You are a sentiment classifier. Classify the emotional tone of "
                "the user's message. Be concise and accurate.\n\n"
                "Valid tones: frustration, urgency, happiness, confusion, "
                "curious, sarcasm, fatigue, neutral\n\n"
                "Respond with ONLY a JSON object (no markdown, no explanation):\n"
                '{"tone": "<one of the valid tones>", '
                '"confidence": <0.0-1.0>, '
                '"signal": "<short reason for classification>"}'
            )
            user = f"Classify this message: {text[:500]}"

            raw = _call(system, user)
            if not raw:
                return None

            parsed = _parse_json_response(raw, required_keys=["tone", "confidence"])
            if not parsed or not isinstance(parsed, dict):
                return None

            tone = str(parsed.get("tone", "")).strip().lower()
            # Normalize LLM variant to canonical deterministic tone name.
            _TONE_CANONICAL = {
                "curious": "curiosity",
            }
            tone = _TONE_CANONICAL.get(tone, tone)
            valid_tones = {
                "frustration", "urgency", "happiness", "confusion",
                "curiosity", "sarcasm", "fatigue", "neutral",
            }
            if tone not in valid_tones:
                return None

            confidence = float(parsed.get("confidence", 0.0))
            if confidence < _MIN_CONFIDENCE:
                return None

            signal = str(parsed.get("signal", "llm classification"))[:200]
            if not signal:
                signal = "llm classification"

            return Sentiment(
                tone=tone,
                confidence=min(1.0, max(0.0, confidence)),
                signal=signal,
            )

        except Exception:
            return None

    def observe(self, text: str, channel: str = "",
                conversation_id: str = "") -> Sentiment:
        """Analyze a message, store the observation, and update rolling window.

        Convenience wrapper around ``analyze()`` + persistent storage.

        Args:
            text: The user's message.
            channel: The channel the message was sent on (``cli``, ``telegram``, etc.).
            conversation_id: Unique ID for the conversation thread.

        Returns:
            The ``Sentiment`` detected (or neutral if none).
        """
        sentiment = self.analyze(text)
        self._store_observation(text, sentiment, channel, conversation_id)
        self._update_rolling(sentiment, conversation_id)
        return sentiment

    def get_rolling_sentiment(self, conversation_id: str = "") -> RollingSentiment:
        """Get the rolling sentiment tracker for a conversation.

        Args:
            conversation_id: The conversation ID.

        Returns:
            A ``RollingSentiment`` for recent tone history.
            Returns an empty RollingSentiment if no data exists.
        """
        return self._rolling.get(conversation_id, RollingSentiment())

    def get_trend_summary(self, conversation_id: str = "",
                          lookback_hours: int = 24) -> dict:
        """Get a summary of recent sentiment trends.

        Args:
            conversation_id: Optional conversation filter.
            lookback_hours: How far back to look for recent observations.

        Returns:
            Dict with keys: ``total_observations``, ``tone_breakdown``,
            ``most_common_tone``, ``rolling_tone``, ``trend``.
        """
        return compute_trend_summary(self._conn, conversation_id, lookback_hours)

    def rolling_is_frustrated(self, conversation_id: str = "") -> bool:
        """Check if the rolling window shows persistent frustration."""
        rolling = self.get_rolling_sentiment(conversation_id)
        return rolling.is_frustrated

    def rolling_is_positive(self, conversation_id: str = "") -> bool:
        """Check if the rolling window shows persistent positivity."""
        rolling = self.get_rolling_sentiment(conversation_id)
        return rolling.is_positive

    # ------------------------------------------------------------------
    # Deterministic scoring (private)
    # ------------------------------------------------------------------

    def _score_frustration(self, text: str, scores: dict, signals: dict) -> None:
        """Score the frustration dimension using keywords and punctuation."""
        lower = text.lower().strip()
        score = 0.0

        # Count matching frustration words.
        words = set(lower.split())
        matches = sum(1 for w in _FRUSTRATION_WORDS if w in words or w in lower)
        if matches == 1:
            score = 0.65
        elif matches == 2:
            score = 0.80
        elif matches >= 3:
            score = 0.90

        # Exclamation-mark density: lots of !!! could indicate anger, not just excitement.
        excl_count = text.count("!")
        upper_ratio = sum(1 for c in text if c.isupper()) / max(len(text), 1)
        if excl_count >= 2:
            score = max(score, 0.60)
        if excl_count >= 3 and upper_ratio > 0.3:
            score = max(score, 0.80)

        # All-caps words.
        caps_words = [w for w in text.split() if w.isupper() and len(w) > 2]
        if len(caps_words) >= 2:
            score = max(score, 0.70)

        if score > 0:
            signals["frustration"] = (
                f"matched {matches} frustration words, {excl_count} excl, "
                f"{len(caps_words)} all-caps words"
            )
            scores["frustration"] = score

    def _score_urgency(self, text: str, scores: dict, signals: dict) -> None:
        """Score the urgency dimension."""
        lower = text.lower().strip()
        score = 0.0
        matches = sum(1 for w in _URGENCY_WORDS if w in lower)

        if matches >= 1:
            score = 0.50 + (matches * 0.10)
        if text.strip().endswith("!") and matches >= 1:
            score = min(score + 0.15, 1.0)
        # Short, imperative sentences.
        if len(text.split()) <= 4 and text.strip().endswith("!"):
            score = max(score, 0.70)

        if score > 0:
            signals["urgency"] = f"matched {matches} urgency words"
            scores["urgency"] = min(score, 1.0)

    def _score_happiness(self, text: str, scores: dict, signals: dict) -> None:
        """Score the happiness/positivity dimension."""
        lower = text.lower().strip()
        score = 0.0
        matches = sum(1 for w in _HAPPINESS_WORDS if w in lower or w in lower.split())

        if matches == 1:
            score = 0.60
        elif matches == 2:
            score = 0.75
        elif matches >= 3:
            score = 0.85

        # Emoji indicators.
        if "😂" in text or "🎉" in text or "✨" in text:
            score = max(score, 0.70)
        if ":)" in text or ":D" in text:
            score = max(score, 0.55)

        if score > 0:
            signals["happiness"] = f"matched {matches} positive words"
            scores["happiness"] = score

    def _score_confusion(self, text: str, scores: dict, signals: dict) -> None:
        """Score the confusion dimension."""
        lower = text.lower().strip()
        score = 0.0
        matches = sum(1 for w in _CONFUSION_WORDS if w in lower)

        if matches == 1:
            score = 0.55
        elif matches >= 2:
            score = 0.70

        # Question mark density.
        qmarks = text.count("?")
        if qmarks >= 2:
            score = max(score, 0.50)
        if qmarks >= 3:
            score = max(score, 0.70)

        if score > 0:
            signals["confusion"] = f"matched {matches} confusion words, {qmarks} ?"
            scores["confusion"] = score

    def _score_curiosity(self, text: str, scores: dict, signals: dict) -> None:
        """Score the curiosity dimension."""
        lower = text.lower().strip()
        score = 0.0
        matches = sum(1 for w in _CURIOSITY_WORDS if w in lower)

        if matches == 1:
            score = 0.55
        elif matches >= 2:
            score = 0.70

        # Questions that aren't confusion are often curiosity.
        if text.strip().endswith("?") and "confusion" not in scores:
            score = max(score, 0.45)

        if score > 0:
            signals["curiosity"] = f"matched {matches} curiosity words"
            scores["curiosity"] = score

    def _score_sarcasm(self, text: str, scores: dict, signals: dict) -> None:
        """Score the sarcasm dimension using pattern matching."""
        score = 0.0

        for pat in _SARCASM_PATTERNS:
            if pat.search(text):
                score = max(score, 0.65)

        # Tone mismatch: positive words with negative context.
        # Heuristic: if a statement ends with "..." and has positive words,
        # it might be sarcastic.
        if text.strip().endswith("...") and any(
            w in text.lower() for w in ("great", "nice", "perfect", "awesome", "lovely")
        ):
            score = max(score, 0.60)

        if score > 0:
            signals["sarcasm"] = "detected sarcasm pattern"
            scores["sarcasm"] = score

    def _score_fatigue(self, text: str, scores: dict, signals: dict) -> None:
        """Score the fatigue/low-energy dimension."""
        lower = text.lower().strip()
        score = 0.0
        matches = sum(1 for w in _FATIGUE_WORDS if w in lower)

        if matches == 1:
            score = 0.55
        elif matches >= 2:
            score = 0.70
        elif matches >= 3:
            score = 0.80

        # Short, low-energy responses.
        words = text.split()
        if len(words) <= 2 and text.strip().rstrip(".!").lower() in (
            "ok", "k", "fine", "sure", "whatever", "alright", "okay",
        ):
            score = max(score, 0.50)

        if score > 0:
            signals["fatigue"] = f"matched {matches} fatigue words"
            scores["fatigue"] = score

    def _score_short_response(self, text: str, scores: dict, signals: dict) -> None:
        """Modify scores for short responses that could indicate dismissal.

        A short response like ``ok``, ``k``, ``fine`` doesn't independently
        signal a tone — it modifies existing signals (e.g., adds frustration
        context to a ``fine`` that was preceded by frustration).
        """
        words = text.split()
        if len(words) > _SHORT_RESPONSES_THRESHOLD:
            return

        lower = text.strip().rstrip(".!").lower().strip()
        if lower in ("ok", "k", "kk", "fine", "sure", "whatever", "alright", "okay"):
            # Short response alone is ambiguous — check rolling context.
            # Without rolling context, it's neutral with moderate confidence.
            if not scores:
                scores["neutral"] = 0.60
                signals["neutral"] = "short response, no context"
        elif lower in ("yes", "yep", "yeah", "yup", "no", "nope", "nah"):
            if not scores:
                scores["neutral"] = 0.50
                signals["neutral"] = "short affirmative/negative"

    # ------------------------------------------------------------------
    # Storage & Rolling
    # ------------------------------------------------------------------

    def _store_observation(self, text: str, sentiment: Sentiment,
                           channel: str, conversation_id: str) -> None:
        """Store a sentiment observation in the DB.

        Only stores when confidence >= ``_MIN_CONFIDENCE``.
        Deduplicates identical messages (same hash) within a conversation.
        """
        if self._conn is None:
            return
        if sentiment.confidence < _MIN_CONFIDENCE:
            return

        try:
            msg_hash = _hash_message(text)

            # Deduplicate: skip if we've already seen this exact message hash
            # in this conversation within the last minute.
            one_min_ago = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
            existing = self._conn.execute(
                "SELECT id FROM sentiment_observations "
                "WHERE message_hash = ? AND conversation_id = ? AND "
                "timestamp >= ?",
                (msg_hash, conversation_id, one_min_ago),
            ).fetchone()
            if existing:
                return

            self._conn.execute(
                "INSERT INTO sentiment_observations "
                "(timestamp, channel, message_hash, tone, confidence, signal, conversation_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (now_iso(), channel, msg_hash, sentiment.tone,
                 sentiment.confidence, sentiment.signal[:200], conversation_id),
            )
            self._conn.commit()
        except Exception:
            pass

    def _update_rolling(self, sentiment: Sentiment,
                        conversation_id: str) -> None:
        """Update the in-memory rolling window for a conversation."""
        if conversation_id not in self._rolling:
            self._rolling[conversation_id] = RollingSentiment()
        self._rolling[conversation_id].window.append(sentiment)


# ---------------------------------------------------------------------------
# Trend computation
# ---------------------------------------------------------------------------


def compute_trend_summary(conn, conversation_id: str = "",
                           lookback_hours: int = 24) -> dict:
    """Compute a summary of recent sentiment trends from the DB."""
    if conn is None:
        return {
            "total_observations": 0,
            "tone_breakdown": {},
            "most_common_tone": "neutral",
            "rolling_tone": "neutral",
            "trend": "insufficient_data",
        }

    result: dict = {
        "total_observations": 0,
        "tone_breakdown": {},
        "most_common_tone": "neutral",
        "rolling_tone": "neutral",
        "trend": "stable",
    }

    try:
        from datetime import datetime, timedelta, timezone
        since = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
        params: list = [since.isoformat()]
        query = (
            "SELECT tone, confidence, timestamp FROM sentiment_observations "
            "WHERE timestamp >= ?"
        )
        if conversation_id:
            query += " AND conversation_id = ?"
            params.append(conversation_id)
        query += " ORDER BY timestamp DESC"

        rows = conn.execute(query, params).fetchall()
        if not rows:
            return result

        result["total_observations"] = len(rows)

        # Tone breakdown.
        tone_counts: dict[str, int] = {}
        for r in rows:
            tone_counts[r["tone"]] = tone_counts.get(r["tone"], 0) + 1

        total = len(rows)
        result["tone_breakdown"] = {
            tone: {
                "count": count,
                "percentage": round(count / total * 100, 1),
            }
            for tone, count in sorted(tone_counts.items(), key=lambda x: -x[1])
        }

        result["most_common_tone"] = max(tone_counts, key=tone_counts.get)

        # Rolling tone (last 10 observations).
        recent = list(rows)[:10]
        recent_tones: dict[str, int] = {}
        for r in recent:
            recent_tones[r["tone"]] = recent_tones.get(r["tone"], 0) + 1
        if recent_tones:
            result["rolling_tone"] = max(recent_tones, key=recent_tones.get)

        # Trend direction: compare first half vs second half of recent window.
        if len(rows) >= 6:
            half = len(rows) // 2
            first_half = rows[half:]  # older
            second_half = rows[:half]  # newer

            first_frustration = sum(
                1 for r in first_half if r["tone"] in ("frustration", "fatigue")
            )
            second_frustration = sum(
                1 for r in second_half if r["tone"] in ("frustration", "fatigue")
            )

            if second_frustration > first_frustration + 1:
                result["trend"] = "increasing_frustration"
            elif first_frustration > second_frustration + 1:
                result["trend"] = "decreasing_frustration"

            first_positive = sum(
                1 for r in first_half if r["tone"] in ("happiness", "curious")
            )
            second_positive = sum(
                1 for r in second_half if r["tone"] in ("happiness", "curious")
            )
            if second_positive > first_positive + 1:
                result["trend"] = "increasing_positivity"
            elif first_positive > second_positive + 1:
                result["trend"] = "decreasing_positivity"
            else:
                result["trend"] = "stable"

        return result
    except Exception:
        return result
