"""Knowledge Engine package (Milestone 8.1).

Transforms observations and sessions into durable engineering knowledge.
"""

from .confidence import update_confidence, verify_knowledge
from .engine import KnowledgeEngine, KnowledgeBuildResult
from .evolution import (
    evolve,
    evidence_age_weight,
    evolution_timeline,
    history_timeline,
    weighted_evidence_score,
)
from .models import (
    Knowledge,
    KnowledgeConfidence,
    KnowledgeStatus,
    KnowledgeType,
    Relationship,
    Trend,
    TrendDirection,
)
from .patterns import (
    detect_habits,
    detect_project_switching,
    detect_repeated_sequences,
    detect_repeated_usage,
)
from .relationships import detect_project_evolution, detect_relationships
from .static import detect_static_knowledge
from .store import (
    count_knowledge,
    delete_knowledge,
    get_all_knowledge,
    get_knowledge_by_id,
    get_knowledge_by_status,
    get_knowledge_by_subject,
    get_knowledge_by_type,
    insert_knowledge,
)
from .trends import detect_trends

__all__ = [
    "KnowledgeEngine",
    "KnowledgeBuildResult",
    "evolve",
    "evidence_age_weight",
    "weighted_evidence_score",
    "history_timeline",
    "evolution_timeline",
    "Knowledge",
    "KnowledgeType",
    "KnowledgeStatus",
    "KnowledgeConfidence",
    "Trend",
    "TrendDirection",
    "Relationship",
    "detect_trends",
    "detect_repeated_usage",
    "detect_repeated_sequences",
    "detect_project_switching",
    "detect_habits",
    "detect_relationships",
    "detect_project_evolution",
    "detect_static_knowledge",
    "update_confidence",
    "verify_knowledge",
    "insert_knowledge",
    "get_all_knowledge",
    "get_knowledge_by_id",
    "get_knowledge_by_type",
    "get_knowledge_by_subject",
    "get_knowledge_by_status",
    "count_knowledge",
    "delete_knowledge",
]
