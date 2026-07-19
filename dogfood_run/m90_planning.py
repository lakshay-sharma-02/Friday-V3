"""Milestone 9.0 dogfood transcript — Planning Engine end-to-end.

Drives the REAL engines (no LLM, no mock) through the full live chain:

  Knowledge -> Understanding -> Initiatives -> Insights -> Plans

Generates plans for the spec's five goals and prints every section
(milestones, dependencies, risks, verification, rollback, confidence). The Plan
is a STRUCTURED object first; text is rendered only at the end. Every section
references evidence (initiative/insight/understanding/knowledge ids). Lower
layers are only fed their normal output rows; the Planning Engine is purely
additive and never executes.

Run:  PYTHONPATH=. python dogfood_run/m90_planning.py
"""

from __future__ import annotations

import sqlite3
import tempfile, os

from src.friday.db import connect
from src.friday.knowledge.models import (
    Knowledge, KnowledgeConfidence, KnowledgeStatus, KnowledgeType)
from src.friday.knowledge.store import insert_knowledge, get_all_knowledge
from src.friday.understanding import UnderstandingEngine
from src.friday.understanding.models import (
    Understanding, UnderstandingConfidence, UnderstandingStatus, UnderstandingType)
from src.friday.understanding.engine import insert_understanding
from src.friday.initiative.models import (
    Initiative, InitiativeConfidence, InitiativeStatus, InitiativeType)
from src.friday.db import insert_initiative
from src.friday.initiative import InitiativeEngine
from src.friday.insight import InsightEngine
from src.friday.planning import PlanEngine, PlanType


BASE = "2026-07-15T00:00:00+00:00"
SEEN = set()
N = 0


def k(ldb, subject, stmt, ktype, kconf="medium", evidence_ids=("repo:a",)):
    global N
    real = subject
    while real in SEEN:
        N += 1
        real = f"{subject}_{N}"
    SEEN.add(real)
    N += 1
    insert_knowledge(ldb, [Knowledge(
        type=KnowledgeType.from_str(ktype), subject=real, statement=stmt,
        confidence=KnowledgeConfidence.from_str(kconf), evidence_ids=list(evidence_ids),
        status=KnowledgeStatus.VERIFIED, created_at=BASE, updated_at=BASE, id=None)])
    return real


def u(ldb, subject, stmt, utype, uconf="medium", knowledge_subjects=()):
    kmap = {kk.subject: kk.id for kk in get_all_knowledge(ldb)}
    kids = [kmap[x] for x in knowledge_subjects if x in kmap]
    insert_understanding(ldb, [Understanding(
        type=UnderstandingType.from_str(utype), subject=subject, statement=stmt,
        confidence=UnderstandingConfidence.from_str(uconf),
        status=UnderstandingStatus.OBSERVED, knowledge_ids=kids,
        build_at=BASE, created_at=BASE, updated_at=BASE, id=None).to_row()])


def i(ldb, title, itype="platform", repos=("repo:a",), u_subjects=(), k_subjects=()):
    u_by = {uu.subject: uu.id for uu in UnderstandingEngine(ldb).all_understanding()}
    k_by = {kk.subject: kk.id for kk in get_all_knowledge(ldb)}
    uids = [u_by[s] for s in u_subjects if s in u_by]
    kids = [k_by[s] for s in k_subjects if s in k_by]
    init = Initiative(
        type=InitiativeType.from_str(itype), title=title,
        status=InitiativeStatus.ACTIVE,
        confidence=InitiativeConfidence.from_str("medium"),
        participating_repositories=list(repos), understanding_ids=uids,
        knowledge_ids=kids, build_at=BASE, started_at=BASE, statement="",
        created_at=BASE, updated_at=BASE, id=None)
    insert_initiative(ldb, [init.to_row()])


GOALS = [
    "Implement OAuth",
    "Refactor authentication",
    "Extract shared Rust crates",
    "Build worker system",
    "Improve Vivaha architecture",
]


def main():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    conn = connect(path)

    print("=" * 78)
    print("M9.0 DOGFOOD: Planning Engine end-to-end (real engines, no LLM)")
    print("=" * 78)

    print("\n[1] Knowledge (lower-layer output)")
    auth_a = k(conn, "auth", "auth solved in api", "recurring_pattern", "medium", ["repo:a"])
    auth_b = k(conn, "auth", "auth solved in web", "recurring_pattern", "medium", ["repo:b"])
    rust1 = k(conn, "rust", "rust infra 1", "engineering_trend", "strong", ["repo:a"])
    rust2 = k(conn, "rust", "rust infra 2", "technology_investment", "strong", ["repo:b"])
    viv = k(conn, "vivaha", "vivaha architecture", "project_architecture", "strong", ["repo:c"])
    wk = k(conn, "worker", "worker scheduling", "engineering_trend", "medium", ["repo:a"])
    print(f"    knowledge rows: {len(get_all_knowledge(conn))}")

    print("\n[2] Understanding (lower-layer output)")
    u(conn, "auth a", "Auth solved in api.", "engineering_habit", "medium", [auth_a])
    u(conn, "auth b", "Auth solved in web.", "engineering_habit", "medium", [auth_b])
    u(conn, "rust a", "Rust investment rising.", "engineering_direction", "strong", [rust1, rust2])
    u(conn, "rust b", "Rust direction strong.", "engineering_direction", "strong", [rust1, rust2])
    u(conn, "viv", "Vivaha structure.", "engineering_direction", "strong", [viv])
    u(conn, "wk", "Worker scheduling emerging.", "engineering_direction", "medium", [wk])

    print("\n[3] Initiatives (lower-layer output)")
    i(conn, "Authentication Infrastructure", itype="infrastructure",
      repos=["repo:a", "repo:b"], u_subjects=["auth a", "auth b"])
    i(conn, "Engineering Platform", itype="platform",
      repos=["repo:a", "repo:b"], u_subjects=["rust a", "rust b"])
    i(conn, "Vivaha Platform", itype="platform", repos=["repo:c"], u_subjects=["viv"])
    i(conn, "Worker System", itype="infrastructure", repos=["repo:a"], u_subjects=["wk"])

    print("\n[4] Insights (lower-layer output, build)")
    InsightEngine(conn).build()

    print("\n[5] Plans (generate)  <-- M9.0 NEW LAYER")
    eng = PlanEngine(conn)
    for g in GOALS:
        p = eng.generate(g)
        ev = (p.initiative_count + p.insight_count
              + p.understanding_count + p.knowledge_count)
        print(f"\n  Goal: {p.goal}")
        print(f"    type={p.plan_type.value} confidence={p.confidence.value} "
              f"status={p.status.value} complexity={p.estimated_complexity} "
              f"effort={p.estimated_effort} evidence={ev}")
        print(f"    milestones ({len(p.milestones)}): "
              + " -> ".join(m["title"] for m in p.milestones))
        print(f"    dependencies ({len(p.dependencies)}): "
              + "; ".join(f"{d['kind']}:{d['target']}" for d in p.dependencies)
              or "    (none)")
        print(f"    risks ({len(p.risks)}): "
              + "; ".join(f"{r['severity']}:{r['kind']}" for r in p.risks)
              or "    (none)")
        print(f"    verification ({len(p.verification)}): "
              + ", ".join(v["method"] for v in p.verification))
        print(f"    rollback ({len(p.rollback)}): "
              + ", ".join(r["strategy"] for r in p.rollback))
        print(f"    evidence: initiatives={p.affected_initiative_ids} "
              f"insights={p.affected_insight_ids}")

    print("\n[6] Plan evolution timeline")
    for e in eng.evolution():
        print(f"    {e.timestamp[:19]}  {e.event_type:12}  {e.plan_id}")

    conn.close()
    os.remove(path)
    print("\n" + "=" * 78)
    print("DOGFOOD COMPLETE — structured plans derived; no execution performed.")
    print("=" * 78)


if __name__ == "__main__":
    main()
