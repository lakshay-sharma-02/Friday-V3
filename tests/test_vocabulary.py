"""Tests for vocabulary consolidation.

Each test verifies that an exported vocabulary set matches the exact original
inline literal it replaces. This is a regression check for a pure refactor —
any content change here means the refactor shifted behavior.
"""

from friday.vocabulary import (
    PLAN_TYPE_KEYWORDS,
    SIGNAL_WORDS,
    UNDERSTANDING_TEMPLATES,
    KNOWLEDGE_TEMPLATES,
    STOPWORDS,
    CONCEPT_KEYWORDS,
    TITLE_CATEGORY,
    COMPONENT_STRENGTH,
    CONCEPT_COMPONENTS,
    RELATIONSHIP_STRENGTH,
    PATTERN_CAP_HINTS,
    PATTERN_VERIFY_METHODS,
    CHITCHAT_WORDS,
    DEPRECATED_INTENTS,
    REL_PREFERENCE,
    STOP_WORDS,
    FOLLOWUP_STOPWORDS,
    FOLLOWUP_PRONOUNS,
    COMPARE_PREFIX,
    CONTRAST_PREFIX,
    NEXT_PREFIX,
    AGE_PREFIX,
    META_CONFIDENCE,
    META_EVIDENCE,
    META_SUMMARIZE,
    META_EXPAND,
    META_CHANGED,
    PORTFOLIO_STRENGTHS,
    PORTFOLIO_EFFORT,
    PORTFOLIO_IDENTITY,
    STRATEGY_IMPACT,
    STRATEGY_PLATFORM,
    STRATEGY_LEARNING,
    STRATEGY_OPPORTUNITY,
    STRATEGY_PRIORITY,
    STRATEGY_CONVERGE,
    STRATEGY_MERGE,
)


# ---------------------------------------------------------------------------
# PlanType.from_goal keyword table
# ---------------------------------------------------------------------------

def test_plan_type_keywords_match_original():
    """PLAN_TYPE_KEYWORDS must reproduce the exact original table from models.py.

    Original (planning/models.py:from_goal):
      table = [
          ("refactor", cls.REFACTOR),
          ("extract", cls.REFACTOR),
          ...
          ("create", cls.FEATURE),
      ]
    """
    expected = [
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
        ("oauth", "feature"),
        ("auth", "feature"),
        ("implement", "feature"),
        ("add ", "feature"),
        ("build", "feature"),
        ("create", "feature"),
    ]
    assert PLAN_TYPE_KEYWORDS == expected, f"PLAN_TYPE_KEYWORDS mismatch"


# ---------------------------------------------------------------------------
# Signal words (graph_engine.py)
# ---------------------------------------------------------------------------

def test_signal_words_match_original():
    expected = [
        "recurring", "accumulating", "diverging", "stabilizing",
        "forming", "appearing", "clear", "strong", "growing",
        "emerging", "shifting", "converging", "visible", "evident",
    ]
    assert SIGNAL_WORDS == expected


# ---------------------------------------------------------------------------
# Understanding templates (graph_engine.py)
# ---------------------------------------------------------------------------

def test_understanding_templates_keys_match_original():
    """UNDERSTANDING_TEMPLATES must have the same keys and task_types as the original."""
    expected_keys = {
        "engineering_weakness",
        "project_divergence",
        "engineering_risk",
        "engineering_blind_spot",
        "engineering_strength",
        "engineering_identity",
        "architectural_style",
        "project_convergence",
    }
    assert set(UNDERSTANDING_TEMPLATES) == expected_keys
    # Verify each maps to the expected task_type
    assert UNDERSTANDING_TEMPLATES["engineering_weakness"][0] == "analysis"
    assert UNDERSTANDING_TEMPLATES["project_divergence"][0] == "refactor"
    assert UNDERSTANDING_TEMPLATES["engineering_risk"][0] == "refactor"
    assert UNDERSTANDING_TEMPLATES["engineering_blind_spot"][0] == "analysis"
    assert UNDERSTANDING_TEMPLATES["engineering_strength"][0] == "implementation"
    assert UNDERSTANDING_TEMPLATES["engineering_identity"][0] == "documentation"
    assert UNDERSTANDING_TEMPLATES["architectural_style"][0] == "analysis"
    assert UNDERSTANDING_TEMPLATES["project_convergence"][0] == "design"


# ---------------------------------------------------------------------------
# Knowledge templates (graph_engine.py)
# ---------------------------------------------------------------------------

def test_knowledge_templates_keys_match_original():
    expected_keys = {
        "project_architecture",
        "project_stack",
        "portfolio_integration",
        "project_identity",
        "portfolio_technology",
    }
    assert set(KNOWLEDGE_TEMPLATES) == expected_keys
    assert KNOWLEDGE_TEMPLATES["project_architecture"][0] == "analysis"


# ---------------------------------------------------------------------------
# Stopwords (graph_engine.py)
# ---------------------------------------------------------------------------

def test_stopwords_match_original():
    expected = {
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
    assert STOPWORDS == expected


# ---------------------------------------------------------------------------
# Concept keywords (initiative/engine.py + cli_watch.py)
# ---------------------------------------------------------------------------

def test_concept_keywords_match_original():
    """Verify the 42-entry superset list (identical in both engine.py and cli_watch.py)."""
    expected = [
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
    assert CONCEPT_KEYWORDS == expected
    assert len(CONCEPT_KEYWORDS) == 42


# ---------------------------------------------------------------------------
# Calendar category mapping (calendar_observer.py)
# ---------------------------------------------------------------------------

def test_title_category_match_original():
    expected = [
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
    assert TITLE_CATEGORY == expected


# ---------------------------------------------------------------------------
# Component / Relationship strength (judgment.py)
# ---------------------------------------------------------------------------

def test_component_strength_match_original():
    expected = {
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
    assert COMPONENT_STRENGTH == expected
    assert CONCEPT_COMPONENTS == set(expected)


def test_relationship_strength_match_original():
    expected = {
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
    assert RELATIONSHIP_STRENGTH == expected


# ---------------------------------------------------------------------------
# Pattern classifier helpers (patterns.py)
# ---------------------------------------------------------------------------

def test_pattern_cap_hints_match_original():
    expected = {
        "analysis": ["python"],
        "refactor": ["python"],
        "implementation": ["python"],
        "cleanup": ["python"],
        "configuration": ["configuration"],
        "testing": ["testing"],
        "verification": ["testing"],
        "review": ["research"],
    }
    assert PATTERN_CAP_HINTS == expected


def test_pattern_verify_methods_match_original():
    expected = {
        "analysis": "static_analysis",
        "refactor": "build",
        "implementation": "build",
        "cleanup": "build",
        "configuration": "format",
        "testing": "tests",
        "verification": "tests",
        "review": "review",
    }
    assert PATTERN_VERIFY_METHODS == expected


# ---------------------------------------------------------------------------
# ask.py keyword sets
# ---------------------------------------------------------------------------

def test_chitchat_words_match_original():
    expected = {
        "hello", "hi", "hey", "thanks", "thank you", "ok", "okay", "cool", "nice",
        "who are you", "what are you", "help",
    }
    assert CHITCHAT_WORDS == expected


def test_deprecated_intents_match_original():
    expected = {
        "chitchat", "compare", "related", "architecture", "describe", "similarity",
        "inactive", "newest", "recommend", "portfolio", "value", "overlap",
        "integration", "workspace", "by-tech", "insights", "strategy", "general",
        "merge",
    }
    assert DEPRECATED_INTENTS == expected


def test_rel_preference_match_original():
    expected = {
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
    assert REL_PREFERENCE == expected


def test_stop_words_match_original():
    expected = {
        "what", "which", "why", "who", "how", "are", "is", "the", "a", "an", "and",
        "or", "of", "to", "do", "does", "did", "you", "your", "my", "me", "i", "use",
        "using", "project", "projects", "repository", "repositories", "repo", "repos",
        "share", "sharing", "with", "about", "tell", "describe", "compare", "related",
        "between", "inactive", "abandoned", "stale", "newest", "recent", "latest",
        "most", "active", "should", "next", "insight", "observations", "overview",
    }
    assert STOP_WORDS == expected


def test_followup_stopwords_and_pronouns_match_original():
    assert FOLLOWUP_STOPWORDS == ("to", "with", "against", "and", "the")
    assert FOLLOWUP_PRONOUNS == ("that", "this", "it", "them")


def test_prefixes_match_original():
    assert COMPARE_PREFIX == ("compare", "contrast", "versus", "vs")
    assert CONTRAST_PREFIX == ("why not", "what about", "how about", "and not", "but not")
    assert NEXT_PREFIX == ("what next", "and then", "after that", "then what",
                           "what should i do next", "what do i do next",
                           "next step", "next steps")
    assert AGE_PREFIX == ("how long", "how old", "since when", "age of", "how stale")


def test_meta_phrases_match_original():
    assert META_CONFIDENCE == ("how confident", "confidence", "how sure", "how certain",
                               "sure are you", "certain are you")
    assert META_EVIDENCE == ("what evidence", "which evidence", "what supports",
                             "evidence for", "based on what", "how do you know",
                             "where did you", "sources", "what backs this")
    assert META_SUMMARIZE == ("summarize", "sum it up", "summarise", "tl;dr",
                              "in short", "recap", "wrap up", "give me the gist")
    assert META_EXPAND == ("explain further", "expand", "more on that",
                           "tell me everything", "go deeper", "dive deeper",
                           "more about that", "elaborate further",
                           "what else", "anything else")
    assert META_CHANGED == ("what changed", "has it changed", "what's changed",
                            "what is changed", "recent change", "what's new",
                            "anything changed")


def test_portfolio_keywords_match_original():
    assert PORTFOLIO_STRENGTHS == ("strengths", "skills am i", "developing",
                                   "good at", "capabilities", "what can i",
                                   "engineering ability")
    assert PORTFOLIO_EFFORT == ("effort", "where is my", "where.*going",
                                "attention", "spending my", "time going",
                                "focus", "currently investing")
    assert PORTFOLIO_IDENTITY == ("kind of engineer", "kind of developer",
                                  "type of engineer", "type of developer",
                                  "what kind of ", "what sort of engineer",
                                  "am i a", "engineer am i", "developer am i")


def test_strategy_keywords_match_original():
    assert STRATEGY_IMPACT == ("impact", "highest impact", "most impact",
                               "most valuable impact")
    assert STRATEGY_PLATFORM == ("platform", "should become a platform",
                                 "become a platform", "turn into a platform",
                                 "platform play")
    assert STRATEGY_LEARNING == ("teaches me", "teach me", "teaching me",
                                 "learning", "learn the most", "stretched me",
                                 "taught me")
    assert STRATEGY_OPPORTUNITY == ("opportunit", "leverage", "missing out",
                                    "am i missing", "missing", "left on the table",
                                    "not yet doing")
    assert STRATEGY_PRIORITY == ("center of my", "center of the",
                                 "engineering universe", "heart of my",
                                 "should become the center")
    assert STRATEGY_CONVERGE == ("ultimately trying to build", "ultimately build",
                                 "converging on", "what am i converging",
                                 "trying to build", "am i really building",
                                 "what am i really building")
    assert STRATEGY_MERGE == ("never merge", "shouldn't merge", "should not merge",
                              "keep separate", "stay independent", "don't merge",
                              "do not merge")
