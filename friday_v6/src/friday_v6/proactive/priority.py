"""Priority Inference — decides what FRIDAY should speak up about vs save for later.

FRIDAY should never interrupt deep focus for trivial things. This module
determines the importance and urgency of each potential notification or
suggestion, considering:
  - The user's current focus level (deep_focus, light, away)
  - The time sensitivity of the information
  - How many times this has been suggested before
  - The user's expressed preferences about interruptions
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("friday_v6.proactive.priority")

_FEEDBACK_FILE = Path.home() / ".friday" / "interrupt_feedback.json"


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------


@dataclass
class PrioritizedItem:
    """An item with a calculated priority score and handling guidance."""
    text: str
    category: str           # "suggestion", "alert", "briefing", "reminder"
    priority_score: int     # 0-100: higher = more important
    urgency: str            # "immediate", "soon", "when_free", "background"
    context: str = ""
    source: str = "proactive"
    times_suggested: int = 0
    should_speak: bool = False
    should_queue: bool = False
    should_notify: bool = False


# ---------------------------------------------------------------------------
# Priority Inference Engine
# ---------------------------------------------------------------------------


class PriorityInference:
    """Decides the importance and handling of each potential interruption.

    Scoring factors:
      - Focus level (deep_focus = -30, light = -10, idle = +10)
      - Time sensitivity (security = +40, daily brief = +10)
      - Suggestion fatigue (-5 per previous suggestion of same type)
      - Time of day (morning briefings score higher)
      - User preferences (stored in ~/.friday/interrupt_feedback.json)

    Output: one of "speak now", "queue", "notify only", or "suppress"
    """

    # Thresholds for action
    SPEAK_THRESHOLD = 60      # Score >= 60 → speak aloud
    NOTIFY_THRESHOLD = 40     # Score >= 40 → desktop notification
    QUEUE_THRESHOLD = 20      # Score >= 20 → queue for next briefing
    SUPPRESS_THRESHOLD = 0    # Score < 20 → suppress entirely

    def __init__(self):
        self._feedback = self._load_feedback()

    def _load_feedback(self) -> dict:
        """Load user feedback on interruptions."""
        try:
            if _FEEDBACK_FILE.exists():
                with open(_FEEDBACK_FILE) as f:
                    return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
        return {}

    def _save_feedback(self):
        """Save user feedback."""
        try:
            _FEEDBACK_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(_FEEDBACK_FILE, "w") as f:
                json.dump(self._feedback, f, indent=2)
        except OSError as exc:
            logger.warning(f"Could not save feedback: {exc}")

    def evaluate(
        self,
        item: PrioritizedItem,
        focus_level: str = "light",
        time_of_day: str = "afternoon",
    ) -> PrioritizedItem:
        """Evaluate an item and determine how it should be handled.

        Args:
            item: The item to evaluate.
            focus_level: Current focus level (deep_focus, light, idle, away).
            time_of_day: Current time context.

        Returns:
            The same PrioritizedItem with updated priority and handling flags.
        """
        score = 50  # Base score

        # ── Focus Level Adjustment ─────────────────────────────────
        focus_penalties = {
            "deep_focus": -30,
            "light": -10,
            "active": 0,
            "idle": 10,
            "away": -20,
        }
        score += focus_penalties.get(focus_level, 0)

        # ── Category Adjustment ───────────────────────────────────
        category_scores = {
            "alert": 30,       # Security alerts are urgent
            "briefing": 15,    # Morning briefings are valuable
            "suggestion": 5,   # Proactive suggestions are nice-to-have
            "reminder": -5,    # Reminders can wait
            "notification": 10,
        }
        score += category_scores.get(item.category, 0)

        # ── Time Sensitivity ───────────────────────────────────────
        urgency_scores = {
            "immediate": 30,
            "soon": 15,
            "when_free": 0,
            "background": -10,
        }
        score += urgency_scores.get(item.urgency, 0)

        # ── Suggestion Fatigue ─────────────────────────────────────
        # Reduce score for repeated suggestions of the same type
        fatigue = self._feedback.get(item.category, {}).get("times_suggested", 0)
        score -= min(fatigue * 5, 25)  # Max -25 for fatigue
        item.times_suggested = fatigue

        # ── Time of Day Boost ──────────────────────────────────────
        time_boosts = {
            "morning": 10,    # Morning briefings are welcome
            "afternoon": 0,
            "evening": -5,
            "night": -15,     # Don't interrupt at night
        }
        score += time_boosts.get(time_of_day, 0)

        # ── Clamp to 0-100 ────────────────────────────────────────
        item.priority_score = max(0, min(100, score))

        # ── Determine handling ─────────────────────────────────────
        if item.priority_score >= self.SPEAK_THRESHOLD:
            item.should_speak = True
            item.should_queue = False
            item.should_notify = True
            item.urgency = "immediate"
        elif item.priority_score >= self.NOTIFY_THRESHOLD:
            item.should_speak = False
            item.should_queue = True
            item.should_notify = True
            item.urgency = "soon"
        elif item.priority_score >= self.QUEUE_THRESHOLD:
            item.should_speak = False
            item.should_queue = True
            item.should_notify = False
            item.urgency = "when_free"
        else:
            item.should_speak = False
            item.should_queue = False
            item.should_notify = False
            item.urgency = "suppressed"

        return item

    def record_feedback(self, category: str, positive: bool):
        """Record user feedback on a type of interruption.

        Args:
            category: The category of the interruption.
            positive: True if the user responded well, False if annoyed.
        """
        if category not in self._feedback:
            self._feedback[category] = {
                "times_suggested": 0,
                "positive_responses": 0,
                "negative_responses": 0,
            }

        self._feedback[category]["times_suggested"] += 1
        if positive:
            self._feedback[category]["positive_responses"] += 1
        else:
            self._feedback[category]["negative_responses"] += 1

        self._save_feedback()

    def get_queue(self) -> list[PrioritizedItem]:
        """Get all queued items sorted by priority."""
        queue = []

        # Load queue from disk
        queue_file = Path.home() / ".friday" / "proactive_queue.json"
        try:
            if queue_file.exists():
                with open(queue_file) as f:
                    data = json.load(f)
                    for item_data in data:
                        queue.append(PrioritizedItem(**item_data))
        except (json.JSONDecodeError, OSError):
            pass

        # Sort by priority (highest first)
        queue.sort(key=lambda x: x.priority_score, reverse=True)
        return queue

    def save_queue(self, items: list[PrioritizedItem]):
        """Save queued items to disk."""
        queue_file = Path.home() / ".friday" / "proactive_queue.json"
        try:
            queue_file.parent.mkdir(parents=True, exist_ok=True)
            with open(queue_file, "w") as f:
                data = [item.__dict__ for item in items]
                json.dump(data, f, indent=2, default=str)
        except OSError as exc:
            logger.warning(f"Could not save queue: {exc}")

    def clear_queue(self):
        """Clear the proactive queue."""
        queue_file = Path.home() / ".friday" / "proactive_queue.json"
        queue_file.unlink(missing_ok=True)
