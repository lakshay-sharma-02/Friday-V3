"""Knowledge storage layer (Milestone 8.1).

Append-only storage for knowledge. History is preserved, knowledge may evolve.
"""

from __future__ import annotations

from typing import List, Optional

from ..db import connect, commit_if_top
from .models import Knowledge


def insert_knowledge(conn, knowledge: List[Knowledge]) -> None:
    """Insert or update knowledge entries."""
    for k in knowledge:
        conn.execute(
            """
            INSERT INTO knowledge
                (id, type, subject, statement, confidence, evidence_ids,
                 status, created_at, updated_at, last_verified, verification_count,
                 is_static, schema_version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                type=excluded.type,
                subject=excluded.subject,
                statement=excluded.statement,
                confidence=excluded.confidence,
                evidence_ids=excluded.evidence_ids,
                status=excluded.status,
                updated_at=excluded.updated_at,
                last_verified=excluded.last_verified,
                verification_count=excluded.verification_count,
                is_static=excluded.is_static,
                schema_version=excluded.schema_version
            """,
            (
                k.id or k._generate_id(),
                k.type.value,
                k.subject,
                k.statement,
                k.confidence.value,
                ",".join(k.evidence_ids),
                k.status.value,
                k.created_at,
                k.updated_at,
                k.last_verified,
                k.verification_count,
                int(bool(k.is_static)),
                k.schema_version,
            ),
        )
    commit_if_top(conn)


def get_all_knowledge(conn) -> List[Knowledge]:
    """Retrieve all knowledge entries, newest first."""
    rows = conn.execute(
        "SELECT * FROM knowledge ORDER BY updated_at DESC"
    ).fetchall()
    return [Knowledge.from_row(r) for r in rows]


def get_knowledge_by_id(conn, knowledge_id: str) -> Optional[Knowledge]:
    """Retrieve a specific knowledge entry."""
    row = conn.execute(
        "SELECT * FROM knowledge WHERE id = ?", (knowledge_id,)
    ).fetchone()
    return Knowledge.from_row(row) if row else None


def get_knowledge_by_type(conn, knowledge_type: str) -> List[Knowledge]:
    """Retrieve all knowledge of a specific type."""
    rows = conn.execute(
        "SELECT * FROM knowledge WHERE type = ? ORDER BY updated_at DESC",
        (knowledge_type,)
    ).fetchall()
    return [Knowledge.from_row(r) for r in rows]


def get_knowledge_by_subject(conn, subject: str) -> List[Knowledge]:
    """Retrieve all knowledge about a specific subject."""
    rows = conn.execute(
        "SELECT * FROM knowledge WHERE subject = ? ORDER BY updated_at DESC",
        (subject,)
    ).fetchall()
    return [Knowledge.from_row(r) for r in rows]


def get_knowledge_by_status(conn, status: str) -> List[Knowledge]:
    """Retrieve all knowledge with a specific status."""
    rows = conn.execute(
        "SELECT * FROM knowledge WHERE status = ? ORDER BY updated_at DESC",
        (status,)
    ).fetchall()
    return [Knowledge.from_row(r) for r in rows]


def count_knowledge(conn) -> int:
    """Count total knowledge entries."""
    row = conn.execute("SELECT COUNT(*) as count FROM knowledge").fetchone()
    return row["count"] if row else 0


def delete_knowledge(conn, knowledge_id: str) -> None:
    """Delete a knowledge entry (for retired knowledge)."""
    conn.execute("DELETE FROM knowledge WHERE id = ?", (knowledge_id,))
    conn.commit()
