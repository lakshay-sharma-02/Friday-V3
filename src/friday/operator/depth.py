"""Relationship Depth — compute Friday's relationship depth level with the operator.

Depth levels (monotonically increasing, never decrease):
  Level 0 — Stranger:     First 5 conversations
  Level 1 — Acquaintance: 5-20 conversations, or name known
  Level 2 — Partner:      20-50 conversations, some preferences known
  Level 3 — Confidant:    50-100 conversations, preferences + habits known
  Level 4 — Trusted:      100+ conversations, deep history

Depth is computed from:
- Total conversation count (from conversation_log)
- Whether the operator's name is known (from operator_preferences)
- Number of explicit/derived preferences
- Positive sentiment ratio (accelerates depth progression)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from ..db import now_iso


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Conversation count thresholds for each level.
_DEPTH_THRESHOLDS = {
    0: 0,      # Start here
    1: 5,      # 5 conversations → Acquaintance
    2: 20,     # 20 conversations → Partner
    3: 50,     # 50 conversations → Confidant
    4: 100,    # 100 conversations → Trusted
}

#: Minimum preferences known to qualify for a level (accelerator).
_PREFERENCE_THRESHOLDS = {
    0: 0,
    1: 0,      # No minimum for Acquaintance (name counts separately)
    2: 2,      # 2+ preferences → Partner
    3: 5,      # 5+ preferences → Confidant
    4: 8,      # 8+ preferences → Trusted
}

#: Sentiment boost: if rolling positivity ratio >= this, add 1 to effective
#: conversation count (up to 1 level boost).
_POSITIVITY_BOOST_THRESHOLD = 0.6

#: DB table for tone_history (created via migration 027).
_TONE_TABLE = """
CREATE TABLE IF NOT EXISTS tone_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL DEFAULT '',
    depth_at_time   INTEGER NOT NULL DEFAULT 0,
    tone_used       TEXT NOT NULL DEFAULT 'neutral',
    user_sentiment_avg REAL NOT NULL DEFAULT 0.0,
    recorded_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tone_history_conversation
    ON tone_history(conversation_id);
"""


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class RelationshipDepth:
    """Friday's current relationship depth level with the operator.

    Attributes:
        level: 0-4 relationship depth level.
        label: Human-readable label for the level.
        total_conversations: Number of conversation exchanges logged.
        preferences_known: Number of explicit preferences set.
        name_known: Whether the operator's name is known.
        positive_sentiment_ratio: Ratio of positive to total sentiment observations.
        description: Human description of what this level means.
    """
    level: int = 0
    label: str = "Stranger"
    total_conversations: int = 0
    preferences_known: int = 0
    name_known: bool = False
    positive_sentiment_ratio: float = 0.0
    description: str = ""

    _LEVEL_LABELS = {
        0: "Stranger",
        1: "Acquaintance",
        2: "Partner",
        3: "Confidant",
        4: "Trusted",
    }

    _LEVEL_DESCRIPTIONS = {
        0: "Formal, polite, slightly reserved. First impressions matter.",
        1: "Warm, direct. Uses the operator's name naturally in conversation.",
        2: "Casual, occasionally witty, proactive. Feels like a partner.",
        3: "Can be blunt, uses familiar references, reads the room.",
        4: "Deep history. References past conversations naturally. Finishes thoughts.",
    }


# ---------------------------------------------------------------------------
# Computation
# ---------------------------------------------------------------------------


def _ensure_tone_table(conn) -> None:
    """Create the tone_history table if it doesn't exist."""
    try:
        conn.executescript(_TONE_TABLE)
        conn.commit()
    except Exception:
        pass


def compute_relationship_depth(conn) -> RelationshipDepth:
    """Compute Friday's current relationship depth with the operator.

    Pure function that reads from the database and returns a
    ``RelationshipDepth``. Never raises — returns Level 0 on error.

    Args:
        conn: Database connection.

    Returns:
        A ``RelationshipDepth`` with the current level and supporting info.
    """
    try:
        return _compute(conn)
    except Exception:
        return RelationshipDepth()


def _compute(conn) -> RelationshipDepth:
    """Internal computation with exception propagation."""
    _ensure_tone_table(conn)

    # 1. Conversation count.
    row = conn.execute(
        "SELECT COUNT(*) AS cnt FROM conversation_log"
    ).fetchone()
    total_conversations = row["cnt"] if row else 0

    # 2. Name known.
    name_row = conn.execute(
        "SELECT value FROM operator_preferences WHERE key = 'name'"
    ).fetchone()
    name_known = bool(name_row and name_row["value"].strip())

    # 3. Preferences known (excluding 'name').
    pref_row = conn.execute(
        "SELECT COUNT(*) AS cnt FROM operator_preferences WHERE key != 'name'"
    ).fetchone()
    preferences_known = pref_row["cnt"] if pref_row else 0

    # 4. Positive sentiment ratio.
    positive_ratio = _compute_positive_sentiment_ratio(conn)

    # 5. Effective conversation count (with positivity boost).
    effective_convos = total_conversations
    if positive_ratio >= _POSITIVITY_BOOST_THRESHOLD:
        effective_convos = int(total_conversations * 1.25)

    # 6. Determine level.
    level = _determine_level(effective_convos, preferences_known, name_known)

    label = RelationshipDepth._LEVEL_LABELS.get(level, "Stranger")
    description = RelationshipDepth._LEVEL_DESCRIPTIONS.get(level, "")

    return RelationshipDepth(
        level=level,
        label=label,
        total_conversations=total_conversations,
        preferences_known=preferences_known,
        name_known=name_known,
        positive_sentiment_ratio=round(positive_ratio, 3),
        description=description,
    )


def _compute_positive_sentiment_ratio(conn) -> float:
    """Compute the ratio of positive to total sentiment observations."""
    try:
        total_row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM sentiment_observations"
        ).fetchone()
        total = total_row["cnt"] if total_row else 0
        if total == 0:
            return 0.0

        pos_row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM sentiment_observations "
            "WHERE tone IN ('happiness', 'curious')"
        ).fetchone()
        positive = pos_row["cnt"] if pos_row else 0
        return positive / total
    except Exception:
        return 0.0


def _determine_level(effective_conversations: int,
                     preferences_known: int,
                     name_known: bool) -> int:
    """Determine the relationship depth level from signals.

    The design matches the prompt spec:
    - Level 0 — default, first few conversations
    - Level 1 — 5+ conversations, OR name known
    - Level 2 — 20+ conversations AND 2+ preferences
    - Level 3 — 50+ conversations AND 5+ preferences
    - Level 4 — 100+ conversations AND 8+ preferences

    Depth NEVER decreases (monotonic).

    Args:
        effective_conversations: Total conversations (boosted by positivity).
        preferences_known: Number of known preferences.
        name_known: Whether the operator's name is known.

    Returns:
        Integer level 0-4.
    """
    # Level 1 can be reached by name alone ("or known name" per spec),
    # even with 0 conversations. Check this early.
    if name_known:
        return max(1, _determine_level_from_thresholds(
            effective_conversations, preferences_known))

    return _determine_level_from_thresholds(
        effective_conversations, preferences_known)


def _determine_level_from_thresholds(effective_conversations: int,
                                      preferences_known: int) -> int:
    """Determine level based on conversation and preference thresholds only."""
    for level in (4, 3, 2, 1, 0):
        if effective_conversations >= _DEPTH_THRESHOLDS[level] and \
           preferences_known >= _PREFERENCE_THRESHOLDS[level]:
            return level
    return 0


def record_tone_use(conn, conversation_id: str = "",
                    depth: int = 0, tone_used: str = "neutral",
                    user_sentiment_avg: float = 0.0) -> None:
    """Record a tone usage entry in tone_history.

    Args:
        conn: Database connection.
        conversation_id: The conversation this tone was used in.
        depth: Relationship depth at the time of use.
        tone_used: The tone variant that was used.
        user_sentiment_avg: Average user sentiment during this conversation.
    """
    try:
        _ensure_tone_table(conn)
        conn.execute(
            "INSERT INTO tone_history "
            "(conversation_id, depth_at_time, tone_used, user_sentiment_avg, recorded_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (conversation_id, depth, tone_used, user_sentiment_avg, now_iso()),
        )
        conn.commit()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Tone modulation — map depth to tone parameters
# ---------------------------------------------------------------------------


@dataclass
class ToneParams:
    """Tone parameters derived from relationship depth.

    These are injected into the persona prompt to modulate Friday's tone.
    """
    formality: float = 1.0   # 0.0 = casual, 1.0 = formal
    warmth: float = 0.3      # 0.0 = cold, 1.0 = very warm
    humor_allowed: bool = False
    brevity: float = 0.5     # 0.0 = verbose, 1.0 = very brief
    proactiveness: float = 0.0  # 0.0 = never interrupt, 1.0 = very proactive
    use_name: bool = False
    use_past_references: bool = False

    def to_prompt_fragment(self) -> str:
        """Convert tone parameters to a prompt fragment for injection.

        Returns a string like::

            Tone: warm, conversational. You can be brief and direct.
            Address by name. Proactive suggestions are welcome.
        """
        parts: list[str] = []

        # Formality + warmth.
        if self.formality > 0.7:
            parts.append("Be formal and polite.")
        elif self.warmth > 0.6:
            parts.append("Be warm and conversational.")
        else:
            parts.append("Be warm but professional.")

        # Brevity.
        if self.brevity > 0.7:
            parts.append("Keep responses concise.")
        elif self.brevity < 0.3:
            parts.append("Be thorough in responses.")

        # Humor.
        if self.humor_allowed:
            parts.append("Occasional subtle humor is appropriate.")

        # Name usage.
        if self.use_name:
            parts.append("Address them by name naturally.")

        # Proactiveness.
        if self.proactiveness > 0.5:
            parts.append("Offer proactive suggestions when relevant.")
        elif self.proactiveness < 0.1:
            parts.append("Let them take the lead.")

        # Past references.
        if self.use_past_references:
            parts.append("Reference past conversations when naturally relevant.")

        return " ".join(parts) if parts else "Be natural and helpful."


_DEPTH_TONE_MAP: dict[int, ToneParams] = {
    0: ToneParams(
        formality=0.9, warmth=0.3, humor_allowed=False,
        brevity=0.4, proactiveness=0.0, use_name=False,
        use_past_references=False,
    ),
    1: ToneParams(
        formality=0.6, warmth=0.6, humor_allowed=False,
        brevity=0.5, proactiveness=0.1, use_name=True,
        use_past_references=False,
    ),
    2: ToneParams(
        formality=0.3, warmth=0.8, humor_allowed=True,
        brevity=0.7, proactiveness=0.4, use_name=True,
        use_past_references=True,
    ),
    3: ToneParams(
        formality=0.2, warmth=0.9, humor_allowed=True,
        brevity=0.8, proactiveness=0.6, use_name=True,
        use_past_references=True,
    ),
    4: ToneParams(
        formality=0.1, warmth=1.0, humor_allowed=True,
        brevity=0.9, proactiveness=0.7, use_name=True,
        use_past_references=True,
    ),
}


def get_tone_params(depth: int) -> ToneParams:
    """Get tone parameters for a given relationship depth level.

    Args:
        depth: Relationship depth level (0-4).

    Returns:
        ``ToneParams`` for that level. Falls back to Level 0 if depth
        is out of range.
    """
    return _DEPTH_TONE_MAP.get(depth, _DEPTH_TONE_MAP[0])
