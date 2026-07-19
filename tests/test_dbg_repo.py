import sqlite3
from src.friday.db import SCHEMA, _migrate
from src.friday.knowledge.store import get_all_knowledge, insert_knowledge
from src.friday.understanding import UnderstandingEngine
from src.friday.understanding.models import (Understanding, UnderstandingType, UnderstandingStatus, UnderstandingConfidence)
from src.friday.knowledge.models import (Knowledge, KnowledgeType, KnowledgeConfidence, KnowledgeStatus)
from src.friday.understanding.engine import insert_understanding
from src.friday.initiative import InitiativeEngine
from src.friday.initiative.derivation import detect


def test_dbg_repo():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA); _migrate(conn)
    base = "2026-07-01T00:00:00+00:00"
    insert_knowledge(conn, [Knowledge(type=KnowledgeType.RECURRING_PATTERN, subject="auth",
        statement="auth work.", confidence=KnowledgeConfidence.MEDIUM,
        evidence_ids=["repo:a"], status=KnowledgeStatus.VERIFIED,
        created_at=base, updated_at=base, id=None)])
    km = {k.subject: k.id for k in get_all_knowledge(conn)}
    u = Understanding(type=UnderstandingType.ENGINEERING_HABIT, subject="auth",
        statement="Repeated auth.", confidence=UnderstandingConfidence.MEDIUM,
        status=UnderstandingStatus.OBSERVED, knowledge_ids=[km["auth"]],
        build_at=base, created_at=base, updated_at=base, id=None)
    insert_understanding(conn, [u.to_row()])
    # add repo:b
    insert_knowledge(conn, [Knowledge(type=KnowledgeType.RECURRING_PATTERN, subject="auth",
        statement="auth also in b.", confidence=KnowledgeConfidence.MEDIUM,
        evidence_ids=["repo:b"], status=KnowledgeStatus.VERIFIED,
        created_at=base, updated_at=base, id=None)])
    und = UnderstandingEngine(conn).all_understanding()
    know = get_all_knowledge(conn)
    cands = detect(und, know)
    for c in cands:
        if c.title == "Authentication Infrastructure":
            print("REPOS:", c.repos, "kids:", c.knowledge_ids)
    conn.close()
