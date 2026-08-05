"""Proactive Intelligence — Anticipation, context understanding, priority inference.

The crown jewel of V4. Where V3 reacts to daemon cycles, V4 anticipates needs
before the user asks. Uses context (desktop state, open files, recent actions,
calendar, git activity) and historical patterns to predict what the user is
about to do and offer help proactively.

Capabilities:
    - Need anticipation: "You just opened main.py — want me to review recent changes?"
    - Deep context: Understands what you're working on at a glance
    - Pattern learning: Learns your workflow from repeated behavior
    - Session memory: Remembers what you've done across sessions
    - Priority inference: What matters NOW vs later — never interrupts deep focus
"""

from .anticipation import AnticipationEngine, PrioritizedItem
from .context_engine import DeepContextEngine, WorkContext
from .pattern_learner import PatternLearner
from .priority import PriorityInference
from .session_memory import SessionStore
from .v3source import V3DataSource

__all__ = [
    "AnticipationEngine",
    "PrioritizedItem",
    "DeepContextEngine",
    "WorkContext",
    "PatternLearner",
    "SessionStore",
    "PriorityInference",
    "V3DataSource",
]
