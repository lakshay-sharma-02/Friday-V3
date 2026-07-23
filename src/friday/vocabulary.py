"""
vocabulary — single source of truth for keyword sets.

Consolidates ~12 scattered keyword-matching/vocabulary sites into one module.
This is a PURE REFACTOR: each exported set matches the exact original literal
it replaces. Zero behavior change anywhere.

Exports are named by CONCEPT, not by site, so the same vocabulary can be shared
across modules that need it (e.g. CONCEPT_KEYWORDS powers both
initiative/engine.py and cli_watch.py).

Do NOT add new keywords here as "improvements" — that's a separate, later
change requiring its own review. Move lists exactly as they are today.
"""

from __future__ import annotations

import re
from typing import Callable, List, Optional, Tuple


# ---------------------------------------------------------------------------
# PlanType.from_goal() keyword table  (src/friday/planning/models.py)
# ---------------------------------------------------------------------------
# Ordered: most specific first. Generic verbs (implement/build/add/create) are
# last so a specific keyword always wins.

PLAN_TYPE_KEYWORDS: list[tuple[str, str]] = [
    # Structural / activity keywords FIRST — they are the plan's true
    # shape and must win over broad topic words (auth) or verbs.
    ("refactor", "refactor"),
    ("extract", "refactor"),
    ("restructure", "refactor"),
    ("migrat", "migration"),
    ("upgrade", "migration"),
    ("worker", "infrastructure"),
    ("infrastructure", "infrastructure"),
    ("architecture", "architecture"),
    ("architect", "architecture"),
    ("optimi", "optimization"),
    ("performance", "optimization"),
    ("integrate", "integration"),
    ("commercial", "commercial"),
    ("product", "commercial"),
    ("research", "research"),
    ("study", "research"),
    ("learn", "learning"),
    ("mainten", "maintenance"),
    ("document", "documentation"),
    ("docs", "documentation"),
    ("test", "testing"),
    ("release", "release"),
    ("ship", "release"),
    # Topic words next (auth/oauth narrow a feature).
    ("oauth", "feature"),
    ("auth", "feature"),
    # Generic verbs LAST — any specific keyword above wins first.
    ("implement", "feature"),
    ("add ", "feature"),
    ("build", "feature"),
    ("create", "feature"),
]


# ---------------------------------------------------------------------------
# Signal words for understanding evidence differentiation
# (src/friday/planning/graph_engine.py — _evidence_type_to_milestone)
# ---------------------------------------------------------------------------

SIGNAL_WORDS: list[str] = [
    "recurring", "accumulating", "diverging", "stabilizing",
    "forming", "appearing", "clear", "strong", "growing",
    "emerging", "shifting", "converging", "visible", "evident",
]


# ---------------------------------------------------------------------------
# Understanding type → milestone template
# (src/friday/planning/graph_engine.py — _evidence_type_to_milestone)
# ---------------------------------------------------------------------------
# Each template is a (task_type, title_fn) pair where title_fn accepts
# (subject: str, key_phrase: str) and returns the milestone title string.
# The key_phrase is an extracted signal word (from SIGNAL_WORDS) or "".

def _collapse(text: str) -> str:
    """Collapse excess whitespace (handles double-space gaps from empty key phrases)."""
    return ' '.join(text.split())


UNDERSTANDING_TEMPLATES: dict[str, tuple[str, Callable[[str, str], str]]] = {
    "engineering_weakness": (
        "analysis",
        lambda s, kw: (
            _collapse(f"Investigate {s} {kw} weakness") if kw
            else f"Investigate {s} weakness"
        ),
    ),
    "project_divergence": (
        "refactor",
        lambda s, kw: f"Address {s} engineering divergence",
    ),
    "engineering_risk": (
        "refactor",
        lambda s, kw: (
            _collapse(f"Mitigate {kw} risk in {s}") if kw
            else f"Mitigate {s} risk"
        ),
    ),
    "engineering_blind_spot": (
        "analysis",
        lambda s, kw: f"Address {s} blind spot",
    ),
    "engineering_strength": (
        "implementation",
        lambda s, kw: (
            _collapse(f"Leverage {s} {kw} strength") if kw
            else f"Leverage {s} engineering strength"
        ),
    ),
    "engineering_identity": (
        "documentation",
        lambda s, kw: f"Document {s} engineering identity",
    ),
    "architectural_style": (
        "analysis",
        lambda s, kw: f"Assess {s} architecture stability",
    ),
    "project_convergence": (
        "design",
        lambda s, kw: f"Support {s} project convergence",
    ),
}


# ---------------------------------------------------------------------------
# Knowledge type → milestone template
# (src/friday/planning/graph_engine.py — _knowledge_to_milestone)
# ---------------------------------------------------------------------------
# Each template is a (task_type, title_format_str) pair. Unknown knowledge
# types fall through to the generic audit (safe fallback).

KNOWLEDGE_TEMPLATES: dict[str, tuple[str, str]] = {
    "project_architecture": (
        "analysis",
        "Review {subject} architecture",
    ),
    "project_stack": (
        "analysis",
        "Audit {subject} technology stack",
    ),
    "portfolio_integration": (
        "analysis",
        "Evaluate {subject} integration potential",
    ),
    "project_identity": (
        "documentation",
        "Document {subject} project identity",
    ),
    "portfolio_technology": (
        "configuration",
        "Standardize {subject} usage across projects",
    ),
}


# ---------------------------------------------------------------------------
# Stopwords for evidence-matching tokenization
# (src/friday/planning/graph_engine.py — _llm_initiative_milestones)
# ---------------------------------------------------------------------------

STOPWORDS: set = {
    'the', 'a', 'an', 'is', 'are', 'was', 'were', 'be', 'been',
    'being', 'have', 'has', 'had', 'do', 'does', 'did', 'will',
    'would', 'could', 'should', 'may', 'might', 'shall', 'can',
    'to', 'of', 'in', 'for', 'on', 'with', 'at', 'by', 'from',
    'as', 'into', 'through', 'during', 'before', 'after', 'above',
    'below', 'between', 'and', 'but', 'or', 'nor', 'not', 'so',
    'yet', 'both', 'either', 'neither', 'this', 'that', 'these',
    'those', 'it', 'its', 'they', 'them', 'their', 'we', 'our',
    'you', 'your', 'i', 'me', 'my', 'he', 'she', 'his', 'her',
}


# ---------------------------------------------------------------------------
# Concept keyword classification  (initiative/engine.py + cli_watch.py)
# ---------------------------------------------------------------------------
# NOTE: These two lists had diverged — the cli_watch.py copy had 12 additional
# entries not present in initiative/engine.py. The consolidated list below uses
# the SUPERSET (cli_watch.py's version, which includes all 30 of the original
# engine.py entries plus 12 more). See KNOWN_ISSUES.md for the divergence log.

CONCEPT_KEYWORDS: list[tuple[str, str]] = [
    ("architecture", "architectural evolution"),
    ("stabilizing", "stabilizing architecture"),
    ("purpose", "purpose evolution"),
    ("fit", "integration fit"),
    ("integrate", "integration opportunity"),
    ("platform", "platform convergence"),
    ("frontend", "frontend experience"),
    ("authentication", "authentication infrastructure"),
    ("auth", "authentication infrastructure"),
    ("session", "session management"),
    ("jwt", "JWT handling"),
    ("credential", "credential management"),
    ("oauth", "OAuth integration"),
    ("router", "AI routing"),
    ("llm", "LLM integration"),
    ("agent", "agent coordination"),
    ("knowledge", "knowledge evolution"),
    ("memory", "memory systems"),
    ("rust", "systems infrastructure"),
    ("kernel", "kernel development"),
    ("filesystem", "filesystem operations"),
    ("runtime", "runtime optimization"),
    ("compiler", "compiler development"),
    ("migration", "technology migration"),
    ("documentation", "documentation"),
    ("test", "test coverage"),
    ("ci/cd", "CI/CD pipeline"),
    ("docker", "container deployment"),
    ("database", "data layer"),
    ("api", "API design"),
    # --- cli_watch.py additions (not present in engine.py) ---
    ("backend", "backend services"),
    ("project", "project evolution"),
    ("project convergence", "project convergence"),
    ("project divergence", "project divergence"),
    ("weakness", "emerging weakness"),
    ("direction", "technology direction"),
    ("engineering identity", "engineering identity"),
    ("converging", "converging efforts"),
    ("diverging", "diverging direction"),
    ("recurring", "recurring patterns"),
    ("blind spot", "blind spot detected"),
    ("risk", "engineering risk"),
]


# ---------------------------------------------------------------------------
# Calendar event title → category classification
# (src/friday/observation/calendar_observer.py — TITLE_CATEGORY)
# ---------------------------------------------------------------------------

# Keywords are strings to import from calendar_observer without circular deps.
TITLE_CATEGORY: list[tuple[str, str]] = [
    ("deadline", "Deadline"),
    ("due", "Deadline"),
    ("sprint", "Sprint"),
    ("standup", "Meeting"),
    ("meeting", "Meeting"),
    ("sync", "Meeting"),
    ("review", "Review"),
    ("code review", "Review"),
    ("release", "Release"),
    ("deploy", "Deployment"),
    ("rollout", "Deployment"),
    ("assignment", "Assignment"),
    ("homework", "Assignment"),
    ("exam", "Exam"),
    ("midterm", "Exam"),
    ("final", "Exam"),
    ("conference", "Conference"),
    ("talk", "Conference"),
    ("presentation", "Presentation"),
    ("demo", "Presentation"),
    ("birthday", "Personal"),
    ("personal", "Personal"),
]


# ---------------------------------------------------------------------------
# Component strength  (src/friday/judgment.py — COMPONENT_STRENGTH)
# ---------------------------------------------------------------------------

COMPONENT_STRENGTH: dict[str, str] = {
    "Authentication": "Weak",
    "Database": "Weak",
    "Configuration": "Weak",
    "Routing": "Weak",
    "Storage": "Weak",
    "Logging": "Weak",
    "CLI": "Weak",
    "LLM interface": "Weak",
    "Caching": "Weak",
    "Networking": "Weak",
    "Testing": "Weak",
}

CONCEPT_COMPONENTS: set = set(COMPONENT_STRENGTH)


# ---------------------------------------------------------------------------
# Relationship strength  (src/friday/judgment.py — RELATIONSHIP_STRENGTH)
# ---------------------------------------------------------------------------

RELATIONSHIP_STRENGTH: dict[str, str] = {
    "shared-implementation": "Strong",
    "shared-abstraction": "Strong",
    "shared-architecture": "Medium",
    "shared-framework": "Medium",
    "shared-deployment": "Medium",
    "shared-db": "Medium",
    "shared-config": "Medium",
    "potential-reuse": "Medium",
    "duplicated-functionality": "Medium",
    "shared-tech": "Medium",
    "shared-lang-ecosystem": "Weak",
    "shared-language": "Weak",
    "shared-org": "Weak",
    "shared-author": "Weak",
}


# ---------------------------------------------------------------------------
# Pattern classifier capability hints  (src/friday/planning/patterns.py)
# ---------------------------------------------------------------------------

PATTERN_CAP_HINTS: dict[str, list[str]] = {
    "analysis": ["python"],
    "refactor": ["python"],
    "implementation": ["python"],
    "cleanup": ["python"],
    "configuration": ["configuration"],
    "testing": ["testing"],
    "verification": ["testing"],
    "review": ["research"],
}

PATTERN_VERIFY_METHODS: dict[str, str] = {
    "analysis": "static_analysis",
    "refactor": "build",
    "implementation": "build",
    "cleanup": "build",
    "configuration": "format",
    "testing": "tests",
    "verification": "tests",
    "review": "review",
}


# ---------------------------------------------------------------------------
# Pattern classifier regex patterns  (src/friday/planning/patterns.py)
# ---------------------------------------------------------------------------
# These are compiled regex objects used for goal classification.

PATTERN_RENAME = re.compile(
    r"\brename\s+(?:the\s+)?(\w+)\s+(?:to|into|as|=>)\s+(\w+)", re.IGNORECASE)
PATTERN_EXTRACT = re.compile(r"\bextract\b", re.IGNORECASE)
PATTERN_REFACTOR = re.compile(r"\brefactor\b", re.IGNORECASE)
PATTERN_FEATURE = re.compile(
    r"\badd\b.{1,40}?\b(?:to|into|for)\b\s+(\w+)", re.IGNORECASE)
PATTERN_BUGFIX = re.compile(r"\bfix\b", re.IGNORECASE)
PATTERN_MAINTENANCE = re.compile(
    r"\b(?:remove|delete|clean\s*up|prune|drop)\b", re.IGNORECASE)
PATTERN_MAINTENANCE_TARGET = re.compile(
    r"\b(?:remove|delete|clean\s*up|prune|drop)\b\s+(?:the\s+|unused\s+|dead\s+)?"
    r"(\w+)", re.IGNORECASE)


# ---------------------------------------------------------------------------
# ask.py — keyword sets for offline question classification
# (deprecated compat layer; kept for benchmark compatibility)
# ---------------------------------------------------------------------------

CHITCHAT_WORDS: set = {
    "hello", "hi", "hey", "thanks", "thank you", "ok", "okay", "cool", "nice",
    "who are you", "what are you", "help",
}

LOW_CONFIDENCE_OBJECTIVES: set = {
    "general", "value", "universe", "overlap", "insights", "drift",
}

DEPRECATED_INTENTS: set = {
    "chitchat", "compare", "related", "architecture", "describe", "similarity",
    "inactive", "newest", "recommend", "portfolio", "value", "overlap",
    "integration", "workspace", "by-tech", "insights", "strategy", "general",
    "merge",
}

REL_PREFERENCE: dict[str, int] = {
    "duplicated-functionality": 0,
    "shared-abstraction": 1,
    "shared-implementation": 2,
    "shared-architecture": 3,
    "shared-framework": 4,
    "shared-deployment": 5,
    "shared-db": 6,
    "shared-config": 7,
    "shared-tech": 8,
    "potential-reuse": 9,
}

# Tokenization stop words (ask.py _STOP set)
STOP_WORDS: set = {
    "what", "which", "why", "who", "how", "are", "is", "the", "a", "an", "and",
    "or", "of", "to", "do", "does", "did", "you", "your", "my", "me", "i", "use",
    "using", "project", "projects", "repository", "repositories", "repo", "repos",
    "share", "sharing", "with", "about", "tell", "describe", "compare", "related",
    "between", "inactive", "abandoned", "stale", "newest", "recent", "latest",
    "most", "active", "should", "next", "insight", "observations", "overview",
}

# Follow-up processing stop words (ask.py _STOPWORDS / _PRONOUNS)
FOLLOWUP_STOPWORDS: tuple = ("to", "with", "against", "and", "the")
FOLLOWUP_PRONOUNS: tuple = ("that", "this", "it", "them")

# Follow-up routing prefixes
COMPARE_PREFIX: tuple = ("compare", "contrast", "versus", "vs")
CONTRAST_PREFIX: tuple = ("why not", "what about", "how about", "and not", "but not")
NEXT_PREFIX: tuple = ("what next", "and then", "after that", "then what",
                       "what should i do next", "what do i do next",
                       "next step", "next steps")
AGE_PREFIX: tuple = ("how long", "how old", "since when", "age of", "how stale")

# Meta-follow-up phrases
META_CONFIDENCE: tuple = ("how confident", "confidence", "how sure", "how certain",
                           "sure are you", "certain are you")
META_EVIDENCE: tuple = ("what evidence", "which evidence", "what supports",
                         "evidence for", "based on what", "how do you know",
                         "where did you", "sources", "what backs this")
META_SUMMARIZE: tuple = ("summarize", "sum it up", "summarise", "tl;dr",
                          "in short", "recap", "wrap up", "give me the gist")
META_EXPAND: tuple = ("explain further", "expand", "more on that",
                       "tell me everything", "go deeper", "dive deeper",
                       "more about that", "elaborate further",
                       "what else", "anything else")
META_CHANGED: tuple = ("what changed", "has it changed", "what's changed",
                        "what is changed", "recent change", "what's new",
                        "anything changed")

# Portfolio sub-cut detection keywords (deprecated compat layer)
PORTFOLIO_STRENGTHS: tuple = ("strengths", "skills am i", "developing",
                               "good at", "capabilities", "what can i",
                               "engineering ability")
PORTFOLIO_EFFORT: tuple = ("effort", "where is my", "where.*going",
                            "attention", "spending my", "time going",
                            "focus", "currently investing")
PORTFOLIO_IDENTITY: tuple = ("kind of engineer", "kind of developer",
                              "type of engineer", "type of developer",
                              "what kind of ", "what sort of engineer",
                              "am i a", "engineer am i", "developer am i")

# Strategy axis detection keywords (deprecated compat layer)
STRATEGY_IMPACT: tuple = ("impact", "highest impact", "most impact",
                           "most valuable impact")
STRATEGY_PLATFORM: tuple = ("platform", "should become a platform",
                             "become a platform", "turn into a platform",
                             "platform play")
STRATEGY_LEARNING: tuple = ("teaches me", "teach me", "teaching me",
                             "learning", "learn the most", "stretched me",
                             "taught me")
STRATEGY_OPPORTUNITY: tuple = ("opportunit", "leverage", "missing out",
                                "am i missing", "missing", "left on the table",
                                "not yet doing")
STRATEGY_PRIORITY: tuple = ("center of my", "center of the",
                             "engineering universe", "heart of my",
                             "should become the center")
STRATEGY_CONVERGE: tuple = ("ultimately trying to build", "ultimately build",
                             "converging on", "what am i converging",
                             "trying to build", "am i really building",
                             "what am i really building")
STRATEGY_MERGE: tuple = ("never merge", "shouldn't merge", "should not merge",
                          "keep separate", "stay independent", "don't merge",
                          "do not merge")
