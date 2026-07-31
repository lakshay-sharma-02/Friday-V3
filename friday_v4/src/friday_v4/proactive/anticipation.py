"""Anticipation Engine — FRIDAY's ability to anticipate your needs.

The crown jewel of proactive intelligence. Combines:
  - DeepContextEngine (what you're doing right now)
  - PatternLearner (what you typically do in this situation)
  - SessionStore (what you've done recently)
  - PriorityInference (is this worth interrupting for?)

To produce high-quality, contextual suggestions delivered at the right time.

Usage:
    engine = AnticipationEngine()
    suggestions = engine.get_suggestions()
    for s in suggestions:
        if s.should_speak:
            pipeline.speak(s.text)
"""

from __future__ import annotations

import logging
from typing import Optional

from .context_engine import DeepContextEngine, WorkContext
from .pattern_learner import PatternLearner
from .session_memory import SessionStore
from .priority import PriorityInference, PrioritizedItem

logger = logging.getLogger("friday_v4.proactive.anticipation")


class AnticipationEngine:
    """Generates proactive suggestions by combining context + patterns + priority.

    The main loop (called periodically or on relevant events):
      1. Get current context (desktop, time, git)
      2. Get learned patterns (action pairs, app transitions, timing)
      3. Generate suggestions from both sources
      4. Prioritize each suggestion based on focus level and fatigue
      5. Return: speak now, notify, queue, or suppress
    """

    def __init__(self):
        self.session_store = SessionStore()
        self.context_engine = DeepContextEngine(session_store=self.session_store)
        self.pattern_learner = PatternLearner()
        self.priority = PriorityInference()

    def get_suggestions(self, force: bool = False) -> list[PrioritizedItem]:
        """Get prioritized proactive suggestions for the current context.

        Args:
            force: If True, skip suppression and return all suggestions.
                   Used for `friday proactive suggest` command.

        Returns:
            List of PrioritizedItem objects sorted by priority.
        """
        all_items: list[PrioritizedItem] = []

        # 1. Get current context
        context = self.context_engine.get_context()

        # 2. Context-based suggestions
        for suggestion_text in self.context_engine.get_proactive_suggestions():
            item = PrioritizedItem(
                text=suggestion_text,
                category="suggestion",
                priority_score=50,
                urgency="soon",
                context=context.describe(),
                source="context",
            )
            all_items.append(item)

        # 3. Pattern-based suggestions (if we have learned patterns)
        pattern_suggestions = self.pattern_learner.get_suggestions({
            "active_app_class": context.active_app_class,
            "last_action": "edit_file",  # Simplified — real tracking needs event hooks
        })
        for suggestion_text in pattern_suggestions:
            item = PrioritizedItem(
                text=suggestion_text,
                category="suggestion",
                priority_score=45,
                urgency="when_free",
                context=f"Pattern match for {context.active_app}",
                source="pattern",
            )
            all_items.append(item)

        # 4. Session-based suggestions
        if context.session_minutes > 30 and context.recent_commits_week > 0:
            all_items.append(PrioritizedItem(
                text=f"You've been working for {context.session_minutes} minutes. "
                     f"Would you like me to stage and commit your changes?",
                category="suggestion",
                priority_score=40,
                urgency="when_free",
                context=f"{context.session_minutes} min session, {context.dirty_repos} dirty repos",
                source="session",
            ))

        # 5. Evaluate and prioritize each item
        evaluated = []
        for item in all_items:
            evaluated_item = self.priority.evaluate(
                item,
                focus_level=context.focus_level,
                time_of_day=context.time_of_day,
            )
            evaluated.append(evaluated_item)

        # 6. Sort by priority score
        evaluated.sort(key=lambda x: x.priority_score, reverse=True)

        return evaluated

    def observe_activity(self, action_type: str, context: Optional[dict] = None):
        """Observe user activity to feed the pattern learner.

        Call this when:
          - User switches apps -> observe_activity("app_switch", {"app": "kitty"})
          - User edits a file -> observe_activity("edit_file", {"file": "main.py"})
          - User runs tests -> observe_activity("run_tests")
          - User commits -> observe_activity("git_commit")
        """
        if context is None:
            context = {}

        # Update session
        app_class = context.get("app", "")
        repo = context.get("repo", "")
        self.session_store.update_activity(app_class=app_class, repo=repo)

        # Learn pattern
        self.pattern_learner.observe_action(action_type, context)

        # Learn timing
        self.pattern_learner.observe_timing(action_type)

    def observe_app_switch(self, from_app: str, to_app: str):
        """Observe app switches for sequence learning."""
        self.pattern_learner.observe_app_transition(from_app, to_app)

    def get_context_summary(self) -> str:
        """Get a spoken summary of what FRIDAY knows about your context."""
        ctx = self.context_engine.get_context()

        if not ctx.active_app:
            return "I can't see your desktop right now."

        parts = []
        if ctx.active_app:
            parts.append(f"You're in {ctx.active_app}")

        if ctx.work_mode:
            parts.append(f"in {ctx.work_mode} mode")

        if ctx.active_repo:
            parts.append(f"working on {ctx.active_repo}")

        if ctx.session_minutes > 0:
            parts.append(f"session: {ctx.session_minutes} minutes")

        # Add pattern insight if available
        stats = self.pattern_learner.get_stats()
        if stats["action_pairs_learned"] > 5:
            parts.append(f"I've learned {stats['action_pairs_learned']} patterns from your work")

        return ". ".join(parts) + "."

    def get_learning_stats(self) -> dict:
        """Get statistics about what FRIDAY has learned."""
        return {
            "patterns": self.pattern_learner.get_stats(),
            "sessions_today": self.session_store.get_today_stats(),
            "sessions_this_week": self.session_store.get_weekly_stats(),
        }

    def cleanup(self):
        """End the current session and clean up resources."""
        self.session_store.end_session()
        self.context_engine.cleanup()
