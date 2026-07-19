"""Knowledge Engine (Milestone 8.1).

Transforms observations and sessions into durable engineering knowledge.

The engine consumes:
- Sessions (from Context Engine)
- Observations (from Observation Engine)
- Portfolio (repository metadata)
- Identity (project understanding)

The engine produces:
- Knowledge (accumulated understanding)

Idempotent. Deterministic. Running twice changes nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from ..context.models import EngineeringSession
from ..db import atomic, connect, observations_all, sessions_all
from ..observation.model import Observation
from .confidence import update_confidence, verify_knowledge
from .models import Knowledge, KnowledgeStatus, now_iso
from .patterns import (
    detect_habits,
    detect_project_switching,
    detect_repeated_sequences,
    detect_repeated_usage,
)
from .relationships import detect_project_evolution, detect_relationships
from .static import detect_static_knowledge
from .store import get_all_knowledge, insert_knowledge
from .trends import detect_trends


@dataclass
class KnowledgeBuildResult:
    """Outcome of a knowledge build pass."""

    total: int
    created: int
    updated: int
    verified: int
    candidates: int
    stable: int
    static: int = 0
    temporal: int = 0

    def to_text(self) -> str:
        lines = [
            "Knowledge Engine",
            "",
            f"Total knowledge: {self.total}",
            f"  Static (available now): {self.static}",
            f"  Temporal (from history): {self.temporal}",
            f"Created: {self.created}",
            f"Updated: {self.updated}",
            f"Verified: {self.verified}",
            f"Candidates: {self.candidates}",
            f"Stable: {self.stable}",
            "",
            "Done.",
        ]
        return "\n".join(lines) + "\n"


class KnowledgeEngine:
    """Transforms observations and sessions into knowledge.

    The Brain never computes knowledge. It only consumes it.
    """

    def __init__(self, conn) -> None:
        self.conn = conn

    def build(self) -> KnowledgeBuildResult:
        """Derive knowledge from observations and sessions.

        WRITE operation. The only mutating entrypoint. Idempotent — running
        twice on the same observations/sessions produces the same knowledge.
        """
        # Load all observations and sessions
        obs_rows = observations_all(self.conn)
        observations = [Observation.from_row(r) for r in obs_rows]

        sess_rows = sessions_all(self.conn)
        sessions = [EngineeringSession.from_row(r) for r in sess_rows]

        # Load existing knowledge
        existing = get_all_knowledge(self.conn)
        existing_map = {(k.type.value, k.subject): k for k in existing}

        # Detect new knowledge
        new_knowledge: List[Knowledge] = []

        # Static knowledge — available immediately after ingest, no history.
        new_knowledge.extend(detect_static_knowledge(self.conn))

        # Temporal knowledge — requires observation history / sessions.
        # Trends
        new_knowledge.extend(detect_trends(observations, sessions))

        # Patterns
        new_knowledge.extend(detect_repeated_usage(observations))
        new_knowledge.extend(detect_repeated_sequences(sessions))
        new_knowledge.extend(detect_project_switching(sessions))

        # Habits
        new_knowledge.extend(detect_habits(sessions))

        # Relationships
        new_knowledge.extend(detect_relationships(sessions))
        new_knowledge.extend(detect_project_evolution(sessions))

        # Merge with existing knowledge
        created = 0
        updated = 0
        verified = 0

        to_persist: List[Knowledge] = []

        for k in new_knowledge:
            key = (k.type.value, k.subject)
            if key in existing_map:
                # Update existing knowledge
                existing_k = existing_map[key]
                # Preserve evidence-driven lifecycle: Dormant/Retired are not
                # promoted back to active by a rebuild — only an explicit
                # reactivation event (evolution.py) clears them.
                preserved = existing_k.status if existing_k.status in (
                    KnowledgeStatus.DORMANT, KnowledgeStatus.RETIRED
                ) else None
                updated_k = update_confidence(existing_k, k.evidence_ids)
                updated_k.updated_at = now_iso()
                # Verification is evidence-driven: only count a verification when
                # genuinely NEW evidence arrives. A bare rebuild over the same
                # observations must NOT inflate verification_count/status.
                new_evidence = set(k.evidence_ids) - set(existing_k.evidence_ids)
                if new_evidence:
                    updated_k = verify_knowledge(updated_k)
                if preserved is not None:
                    updated_k.status = preserved
                to_persist.append(updated_k)
                updated += 1
                verified += 1
            else:
                # New knowledge
                k.created_at = now_iso()
                k.updated_at = now_iso()
                to_persist.append(k)
                created += 1

        # Persist all knowledge atomically (Part F).
        with atomic(self.conn):
            insert_knowledge(self.conn, to_persist)

        # Count by status
        all_knowledge = get_all_knowledge(self.conn)
        candidates = sum(1 for k in all_knowledge if k.status == KnowledgeStatus.CANDIDATE)
        stable = sum(1 for k in all_knowledge if k.status == KnowledgeStatus.STABLE)
        static = sum(1 for k in all_knowledge if k.is_static)
        temporal = sum(1 for k in all_knowledge if not k.is_static)

        return KnowledgeBuildResult(
            total=len(all_knowledge),
            created=created,
            updated=updated,
            verified=verified,
            candidates=candidates,
            stable=stable,
            static=static,
            temporal=temporal,
        )

    # --- READ operations (never mutate) -------------------------------------

    def all_knowledge(self) -> List[Knowledge]:
        """Retrieve all knowledge entries."""
        return get_all_knowledge(self.conn)

    def static_knowledge(self) -> List[Knowledge]:
        """Retrieve only static (ingest-time) knowledge."""
        return [k for k in get_all_knowledge(self.conn) if k.is_static]

    def temporal_knowledge(self) -> List[Knowledge]:
        """Retrieve only temporal (history-derived) knowledge."""
        return [k for k in get_all_knowledge(self.conn) if not k.is_static]

    def knowledge_by_type(self, knowledge_type: str) -> List[Knowledge]:
        """Retrieve knowledge of a specific type."""
        from .store import get_knowledge_by_type

        return get_knowledge_by_type(self.conn, knowledge_type)

    def knowledge_by_subject(self, subject: str) -> List[Knowledge]:
        """Retrieve knowledge about a specific subject."""
        from .store import get_knowledge_by_subject

        return get_knowledge_by_subject(self.conn, subject)

    def knowledge_by_id(self, knowledge_id: str) -> Optional[Knowledge]:
        """Retrieve a specific knowledge entry."""
        from .store import get_knowledge_by_id

        return get_knowledge_by_id(self.conn, knowledge_id)

    def stable_knowledge(self) -> List[Knowledge]:
        """Retrieve only stable, verified knowledge."""
        from .store import get_knowledge_by_status

        return get_knowledge_by_status(self.conn, KnowledgeStatus.STABLE.value)
